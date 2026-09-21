"""ニュースと日足の確認記録。候補は人による確認対象であり売買シグナルではない。"""
import argparse
from contextlib import closing
from datetime import datetime, timezone, timedelta, date
import hashlib
import json
from pathlib import Path
import sqlite3

from daily_backtest import positive, split_factor
from jquants_history import ROOT, HistoryError, symbol_code

JST = timezone(timedelta(hours=9))


def stamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('時刻にはタイムゾーンが必要です')
    return result


def indicators(records, asof, mode):
    cutoff = asof.astimezone(JST).date()
    eligible = [(row, fetched) for row, fetched in records
                if date.fromisoformat(row['Date']) < cutoff
                and (mode == 'replay' or stamp(fetched) <= asof)]
    rows = [row for row, _ in eligible]
    if len(rows) < 21:
        raise ValueError('参照可能な日足が21本未満')
    days = [r['Date'] for r in rows]
    if days != sorted(set(days)):
        raise ValueError('日足の重複・逆行')
    rows = rows[-21:]
    closes, volumes = [], []
    for row in rows:
        factor = split_factor(row)
        if not positive(row['C']) or not positive(row['Vo']):
            raise ValueError('終値・出来高が欠損または不正')
        closes = [v * factor for v in closes] + [row['C']]
        volumes = [v / factor for v in volumes] + [row['Vo']]
    return {'price_date': rows[-1]['Date'], 'close': closes[-1],
            'ma5': sum(closes[-5:]) / 5, 'ma20': sum(closes[-20:]) / 20,
            'return_1d_pct': (closes[-1] / closes[-2] - 1) * 100,
            'return_5d_pct': (closes[-1] / closes[-6] - 1) * 100,
            'volume_ratio_20d': volumes[-1] / (sum(volumes[:-1]) / 20),
            'age_calendar_days': (cutoff - date.fromisoformat(rows[-1]['Date'])).days,
            'input_sha256': hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
            'bars': rows, 'fetched_at': [f for _, f in eligible[-21:]]}


