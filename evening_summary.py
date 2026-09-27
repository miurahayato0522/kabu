"""Concise human view; full immutable inputs and decisions remain in report.json."""
from html import escape
from collections import Counter
import json


def enrich(report,c,now):
    from news_quality import annotate
    from corporate_review import review
    articles=report['inputs']['articles']
    audited=annotate([dict(a['source'],_index=i) for i,a in enumerate(articles)],c,now)
    for a in audited:articles[a['_index']]['quality']=a['quality']
    grouped={k:[] for k in ('company_direct','industry','macro','noise_or_reference')}
    for a in articles:grouped[a['quality']['category']].append(a)
    for key in grouped:grouped[key].sort(key=lambda a:a['source'].get('published_at') or '',reverse=True)
    report['news_sections']=grouped
    report['corporate_review']=review(c,now,status=report['status'])
    corporate={r['symbol']:r for r in report['corporate_review']}
    for r in report['results']:
        code=r['symbol'][:4]
        direct=[a for a in grouped['company_direct'] if code in a['quality']['direct_symbols']]
        # Sector matches are background links, never direct favorable/unfavorable evidence.
        industry=[]
        r['company_direct']=direct
        r['industry_background']=industry
        r['indirect_materials']=[e for e in r['news'].get('events',[]) if e.get('relation')=='間接']
        fetch=[q for q in report['status']['rss_queries'] if q['symbol']==code]
        collection=('正常' if fetch[-1]['status']=='ok' else '失敗') if fetch else ('未取得' if code in c['symbols'] else '対象範囲外')
        if fetch and collection=='正常':
            from system_data import stamp
            if (now-stamp(fetch[-1]['finished_at'])).total_seconds()>c['intervals']['news']*2:collection='収集成功・鮮度外'
        related=[a for a in articles if code in a['quality']['direct_symbols']]
        counts=Counter('excluded' if a['quality']['reason'] else a.get('analysis_status','pending') for a in related)
        r['news_status']=dict(collection=collection,candidates=len(related),ai_ok=counts['ok'],pending=counts['pending'],failed=counts['failed'],
            excluded=counts['excluded'],exclusion_reasons=dict(Counter(a['quality']['reason'] for a in related if a['quality']['reason'])),new_events='新着ニュースなし' if not direct else '候補あり')
        reasons=[]
        if r['decision']['prediction']['error']:reasons.append('DATA_STALE' if r['last_data_day'] else 'PRICE_UNAVAILABLE')
        if r['news']['status']=='UNKNOWN':reasons.append('NEWS_PENDING')
        if corporate[code]['status']!='CONFIRMED':reasons.append('CORPORATE_ACTION_UNVERIFIED')
        if report['status']['market_state']!='取引時間内':reasons.append('MARKET_CLOSED')
        if report['status']['stop_new_reasons']:reasons.append('SAFETY_CHECK_REQUIRED')
        if any('上限' in x or 'リスク' in x for x in report['status']['stop_new_reasons']):reasons.append('RISK_BLOCK')
        signal=(r.get('chart_signal') or r['ma_signal'] or {}).get('direction',0)
        reasons.append('BUY_CANDIDATE' if signal>0 else ('SELL_SIGNAL' if signal<0 else 'NO_SIGNAL'))
        r['display_reasons']=reasons
    # One lookup per report; no LLM and no inferred corporate role.
    from news_discovery import settings
    from company_graph import companies
    from pathlib import Path
    p=settings(c)['companies_db']
    registry={x['symbol']:x for x in companies(p,now)} if Path(p).is_file() else {}
    for r in report['results']:
        topics={x['topic'] for x in registry.get(r['symbol'][:4],{}).get('relations',[]) if x['state']=='確認済み'}
        r['industry_background']=[a for a in grouped['industry'] if topics.intersection(a['quality']['topics'])]
    return report


LABELS=dict(NO_SIGNAL='売買シグナルなし',NEWS_PENDING='ニュース確認待ち',CORPORATE_ACTION_UNVERIFIED='企業行動未確認',
    PRICE_UNAVAILABLE='価格不足',RISK_BLOCK='リスク制限',MARKET_CLOSED='市場時間外',DATA_STALE='日足不足・未確定',
    SELL_SIGNAL='売りシグナル',BUY_CANDIDATE='買い候補',SAFETY_CHECK_REQUIRED='安全条件未達')

