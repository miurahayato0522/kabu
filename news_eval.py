"""実資料の期待値と保存済みAI分析を比較。API通信なし。"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import html
import json
from pathlib import Path
import sqlite3
from news_documents import register
from news_numbers import calculate

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / 'evaluation/real_news_cases.json'
DOCS = ROOT / 'data/news_eval_documents.sqlite3'
CACHE = ROOT / 'data/news_eval_ai.sqlite3'
REVIEWS = ROOT / 'data/news_eval_reviews.sqlite3'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_cases(path):
    dataset = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    ids = [c['id'] for c in dataset['cases']]
    if len(ids) != len(set(ids)):
        raise ValueError('ケースIDが重複しています')
    for c in dataset['cases']:
        for key in ('before_yen', 'after_yen', 'delta_yen', 'revision_pct'):
            if c['expected'][key] is not None and not Decimal(c['expected'][key]).is_finite():
                raise ValueError('期待値が有限数ではありません')
    return dataset


def comparison(case, result, source):
    computed = calculate(result, source)
    expected = case['expected']
    changes = computed['changes']
    actual = changes[0] if len(changes) == 1 else {}
    rows = [dict(field='計算対象の組数', expected=1, actual=len(changes), match=len(changes) == 1)]
    for key in ('metric', 'period', 'before_yen', 'after_yen', 'delta_yen', 'revision_pct', 'change'):
        want, got = expected[key], actual.get(key)
        match = key in actual and want == got
        if key in ('before_yen', 'after_yen', 'delta_yen', 'revision_pct') and want is not None and got is not None:
            tolerance = Decimal('0.000001') if key == 'revision_pct' else Decimal(0)
            match = abs(Decimal(want) - Decimal(got)) <= tolerance
        rows.append(dict(field=key, expected=want, actual=got, match=match))
    relations = result.get('relations', [])
    relation = relations[0].get('relation') if len(relations) == 1 and relations[0].get('symbol') == case['symbol'] else None
    rows.append(dict(field='企業関係', expected=expected['relation'], actual=relation, match=expected['relation'] == relation))
    if computed['issues']:
        rows.append(dict(field='計算の確認事項', expected=[], actual=computed['issues'], match=False))
    return {'checks': rows, 'calculations': computed, 'match': all(r['match'] for r in rows),
            'scope_note': '連結/単体は現行AI出力に専用項目がないため自動採点対象外。原文と引用を人が照合してください。'}


def read_reviews(path):
    if not Path(path).exists():
        return {}
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        return {r[0]: {'reviewer': r[1], 'reviewed_at': r[2]} for r in db.execute('SELECT case_hash,reviewer,reviewed_at FROM reviews')}


def report(dataset, cache, reviews, model):
    records = []
    if Path(cache).exists():
        with closing(sqlite3.connect(Path(cache).resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            records = [dict(r) for r in db.execute('SELECT * FROM ai_analyses ORDER BY started_at DESC,request_hash')]
    items = []
    for case in dataset['cases']:
        item = dict(case=case, case_hash=digest(case), human_review=reviews.get(digest(case)), status='未分析')
        for record in records:
            source = json.loads(record['source_json'])
            requested_model = json.loads(record['request_json'])['model']
            if requested_model != model or source.get('body') != case['body'] or source.get('url') != case['url'] or source.get('title') != case['title'] or source.get('symbols') != [case['symbol']] or not record['version'].startswith('body-'):
                continue
            item.update(analysis_hash=record['request_hash'], analysis_version=record['version'], model=record['response_model'])
            if record['status'] != 'ok':
                item.update(status='分析失敗・未完了', error=record['error'])
            else:
                result = json.loads(record['result_json'])
                check = comparison(case, result, source)
                item.update(comparison=check, ai_result=result,
                            status=('一致' if item['human_review'] else '暫定一致（期待値未確認）') if check['match'] else '不一致')
            break
        items.append(item)
    return dict(version='news-eval-v1', created_at=datetime.now(timezone.utc).isoformat(), dataset_hash=digest(dataset),
                note=dataset['note'], model=model, items=items)


def render(report):
    esc = lambda v: html.escape(str(v))
    parts = ['<!doctype html><meta charset="utf-8"><title>ニュース抽出の検証</title>',
             '<style>body{font-family:Meiryo,sans-serif;max-width:1100px;margin:30px auto;padding:15px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:8px;text-align:left}pre{white-space:pre-wrap}section{margin:35px 0}.bad{background:#ffe5e5}</style>',
             '<h1>期待値・AI抽出・Python計算の比較</h1><p>' + esc(report['note']) + '</p>',
             '<p>少数の検証ケースです。全般的な抽出精度や売買成績を保証しません。</p>']
    for item in report['items']:
        case = item['case']
        parts += ['<section><h2>' + esc(case['id'] + '：' + item['status']) + '</h2>',
                  '<p>' + esc(case['title']) + '</p><p>出典URL：' + esc(case['url']) + '</p>',
                  '<p>確認箇所：' + esc(case['location']) + '</p><p>人による期待値確認：' + esc(item['human_review'] or '未確認') + '</p>',
                  '<h3>入力（表から転記・整形）</h3><pre>' + esc(case['body']) + '</pre>',
                  '<h3>期待値</h3><pre>' + esc(json.dumps(case['expected'], ensure_ascii=False, indent=2)) + '</pre>']
        if 'comparison' in item:
            parts += ['<table><tr><th>項目</th><th>期待値</th><th>抽出・計算結果</th><th>一致</th></tr>']
            for check in item['comparison']['checks']:
                parts.append('<tr' + ('' if check['match'] else ' class="bad"') + '>' + ''.join('<td>' + esc(check[k]) + '</td>' for k in ('field','expected','actual','match')) + '</tr>')
            parts += ['</table><p>' + esc(item['comparison']['scope_note']) + '</p>',
                      '<details><summary>AI抽出・根拠引用の全項目</summary><pre>' + esc(json.dumps(item['ai_result'], ensure_ascii=False, indent=2)) + '</pre></details>']
        if item.get('error'):
            parts.append('<p>' + esc(item['error']) + '</p>')
        parts.append('</section>')
    return '\n'.join(parts)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['prepare', 'report', 'review'])
    p.add_argument('--dataset', type=Path, default=DATASET)
    p.add_argument('--documents-db', type=Path, default=DOCS)
    p.add_argument('--cache', type=Path, default=CACHE)
    p.add_argument('--reviews-db', type=Path, default=REVIEWS)
    p.add_argument('--model', choices=['gpt-5-mini', 'gpt-5-nano'], default='gpt-5-mini')
    p.add_argument('--case')
    p.add_argument('--reviewer')
    p.add_argument('--output', type=Path)
    a = p.parse_args(argv)
    try:
        dataset = load_cases(a.dataset)
        if a.command == 'prepare':
            for c in dataset['cases']:
                revision, inserted = register(a.documents_db,c['body'],c['title'],c['url'],[c['symbol']],kind='excerpt')
                print(c['id'], '登録' if inserted else '登録済み', revision[:12])
            print('期待値はAPI入力に含めません。公式表の転記でありPDF原文全文ではありません。')
        elif a.command == 'review':
            matches = [c for c in dataset['cases'] if c['id'] == a.case]
            if len(matches) != 1 or not a.reviewer or not a.reviewer.strip():
                raise ValueError('原資料を確認後、--case ケースID --reviewer 確認者名 を指定してください')
            a.reviews_db.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(a.reviews_db)) as db, db:
                db.execute('CREATE TABLE IF NOT EXISTS reviews (case_hash TEXT PRIMARY KEY,reviewer TEXT,reviewed_at TEXT)')
                db.execute('INSERT OR REPLACE INTO reviews VALUES (?,?,?)',(digest(matches[0]),a.reviewer,datetime.now(timezone.utc).isoformat()))
            print('人による確認を記録しました。入力や期待値が変更された場合は再確認が必要です。')
        else:
            value = report(dataset,a.cache,read_reviews(a.reviews_db),a.model)
            output = a.output or ROOT / 'data' / ('news_eval_' + datetime.now().strftime('%Y%m%d_%H%M%S%f'))
            output.mkdir(parents=True, exist_ok=False)
            (output/'report.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
            (output/'report.html').write_text(render(value),encoding='utf-8')
            for item in value['items']:
                print(item['case']['id'], item['status'])
            print('比較レポート:',output/'report.html')
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print('停止:',exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
