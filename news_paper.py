"""ニュース条件とチャート条件の前向き観測。発注・API通信・資金配分なし。"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import exchange_calendars as xcals
from news_market import indicators, stamp, JST
from news_numbers import calculate
from daily_backtest import positive, split_factor
from jquants_history import symbol_code, HistoryError

ROOT = Path(__file__).resolve().parent
POLICY = 'paper-v1'


def encode(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False)


def schedule(when):
    start = when.astimezone(JST).date() + timedelta(days=1)
    end = start + timedelta(days=90)
    cal = xcals.get_calendar('XTKS', start=str(start), end=str(end))
    return [d.date().isoformat() for d in cal.sessions if start<=d.date()<=end][:20]


def prices(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        sources = [r[0] for r in db.execute('SELECT DISTINCT source FROM daily_prices')]
        if len(sources)!=1 or sources[0] not in ('yahoo_reconstructed_v1','jquants_v2'):
            raise ValueError('価格DBの取得元が空・混在・未対応です')
        result = {}
        for code, fetched, payload in db.execute('SELECT code,fetched_at,payload FROM daily_prices ORDER BY day'):
            result.setdefault(code,[]).append((json.loads(payload),fetched))
    return sources[0], result


def decision(analysis, records, now, price_source, test_data=False):
    source, ai = json.loads(analysis['source_json']), json.loads(analysis['result_json'])
    times = [stamp(analysis['finished_at']),stamp(source['first_seen_at']),stamp(source['observed_at'])]
    if source.get('body_received_at'):
        times.append(stamp(source['body_received_at']))
    if max(times)>now:
        raise ValueError('分析・取得日時が現在より未来です')
    chart_reasons, news_reasons, chart = [], [], None
    try:
        chart = indicators(records,now,'recorded')
        day=now.astimezone(JST).date()
        cal=xcals.get_calendar('XTKS',start=str(day-timedelta(days=100)),end=str(day))
        expected=[d.date().isoformat() for d in cal.sessions if d.date()<day][-21:]
        if [r['Date'] for r in chart['bars']]!=expected:
            chart_reasons.append('直前21営業日の価格に欠損または日付の不一致')
        if chart['age_calendar_days']>7:
            chart_reasons.append('日足が7暦日超古い')
        if not (chart['close']>chart['ma20'] and chart['ma5']>chart['ma20'] and chart['volume_ratio_20d']>=1):
            chart_reasons.append('価格・出来高条件を満たさない')
    except (ValueError,KeyError,HistoryError) as exc:
        chart_reasons.append(str(exc))
    if not source.get('published_at'):
        news_reasons.append('公表日時不明')
    else:
        age = (now-stamp(source['published_at'])).total_seconds()
        if not 0<=age<=7*86400:
            news_reasons.append('公表日時が未来または7日超前')
    symbol = source['symbols'][0]
    if len(source['symbols'])!=1 or not any(r['symbol']==symbol and r['relation']=='直接' for r in ai.get('relations',[])):
        news_reasons.append('対象企業への直接関連を特定できない')
    numbers = calculate(ai,source) if 'body' in source else None
    revisions = [r for r in (numbers or {}).get('changes',[]) if r['metric'] in ('営業利益','連結営業利益')]
    if not analysis['version'].startswith('body-') or not numbers or numbers['issues'] or ai.get('quality_warnings'):
        news_reasons.append('本文・数値抽出が未確認または品質上の問題あり')
    if len(revisions)!=1 or float(revisions[0]['before_yen'])<=0 or float(revisions[0]['after_yen'])<=float(revisions[0]['before_yen']):
        news_reasons.append('正の営業利益予想を上方修正した一組の数値がない')
    return dict(policy=POLICY,article_id=analysis['article_id'],analysis_hash=analysis['request_hash'],
                symbol=symbol,title=source['title'],decision_at=now.isoformat(),test_data=test_data,
                source=source,ai_result=ai,price_source=price_source,chart=chart,number_calculations=numbers,
                chart_candidate=not chart_reasons,combined_candidate=not chart_reasons and not news_reasons,
                chart_reasons=chart_reasons,news_reasons=news_reasons,planned_sessions=schedule(now),
                calendar='XTKS',calendar_version=xcals.__version__,slippage_bps_each_side=5)


def open_ledger(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path)
    db.execute('CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
    db.commit()
    return db


def save(db, item):
    # 同一記事の後日の再実行・再分析で条件を選び直さない。
    ident=hashlib.sha256(encode([item['policy'],item['article_id'],item['symbol'],item['test_data']]).encode()).hexdigest()
    with db:
        return db.execute('INSERT OR IGNORE INTO decisions VALUES (?,?)',(ident,encode(item))).rowcount==1


def observe(item, records, now):
    eligible={}
    for row,fetched in records:
        if row['Date']<now.astimezone(JST).date().isoformat() and stamp(fetched)<=now:
            if row['Date'] in eligible:
                raise ValueError('価格に日付の重複があります')
            eligible[row['Date']]=row
    outcomes=[]
    for n in (1,5,20):
        days=item['planned_sessions'][:n]
        out=dict(sessions=n,entry_date=days[0],exit_date=days[-1],status='待機中',return_pct=None)
        if days[-1]>=now.astimezone(JST).date().isoformat():
            outcomes.append(out);continue
        if any(d not in eligible for d in days):
            out.update(status='価格欠損',missing_dates=[d for d in days if d not in eligible])
            outcomes.append(out);continue
        rows=[eligible[d] for d in days]
        try:
            if not positive(rows[0]['O']) or not positive(rows[-1]['C']):
                raise ValueError('始値・終値が不正')
            shares=1.0
            for row in rows[1:]:
                shares/=split_factor(row)
            entry=rows[0]['O'];exit_value=rows[-1]['C']*shares
            cost=item['slippage_bps_each_side']/10000
            out.update(status='評価済み',entry_open=entry,exit_close=rows[-1]['C'],
                       gross_return_pct=(exit_value/entry-1)*100,
                       return_pct=(exit_value*(1-cost)/(entry*(1+cost))-1)*100,
                       price_rows=rows,price_hash=hashlib.sha256(encode(rows).encode()).hexdigest())
        except (ValueError,HistoryError,KeyError) as exc:
            out.update(status='価格・調整エラー',error=str(exc))
        outcomes.append(out)
    return outcomes


def summarize(items):
    result=[]
    for n in (1,5,20):
        for strategy in ('chart_candidate','combined_candidate'):
            selected=[i for i in items if i['decision'][strategy] and not i['decision']['test_data']]
            values=[o['return_pct'] for i in selected for o in i['outcomes'] if o['sessions']==n and o['status']=='評価済み']
            result.append(dict(strategy=strategy,sessions=n,candidates=len(selected),evaluated=len(values),
                               unresolved=len(selected)-len(values),mean_return_pct=sum(values)/len(values) if values else None))
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['record','report'])
    p.add_argument('--ai-db',type=Path,default=ROOT/'data/news_ai.sqlite3')
    p.add_argument('--prices-db',type=Path,default=ROOT/'data/yahoo.sqlite3')
    p.add_argument('--ledger',type=Path,default=ROOT/'data/news_paper.sqlite3')
    p.add_argument('--test-data',action='store_true',help='テスト資料として本番集計から除外')
    a=p.parse_args(argv)
    now=datetime.now(timezone.utc)
    try:
        price_source,market=prices(a.prices_db)
        if a.command=='record':
            with closing(sqlite3.connect(a.ai_db.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                db.row_factory=sqlite3.Row
                analyses=[dict(r) for r in db.execute('SELECT * FROM ai_analyses ORDER BY started_at DESC,request_hash')]
            seen=set()
            with closing(open_ledger(a.ledger)) as ledger:
                for analysis in analyses:
                    if analysis['article_id'] in seen:continue
                    seen.add(analysis['article_id'])
                    if analysis['status']!='ok':
                        print(analysis['article_id'][:12],'分析失敗・未完了のため記録対象外');continue
                    source=json.loads(analysis['source_json'])
                    if len(source['symbols'])!=1:
                        print(analysis['article_id'][:12],'複数企業のため記録対象外');continue
                    item=decision(analysis,market.get(symbol_code(source['symbols'][0]),[]),now,price_source,a.test_data)
                    added=save(ledger,item)
                    if not added:
                        print(item['symbol'],'記録済み（初回の判断を保持）');continue
                    print(item['symbol'],'記録' if added else '記録済み',
                          'チャートのみ:',item['chart_candidate'],'ニュース併用:',item['combined_candidate'],
                          '理由:', ' / '.join(item['chart_reasons']+item['news_reasons']) or '条件一致')
        else:
            with closing(sqlite3.connect(a.ledger.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                decisions=[json.loads(r[0]) for r in db.execute('SELECT payload FROM decisions ORDER BY id')]
            items=[]
            for item in decisions:
                if item['price_source']!=price_source:raise ValueError('判断時と評価時の価格取得元が異なります')
                items.append(dict(decision=item,outcomes=observe(item,market.get(symbol_code(item['symbol']),[]),now)))
            report=dict(policy=POLICY,evaluated_at=now.isoformat(),summary=summarize(items),items=items,
                        limitations='同じニュース観測対象内での比較。日々の全銘柄戦略やポートフォリオ損益ではない。配当・税金・手数料なし。往復各5bpsの滑りを仮定。価格訂正で事後評価が変わる可能性あり。')
            output=ROOT/'data'/('news_paper_'+datetime.now().strftime('%Y%m%d_%H%M%S%f'))
            output.mkdir(parents=True,exist_ok=False)
            (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            lines=['# 仮想判断の追跡','',report['limitations'],'','## 比較（テスト資料を除く）','',
                   '|条件|営業日数|候補数|評価済み|未評価|平均騰落率|','|---|---:|---:|---:|---:|---:|']
            for s in report['summary']:
                lines.append(f"|{s['strategy']}|{s['sessions']}|{s['candidates']}|{s['evaluated']}|{s['unresolved']}|{s['mean_return_pct']}|")
            for i in items:
                d=i['decision']
                lines+=['',f"## {d['symbol']} {d['title']}",f"判断時刻: {d['decision_at']} / テスト: {d['test_data']}",
                        '理由: '+' / '.join(d['chart_reasons']+d['news_reasons'])]
                lines += [f"{o['sessions']}営業日: {o['status']} / 騰落率 {o['return_pct']} / {o['entry_date']} → {o['exit_date']}" for o in i['outcomes']]
            (output/'report.md').write_text('\n\n'.join(lines),encoding='utf-8')
            print('結果:',output/'report.md')
        return 0
    except (OSError,ValueError,KeyError,sqlite3.Error,HistoryError) as exc:
        print('停止:',exc);return 1


if __name__=='__main__':
    raise SystemExit(main())