def render(r):
    e=lambda x:escape(str(x))
    def news(items,limit):
        visible=[a for a in items if a.get('quality',{}).get('reason') not in ('DUPLICATE_EVENT','STALE_NEWS','UNKNOWN_PUBLICATION_TIME')][:limit]
        return '<ul>'+''.join('<li>'+e(a['source']['title'])+' — '+e(a['state'])+'</li>' for a in visible)+'</ul>'
    s=r['status'];sections=r.get('news_sections',{})
    summary='<section><h2>Executive Summary</h2><p>分析対象 '+e(r['analysis_day'])+' / 翌営業日 '+e(r['next_session'])+'</p>'
    summary+='<p>Bot: '+e(s['bot_state'])+' / '+e(s['market_state'])+' / 日足: '+e(s['daily_prices']['day'])+'</p>'
    summary+='<p>現在値最終受信: '+e(s['last_tick_received_at'])+' / 収集: '+e(s['jobs'].get('news',{}).get('status','未確認'))+' / AI: '+('DRY_RUN' if s['queue']['dry_run'] else e(s['jobs'].get('analysis',{}).get('status')) )+'</p>'
    missing=sum(x['status']!='CONFIRMED' for x in r.get('corporate_review',[]))
    summary+='<p>企業行動: 未確認 '+str(missing)+'社 / 仮想資産: '+e(s['account'].get('equity'))+'円 / 現金: '+e(s['account'].get('cash'))+'円</p>'
    summary+='<p>新規買い: '+('停止' if s['stop_new_reasons'] or s['market_state']!='取引時間内' else '実行時に価格・リスクを再確認')+'</p><ul>'+''.join('<li>'+e(x)+'</li>' for x in s['stop_new_reasons'])+'</ul></section>'
    summary+='<section><h2>注目企業（注文指示ではありません）</h2><ul>'+''.join('<li>'+e(x['name'])+' / '+e('・'.join(LABELS.get(k,k) for k in x.get('display_reasons',[])))+'</li>' for x in r['results'] if 'BUY_CANDIDATE' in x.get('display_reasons',[]) or 'SELL_SIGNAL' in x.get('display_reasons',[]))+'</ul></section>'
    for key,title in [('macro','重要な市場全体の材料'),('industry','重要な業界別材料'),('company_direct','新しい企業固有ニュース')]:
        summary+='<section><h2>'+title+'</h2>'+news(sections.get(key,[]),5)+'</section>'
    cards=[]
    for x in r['results']:
        cards.append('<details><summary>'+e(x['name'])+' '+e(x['symbol'])+' — '+e('・'.join(LABELS.get(k,k) for k in x.get('display_reasons',[])))+'</summary>'+
            '<p>終値 '+e(x['close'])+' / MA5 '+e(x['ma5'])+' / MA20 '+e(x['ma20'])+' / 保有 '+e(x['held'])+'</p>'+
            '<p>'+e(x['forecast_label'])+' / チャート予測 '+e(x['chart_return'])+'</p>'+
            '<p>ニュース: '+e(x.get('news_status'))+'</p><h3>企業固有材料（上位3件）</h3>'+news(x.get('company_direct',[]),3)+
            '<h3>業界の背景（上位2件・好悪未確定）</h3>'+news(x.get('industry_background',[]),2)+
            ('<img alt="終値・MA" src="'+e(x['symbol'])+'.png">' if x.get('chart_image') else '')+'</details>')
    found=r.get('discovered_companies',[])
    appendix='<details><summary>ニュースから発見した関連銘柄（研究用・上位10件 / 全'+str(len(found))+'件）</summary><ul>'+''.join('<li>'+e(x['company']['name'])+' '+e(x['symbol'])+' / '+e(x['article']['title'])+' / '+e(x['final_state'])+' / 新たな分析候補（監視対象には未追加）</li>' for x in found[:10])+'</ul><p>全件・根拠はJSONに保存。監視対象へ自動追加しません。</p></details>'
    appendix+='<details><summary>企業行動レビュー・性能・診断</summary><pre>'+e(json.dumps(dict(corporate_review=r.get('corporate_review'),performance=r.get('performance'),limitations=r['limitations']),ensure_ascii=False,indent=2))+'</pre></details>'
    return '<!doctype html><html lang="ja"><meta charset="utf-8"><title>翌営業日分析</title><style>body{font-family:system-ui;max-width:1000px;margin:auto;padding:20px;background:#f4f6fa}section,details{background:white;padding:14px;margin:12px 0}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere}img{max-width:100%}</style><h1>翌営業日レポート</h1>'+summary+''.join(cards)+appendix+'<p><a href="report.json">全ニュース・発見候補・入力・判断・診断のJSON</a>（表示省略のみ。保存データは削除しません）</p></html>'
