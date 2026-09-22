"""Explicit, uncalibrated fallback while independent news events are scarce."""
from dataclasses import replace
from system_data import daily_cutoff
from system_strategy import Prediction


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
