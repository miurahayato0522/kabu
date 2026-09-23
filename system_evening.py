"""Immutable next-session research reports from saved data only. No execution."""
from contextlib import closing
from datetime import datetime,timezone
from html import escape
import json
from pathlib import Path
import sqlite3
from system_data import provider,daily_groups,daily_cutoff,stamp,digest,encode,JST
from system_strategy import MovingAverage,Prediction,decide
from system_integration import BusinessImpactStrategy
from system_news import NewsStore
from system_status import snapshot,sessions
from system_queue import eligibility


class ReportNews(NewsStore):
    def assessment(self,symbol,at,allow_indirect=False):
        result=super().assessment(symbol,at,allow_indirect)
        if symbol[:4] in self.pending_codes:result['status']='UNKNOWN'
        return result


def context(c,now,status):
    coverage=[now.isoformat(),now.isoformat()] if status['news_coverage'] else None
    news=ReportNews(c['paths']['ai'] if Path(c['paths']['ai']).is_file() else None,coverage)
    news.pending_codes=set()
    from system_runtime import candidates
    selected=candidates(c,now)
    articles=[]
    for a in selected:
        if a.get('observed_at') and stamp(a['observed_at'])>now:continue
        reason=eligibility(a,c,now)
        known=[i for i in news.items if i['source'].get('url')==a['url'] and stamp(i['started_at'])<=now
               and i['source'].get('title')==a['title'] and i['source'].get('body')==a.get('body')
               and i['source'].get('published_at')==a.get('published_at')]
        item=known[-1] if known else None
        usable=bool(item and item['status']=='ok' and item['available_at'] and stamp(item['available_at'])<=now and not item['quality'] and item['result'].get('impacts'))
        if (not reason and not usable) or (reason and not reason.startswith('old_')):
            news.pending_codes.update(a['symbols'])
        articles.append(dict(source=a,state='対象外: '+reason if reason else ('解析済み' if usable else '解析待ち・失敗・未確認'),
            analysis_id=item['analysis_id'] if usable else None,result=item['result'] if usable else None,
            model=item['model'] if usable else None))
    # Archive only information actually available at this report's timestamp.
    news.items=[i for i in news.items if i['first_seen_at'] and stamp(i['first_seen_at'])<=now and stamp(i['started_at'])<=now]
    return news,articles


def averages(history):
    closes=[];points=[]
    for b in history:
        closes=[p*b.split_factor for p in closes]
        closes.append(b.close)
        points.append(dict(day=b.day,close=b.close,ma5=sum(closes[-5:])/5 if len(closes)>=5 else None,
                           ma20=sum(closes[-20:])/20 if len(closes)>=20 else None))
    change=(history[-1].close/(history[-2].close*history[-1].split_factor)-1)*100 if len(history)>1 else None
    return points,change


