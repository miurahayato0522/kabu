"""Research-only bridges to existing chart and backtest contracts; no paper wiring."""
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import sqlite3
from system_data import stamp,daily_cutoff,provider,daily_groups,digest
from system_strategy import Prediction,MovingAverage,decide
from system_integration import BusinessImpactStrategy
from news_discovery import candidate_rows,settings,for_horizon


class DiscoveryNews:
    def __init__(self,rows,c):self.rows=rows;self.s=settings(c)
    def assessment(self,symbol,at,allow_indirect=False):
        when=stamp(at);events=[];unknown=False
        for r in self.rows:
            if r['symbol']!=symbol[:4] or stamp(r['discovered_at'])>when:continue
            a=r['article'];i=r['impact'];event=r['event']
            if not a.get('published_at') or not 0<=(when-stamp(a['published_at'])).total_seconds()<=7*86400:
                continue
            if stamp(a['first_seen_at'])>when or stamp(r['company']['recorded_at'])>when:continue
            if any(x['status']!='ok' or not x.get('finished_at') or stamp(x['finished_at'])>when for x in (i,event)):
                unknown=True;continue
            result=i['result']
            if r['relation_state']!='確認済み' or result['relation'] in ('不明','無関係') or (not r['named_in_news'] and not allow_indirect):
                unknown=True;continue
            direction=result[self.s['horizon']]
            if direction=='不明':unknown=True;continue
            available=max(stamp(a['first_seen_at']),stamp(a['published_at']),stamp(i['finished_at']),stamp(event['finished_at']),stamp(r['company']['recorded_at']))
            events.append(dict(short_term=direction,importance=result['importance'],event_id=r['article']['id'],
                analysis_id=i['id'],available_at=available.isoformat(),impact=result,candidate_id=r['id']))
        signs={e['short_term'] for e in events}
        return dict(status='UNKNOWN' if unknown or not events else 'AVAILABLE',events=events,
            positive='ポジティブ' in signs,negative='ネガティブ' in signs,
            severe_negative=any(e['short_term']=='ネガティブ' and e['importance']=='高' for e in events))


class NewsOnlyResearch:
    """No return forecast. Entries require verified news, two observed post-news closes,
    turnover and bounded price reaction. Existing Account applies cost/risk limits."""
    def __init__(self,news,c):
        self.news=news;self.s=settings(c);self.costs=c['costs'];self.name='discovery-news-only-v1:'+digest([self.s,self.costs])[:12]
    def predict(self,history,at=None):
        when=at or daily_cutoff(history[-1].day);now=stamp(when);b=history[-1]
        n=self.news.assessment(b.symbol,when,self.s['allow_indirect'])
        direction=0;reasons=['ニュース単独の研究戦略。上昇確率・予測リターンではない']
        if n['severe_negative']:direction=-1
        elif n['status']=='AVAILABLE' and n['positive'] and not n['negative']:
            event=max(stamp(e['available_at']) for e in n['events'])
            import exchange_calendars as xcals
            cal=xcals.get_calendar('XTKS',start=history[0].day,end=b.day)
            recent=[x for x in history if cal.session_close(x.day).to_pydatetime()>=event and stamp(x.available_at)<=now]
            enough=len(recent)>=2 and recent[-1].id==b.id
            if enough:
                import math
                reaction=b.close/(recent[0].close*math.prod(x.split_factor for x in recent[1:]))-1
                friction=2*self.costs['slippage_bps']/10000+self.costs['spread_bps']/10000+2*self.costs['fee']/(b.close*100)
                fresh=0<=(now-stamp(b.available_at)).total_seconds()<=86400 and 0<=(now-stamp(daily_cutoff(b.day))).total_seconds()<=86400
                liquid=b.close*b.volume>=self.s['min_turnover']
                direction=int(fresh and liquid and friction<reaction<=self.s['max_reaction'])
                reasons += [f'観測後の反応={reaction}; 往復コスト目安={friction}; fresh={fresh}; liquid={liquid}']
            else:reasons.append('分析完了後に利用可能な終値が2本未満。取得時点価格を補完しません')
        else:reasons.append('関連性・企業別影響が未確認、または好材料条件を満たしません')
        return Prediction(b.symbol,when,self.s['periods'][self.s['horizon']][0],None,self.name,direction,reasons,
                          [b.id]+['event:'+e['event_id'] for e in n['events']])


