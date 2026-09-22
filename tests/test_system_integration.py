import unittest
from system_integration import IntegratedStrategy
from system_strategy import Prediction, decide
from system_backtest import run
from test_system_backtest import bars


class FixedChart:
    name='fixed'
    def predict(self,history,at):
        return Prediction(history[-1].symbol,at,5,.02,self.name,1,[],[history[-1].id])


class FixedNews:
    def __init__(self,missing=0,revision=-10):
        self.missing,self.revision=missing,revision
    def features(self,symbol,at):
        return dict(news_revision_pct=self.revision,news_direct=1,news_body=1,
                    news_quality_warning=0,news_missing=self.missing),['event']


class IntegrationTests(unittest.TestCase):
    def test_negative_news_veto_and_prediction_separation(self):
        data=bars([100]*70)
        combined=IntegratedStrategy(FixedChart(),FixedNews())
        p=combined.predict(data)
        self.assertEqual(p.value,.02)
        self.assertEqual(decide(p,100)['action'],'SELL')
        self.assertEqual(decide(p,0)['action'],'NO_TRADE')
        uncertain=IntegratedStrategy(FixedChart(),FixedNews(missing=1,revision=10))
        self.assertEqual(decide(uncertain.predict(data),0)['action'],'NO_TRADE')
        a=run(data,FixedChart(),calendar=False)
        c=run(data,combined,calendar=False)
        self.assertEqual(len(a['decisions']),len(c['decisions']))
        self.assertGreater(a['trade_count'],c['trade_count'])


if __name__=='__main__':
    unittest.main()
