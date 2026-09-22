"""Explicit, uncalibrated fallback while independent news events are scarce."""
from dataclasses import replace
from system_data import daily_cutoff
from system_strategy import Prediction
from system_data import digest


class IntegratedStrategy:
    def __init__(self, chart, news, news_only=False):
        self.chart, self.news, self.news_only = chart, news, news_only
        self.name = 'B-news-events-rule-v1' if news_only else 'C-chart-news-veto-v1+'+chart.name

    def predict(self, history, at=None):
        b = history[-1]
        when = at or daily_cutoff(b.day)
        f, events = self.news.features(b.symbol, when)
        if self.news_only:
            p = Prediction(b.symbol, when, 5, None, self.name, 0,
                           ['News-event rule; outside events no trade'], events)
        else:
            p = self.chart.predict(history, when)
        if p.error:
            return p
        revision = f['news_revision_pct']
        confirmed = f['news_direct'] and f['news_body'] and not f['news_quality_warning'] and revision is not None
        reasons = list(p.reasons)
        direction = p.direction
        if self.news_only:
            direction = (int(revision > 0)-int(revision < 0)) if confirmed else 0
        elif confirmed and revision < 0:
            # A transparent veto/exit rule, not a learned weight or probability.
            direction = -1
            reasons.append('Confirmed negative operating-profit revision: exit/veto')
        if f['news_missing']:
            if direction > 0:
                direction = 0
            reasons.append('News coverage/analysis incomplete: new buys blocked')
        return replace(p, model=self.name+'+'+p.model, direction=direction,
                       reasons=reasons, refs=p.refs+events)


class BusinessImpactStrategy:
    """Research policy: chart return is immutable; news changes only direction/reasons."""
    def __init__(self, chart, news, policy=None):
        self.chart,self.news=chart,news
        self.policy=dict(no_news='allow_chart',unknown='block_buy',negative='veto_buy',
                         severe_negative='sell',allow_indirect=False)
        if policy:
            self.policy.update(policy)
        if set(self.policy)!={'no_news','unknown','negative','severe_negative','allow_indirect'} or self.policy['no_news'] not in ('allow_chart','block_buy') or self.policy['unknown']!='block_buy' or self.policy['negative'] not in ('veto_buy','sell') or self.policy['severe_negative'] not in ('sell','veto_buy') or type(self.policy['allow_indirect']) is not bool:
            raise ValueError('Invalid integration policy')
        self.name='business-impact-v1:'+chart.name+':'+digest(self.policy)[:12]

    def predict(self,history,at=None):
        when=at or daily_cutoff(history[-1].day)
        p=self.chart.predict(history,when)
        if p.error:
            return p
        news=self.news.assessment(p.symbol,when,self.policy['allow_indirect'])
        direction,reasons=p.direction,list(p.reasons)
        if news['severe_negative'] and self.policy['severe_negative']=='sell':
            direction=-1
            reasons.append('E: severe negative business news; exit candidate')
        elif news['negative']:
            direction=-1 if self.policy['negative']=='sell' else min(direction,0)
            reasons.append('B: negative business news; buy veto')
        elif news['status']=='UNKNOWN':
            direction=min(direction,0)
            reasons.append('News collection/analysis incomplete; new buys blocked')
        elif news['status']=='NONE':
            if self.policy['no_news']=='block_buy':
                direction=min(direction,0)
            reasons.append('C: no relevant analyzed event in monitored scope; '+self.policy['no_news'])
        elif news['positive']:
            reasons.append('A: chart and positive business news' if direction>0 else 'D: positive news without chart buy; watch only')
        else:
            reasons.append('Neutral business news; retain chart direction')
        return replace(p,model=self.name,direction=direction,reasons=reasons,
                       refs=p.refs+['event:'+e['event_id'] for e in news['events']])
