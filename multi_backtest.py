"""銘柄ごとに独立した資金で検証する。実注文機能なし。"""
import argparse
from datetime import date, datetime
import getpass
import json
from pathlib import Path
import sqlite3
import time

from daily_backtest import simulate, positive
from jquants_history import ROOT, DEFAULT_DB, HistoryError, symbol_code, fetch_daily, save_daily, load_daily
from plot_backtest import comparison, drawdown


def read_symbols(path):
    values = json.loads(Path(path).read_text(encoding="utf-8-sig"))["symbols"]
    if not isinstance(values, list) or not values:
        raise HistoryError("銘柄一覧が空または不正です")
    codes = [symbol_code(str(value)) for value in values]
    if len(set(codes)) != len(codes):
        raise HistoryError("銘柄が重複しています。4文字・5文字表記の重複も確認してください")
    return codes


def fetch_many(codes, key, db, fetch=fetch_daily, save=save_daily, sleep=time.sleep):
    results = []
    for index, code in enumerate(codes):
        if index:
            sleep(15)
        print(f"取得 {index+1}/{len(codes)}: {code}", flush=True)
        try:
            count, start, end = save(db, code, fetch(key, code))
            results.append({"symbol": code, "status": "ok", "days": count, "start": start, "end": end})
            print(f"  保存成功: {count}日 / {start} ～ {end}", flush=True)
        except (HistoryError, OSError, sqlite3.Error, ValueError) as exc:
            message = str(exc) if isinstance(exc, HistoryError) else type(exc).__name__
            results.append({"symbol": code, "status": "failed", "reason": message})
            print(f"  失敗: {message}（既存データがある場合は以前のままです）", flush=True)
    return results