def build(c,now=None):
    now=now or datetime.now(timezone.utc)
    status=snapshot(c,now);calendar=status['calendar'];day=calendar['analysis_day']
    news,articles=context(c,now,status)
    chart=MovingAverage();model_error=None;metadata=None
    if c['chart']['strategy']=='lightgbm':
        try:
            from system_model import ReturnModel
            chart=ReturnModel(c['chart']['model'],c['chart'].get('threshold',0))
            metadata=chart.metadata
            if stamp(metadata['created_at'])>now:raise ValueError('future_model')
            if metadata['kind']!='chart':raise ValueError('chart_model_required')
        except (OSError,ValueError,KeyError,TypeError):model_error='モデルを読み込めないか、分析時点で利用できないモデルです'
    integrated=BusinessImpactStrategy(chart,news,c['integration'])
    results=[];inputs=[]
    for code,name in c['symbols'].items():
        symbol=code+'0';history=[];points=[];change=None;error=None;last=None
        try:
            available=provider(c['paths']['prices']).bars([code])
            history=[b for b in available if b.day<=day and stamp(b.available_at)<=now]
            last=max((b.day for b in history),default=None)
            if now<stamp(daily_cutoff(day)):raise ValueError('当日足は未確定扱い（16時JST以降に再実行）')
            if not history or last!=day:raise ValueError('分析対象営業日の確定日足がありません。日足更新が必要です')
            history=daily_groups(history)[symbol]
            if stamp(history[-1].fetched_at)<stamp(daily_cutoff(day)):raise ValueError('取引終了後に取得した日足が必要です')
            if len(history)<21:raise ValueError('5/20クロスに必要な21営業日の履歴がありません')
            points,change=averages(history)
            if model_error:raise ValueError(model_error)
            p=integrated.predict(history,now.isoformat())
        except (OSError,ValueError,KeyError,sqlite3.Error) as exc:
            error=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
            try:encode([b.record() for b in history])
            except ValueError:history=[]  # Invalid non-finite data cannot enter the JSON snapshot.
            p=Prediction(symbol,now.isoformat(),metadata['horizon'] if metadata else 1,None,
                         'lightgbm-unavailable' if model_error else chart.name,0,[error],[],error)
        held=status['account']['positions'].get(symbol,0)
        relevant=[a for a in articles if code in a['source']['symbols']]
        assessment=news.assessment(symbol,now.isoformat(),c['integration'].get('allow_indirect',False))
        ma=MovingAverage().predict(history,now.isoformat()).record() if len(history)>=21 and points else None
        final=decide(p,held)
        results.append(dict(symbol=symbol,name=name,analysis_day=day,last_data_day=last,
            close=history[-1].close if history else None,change_pct=change,
            ma5=points[-1]['ma5'] if points else None,ma20=points[-1]['ma20'] if points else None,
            ma_signal=ma,chart_return=p.value,forecast_horizon_sessions=None if model_error and not metadata else p.horizon,
            forecast_label='予測保留（モデル未確認）' if model_error else (f'{p.horizon}営業日後の終値リターン予測（翌日リターンではありません）' if metadata else '5/20移動平均クロス（リターン予測なし）'),
            chart_model='lightgbm-unavailable' if model_error else chart.name,model_version=metadata,news=assessment,articles=relevant,
            decision=final,held=held,points=points[-120:],
            fetched_at=history[-1].fetched_at if history else None,
            freshness='確定足あり' if not error else '保留',
            checks=['翌営業日の企業行動確認','寄付後の新鮮な価格・独立したリスク上限確認',
                    'レポート候補を自動注文しない。実行時に再判断']+status['stop_new_reasons']))
        inputs += [b.record() for b in history]
    # AI results unfinished at analysis time are not copied as known facts.
    known=[i for i in news.items if i['available_at'] and stamp(i['available_at'])<=now and i['status']=='ok']
    source=dict(bars=inputs,news=known,articles=articles,configuration=c,
                account_snapshot=status['account'],queue_snapshot=status['queue'],jobs_snapshot=status['jobs'])
    from discovery_integration import report_rows
    try:discovered=report_rows(c,now)
    except (OSError,ValueError,KeyError,sqlite3.Error):
        discovered=[];status['warnings'].append('関連銘柄DB/設定を確認してください。発見結果は利用不可')
    source['discovery']=discovered
    return dict(version='evening-v1',at=now.isoformat(),at_jst=now.astimezone(JST).isoformat(),
        analysis_day=day,next_session=calendar['next_session'],status=status,results=results,
        discovered_companies=discovered,
        input_hash=digest(source),inputs=source,
        limitations=['研究用の翌営業日候補。実注文・仮想注文の予約ではありません',
                    '16時JSTを確定足利用の保守的な境界とします。配信元の完全性は保証しません',
                    '口座は最終保存時点の評価です。ニュースの影響分類は株価予測ではありません'])


