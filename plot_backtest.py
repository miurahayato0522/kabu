"""保存済みバックテストの可視化。ネット接続・注文なし。"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

from jquants_history import DEFAULT_DB, ROOT, load_daily
from daily_backtest import split_factor, split_shares, chart_adjustments


def drawdown(values, initial):
    peak = initial
    result = []
    for value in values:
        peak = max(peak, value)
        result.append((value / peak - 1) * 100)
    return result


def comparison(rows, report):
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    if digest != report["input_sha256"]:
        raise ValueError("元の株価データとバックテストのハッシュが一致しません。再検証してください。")
    p = report["parameters"]
    # 同じ保有株数・資金。余剰資金は無利息の現金として残す。
    cost = rows[0]["O"] * (1 + p["slippage_bps"] / 10000) * p["quantity"] + p["fee_per_order"]
    if cost > p["initial"]:
        raise ValueError("比較対象の初日購入に必要な資金がありません")
    hold, shares = [], p["quantity"]
    for index, row in enumerate(rows):
        factor = split_factor(row)
        if index:
            shares = split_shares(shares, factor, row["Date"])
        hold.append(p["initial"] - cost + shares * row["C"])
    pairs, entry = [], None
    for trade in report["trades"]:
        if trade["side"] == "buy":
            entry = trade
        elif entry is not None:
            pairs.append({"buy_date": entry["date"], "sell_date": trade["date"],
                          "buy_price": entry["price"], "sell_price": trade["price"],
                          "pnl": trade["realized_pnl"]})
            entry = None
    return hold, pairs


def render(rows, report, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib import font_manager
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in ("Yu Gothic", "Meiryo", "Noto Sans CJK JP", "MS Gothic"):
        if name in available:
            plt.rcParams["font.family"] = name
            break
    plt.rcParams.update({"axes.unicode_minus": False, "font.size": 10})
    hold, pairs = comparison(rows, report)
    p = report["parameters"]
    dates = [datetime.fromisoformat(r["Date"]) for r in rows]
    adjustments = chart_adjustments(rows)
    closes = [r["C"] * adjustments[r["Date"]] for r in rows]
    strategy = [r["equity"] for r in report["equity_curve"]]
    blue, orange = "#1368b1", "#c46a0a"
    fig, axes = plt.subplots(4, 1, figsize=(15, 15), gridspec_kw={"height_ratios": [2.4, 1.7, 1.2, 1.5]})
    fig.patch.set_facecolor("#f5f7fa")
    for ax in axes:
        ax.grid(alpha=.18)
        ax.spines[["top", "right"]].set_visible(False)
    axes[0].plot(dates, closes, color="#283b50", linewidth=1, label="終値")
    for window, color in [(p["short"], "#399e86"), (p["long"], "#976eb4")]:
        means = [sum(closes[i-window+1:i+1]) / window if i >= window - 1 else float("nan") for i in range(len(rows))]
        axes[0].plot(dates, means, color=color, linewidth=1, alpha=.85, label=f"{window}日移動平均")
    for side, marker, color, label in [("buy", "^", blue, "買い（調整後）"), ("sell", "v", "#d34343", "売り（調整後）")]:
        trades = [t for t in report["trades"] if t["side"] == side]
        axes[0].scatter([datetime.fromisoformat(t["date"]) for t in trades], [t["price"] * adjustments[t["date"]] for t in trades],
                        marker=marker, color=color, s=45, zorder=4, label=label)
    worst = sorted(pairs, key=lambda x: x["pnl"])[:3]
    for index, pair in enumerate(worst, 1):
        if pair["pnl"] < 0:
            x = datetime.fromisoformat(pair["sell_date"])
            axes[0].axvspan(datetime.fromisoformat(pair["buy_date"]), x, color="#d34343", alpha=.08)
            axes[0].annotate(f"損失{index}", (x, pair["sell_price"] * adjustments[pair["sell_date"]]), xytext=(0, -28-index*4),
                             textcoords="offset points", fontsize=9, ha="center", color="#ab3030")
    axes[0].set_title("株価と売買位置｜薄赤の期間は損失額の大きい3取引", loc="left", fontweight="bold")
    axes[0].set_ylabel("株価（円・最終日基準で分割調整）")
    axes[0].legend(loc="upper left", ncol=3, fontsize=9)
    axes[1].plot(dates, [v/10000 for v in strategy], color=blue, label="移動平均ルール")
    axes[1].plot(dates, [v/10000 for v in hold], color=orange, label=f"初日に{p['quantity']}株購入・保有継続")
    axes[1].axhline(p["initial"]/10000, color="#777", linestyle=":", label="初期資金")
    axes[1].set_title("資産推移｜両者とも余剰資金を現金で保持", loc="left", fontweight="bold")
    axes[1].set_ylabel("資産（万円）")
    axes[1].legend(loc="best", fontsize=9)
    for values, color, label in [(strategy, blue, "移動平均ルール"), (hold, orange, "保有継続")]:
        axes[2].plot(dates, drawdown(values, p["initial"]), color=color, label=label)
    axes[2].set_title("過去の資産最高値からの下落率｜日次終値ベース", loc="left", fontweight="bold")
    axes[2].set_ylabel("下落率（%）")
    for ax in axes[:3]:
        ax.set_xlim(dates[0], dates[-1])
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    axes[3].bar(range(1, len(pairs)+1), [t["pnl"]/10000 for t in pairs],
                color=["#d34343" if t["pnl"] < 0 else "#399e86" for t in pairs])
    axes[3].axhline(0, color="#777", linewidth=.8)
    axes[3].set_xticks(range(1, len(pairs)+1))
    axes[3].set_xlabel("完結取引の番号（購入順）")
    axes[3].set_ylabel("損益（万円）")
    axes[3].set_title("1往復ごとの確定損益｜取引コスト控除後", loc="left", fontweight="bold")
    fig.suptitle(f"{report['symbol']} 日足バックテスト｜{report['start']} — {report['end']}\n"
                 f"移動平均ルール {strategy[-1]-p['initial']:+,.0f}円 ／ 保有継続 {hold[-1]-p['initial']:+,.0f}円", fontsize=17, fontweight="bold")
    fig.text(.06, .016, f"初期資金 {p['initial']:,.0f}円・{p['quantity']}株 ／ 片道固定手数料 {p['fee_per_order']:,.0f}円・価格ずれ {p['slippage_bps']}bps\n"
             "保有継続は期間初日の始値で購入。移動平均には助走期間あり。最終保有は終値評価（未売却）。配当・税金なし。約定は仮定。", fontsize=9, color="#465568")
    fig.tight_layout(rect=(.02, .06, .99, .94), h_pad=2)
    output.mkdir(parents=True, exist_ok=False)
    fig.savefig(output / "backtest.png", dpi=150)
    fig.savefig(output / "backtest.pdf")
    plt.close(fig)
    summary = {"strategy_pnl": strategy[-1]-p["initial"], "hold_pnl": hold[-1]-p["initial"],
               "strategy_drawdown_pct": -min(drawdown(strategy, p["initial"])),
               "hold_drawdown_pct": -min(drawdown(hold, p["initial"])),
               "round_trips": len(pairs), "winning_trades": sum(t["pnl"] > 0 for t in pairs),
               "worst_trades": worst, "all_round_trips": pairs}
    (output / "comparison.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 売買シミュレーションの比較", "", f"対象: {report['symbol']} / {report['start']} ～ {report['end']}", "",
             f"移動平均ルールの損益: {summary['strategy_pnl']:+,.2f}円",
             f"初日に{p['quantity']}株購入・保有継続の損益: {summary['hold_pnl']:+,.2f}円", "",
             "株式分割・併合で保有株数を変更。株価グラフと売買マーカーは最終日基準に調整し、下表の約定価格は当時の実価格です。",
             "比較対象は初日に同じ株数を購入し、残金は現金で保有。初期資金、購入時コスト条件は共通です。",
             "保有継続は最終日に売却せず終値評価。配当・税金は含みません。投資期間・市場への露出は同一ではありません。", "",
             "## 損失額が大きかった取引", "", "|買い日|売り日|買値|売値|損益（円）|", "|---|---|---:|---:|---:|"]
    for t in worst:
        lines.append(f"|{t['buy_date']}|{t['sell_date']}|{t['buy_price']:,.2f}|{t['sell_price']:,.2f}|{t['pnl']:+,.2f}|")
    lines += ["", "![グラフ](backtest.png)", "", "この結果は単一銘柄・単一期間の事後検証です。データは元のバックテストとのハッシュ一致を確認済みです。"]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.loads(args.result.read_text(encoding="utf-8"))
    rows = [r for r in load_daily(args.db, report["symbol"]) if report["start"] <= r["Date"] <= report["end"]]
    output = args.output or ROOT / "data" / ("charts_" + datetime.now().strftime("%Y%m%d_%H%M%S%f"))
    summary = render(rows, report, output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("出力先:", output.resolve())


if __name__ == "__main__":
    main()
