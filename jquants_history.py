"""J-Quants V2日足取得。認証情報を保存しない。"""
import argparse
from contextlib import closing
from datetime import date, datetime, timezone
import getpass
import json
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "historical.sqlite3"


class HistoryError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def symbol_code(value):
    value = value.upper()
    if not re.fullmatch(r"[0-9A-Z]{4}0?", value):
        raise argparse.ArgumentTypeError("普通株の4文字コード、または末尾0の5文字コードを指定してください")
    return value + "0" if len(value) == 4 else value


def fetch_daily(api_key, code, opener=None, sleep=time.sleep):
    opener = opener or urllib.request.build_opener(NoRedirect())
    params, result, seen = {"code": code}, [], set()
    for page in range(1000):
        if page:
            sleep(15)  # 無料プランも想定して連続要求を抑える。
        url = "https://api.jquants.com/v2/equities/bars/daily?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"x-api-key": api_key, "Accept": "application/json"})
        try:
            with opener.open(req, timeout=30) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            hint = {401: "APIキーを確認してください。", 403: "APIキー・契約・取得権限を確認してください。",
                    429: "利用上限です。時間を置いて再実行してください。"}.get(status, "サービス状況を確認してください。")
            raise HistoryError(f"J-Quants HTTP {status}。{hint}") from None
        except (urllib.error.URLError, OSError):
            raise HistoryError("J-Quantsに接続できません。ネットワークを確認してください。") from None
        except (ValueError, UnicodeError):
            raise HistoryError("API応答のJSONを読み取れませんでした") from None
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise HistoryError("API応答にdata配列がありません")
        result.extend(body["data"])
        cursor = body.get("pagination_key")
        if not cursor:
            return result
        if not isinstance(cursor, str) or cursor in seen:
            raise HistoryError("ページ継続キーが不正です。部分データは保存しません。")
        seen.add(cursor)
        params["pagination_key"] = cursor
    raise HistoryError("取得ページ上限に達しました。部分データは保存しません。")


def save_daily(path, code, records):
    if not records:
        raise HistoryError("取得結果が0件です。契約状態・対象銘柄を確認してください。既存データは変更しません。")
    unique = {}
    for row in records:
        if not isinstance(row, dict) or row.get("Code") != code:
            raise HistoryError("応答の銘柄コードが要求と一致しません")
        try:
            day = date.fromisoformat(row["Date"]).isoformat()
        except (KeyError, ValueError, TypeError):
            raise HistoryError("応答の日付が不正です") from None
        if day in unique and unique[day] != row:
            raise HistoryError("同じ日付に異なるデータがあります")
        unique[day] = row
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fetched = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS daily_prices (
            code TEXT NOT NULL, day TEXT NOT NULL, source TEXT NOT NULL,
            fetched_at TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(code, day, source))""")
        # この銘柄を今回の完全な取得結果で置換する。取得失敗時はここへ到達しない。
        db.execute("DELETE FROM daily_prices WHERE code=? AND source='jquants_v2'", (code,))
        db.executemany("INSERT INTO daily_prices VALUES (?, ?, 'jquants_v2', ?, ?)",
                       [(code, day, fetched, json.dumps(row, ensure_ascii=False, allow_nan=False))
                        for day, row in unique.items()])
    return len(unique), min(unique), max(unique)


def load_daily(path, code):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        sources = [r[0] for r in db.execute('SELECT DISTINCT source FROM daily_prices')]
        if len(sources) > 1 or sources and sources[0] not in ('jquants_v2', 'yahoo_reconstructed_v1'):
            raise HistoryError('データ取得元が混在または未対応です。取得元ごとにDBを分けてください')
        source = sources[0] if sources else 'jquants_v2'
        return [json.loads(r[0]) for r in db.execute(
            "SELECT payload FROM daily_prices WHERE code=? AND source=? ORDER BY day", (code, source))]


def main(argv=None):
    parser = argparse.ArgumentParser(description="J-Quants V2から取得可能な日足を保存（kabuステーション不要）")
    parser.add_argument("--symbol", type=symbol_code, default="72030")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args(argv)
    try:
        print(f"J-Quants日足取得: {args.symbol} / APIキーは保存しません")
        key = getpass.getpass("J-Quants APIキー（非表示）: ").strip()
        if not key or not key.isascii() or any(c.isspace() for c in key):
            raise HistoryError("APIキーが空、または空白・全角文字を含んでいます")
        records = fetch_daily(key, args.symbol)
        del key
        count, first, last = save_daily(args.db, args.symbol, records)
        print(f"保存成功: {count}日分 / {first} ～ {last}")
        print(f"保存先: {args.db.resolve()}")
        print("取得できた期間だけを保存しました。直近まで揃っていることを意味しません。")
        return 0
    except (HistoryError, sqlite3.Error, OSError, ValueError, EOFError) as exc:
        print("停止: " + (str(exc) if isinstance(exc, HistoryError) else type(exc).__name__))
        return 1
    except KeyboardInterrupt:
        print("\n中止しました。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
