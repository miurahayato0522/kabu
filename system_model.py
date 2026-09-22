"""LightGBM regression, native text model + hashed JSON metadata (no pickle)."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import json
import numpy as np
from system_data import digest, encode, daily_cutoff
from system_features import chart_features, chronological_split, VERSION
from system_strategy import Prediction


def matrix(rows, columns):
    return np.array([[r['features'].get(c) if r['features'].get(c) is not None else np.nan
                      for c in columns] for r in rows], dtype=float)


def metrics(actual, predicted):
    a, p = np.array(actual), np.array(predicted)
    return dict(count=len(a), mae=float(abs(a-p).mean()), rmse=float(np.sqrt(((a-p)**2).mean())),
                correlation=float(np.corrcoef(a, p)[0, 1]) if np.std(a)>1e-12 and np.std(p)>1e-12 else None,
                direction_accuracy=float(np.mean(np.sign(a) == np.sign(p))))


def train(rows, folder, source, kind='chart', min_news_events=100):
    import lightgbm as lgb
    train_rows, val_rows, test_rows, boundaries = chronological_split(rows)
    if len({r['horizon'] for r in rows}) != 1 or any(r['feature_version'] != VERSION for r in rows):
        raise ValueError('Mixed feature versions or target horizons')
    if kind == 'combined':
        # Count independent events, not five near-identical rows per event.
        events = {e for r in train_rows if not r['features'].get('news_missing',1)
                  and not r['features'].get('news_quality_warning',1) for e in r.get('news_event_ids', [])}
        if len(events) < min_news_events:
            return dict(status='SKIPPED', reason='insufficient_independent_training_news_events',
                        count=len(events), required=min_news_events, fallback='chart direction with confirmed news veto')
    elif kind != 'chart':
        raise ValueError('Invalid model kind')
    columns = sorted(train_rows[0]['features'])
    if any(sorted(r['features']) != columns for r in rows):
        raise ValueError('Feature schema mismatch')
    params = dict(objective='regression', metric='l2', learning_rate=.03, num_leaves=15,
                  min_data_in_leaf=20, verbosity=-1, seed=42, deterministic=True,
                  force_col_wise=True, num_threads=1)
    model = lgb.train(params, lgb.Dataset(matrix(train_rows, columns),
        label=[r['label'] for r in train_rows], feature_name=columns), num_boost_round=200,
        valid_sets=[lgb.Dataset(matrix(val_rows, columns), label=[r['label'] for r in val_rows],
                               feature_name=columns)], callbacks=[lgb.early_stopping(20, verbose=False)])
    evaluations, predictions = {}, []
    for name, subset in [('train', train_rows), ('validation', val_rows), ('test', test_rows)]:
        values = model.predict(matrix(subset, columns), num_threads=1)
        evaluations[name] = metrics([r['label'] for r in subset], values)
        predictions += [dict(symbol=r['symbol'], day=r['day'], split=name, actual=r['label'],
                             predicted=float(p), bar_id=r['bar_id']) for r, p in zip(subset, values)]
    # Exclusive directory prevents silently replacing a previously evaluated model.
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    text = model.model_to_string()
    (folder/'model.txt').write_text(text, encoding='utf-8')
    metadata = dict(version='lightgbm-return-v1', kind=kind, feature_version=VERSION,
        features=columns, horizon=rows[0]['horizon'], source=source, parameters=params,
        lightgbm_version=lgb.__version__, created_at=datetime.now(timezone.utc).isoformat(),
        dataset_hash=digest(rows), model_sha256=hashlib.sha256(text.encode()).hexdigest(),
        boundaries=boundaries, ranges={k: [s[0]['day'], s[-1]['day']] for k,s in
            [('train',train_rows),('validation',val_rows),('test',test_rows)]},
        counts=dict(train=len(train_rows), validation=len(val_rows), test=len(test_rows)),
        availability='research_reconstruction', evaluations=evaluations,
        limitations=['Index-relative strength disabled: index data unavailable',
                     'Fixed symbol universe can have selection/survivorship bias',
                     'Target is close-to-close return, not trade profit',
                     'One chronological holdout; no walk-forward evidence yet'])
    for filename, value in [('manifest.json', metadata), ('features.json', rows), ('predictions.json', predictions)]:
        (folder/filename).write_text(encode(value), encoding='utf-8')
    return dict(status='OK', folder=str(folder.resolve()), **metadata)


class ReturnModel:
    def __init__(self, folder, threshold=0.0, news=None):
        import lightgbm as lgb
        folder = Path(folder)
        self.metadata = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        text = (folder/'model.txt').read_text(encoding='utf-8')
        if self.metadata['version'] != 'lightgbm-return-v1' or self.metadata['feature_version'] != VERSION or hashlib.sha256(text.encode()).hexdigest() != self.metadata['model_sha256']:
            raise ValueError('Model hash or feature version mismatch')
        if not np.isfinite(threshold) or threshold < 0:
            raise ValueError('Threshold must be finite and nonnegative')
        try:
            self.model = lgb.Booster(model_str=text)
        except lgb.basic.LightGBMError:
            raise ValueError('Native LightGBM model could not be loaded') from None
        if self.model.feature_name() != self.metadata['features']:
            raise ValueError('Model feature order mismatch')
        self.name = self.metadata['kind'] + ':' + self.metadata['model_sha256'][:16] + ':threshold=' + str(threshold)
        self.threshold, self.news = threshold, news

    def predict(self, history, at=None):
        b = history[-1]
        when = at or daily_cutoff(b.day)
        error, value, direction = None, None, 0
        refs = [b.id, 'history:'+digest([x.id for x in history])]
        features = chart_features(history)
        if b.source != self.metadata['source']:
            error = 'price_source_mismatch'
        elif b.day < self.metadata['boundaries']['test_start']:
            error = 'date_not_after_training_and_validation'
        elif features is None:
            error = 'insufficient_history'
        else:
            if self.metadata['kind'] == 'combined':
                if self.news is None:
                    error = 'news_provider_missing'
                else:
                    values, event_ids = self.news.features(b.symbol, when)
                    features.update(values)
                    refs += event_ids
            if error is None:
                if sorted(features) != self.metadata['features']:
                    error = 'feature_schema_mismatch'
                else:
                    value = float(self.model.predict(matrix([{'features': features}], self.metadata['features']), num_threads=1)[0])
                    direction = int(value > self.threshold)-int(value < -self.threshold)
        return Prediction(b.symbol, when, self.metadata['horizon'], value, self.name, direction,
                          ['Expected close-to-close return; not calibrated probability'], refs, error)
