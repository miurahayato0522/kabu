"""見出しのキーワード分類と確認候補の選定。AI・株価予測・発注なし。"""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import sqlite3
import unicodedata

from news_collector import ROOT, DEFAULT_DB

VERSION = 'keyword-v1'
RULES = {
    '業績修正': ['上方修正', '下方修正', '業績予想', '業績修正'],
    '決算': ['決算', '増益', '減益', '赤字', '黒字', '純利益'],
    '株主還元': ['自社株買い', '自己株式取得', '自己株式の取得', '増配', '減配', '無配'],
    '企業再編': ['買収', '合併', 'TOB', '完全子会社', '資本提携'],
    '事業・製品': ['受注', '新製品', '新商品', '発売', '承認', '治験', '設備投資'],
    'リスク事象': ['不正', 'リコール', '訴訟', '行政処分', '情報漏洩', '情報流出', 'サイバー攻撃'],
}
ALIASES = {
    '7203': ['トヨタ'], '8306': ['三菱UFJ', 'MUFG'], '6758': ['ソニー'],
    '6501': ['日立'], '8058': ['三菱商事'], '7201': ['日産'],
    '4502': ['武田薬品'], '2914': ['日本たばこ', 'JT'], '9432': ['NTT'],
    '9984': ['ソフトバンクグループ', 'ソフトバンクG', 'SBG'],
}


def norm(text):
    return unicodedata.normalize('NFKC', text).casefold()


def classify(article, names):
    title = norm(article['title'])
    evidence = {category: [word for word in words if norm(word) in title]
                for category, words in RULES.items()}
    evidence = {category: words for category, words in evidence.items() if words}
    matched_names = {}
    for symbol in article['symbols']:
        words = [names.get(symbol, '')] + ALIASES.get(symbol, [])
        hits = [word for word in words if word and norm(word) in title]
        if hits:
            matched_names[symbol] = sorted(set(hits))
    # 名前は見出し内の文字一致。子会社等との区別を保証しない。
    decision = '確認候補' if evidence and matched_names else '要確認' if evidence else '保留'
    reasons = []
    if evidence:
        reasons.append('材料キーワードあり')
    else:
        reasons.append('登録した材料キーワードなし（重要でないと確定したわけではありません）')
    if not matched_names:
        reasons.append('検索銘柄名を見出しで確認できず、関連性の確認が必要')
    reasons.append('本文未確認・好悪材料や売買可否は判定していません')
    if article['date_quality'] != 'ok':
        reasons.append('公開日時が不明または不正')
    return dict(article, decision=decision, categories=list(evidence), keyword_evidence=evidence,
                name_evidence=matched_names, reasons=reasons)


def snapshot(path):
    # 最新の観測を選ぶが、分析時刻を公開時刻へ遡及させない。
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        rows = db.execute('''SELECT a.id,a.url,a.first_seen_at,a.body_status,
            o.title,o.publisher,o.published_at,o.date_quality,o.observed_at
            FROM articles a JOIN observations o ON o.rowid=(
                SELECT rowid FROM observations WHERE article_id=a.id
                ORDER BY observed_at DESC,rowid DESC LIMIT 1)
            ORDER BY a.id''').fetchall()
        matches = {}
        for row in db.execute('SELECT article_id,symbol FROM matches ORDER BY symbol'):
            matches.setdefault(row['article_id'], set()).add(row['symbol'])
        return [dict(row, symbols=sorted(matches.get(row['id'], []))) for row in rows]


def build_report(rows, names, symbol=None):
    selected = [row for row in rows if not symbol or symbol in row['symbols']]
    items = [classify(row, names) for row in selected]
    items.sort(key=lambda row: ({'確認候補': 0, '要確認': 1, '保留': 2}[row['decision']],
                                row['id']))
    encoded = json.dumps({'rows': selected, 'names': names, 'rules': RULES, 'aliases': ALIASES},
                         sort_keys=True, ensure_ascii=False)
    return {'version': VERSION, 'analyzed_at': datetime.now(timezone.utc).isoformat(),
            'input_sha256': hashlib.sha256(encoded.encode()).hexdigest(),
            'rules': RULES, 'aliases': ALIASES, 'names': names, 'symbol_filter': symbol,
            'counts': dict(Counter(row['decision'] for row in items)), 'items': items}


def write_report(report, output):
    output.mkdir(parents=True, exist_ok=False)
    (output / 'triage.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    escape = lambda value: html.escape(str(value), quote=True)
    cards = []
    for row in report['items']:
        cards.append(f'''<article data-status="{escape(row['decision'])}">
          <h3>{escape(row['title'])}</h3>
          <p><b>{escape(row['decision'])}</b> | 検索銘柄: {escape(', '.join(row['symbols']))}
          | 分類: {escape(' / '.join(row['categories']) or '未分類')}</p>
          <p>根拠語: {escape(json.dumps(row['keyword_evidence'], ensure_ascii=False))}</p>
          <p>銘柄名の文字一致: {escape(json.dumps(row['name_evidence'], ensure_ascii=False))}</p>
          <p>{escape(' / '.join(row['reasons']))}</p>
          <small>配信元: {escape(row['publisher'])}<br>
          公開(UTC): {escape(row['published_at'] or '不明')}<br>
          初回取得(UTC): {escape(row['first_seen_at'])}<br>
          この見出しの観測(UTC): {escape(row['observed_at'])}</small>
          <p><a href="{escape(row['url'])}" target="_blank" rel="noopener noreferrer">元記事を確認</a></p>
          </article>''')
    document = '''<!doctype html><html lang="ja"><meta charset="utf-8">
      <title>ニュース確認候補</title><style>
      body{font-family:system-ui,sans-serif;max-width:1000px;margin:32px auto;padding:0 20px;background:#f4f6f8;color:#182533}
      article{background:white;padding:20px;margin:16px 0;border:1px solid #dce3ea;border-radius:10px}
      small{color:#465567} h3{margin-top:0} select{padding:8px}</style>
      <h1>ニュース確認候補</h1><p>見出しのキーワード分類です。AI分析・買い推奨・株価予測ではありません。</p>
      <p>保留も含め全件保存。見出しだけでは誤分類・見逃しがあるため、元記事で確認してください。</p>'''
    document += f"<p>分析時刻(UTC): {escape(report['analyzed_at'])} | {escape(report['counts'])}</p>"
    document += '''<label>表示 <select id="filter"><option>すべて</option><option>確認候補</option><option>要確認</option><option>保留</option></select></label>'''
    document += ''.join(cards)
    document += '''<script>document.getElementById('filter').onchange=function(){
        document.querySelectorAll('article').forEach(a=>a.hidden=this.value!=='すべて'&&a.dataset.status!==this.value);
      };</script></html>'''
    (output / 'report.html').write_text(document, encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    parser.add_argument('--config', type=Path, default=ROOT / 'news_sources.json')
    parser.add_argument('--symbol')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        names = json.loads(args.config.read_text(encoding='utf-8-sig'))['symbols']
        if args.symbol and args.symbol not in names:
            raise ValueError('設定にない銘柄です')
        report = build_report(snapshot(args.db), names, args.symbol)
        output = args.output or ROOT / 'data' / ('news_triage_' + datetime.now().strftime('%Y%m%d_%H%M%S%f'))
        write_report(report, output)
        print(f"キーワード分類（AI未使用）: {len(report['items'])}件 / {report['counts']}")
        print('結果:', (output / 'report.html').resolve())
        return 0
    except (OSError, sqlite3.Error, ValueError, KeyError) as exc:
        print('停止:', exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
