"""通信不要の収集診断・観測1分足・模擬データ生成。"""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3

from kabu_collector import Recorder

ROOT = Path(__file__).resolve().parent
JST = timezone(timedelta(hours=9))


def timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(JST) if result.tzinfo else None
    except (TypeError, ValueError):
        return None


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def read_events(path, environment):
    # mode=ro: パス間違いで空のDBを新規作成しない。
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        for row in db.execute("SELECT * FROM events WHERE environment=? ORDER BY id", (environment,)):
            event = dict(row)
            try:
                payload = json.loads(event["payload"])
                event["payload"] = payload if isinstance(payload, dict) else None
            except (ValueError, TypeError):
                event["payload"] = None
            yield event


def analyze(events):
    counts, problems = Counter(), Counter()
    bars, state = {}, {}
    received, market = [], []
    for event in events:
        source = event["source"]
        counts[source] += 1
        rt = timestamp(event["received_at"])
        if rt:
            received.append(rt)
        else:
            problems["invalid_received_at"] += 1
        if source in ("connected", "disconnected", "stopped"):
            # 接続境界を越える累積出来高差は使わない。
            state.clear()
            continue
        if source not in ("push", "snapshot"):
            continue
        payload = event["payload"]
        if payload is None:
            problems["invalid_payload"] += 1
            continue
        pt = timestamp(payload.get("CurrentPriceTime"))
        vt = timestamp(payload.get("TradingVolumeTime"))
        price, volume = payload.get("CurrentPrice"), payload.get("TradingVolume")
        for name, valid in [("invalid_price", number(price)), ("invalid_price_time", pt is not None),
                            ("invalid_volume", number(volume)), ("invalid_volume_time", vt is not None)]:
            if not valid:
                problems[name] += 1
        if pt:
            market.append(pt)
        # 単発取得した古い価格をリアルタイム足に混ぜない。
        if source != "push":
            continue
        symbol = payload.get("Symbol")
        exchange = payload.get("Exchange")
        if not isinstance(symbol, str) or type(exchange) is not int:
            problems["invalid_instrument"] += 1
            continue
        instrument = (symbol, exchange)
        if pt is None or not number(price):
            state.pop(instrument, None)
            continue
        minute = pt.replace(second=0, microsecond=0)
        key = (symbol, exchange, minute)
        previous = state.get(instrument)
        if previous and pt < previous[0]:
            problems["out_of_order"] += 1
            if key in bars:
                bars[key]["flags"].add("out_of_order")
            continue
        if previous and (pt, price, vt, volume) == previous[:4]:
            problems["duplicate_price_observation"] += 1
            continue
        bar = bars.setdefault(key, {"symbol": symbol, "exchange": exchange,
            "minute": minute.isoformat(), "open": price, "high": price, "low": price,
            "close": price, "observations": 0, "observed_volume": 0, "flags": set()})
        bar["high"], bar["low"] = max(bar["high"], price), min(bar["low"], price)
        bar["close"] = price
        bar["observations"] += 1
        valid_volume = number(volume) and vt is not None and vt.replace(second=0, microsecond=0) == minute
        if not previous or previous[0].date() != pt.date():
            bar["flags"].add("no_volume_baseline")
        elif not valid_volume or not number(previous[3]) or previous[2] is None:
            bar["flags"].add("volume_time_or_value_invalid")
        elif volume < previous[3]:
            bar["flags"].add("volume_reset")
            problems["volume_reset"] += 1
        elif previous[2] > vt:
            bar["flags"].add("volume_time_reversed")
        elif volume > previous[3]:
            if previous[2].replace(second=0, microsecond=0) == minute:
                bar["observed_volume"] += volume - previous[3]
            else:
                bar["flags"].add("volume_crosses_minute")
        if not valid_volume:
            bar["flags"].add("volume_time_or_value_invalid")
        state[instrument] = (pt, price, vt, volume)
    output = []
    for key in sorted(bars):
        bar = bars[key]
        # PUSHは全約定ではない。正式な取引所OHLCVとの一致を保証しない。
        bar["flags"] = sorted(bar["flags"] | {"sampled_prices_not_official_ohlcv"})
        output.append(bar)
    def extent(values):
        return [min(values).isoformat(), max(values).isoformat()] if values else None
    return {"event_counts": dict(counts), "received_period_jst": extent(received),
            "market_period_jst": extent(market), "quality_counts": dict(problems),
            "bar_count": len(output), "bars": output}


def demo(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 既存ファイル（実データを含む）は絶対に上書き・追記しない。
    with path.open("xb"):
        pass
    recorder = Recorder(path, "demo")
    try:
        base = datetime(2026, 9, 14, 9, 0, tzinfo=JST)
        for day in range(2):
            volume = 10000
            for i in range(120):
                instant = base + timedelta(days=day, seconds=i * 5)
                volume += 100
                payload = {"Symbol": "7203", "SymbolName": "模擬トヨタ（架空データ）", "Exchange": 1,
                           "CurrentPrice": 3000 + (i % 11) - 5 + day * 10,
                           "CurrentPriceTime": instant.isoformat(), "TradingVolume": volume,
                           "TradingVolumeTime": instant.isoformat()}
                recorder.write("push", payload)
                recorder.db.execute("UPDATE events SET received_at=? WHERE id=last_insert_rowid()",
                                    ((instant + timedelta(milliseconds=100)).isoformat(),))
        recorder.db.commit()
    finally:
        recorder.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="夜間でも使えるオフライン分析（API接続なし）")
    parser.add_argument("command", choices=["demo", "status", "bars"])
    parser.add_argument("--environment", choices=["production", "verification", "demo"])
    parser.add_argument("--db", type=Path)
    parser.add_argument("--output", type=Path, help="新規JSONファイルへ出力（既存ファイルは上書きしません）")
    args = parser.parse_args(argv)
    environment = args.environment or ("demo" if args.command == "demo" else "production")
    if args.command == "demo" and environment != "demo":
        parser.error("模擬データはdemo環境だけに生成できます")
    path = args.db or ROOT / "data" / f"{environment}.sqlite3"
    try:
        if args.command == "demo":
            demo(path)
            print(f"模擬データ240件を作成: {path.resolve()}")
            return 0
        result = analyze(read_events(path, environment))
        print(f"環境: {environment} | 模擬データ: {environment == 'demo'}")
        print("記録件数:", result["event_counts"])
        print("受信期間（日本時間）:", result["received_period_jst"])
        print("価格時刻の範囲:", result["market_period_jst"])
        print("欠損・品質チェック:", result["quality_counts"] or "検出なし（完全性を保証するものではありません）")
        print("観測1分足:", result["bar_count"], "件")
        if args.command == "bars":
            print("時刻 / 銘柄 / 始値 / 高値 / 安値 / 終値 / 観測出来高（同一分内の差分のみ）")
            for bar in result["bars"][-10:]:
                print(bar["minute"], bar["symbol"], bar["open"], bar["high"], bar["low"], bar["close"], bar["observed_volume"])
            print("未観測の分は補完しません。最終分も未確定の可能性があります。")
        if args.output:
            report = {"environment": environment, "input_db": str(path.resolve()), **result}
            if args.command == "status":
                report.pop("bars")
            with args.output.open("x", encoding="utf-8") as file:
                json.dump(report, file, ensure_ascii=False, indent=2)
            print("JSON保存先:", args.output.resolve())
        return 0
    except (OSError, sqlite3.Error) as exc:
        print(f"停止: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