def build(ai_path, prices_path, mode='replay'):
    with closing(sqlite3.connect(Path(ai_path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        analyses = [dict(r) for r in db.execute("SELECT * FROM ai_analyses ORDER BY finished_at DESC, request_hash")]
    with closing(sqlite3.connect(Path(prices_path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        sources = [r[0] for r in db.execute('SELECT DISTINCT source FROM daily_prices')]
        if len(sources) != 1 or sources[0] not in ('jquants_v2', 'yahoo_reconstructed_v1'):
            raise ValueError('株価取得元が混在・未対応・空です')
        prices = {}
        for code, fetched, payload in db.execute('SELECT code,fetched_at,payload FROM daily_prices ORDER BY day'):
            prices.setdefault(code, []).append((json.loads(payload), fetched))
    items, seen = [], set()
    for analysis in analyses:
        if analysis['status'] != 'ok' or analysis['article_id'] in seen:
            continue
        seen.add(analysis['article_id'])
        source, result = json.loads(analysis['source_json']), json.loads(analysis['result_json'])
        asof = max(stamp(analysis['finished_at']), stamp(source['first_seen_at']), stamp(source['observed_at']))
        if source.get('body_received_at'):
            asof = max(asof, stamp(source['body_received_at']))
        for symbol in source['symbols']:
            reasons, chart = [], None
            relation = next((r['relation'] for r in result.get('relations', []) if r['symbol'] == symbol), '不明')
            if analysis['version'] not in ('headline-v2', 'body-v1'):
                reasons.append('旧AI形式：企業との直接・間接関係が未評価')
            if relation != '直接':
                reasons.append('対象企業への直接の材料と確認できない（AI推定）')
            if source.get('published_at'):
                published = stamp(source['published_at'])
                if published > asof:
                    reasons.append('公開日時が判断時刻より未来')
                elif (asof - published).total_seconds() > 7 * 86400:
                    reasons.append('公開から7日超：古いニュース')
            else:
                reasons.append('公開日時不明')
            try:
                chart = indicators(prices.get(symbol_code(symbol), []), asof, mode)
                if chart['age_calendar_days'] > 7:
                    reasons.append('株価が7暦日超古い')
                if not chart['close'] > chart['ma20'] or not chart['ma5'] > chart['ma20']:
                    reasons.append('終値・5日平均がともに20日平均を上回る条件を満たさない')
                if chart['volume_ratio_20d'] < 1:
                    reasons.append('出来高が直前20本平均未満')
            except (ValueError, HistoryError, KeyError) as exc:
                reasons.append(str(exc))
            items.append({'article_id': analysis['article_id'], 'analysis_hash': analysis['request_hash'],
                          'model': analysis['response_model'], 'analysis_version': analysis['version'],
                          'symbol': symbol, 'title': source['title'], 'source': source,
                          'ai_result': result, 'decision_at': asof.isoformat(), 'relation': relation,
                          'chart': chart, 'decision': '見送り' if reasons else '確認候補',
                          'reasons': reasons or ['直接関連・上向きの価格条件・出来高条件を満たすため資料の内容確認へ'],
                          'limitations': [('登録本文・抜粋のAI抽出。出典・真偽・数値の解釈は未確認' if 'body' in source else '見出しのみ・AI推定の真偽未確認'), '売買指示・株価予測ではない',
                                          '取引日カレンダー未照合。7暦日の鮮度判定は便宜上の基準']})
    return {'version': 'market-context-v2', 'mode': mode, 'price_source': sources[0],
            'created_at': datetime.now(timezone.utc).isoformat(),
            'policy': '判断日の当日足は使わず、前日以前のみ。AI分析完了・初回取得・見出し観測・本文登録の最も遅い時刻を判断時刻とする。',
            'availability': ('過去データによる再構成。取得・訂正前の値を保証せず、当時の実行可能性は未検証。'
                             if mode == 'replay' else '株価の保存取得日時も判断時刻以前に限定。不足は見送り。'),
            'items': items}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ai-db', type=Path, default=ROOT / 'data/news_ai.sqlite3')
    parser.add_argument('--prices-db', type=Path, default=ROOT / 'data/yahoo.sqlite3')
    parser.add_argument('--mode', choices=['replay', 'recorded'], default='replay')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        report = build(args.ai_db, args.prices_db, args.mode)
        output = args.output or ROOT / 'data' / ('news_market_' + datetime.now().strftime('%Y%m%d_%H%M%S%f'))
        output.mkdir(parents=True, exist_ok=False)
        (output / 'context.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        lines = ['# ニュースとチャートの確認記録', '', report['mode'] + ' / ' + report['price_source'],
                 report['availability'], '', report['policy'], '', '売買指示ではありません。']
        for item in report['items']:
            lines += ['', f"## {item['symbol']}：{item['decision']}", '', item['title'],
                      '判断時刻: ' + item['decision_at'], '理由: ' + ' / '.join(item['reasons'])]
            source, result = item['source'], item['ai_result']
            lines += ['分析形式: ' + item['analysis_version'] + ' / 本文種別: ' + source.get('body_status', 'not_fetched'),
                      '出典URL: ' + source.get('url', ''),
                      '公表日時: ' + (source.get('published_at') or '不明'),
                      '本文登録日時: ' + (source.get('body_received_at') or '未登録'),
                      '要約（AI）: ' + result['summary'],
                      '根拠引用: ' + ' / '.join(result['evidence']),
                      '未確認点: ' + (' / '.join(result['unknowns']) or 'AIの列挙なし。確認済みの意味ではありません'),
                      '制約: ' + ' / '.join(item['limitations'])]
            for number in result.get('numbers', []):
                lines += ['数値（AI抽出）: ' + json.dumps(number, ensure_ascii=False)]
            if item['chart']:
                c = item['chart']
                lines += [f"株価日: {c['price_date']} / 終値 {c['close']:.2f} / MA5 {c['ma5']:.2f} / MA20 {c['ma20']:.2f}",
                          f"1日騰落 {c['return_1d_pct']:.2f}% / 5日騰落 {c['return_5d_pct']:.2f}% / 出来高比 {c['volume_ratio_20d']:.2f}倍"]
        (output / 'report.md').write_text('\n\n'.join(lines), encoding='utf-8')
        print(f"{report['mode']} / {len(report['items'])}件 / 確認候補 {sum(r['decision']=='確認候補' for r in report['items'])}件")
        print('結果:', output.resolve())
        return 0
    except (OSError, sqlite3.Error, ValueError, KeyError, HistoryError) as exc:
        print('停止:', exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