def html(report):
    def e(value):return escape(str(value))
    def pretty(value):return '<pre>'+e(json.dumps(value,ensure_ascii=False,indent=2))+'</pre>'
    cards=[]
    for r in report['results']:
        news='解析待ち・収集未確認' if r['news']['status']=='UNKNOWN' else ('対象範囲に関連解析イベントなし' if r['news']['status']=='NONE' else '好悪材料解析あり')
        recent=sorted(r['articles'],key=lambda a:a['source'].get('published_at') or '',reverse=True)[:5]
        article_html=''.join('<li>'+e(a['source']['title'])+' — '+e(a['state'])+
            '<br>公表: '+e(a['source'].get('published_at'))+' / 初回取得: '+e(a['source'].get('first_seen_at'))+
            ('<details><summary>AI解析の詳細</summary>'+pretty(a['result'])+'</details>' if a['result'] else '')+'</li>' for a in recent)
        cards.append('<section><h2>'+e(r['name'])+' '+e(r['symbol'])+' — '+e(r['decision']['action'])+'</h2>'+
            '<p>日足: '+e(r['last_data_day'])+' / '+e(r['freshness'])+' / 取得: '+e(r['fetched_at'])+'</p>'+
            '<p>終値 '+e(r['close'])+' / 前日比（分割考慮） '+e(r['change_pct'])+'% / MA5 '+e(r['ma5'])+' / MA20 '+e(r['ma20'])+'</p>'+
            '<p>'+e(r['forecast_label'])+' / 予測値 '+e(f"{r['chart_return']*100:.2f}%" if r['chart_return'] is not None else 'なし')+' / 仮想保有 '+e(r['held'])+'株</p>'+
            '<p>MA方向: '+e((r['ma_signal'] or {}).get('direction'))+' / チャートモデル: '+e(r['chart_model'])+'</p>'+
            '<h3>判断根拠・見送り理由</h3>'+pretty(r['decision']['prediction']['reasons'])+
            '<h3>ニュース: '+e(news)+'</h3><details><summary>好悪材料の根拠</summary>'+pretty(r['news'])+'</details><ul>'+article_html+'</ul><p>最新5件まで表示。全件はJSONに保存。</p>'+
            ('<img alt="終値・移動平均と仮想約定" src="'+e(r['symbol'])+'.png">' if r.get('chart_image') else '')+
            '<h3>翌営業日の確認事項</h3>'+pretty(r['checks'])+'</section>')
    s=report['status'];a=s['account']
    discovery_cards=[]
    for r in report.get('discovered_companies',[]):
        impact=r['impact'].get('result') or {};event=r['event'].get('result') or {}
        chart=r.get('chart_prediction') or {}
        discovery_cards.append('<section><h3>'+e(r['company']['name'])+' '+e(r['symbol'])+' — '+e(r['final_state'])+'</h3>'+
            '<p>'+('既存監視銘柄' if r['watched'] else '新たな分析候補（監視対象には未追加）')+'</p>'+
            '<p>'+e(r['article']['title'])+'<br>出典: '+e(r['article']['url'])+' / 公表: '+e(r['article'].get('published_at'))+'</p>'+
            '<p>産業: '+e([x['topic'] for x in event.get('industries',[])])+' / '+e(r['relation'])+' / 関係: '+e(r['relation_state'])+'</p>'+
            '<p>短期 '+e(impact.get('short','解析待ち'))+' / 中期 '+e(impact.get('medium','解析待ち'))+' / 長期 '+e(impact.get('long','解析待ち'))+'</p>'+
            '<p>チャート予測: '+e(chart.get('value'))+' / 対象 '+e(chart.get('horizon'))+'営業日。ニュースの好悪は上昇確率ではありません。</p>'+
            '<p>'+e(r['project_participation'])+'</p>'+pretty(r['checks'])+
            '<details><summary>企業情報・出典・影響根拠・統合判断</summary>'+pretty(dict(company=r['company'],event=event,impact=impact,periods=r['periods'],integrated=r['integrated_decision'],news_only=r['news_only_decision']))+'</details></section>')
    def number(value):return f'{value:,.0f}' if isinstance(value,(int,float)) else '未確認'
    metrics=''.join('<div class="metric"><small>'+e(label)+'</small><strong>'+e(number(value))+'</strong></div>' for label,value in [
        ('仮想資産（円）',a.get('equity')),('現金（円）',a.get('cash')),('実現損益（円）',a.get('realized')),
        ('評価損益（円）',a.get('unrealized')),('保有銘柄数',sum(bool(q) for q in a['positions'].values())),
        ('解析待ち',s['queue']['counts'].get('pending',0)),('次回API送信予定',s['queue']['planned_send_count'])])
    notices='<ul>'+''.join('<li>'+e(x)+'</li>' for x in s['warnings']+s['errors']+s['stop_new_reasons'])+'</ul>'
    job_table='<table><tr><th>処理</th><th>状態</th><th>最終実行（JST）</th></tr>'+''.join(
        '<tr><td>'+e(label)+'</td><td>'+e(s['jobs'].get(key,{}).get('status','未実行'))+'</td><td>'+e(s['jobs'].get(key,{}).get('finished_at_jst'))+'</td></tr>'
        for key,label in [('daily','日足'),('news','ニュース収集'),('analysis','ニュースAI'),('paper','仮想売買')])+'</table>'
    summary='<section><h2>稼働状況・仮想口座</h2><p>'+e(s['bot_state'])+' / '+e(s['market_state'])+'</p><div class="metrics">'+metrics+'</div>'+\
        '<p>口座評価日時: '+e(a.get('at'))+' / '+e(a['status'])+'</p>'+notices+job_table+\
        '<p>日足最終日: '+e(s['daily_prices']['day'])+' / 取得: '+e(s['daily_prices']['at'])+'</p>'+\
        '<details><summary>API予算・保有株・取得時刻の詳細</summary>'+pretty(dict(budget=s['queue']['budget'],positions=a['positions'],last_tick_received_at=s['last_tick_received_at']))+'</details></section>'
    return '<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'+\
        '<title>翌営業日分析</title><style>body{font-family:system-ui;margin:20px auto;padding:0 12px;max-width:1000px;background:#f4f6fa;color:#152238}section{background:white;padding:18px;margin:14px 0;border-radius:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}img{max-width:100%}li{margin:12px 0}h1{font-size:25px}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px}.metric{padding:12px;background:#eef3fa;border-radius:8px}.metric strong{display:block;font-size:22px}table{border-collapse:collapse;width:100%;font-size:13px;overflow-wrap:anywhere}td,th{text-align:left;padding:8px;border-bottom:1px solid #ddd}summary{cursor:pointer;padding:8px}p{overflow-wrap:anywhere}</style>'+\
        '<h1>翌営業日向け分析 — '+e(report['next_session'])+'</h1><p>分析: '+e(report['at_jst'])+' / 日足対象: '+e(report['analysis_day'])+'</p>'+\
        '<p>研究用候補・注文なし。保存済み結果の表示であり、再計算しません。</p>'+\
        '<details><summary>RSS検索別の取得状態（正常0件と失敗）</summary>'+pretty(s['rss_queries'])+'</details>'+\
        summary+''.join(cards)+'<h2>ニュースから発見した関連銘柄（研究用）</h2>'+(''.join(discovery_cards) or '<p>保存済みの発見候補はありません。news_discovery.pyで企業情報の登録と候補探索を実行してください。</p>')+pretty(report['limitations'])+'</html>'


