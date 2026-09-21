"""単一銘柄・現物買いのみの日足シミュレーター。発注はしない。"""
import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from jquants_history import DEFAULT_DB, ROOT, HistoryError, load_daily, symbol_code


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def split_factor(row):
    factor = row.get("AdjFactor")
    if not positive(factor):
        raise HistoryError(f"{row.get('Date')}: 調整係数が欠損または不正です")
    kind = str(row.get("ExRT", ""))
    if kind == "3" or (factor != 1 and kind not in ("1", "2")):
        raise HistoryError(f"{row.get('Date')}: 分割・併合と確認できない権利調整は非対応です")
    return factor


def split_shares(shares, factor, day):
    adjusted = shares / factor
    rounded = round(adjusted)
    if not math.isclose(adjusted, rounded, rel_tol=0, abs_tol=1e-7) or rounded % 100:
        raise HistoryError(f"{day}: 分割・併合後の端株・単元未満株の処理は非対応です")
    return rounded


def chart_adjustments(rows):
    """表示専用: 最終日の株価単位へ換算。売買判断には使用しない。"""
    factors, cumulative = {}, 1.0
    for row in reversed(rows):
        factors[row["Date"]] = cumulative
        cumulative *= split_factor(row)
    return factors


def simulate(rows, initial=1_000_000, short=5, long=20, quantity=100, fee=0, slippage_bps=5):
    if not positive(initial) or not 1 <= short < long or quantity <= 0 or quantity % 100:
        raise HistoryError("資金・移動平均期間・数量（100株単位）を確認してください")
    if not math.isfinite(fee) or fee < 0 or not math.isfinite(slippage_bps) or not 0 <= slippage_bps < 10000:
        raise HistoryError("手数料・スリッページが不正です")
    if len(rows) <= long:
        raise HistoryError(f"最低{long + 1}日分のデータが必要です")
    code, previous_date = rows[0].get("Code"), None
    for row in rows:
        day = date.fromisoformat(row["Date"])
        if row.get("Code") != code or previous_date and day <= previous_date:
            raise HistoryError("銘柄混在または日付の重複・逆行があります")
        previous_date = day
        split_factor(row)
        if not all(positive(row.get(k)) for k in ("O", "H", "L", "C", "Vo")):
            raise HistoryError(f"{day}: 価格・出来高の欠損または非取引日があります。補完せず停止します。")
        if not row["L"] <= min(row["O"], row["C"]) <= max(row["O"], row["C"]) <= row["H"]:
            raise HistoryError(f"{day}: 四本値の大小関係が不正です")
    cash, shares, entry_cost = float(initial), 0, 0.0
    peak, max_drawdown = float(initial), 0.0
    pending, last_relation = None, None
    closes, trades, equity, skipped = [], [], [], []
    realized = 0.0
    actions = []
    for row in rows:
        factor = split_factor(row)
        if factor != 1:
            before = shares
            shares = split_shares(shares, factor, row["Date"])
            # 当日判明した係数だけで過去の終値を当日の株価単位に換算する。
            closes = [value * factor for value in closes]
            actions.append({"date": row["Date"], "factor": factor,
                            "shares_before": before, "shares_after": shares})
            # 総取得原価・現金は不変。初日の分割に対して保有はまだない。
        # 前日までの判断だけを始値で執行。今回の終値はまだ参照しない。
        if pending:
            side, signal_date = pending
            price = row["O"] * (1 + slippage_bps / 10000 * (1 if side == "buy" else -1))
            if side == "buy" and not shares:
                cost = price * quantity + fee
                if cash >= cost:
                    cash -= cost
                    shares, entry_cost = quantity, cost
                    trades.append({"date": row["Date"], "signal_date": signal_date, "side": side,
                                   "quantity": shares, "price": price, "fee": fee})
                else:
                    skipped.append({"date": row["Date"], "reason": "insufficient_cash"})
            elif side == "sell" and shares:
                proceeds = price * shares - fee
                profit = proceeds - entry_cost
                cash += proceeds
                realized += profit
                trades.append({"date": row["Date"], "signal_date": signal_date, "side": side,
                               "quantity": shares, "price": price, "fee": fee, "realized_pnl": profit})
                shares, entry_cost = 0, 0.0
        pending = None
        closes.append(row["C"])
        if len(closes) >= long:
            fast, slow = sum(closes[-short:]) / short, sum(closes[-long:]) / long
            relation = (fast > slow) - (fast < slow)
            if last_relation is not None:
                if last_relation <= 0 < relation and not shares:
                    pending = ("buy", row["Date"])
                elif last_relation >= 0 > relation and shares:
                    pending = ("sell", row["Date"])
            last_relation = relation
        value = cash + shares * row["C"]
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, (peak - value) / peak)
        equity.append({"date": row["Date"], "cash": cash, "shares": shares, "equity": value})
    final = equity[-1]["equity"]
    return {"symbol": code, "data_source": rows[0].get("DataSource", "jquants_v2"), "start": rows[0]["Date"], "end": rows[-1]["Date"], "days": len(rows),
            "parameters": {"initial": initial, "short": short, "long": long, "quantity": quantity,
                           "fee_per_order": fee, "slippage_bps": slippage_bps},
            "input_sha256": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
            "final_equity": final, "total_pnl": final - initial, "realized_pnl": realized,
            "unrealized_pnl": shares * rows[-1]["C"] - entry_cost,
            "return_pct": (final / initial - 1) * 100, "max_drawdown_pct": max_drawdown * 100,
            "ending_shares": shares, "unexecuted_last_signal": pending,
            "corporate_actions": actions, "split_accounting_version": 1,
            "trades": trades, "skipped_orders": skipped, "equity_curve": equity,
            "limitations": ["配当・税金は未計算", "権利割当・端株・単元未満株を生む調整は非対応", "始値で全量約定する仮定",
                            "売買停止・値幅制限・流動性による約定不能は未再現", "最終保有は終値評価、強制売却なし",
                            "取引日カレンダー照合未実装。日付行の欠落は別途確認が必要"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="移動平均クロス・翌日始値の仮想売買（実注文なし）")
    parser.add_argument("--symbol", type=symbol_code, default="72030")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--short", type=int, default=5)
    parser.add_argument("--long", type=int, default=20)
    parser.add_argument("--cash", type=float, default=1_000_000)
    parser.add_argument("--quantity", type=int, default=100)
    parser.add_argument("--fee", type=float, default=0)
    parser.add_argument("--slippage-bps", type=float, default=5)
    parser.add_argument("--from", dest="start", type=date.fromisoformat)
    parser.add_argument("--to", dest="end", type=date.fromisoformat)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.start and args.end and args.start > args.end:
            raise HistoryError("開始日が終了日より後です")
        rows = [r for r in load_daily(args.db, args.symbol)
                if (not args.start or date.fromisoformat(r["Date"]) >= args.start)
                and (not args.end or date.fromisoformat(r["Date"]) <= args.end)]
        result = simulate(rows, args.cash, args.short, args.long, args.quantity, args.fee, args.slippage_bps)
        target = args.output or ROOT / "data" / ("backtest_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as file:
            json.dump(result, file, ensure_ascii=False, indent=2, allow_nan=False)
        print(f"仮想売買のみ / {args.symbol} / {result['start']} ～ {result['end']} / {result['days']}日")
        print(f"最終評価額: {result['final_equity']:,.2f}円 / 損益（含み損益込み）: {result['total_pnl']:,.2f}円")
        print(f"収益率: {result['return_pct']:.2f}% / 日次終値ベース最大下落率: {result['max_drawdown_pct']:.2f}%")
        print(f"売買件数: {len(result['trades'])} / 最終保有: {result['ending_shares']}株 / 資金不足見送り: {len(result['skipped_orders'])}")
        print("配当・税金は未計算。約定は仮定です。将来の利益を示しません。")
        print(f"売買履歴・日次資産推移: {target.resolve()}")
        return 0
    except (HistoryError, OSError, sqlite3.Error, ValueError, KeyError) as exc:
        print("停止: " + (str(exc) if isinstance(exc, HistoryError) else type(exc).__name__))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