def report_rows(c,asof):
    c=for_horizon(c)
    rows=candidate_rows(c,asof);news=DiscoveryNews(rows,c);s=settings(c)
    if not rows:return []
    output=[]
    from system_status import sessions
    from system_status import account
    holdings=account(c,asof)['positions']
    day=sessions(asof)['analysis_day']
    chart=MovingAverage();model_error=None
    if c['chart']['strategy']=='lightgbm':
        try:
            from system_model import ReturnModel
            chart=ReturnModel(c['chart']['model'],c['chart'].get('threshold',0))
            if stamp(chart.metadata['created_at'])>asof or chart.metadata['kind']!='chart':raise ValueError('Unavailable chart model')
        except (OSError,ValueError,KeyError):model_error='chart_model_unavailable'
    policy=dict(c['integration'],allow_indirect=s['allow_indirect'])
    integrated=BusinessImpactStrategy(chart,news,policy)
    for r in rows:
        result=dict(r,chart_prediction=None,integrated_decision=None,news_only_decision=None,
                    final_state='WATCH_NEWS',execution_eligible=False,checks=[])
        try:
            if model_error:raise ValueError(model_error)
            bars=[b for b in provider(c['paths']['prices']).bars([r['symbol']]) if b.day<=day and stamp(b.available_at)<=asof]
            history=daily_groups(bars)[r['symbol']+'0']
            if history[-1].day!=day or asof<stamp(daily_cutoff(day)) or stamp(history[-1].fetched_at)<stamp(daily_cutoff(day)) or len(history)<21:
                raise ValueError('completed_price_history_unavailable')
            p=chart.predict(history,asof.isoformat())
            integrated_prediction=integrated.predict(history,asof.isoformat())
            low,high=s['periods'][s['horizon']]
            if not low<=p.horizon or (high is not None and p.horizon>high):
                integrated_prediction=replace(integrated_prediction,direction=0,
                    reasons=integrated_prediction.reasons+['チャート予測とニュースの選択期間が不一致。統合候補を保留'])
            held=holdings.get(r['symbol']+'0',0) if r['watched'] else 0
            result.update(chart_prediction=p.record(),integrated_decision=decide(integrated_prediction,held),held=held,
                news_only_decision=decide(NewsOnlyResearch(news,c).predict(history,asof.isoformat()),held))
            result['checks'].append('売買代金確認: '+str(history[-1].close*history[-1].volume>=s['min_turnover']))
            if p.error:result['checks'].append(p.error)
        except (OSError,ValueError,KeyError,sqlite3.Error) as exc:result['checks'].append(str(exc) if isinstance(exc,ValueError) else type(exc).__name__)
        result['checks'] += ['候補登録のみ。注文・監視リストの追加なし','企業関連性と当該案件への参画を別途確認','昇格には企業行動・資金・流動性・リスク上限の手動確認が必要']
        if r['impact']['status']!='ok':result['checks'].append('企業別AI解析待ち・失敗')
        if not result['chart_prediction']:result['final_state']='NEEDS_HISTORY_OR_MODEL'
        elif r['impact']['status']!='ok':result['final_state']='ANALYSIS_PENDING'
        elif result['integrated_decision']['action']=='BUY':result['final_state']='CHART_NEWS_CANDIDATE'
        elif result['integrated_decision']['action']=='SELL':result['final_state']='EXIT_REVIEW'
        elif r['impact']['result'][s['horizon']]=='ポジティブ':result['final_state']='NEWS_WATCH'
        else:result['final_state']='REVIEW_OR_NO_TRADE'
        output.append(result)
    return output


def backtest(c,asof):
    c=for_horizon(c)
    from system_backtest import run
    from system_risk import RiskLimits
    rows=candidate_rows(c,asof)
    # Only existing watch symbols. Discovering a new code never expands holdings.
    bars=provider(c['paths']['prices']).bars(list(c['symbols']))
    bars=[b for b in bars if stamp(b.available_at)<=asof]
    strategy=NewsOnlyResearch(DiscoveryNews(rows,c),c)
    return run(bars,strategy,initial=c['initial_cash'],limits=RiskLimits(**c['risk']),mode='recorded',**c['costs'])