def save(c,now=None,folder=None):
    report=build(c,now)
    root=Path(c['paths']['runtime']).parent/'reports'
    target=Path(folder) if folder else root/('evening_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f'))
    target.mkdir(parents=True,exist_ok=False)
    from system_plot import render_evening
    for r in report['results']:
        if r['points']:
            try:
                render_evening(r,report['status']['account'].get('fills',[]),target)
                r['chart_image']=True
            except (ImportError,OSError,ValueError):r['chart_warning']='グラフ生成失敗。JSON・判断結果は保持'
    (target/'report.json').write_text(encode(report),encoding='utf-8')
    (target/'report.html').write_text(html(report),encoding='utf-8')
    return target,report


def auto_report(c,db,now):
    from datetime import timedelta
    retry=db.execute("SELECT value FROM runtime_meta WHERE key='evening_report_check'").fetchone()
    if retry and stamp(retry[0])>now:return
    db.execute("INSERT OR REPLACE INTO runtime_meta VALUES ('evening_report_check',?)",((now+timedelta(minutes=5)).isoformat(),));db.commit()
    cal=sessions(now)
    if now<stamp(daily_cutoff(cal['analysis_day'])):return
    # Generate only after every required closing bar is present; retry on later updates.
    try:
        bars=provider(c['paths']['prices']).bars(list(c['symbols']))
        ready=[b for b in bars if b.day==cal['analysis_day'] and stamp(b.available_at)<=now and stamp(b.fetched_at)>=stamp(daily_cutoff(b.day))]
        if len(ready)!=len(c['symbols']):return
        key=digest([cal['analysis_day'],sorted(b.id for b in ready),c['chart']])
        if db.execute("SELECT value FROM runtime_meta WHERE key='evening_report'").fetchone()==(key,):return
        target,_=save(c,now)
        db.execute("INSERT OR REPLACE INTO runtime_meta VALUES ('evening_report',?)",(key,))
        db.execute("INSERT INTO logs(at,job,status,detail) VALUES (?,'evening_report','ok',?)",(now.isoformat(),str(target)))
        db.commit()
    except (OSError,ValueError,sqlite3.Error) as exc:
        # Reporting failure must not stop collection or paper safety checks.
        db.execute("INSERT INTO logs(at,job,status,detail) VALUES (?,'evening_report','failed',?)",(now.isoformat(),type(exc).__name__))
        db.commit()
