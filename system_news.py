"""Point-in-time news adapter; never invokes an LLM or invents timestamps."""
from contextlib import closing
import json
import re
from system_data import readonly, stamp, digest, daily_cutoff
from news_numbers import calculate

VERSION = 'news-features-v1'


class NewsStore:
    def __init__(self, path=None, coverage=None):
        self.items = []
        # Coverage means verified collector coverage of the chosen universe,
        # not a claim that every news source was captured. Default is unknown.
        self.coverage = coverage
        if coverage:
            if stamp(coverage[0]) > stamp(coverage[1]):
                raise ValueError('Invalid news coverage interval')
        if path:
            with closing(readonly(path)) as db:
                db.row_factory = __import__('sqlite3').Row
                for row in db.execute('SELECT * FROM ai_analyses ORDER BY started_at,request_hash'):
                    raw = dict(row)
                    source = json.loads(raw['source_json'])
                    result = json.loads(raw['result_json']) if raw['result_json'] else {}
                    numbers = calculate(result, source) if 'body' in source and result else None
                    required = [source.get('published_at'), source.get('first_seen_at'),
                                source.get('observed_at'), raw['finished_at']]
                    if source.get('body_received_at'):
                        required.append(source['body_received_at'])
                    available = max(map(stamp, required)).isoformat() if all(required) else None
                    changes = (numbers or {}).get('changes', [])
                    # Exact quantitative match only; vague headline similarity never merges events.
                    key = [source.get('symbols'), source.get('published_at'),
                           [(c['metric'], c['period'], c['before_yen'], c['after_yen']) for c in changes]]
                    certain = bool(changes and available and not numbers['issues'])
                    text = source.get('title','')+' '+source.get('body','')
                    synthetic = any(marker in text for marker in ('架空','動作確認専用','テスト用'))
                    event_id = digest(key) if certain else digest(['article', raw['article_id']])
                    self.items.append(dict(news_id=raw['article_id'], event_id=event_id,
                        analysis_id=raw['request_hash'], source=source, result=result,
                        published_at=source.get('published_at'), first_seen_at=source.get('first_seen_at'),
                        analysis_finished_at=raw['finished_at'], available_at=available,
                        started_at=raw['started_at'], status=raw['status'], model=raw['response_model'],
                        version=raw['version'], numbers=numbers,
                        quality=result.get('quality_warnings', []) + (['synthetic_test_document'] if synthetic else []),
                        dedupe='exact_numeric' if certain else 'review_similarity'))
        self.items.sort(key=lambda item:(stamp(item['started_at']),item['analysis_id']))

    def features(self, symbol, at):
        when = stamp(at)
        symbol = symbol[:4] if len(symbol) == 5 and symbol[-1] == '0' else symbol
        seen = []
        for item in self.items:
            if symbol not in item['source'].get('symbols', []):
                continue
            first = item['first_seen_at']
            if first and stamp(first) <= when and stamp(item['started_at']) <= when:
                # Limit known articles to seven days since first sighting; old
                # date-only documents must not veto a symbol forever.
                if (when-stamp(first)).total_seconds() <= 7*86400:
                    seen.append(item)
        latest = {}
        for item in seen:
            latest[item['news_id']] = item
        usable, pending = {}, False
        for item in latest.values():
            if item['status'] != 'ok' or not item['available_at'] or stamp(item['available_at']) > when:
                pending = True
                continue
            age = (when-stamp(item['published_at'])).total_seconds()/3600
            if not 0 <= age <= 7*24:
                continue
            usable.setdefault(item['event_id'], item)
        coverage = bool(self.coverage and stamp(self.coverage[0]) <= when <= stamp(self.coverage[1]))
        features = dict(news_present=float(bool(usable)), news_missing=float(pending or not coverage),
            news_count=float(len(usable)), news_direct=0.0, news_indirect=0.0,
            news_revision_pct=None, news_age_hours=None, news_body=0.0,
            news_earnings_revision=0.0, news_quality_warning=0.0,
            news_consensus_surprise=None, news_pre_return=None, news_acquisition_reaction=None)
        revisions, ages = [], []
        for item in usable.values():
            result, source = item['result'], item['source']
            relations = [r['relation'] for r in result.get('relations', []) if r['symbol'] == symbol]
            features['news_direct'] = max(features['news_direct'], float('直接' in relations))
            features['news_indirect'] = max(features['news_indirect'], float('間接' in relations))
            features['news_earnings_revision'] = max(features['news_earnings_revision'], float(result.get('category')=='業績修正'))
            features['news_body'] = max(features['news_body'], float('body' in source))
            features['news_quality_warning'] = max(features['news_quality_warning'], float(bool(item['quality'])))
            ages.append((when-stamp(item['published_at'])).total_seconds()/3600)
            numbers = item['numbers']
            if numbers and not numbers['issues'] and not item['quality'] and '直接' in relations:
                for c in numbers['changes']:
                    if c['metric'] in ('営業利益', '連結営業利益') and c['revision_pct'] is not None:
                        revisions.append(float(c['revision_pct']))
        # Conflicting/multiple revisions are not averaged into invented sentiment.
        if len(revisions) == 1:
            features['news_revision_pct'] = revisions[0]
        if ages:
            features['news_age_hours'] = min(ages)
        return features, list(usable)

    def augment(self, rows):
        output = []
        for r in rows:
            features, ids = self.features(r['symbol'], daily_cutoff(r['day']))
            output.append(dict(r, features={**r['features'], **features}, news_event_ids=ids,
                               news_feature_version=VERSION))
        return output

    def assessment(self, symbol, at, allow_indirect=False):
        """Business sentiment for rule integration; does not change model features."""
        when, code = stamp(at), symbol[:4]
        latest = {}
        for item in self.items:
            if code not in item['source'].get('symbols',[]):
                continue
            if not item['first_seen_at'] or stamp(item['first_seen_at'])>when or stamp(item['started_at'])>when:
                continue
            if (when-stamp(item['first_seen_at'])).total_seconds()<=7*86400:
                latest[item['news_id']]=item
        coverage = bool(self.coverage and stamp(self.coverage[0])<=when<=stamp(self.coverage[1]))
        pending, events = not coverage, {}
        for item in latest.values():
            if item['status']!='ok' or not item['available_at'] or stamp(item['available_at'])>when:
                pending=True
                continue
            if not 0 <= (when-stamp(item['published_at'])).total_seconds() <= 7*86400:
                continue
            if item['quality']:
                pending=True
                continue
            relations=[r['relation'] for r in item['result'].get('relations',[]) if r['symbol']==code]
            if not any(r in (('直接','間接') if allow_indirect else ('直接',)) for r in relations):
                continue
            impact=next((i for i in item['result'].get('impacts',[]) if i['symbol']==code),None)
            if impact is None:
                pending=True  # Old results retained, not silently assigned a sentiment.
                continue
            events.setdefault(item['event_id'],dict(impact, event_id=item['event_id'],analysis_id=item['analysis_id']))
        directions={e['short_term'] for e in events.values()}
        if '不明' in directions:
            pending=True
        status='UNKNOWN' if pending else ('NONE' if not events else 'AVAILABLE')
        return dict(status=status,events=list(events.values()),
                    positive='ポジティブ' in directions,negative='ネガティブ' in directions,
                    severe_negative=any(e['short_term']=='ネガティブ' and e['importance']=='高' for e in events.values()))