def run_many(codes, db, initial=1_000_000, short=5, long=20, quantity=100,
             fee=0, slippage_bps=5, start=None, end=None, loader=load_daily,
             after_actions=False):
    datasets, failed = {}, {}
    for code in codes:
        try:
            rows = loader(db, code)
            if not rows:
                raise HistoryError("日足未取得です。先にfetchを実行してください")
            # 期間をそろえる前に日付の重複・逆行も確認する。
            days = [date.fromisoformat(r["Date"]) for r in rows]
            if days != sorted(set(days)):
                raise HistoryError("日付の重複・逆行があります")
            datasets[code] = rows
        except (HistoryError, OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            failed[code] = str(exc) if isinstance(exc, HistoryError) else type(exc).__name__
    if not datasets:
        return {"requested_symbols": codes, "initial_per_symbol": initial, "results": [],
                "failures": failed, "aggregate": None, "aggregate_reason": "有効なデータがありません"}
    common_start = max([r[0]["Date"] for r in datasets.values()] + ([start.isoformat()] if start else []))
    common_end = min([r[-1]["Date"] for r in datasets.values()] + ([end.isoformat()] if end else []))
    original_start = common_start
    if after_actions:
        # 明示オプションだけで短縮。調整係数欠損は回避せずsimulateが拒否する。
        actions = [r["Date"] for rows in datasets.values() for r in rows
                   if common_start <= r["Date"] <= common_end and positive(r.get("AdjFactor")) and r["AdjFactor"] != 1]
        if actions:
            last_action = max(actions)
            next_days = [r["Date"] for rows in datasets.values() for r in rows
                         if last_action < r["Date"] <= common_end]
            common_start = min(next_days) if next_days else "9999-12-31"
    results = []
    for code, rows in datasets.items():
        selected = [r for r in rows if common_start <= r["Date"] <= common_end]
        try:
            result = simulate(selected, initial, short, long, quantity, fee, slippage_bps)
            item = {"symbol": code, "strategy": result}
            try:
                hold, _ = comparison(selected, result)
                item["hold"] = {"equity": hold, "pnl": hold[-1]-initial,
                                "max_drawdown_pct": -min(drawdown(hold, initial))}
            except (ValueError, HistoryError) as exc:
                item["hold"] = None
                item["hold_reason"] = str(exc)
            results.append(item)
        except (HistoryError, ValueError, KeyError, TypeError) as exc:
            failed[code] = str(exc) if isinstance(exc, HistoryError) else type(exc).__name__
    aggregate, reason = None, None
    if failed:
        reason = "失敗銘柄があるため、全銘柄の合計は計算しません"
    elif not results:
        reason = "計算可能な期間がありません"
    else:
        dates = [r["date"] for r in results[0]["strategy"]["equity_curve"]]
        if any([r["date"] for r in item["strategy"]["equity_curve"]] != dates for item in results):
            reason = "銘柄間の日付行が一致しません。欠損を埋めず、合計を停止します"
        else:
            total = initial * len(codes)
            equity = [sum(item["strategy"]["equity_curve"][i]["equity"] for item in results) for i in range(len(dates))]
            hold_equity = ([sum(item["hold"]["equity"][i] for item in results) for i in range(len(dates))]
                           if all(item["hold"] is not None for item in results) else None)
            aggregate = {"dates": dates, "initial": total, "equity": equity,
                         "pnl": equity[-1]-total, "return_pct": (equity[-1]/total-1)*100,
                         "max_drawdown_pct": -min(drawdown(equity, total)), "hold_equity": hold_equity,
                         "hold_pnl": hold_equity[-1]-total if hold_equity else None}
    return {"requested_symbols": codes, "initial_per_symbol": initial, "results": results, "failures": failed,
            "common_start": common_start, "common_end": common_end, "original_common_start": original_start,
            "after_actions": after_actions, "aggregate": aggregate, "aggregate_reason": reason}


def write_report(report, folder):
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    lines = ["# 複数銘柄・独立資金のシミュレーション", "",
             "取得元: " + ", ".join(sorted({item['strategy'].get('data_source', 'jquants_v2') for item in report['results']})), "",
             f"各銘柄 {report['initial_per_symbol']:,.0f}円 × {len(report['requested_symbols'])}銘柄。銘柄間の資金融通なし。", "",
             "新規購入は1回100株が既定、売却は分割後の保有全株です。資金全額を投資する方式ではありません。配当・税金なし、約定は仮定です。",
             "分割・併合は権利落ち日の始値約定前に保有株数を調整し、総取得原価を維持。移動平均は当日までの係数で補正します。", "",
             f"集計対象期間: {report.get('common_start')} ～ {report.get('common_end')}",
             f"調整日後に期間短縮: {report.get('after_actions', False)} / 短縮前開始日: {report.get('original_common_start')}", "",
             "|銘柄|損益（円）|収益率|最大下落率|売買件数|保有継続損益（円）|", "|---|---:|---:|---:|---:|---:|"]
    for item in report["results"]:
        r, hold = item["strategy"], item["hold"]
        hold_text = f"{hold['pnl']:+,.0f}" if hold else "比較不可"
        lines.append(f"|{item['symbol']}|{r['total_pnl']:+,.0f}|{r['return_pct']:.2f}%|{r['max_drawdown_pct']:.2f}%|{len(r['trades'])}|{hold_text}|")
        (folder / f"backtest_{item['symbol']}.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in report["results"]:
        if item["hold"] is None:
            lines.append(f"\n{item['symbol']} 保有継続比較なし: {item['hold_reason']}\n")
    lines += ["", "## 失敗・未取得", ""]
    lines += [f"- {code}: {reason}" for code, reason in report["failures"].items()] or ["なし"]
    aggregate = report["aggregate"]
    if aggregate:
        lines += ["", f"合計初期資金: {aggregate['initial']:,.0f}円 / 合計損益: {aggregate['pnl']:+,.0f}円 / 最大下落率: {aggregate['max_drawdown_pct']:.2f}%"]
    else:
        lines += ["", "合計なし: " + report["aggregate_reason"]]
    if report["results"]:
        plot_summary(report, folder / "summary.png")
        lines += ["", "![比較グラフ](summary.png)"]
    lines += ["", "保有継続は共通期間の最初の日に同じ株数を買い、残金は現金で保持。最終保有は終値評価。",
              "現在の銘柄例を過去に当てはめる検証であり、銘柄選択の偏りがあります。市場全体の成績ではありません。"]
    (folder / "report.md").write_text("\n".join(lines), encoding="utf-8")


def plot_summary(report, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    names = {f.name for f in font_manager.fontManager.ttflist}
    for name in ("Yu Gothic", "Meiryo", "Noto Sans CJK JP", "MS Gothic"):
        if name in names:
            plt.rcParams["font.family"] = name
            break
    plt.rcParams["axes.unicode_minus"] = False
    fig, axes = plt.subplots(3, 1, figsize=(13, 12))
    items = report["results"]
    x = list(range(len(items)))
    axes[0].bar([v-.18 for v in x], [i["strategy"]["total_pnl"]/10000 for i in items], width=.36, label="移動平均ルール")
    axes[0].bar([v+.18 for v in x], [i["hold"]["pnl"]/10000 if i["hold"] else float("nan") for i in items], width=.36, label="保有継続")
    axes[0].set_xticks(x, [i["symbol"] for i in items])
    axes[0].set_ylabel("損益（万円）")
    axes[0].set_title("銘柄別損益｜計算完了した銘柄を表示")
    axes[0].legend()
    for item in items:
        curve = item["strategy"]["equity_curve"]
        axes[1].plot([date.fromisoformat(r["date"]) for r in curve], [r["equity"]/10000 for r in curve], label=item["symbol"])
    axes[1].set_title("銘柄別資産推移（移動平均ルール）")
    axes[1].set_ylabel("資産（万円）")
    axes[1].legend(ncol=5, fontsize=8)
    a = report["aggregate"]
    if a:
        dates = [date.fromisoformat(d) for d in a["dates"]]
        axes[2].plot(dates, [v/10000 for v in a["equity"]], label="移動平均ルール合計")
        if a["hold_equity"]:
            axes[2].plot(dates, [v/10000 for v in a["hold_equity"]], label="保有継続合計")
        else:
            axes[2].text(.02, .94, "保有継続合計なし：比較不可の銘柄あり（詳細はレポート）",
                         transform=axes[2].transAxes, va="top", fontsize=9)
        axes[2].axhline(a["initial"]/10000, linestyle=":", color="gray")
        axes[2].legend(loc="lower right")
    else:
        axes[2].text(.5, .5, report["aggregate_reason"], ha="center", transform=axes[2].transAxes)
    axes[2].set_title("全銘柄の資産合計｜欠損・失敗時は合計しません")
    axes[2].set_ylabel("資産（万円）")
    for ax in axes:
        ax.grid(alpha=.2)
    fig.suptitle(f"独立資金 {report['initial_per_symbol']/10000:,.0f}万円 × {len(report['requested_symbols'])}銘柄\n"
                 f"計算完了 {len(items)} / 計算不可 {len(report['failures'])} | {report.get('common_start')} ～ {report.get('common_end')}", fontsize=15)
    fig.text(.05, .015, "残金は現金保持。配当・税金なし。実注文なし。新規購入100株が既定、分割時は株数調整。保有継続と投資開始時期は異なります。", fontsize=9)
    fig.tight_layout(rect=(0, .04, 1, .93))
    fig.savefig(path, dpi=140)
    plt.close(fig)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["fetch", "run"])
    parser.add_argument("--symbols-file", type=Path, default=ROOT / "symbols_10.json")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--cash", type=float, default=1_000_000, help="1銘柄あたりの資金")
    parser.add_argument("--quantity", type=int, default=100)
    parser.add_argument("--short", type=int, default=5)
    parser.add_argument("--long", type=int, default=20)
    parser.add_argument("--fee", type=float, default=0)
    parser.add_argument("--slippage-bps", type=float, default=5)
    parser.add_argument("--from", dest="start", type=date.fromisoformat)
    parser.add_argument("--to", dest="end", type=date.fromisoformat)
    parser.add_argument("--after-actions", action="store_true", help="全銘柄で最後の分割等の翌データ日以降に期間を短縮")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        codes = read_symbols(args.symbols_file)
        if not positive(args.cash):
            raise HistoryError("資金は正の有限値を指定してください")
        if args.start and args.end and args.start > args.end:
            raise HistoryError("開始日が終了日より後です")
        print(f"{len(codes)}銘柄 / 各{args.cash:,.0f}円 / 合計{args.cash*len(codes):,.0f}円（仮想資金）")
        folder = args.output or ROOT / "data" / ("multi_" + args.command + "_" + datetime.now().strftime("%Y%m%d_%H%M%S%f"))
        if folder.exists():
            raise HistoryError("出力フォルダは既に存在します。別名を指定してください")
        if args.command == "fetch":
            key = getpass.getpass("J-Quants APIキー（非表示・保存なし）: ").strip()
            if not key or not key.isascii() or any(c.isspace() for c in key):
                raise HistoryError("APIキーが空または不正です")
            results = fetch_many(codes, key, args.db)
            del key
            folder.mkdir(parents=True)
            (folder / "fetch_status.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            ok = sum(r["status"] == "ok" for r in results)
            print(f"取得成功 {ok}/{len(codes)}。失敗銘柄のデータは更新されていません。")
            print("結果:", folder.resolve())
            return 0 if ok == len(codes) else 1
        report = run_many(codes, args.db, args.cash, args.short, args.long, args.quantity,
                          args.fee, args.slippage_bps, args.start, args.end, after_actions=args.after_actions)
        write_report(report, folder)
        print(f"共通期間: {report.get('common_start')} ～ {report.get('common_end')}")
        for item in report["results"]:
            r = item["strategy"]
            print(f"{item['symbol']}: {r['total_pnl']:+,.0f}円 / {r['return_pct']:+.2f}% / 売買{len(r['trades'])}回")
        for code, reason in report["failures"].items():
            print(f"{code}: 検証不可 / {reason}")
        if report["aggregate"]:
            print(f"全銘柄合計損益: {report['aggregate']['pnl']:+,.0f}円")
        else:
            print(report["aggregate_reason"])
        print("結果・グラフ:", folder.resolve())
        return 0 if report["aggregate"] else 1
    except (HistoryError, OSError, sqlite3.Error, ValueError, KeyError, TypeError, EOFError, argparse.ArgumentTypeError) as exc:
        print("停止: " + (str(exc) if isinstance(exc, HistoryError) else type(exc).__name__))
        return 1
    except KeyboardInterrupt:
        print("\n中止しました。完了した銘柄の取得データは保存されています。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
