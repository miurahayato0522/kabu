"""保存済みの本文分析を金額へ変換するオフライン処理。API通信・注文なし。"""
import argparse
from contextlib import closing
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
import unicodedata

ROOT = Path(__file__).resolve().parent
VERSION = 'money-v2'
SCALES = {'円': 1, '千円': 1000, '万円': 10000, '百万円': 1000000,
          '億円': 100000000, '兆円': 1000000000000}


def clean(text):
    return unicodedata.normalize('NFKC', text).strip().replace('−', '-').replace('▲', '-').replace('△', '-')


def yen(value, unit):
    """明示された単一単位だけを変換。範囲・概数・複合単位は推測しない。"""
    value, unit = clean(value), clean(unit)
    loss = value.startswith('赤字')
    if loss:
        value = value[2:]
    match = re.fullmatch(r'([+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)\s*(兆円|億円|百万円|万円|千円|円)?', value)
    if not match or unit not in SCALES:
        raise ValueError('未対応の金額表記または単位')
    suffix = match[2]
    if suffix and unit not in (suffix, '円'):
        raise ValueError('値に含まれる単位とunitが矛盾')
    amount = Decimal(match[1].replace(',', '')) * SCALES[suffix or unit]
    if loss:
        amount = -abs(amount)
    return amount


def metric(label):
    label = clean(label)
    label = re.sub(r'\((?:変更前|変更後|前回見通し|今回見通し|前回|今回)\)$', '', label)
    label = re.sub(r'^(?:従来の|従来|新)', '', label)
    label = label.removesuffix('予想').removesuffix('見通し')
    # 指標の取り違えを防ぐため、未知のラベル同士を曖昧に対応させない。
    return label if label in ('売上高', '営業利益', '経常利益', '純利益', '連結売上高', '連結営業利益',
                             '連結経常利益', '連結純利益', '親会社株主に帰属する当期純利益') else None


def decimal_text(value):
    return format(value, 'f')


def calculate(result, source):
    rows, changes, issues = [], [], list(result.get('quality_warnings', []))
    for i, raw in enumerate(result.get('numbers', []), 1):
        row = {'index': i, 'raw': raw, 'yen': None, 'metric': metric(raw.get('label', ''))}
        try:
            quote = raw.get('quote', '')
            if not quote or quote not in source.get('body', '') or raw['value'] not in quote:
                raise ValueError('数値引用を登録本文で確認できない')
            amount = yen(raw['value'], raw['unit'])
            if amount > 0 and '赤字' in quote:
                raise ValueError('赤字の符号が曖昧。値に負号または赤字の明示が必要')
            row['yen'] = decimal_text(amount)
        except (ValueError, KeyError, TypeError) as exc:
            row['issue'] = str(exc)
            issues.append(f'数値{i}: {exc}')
        rows.append(row)
    candidates = [r for r in rows if r['raw'].get('role') in ('変更前', '変更後')]
    if not candidates:
        issues.append('変更前・変更後の数値がありません。旧形式・実績比較等は修正率の対象外です')
    if len(set(source.get('symbols', []))) != 1:
        issues.append('企業を一意に特定できないため数値の組合せは行いません')
        return dict(version=VERSION, numbers=rows, changes=[], issues=issues)
    groups = {}
    for row in candidates:
        raw = row['raw']
        suffix = re.search(r'\((変更前|変更後|前回見通し|今回見通し|前回|今回)\)$', clean(raw.get('label', '')))
        if suffix:
            label_role = '変更前' if suffix[1] in ('変更前', '前回見通し', '前回') else '変更後'
            if label_role != raw['role']:
                issues.append(f"数値{row['index']}: ラベルと変更前後の役割が矛盾するため比較しません")
                continue
        period = raw.get('period', '')
        pq = raw.get('period_quote', '')
        if row['metric'] is None or not period or period == '不明' or not pq or pq not in source.get('body', '') or period not in pq:
            issues.append(f"数値{row['index']}: 指標・対象期間・期間引用を確認できないため比較しません")
            continue
        groups.setdefault((row['metric'], period), []).append(row)
    for (name, period), group in groups.items():
        before = [r for r in group if r['raw']['role'] == '変更前']
        after = [r for r in group if r['raw']['role'] == '変更後']
        if len(before) != 1 or len(after) != 1 or any(r['yen'] is None for r in group):
            issues.append(f'{name} / {period}: 同じ期間の変更前後が一組に定まらないため比較しません')
            continue
        if result.get('quality_warnings'):
            issues.append(f'{name} / {period}: AI抽出の品質警告があるため比較しません')
            continue
        a, b = Decimal(before[0]['yen']), Decimal(after[0]['yen'])
        rate = (b - a) / a * 100 if a > 0 else None
        state = '増加' if b > a else '減少' if b < a else '変化なし'
        if a < 0 and b > 0:
            state = '赤字から黒字'
        elif a > 0 and b < 0:
            state = '黒字から赤字'
        elif a < 0 and b < 0:
            state = '赤字縮小' if b > a else '赤字拡大' if b < a else '変化なし'
        changes.append(dict(metric=name, period=period, before_index=before[0]['index'], after_index=after[0]['index'],
                            before_yen=decimal_text(a), after_yen=decimal_text(b), delta_yen=decimal_text(b-a),
                            revision_pct=decimal_text(rate) if rate is not None else None, change=state,
                            rate_note='変更前がゼロまたは負のため修正率は計算しません' if rate is None else '(変更後−変更前)÷変更前×100'))
    return dict(version=VERSION, numbers=rows, changes=changes, issues=issues)


def build(cache):
    with closing(sqlite3.connect(Path(cache).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        records = db.execute("SELECT article_id,request_hash,source_json,result_json,version FROM ai_analyses WHERE status='ok' ORDER BY finished_at DESC,request_hash").fetchall()
    seen, items = set(), []
    for article_id, fingerprint, source_json, result_json, version in records:
        if article_id in seen or not version.startswith('body-'):
            continue
        seen.add(article_id)
        source, result = json.loads(source_json), json.loads(result_json)
        items.append(dict(article_id=article_id, analysis_hash=fingerprint, analysis_version=version,
                          title=source['title'], calculations=calculate(result, source)))
    return {'version': VERSION, 'notice': 'AI抽出を前提とした計算。原文・企業・指標・期間の意味を保証せず、売買判断には直結しません。', 'items': items}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, default=ROOT / 'data/news_ai.sqlite3')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        report = build(args.cache)
        for item in report['items']:
            print('\n' + item['title'])
            for change in item['calculations']['changes']:
                print(change['metric'], '/', change['period'], '/', change['change'])
                print(f"変更前 {Decimal(change['before_yen']):,}円 → 変更後 {Decimal(change['after_yen']):,}円 / 差額 {Decimal(change['delta_yen']):+,}円")
                print('修正率:', (f"{Decimal(change['revision_pct']):+.2f}%" if change['revision_pct'] is not None else change['rate_note']))
            for issue in item['calculations']['issues']:
                print('要確認:', issue)
        output = args.output or ROOT / 'data' / ('news_numbers_' + datetime.now().strftime('%Y%m%d_%H%M%S%f') + '.json')
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as file:
            json.dump(report, file, ensure_ascii=False, indent=2)
        print('API通信なし。計算結果:', output.resolve())
        return 0
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError) as exc:
        print('停止:', exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
