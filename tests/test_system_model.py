from dataclasses import replace
import tempfile
from pathlib import Path
import unittest
from system_features import chart_features, dataset, chronological_split
from system_model import train, ReturnModel
from test_system_backtest import bars


class ModelTests(unittest.TestCase):
    def data(self):
        return bars([100+(i%31)*.5+i*.01 for i in range(250)])

    def test_causal_features_split_adjusted_label_and_purge(self):
        data = self.data()
        base = dataset(data, calendar=False)
        prefix = dataset(data[:100], calendar=False)
        self.assertEqual([r['features'] for r in prefix], [r['features'] for r in base[:len(prefix)]])
        future = data[:100]+[replace(b, close=b.close*4) for b in data[100:]]
        self.assertEqual(chart_features(data[:100]), chart_features(future[:100]))
        changed = data[:100]+[replace(b, open=b.open*.5, close=b.close*.5, high=b.high*.5, low=b.low*.5,
                   volume=b.volume*2, split_factor=.5 if i==100 else 1) for i,b in enumerate(data[100:],100)]
        adjusted = dataset(changed, calendar=False)
        self.assertAlmostEqual(base[36]['label'], adjusted[36]['label'])
        tr, va, te, bounds = chronological_split(base)
        self.assertLess(max(r['label_end'] for r in tr), min(r['day'] for r in va))
        self.assertLess(max(r['label_end'] for r in va), min(r['day'] for r in te))
        self.assertTrue(all(r['day']>=bounds['test_start'] for r in te))

    def test_save_reload_prediction_and_tamper(self):
        data = self.data()
        rows = dataset(data, calendar=False)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)/'model'
            result = train(rows, folder, 'synthetic')
            self.assertEqual(result['status'], 'OK')
            model = ReturnModel(folder)
            self.assertIsNotNone(model.predict(data).value)
            self.assertEqual(model.predict(data).value, ReturnModel(folder).predict(data).value)
            self.assertEqual(model.predict(data[:100]).error, 'date_not_after_training_and_validation')
            (folder/'model.txt').write_text('altered', encoding='utf-8')
            with self.assertRaises(ValueError):
                ReturnModel(folder)
            skipped = train(rows, Path(tmp)/'combined', 'synthetic', kind='combined')
            self.assertEqual(skipped['status'], 'SKIPPED')
            self.assertFalse((Path(tmp)/'combined').exists())

    def test_combined_model_trains_only_with_observed_independent_events(self):
        data=self.data()
        rows=dataset(data,calendar=False)
        for i,r in enumerate(rows):
            r['features'].update(news_missing=0.,news_quality_warning=0.,news_present=1.,news_revision_pct=float(i%7))
            r['news_event_ids']=['event-'+str(i)]
        # Low threshold is a synthetic test fixture only. Production default is 100.
        with tempfile.TemporaryDirectory() as tmp:
            result=train(rows,Path(tmp)/'combined','synthetic',kind='combined',min_news_events=10)
            self.assertEqual(result['status'],'OK')
            class News:
                def features(self,symbol,at):
                    return dict(news_missing=0.,news_quality_warning=0.,news_present=1.,news_revision_pct=1.),['event']
            model=ReturnModel(Path(tmp)/'combined',news=News())
            self.assertIsNotNone(model.predict(data).value)


if __name__ == '__main__':
    unittest.main()
