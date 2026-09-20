"""kabuステーションの価格取得・記録専用CLI（注文機能なし）。"""
from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request


class CollectorError(Exception):
    pass


class KabuClient:
    def __init__(self, environment: str):
        if environment not in ("production", "verification"):
            raise ValueError("不正な接続環境です")
        self.environment = environment
        port = 18080 if environment == "production" else 18081
        self.base = f"http://localhost:{port}/kabusapi"
        self.ws_url = f"ws://localhost:{port}/kabusapi/websocket"
        self.token = None
        # ローカルAPIへの通信を外部プロキシへ送らない。
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, method, path, body=None):
        allowed = ((method, path) in {("POST", "/token"), ("PUT", "/register")}
                   or (method == "GET" and re.fullmatch(r"/board/[0-9A-Z]{4}@1", path)))
        if not allowed:
            raise CollectorError("このプログラムでは許可されていないAPIです")
        headers = {"Content-Type": "application/json"}
        if path != "/token":
            if not self.token:
                raise CollectorError("先に認証してください")
            headers["X-API-KEY"] = self.token
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base + path, data, headers, method=method)
        try:
            with self.opener.open(request, timeout=10) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            # 応答全文は出さず、公開仕様の数値コードだけを表示する。
            api_code = None
            try:
                error_body = json.loads(exc.read(65536))
                if isinstance(error_body, dict) and type(error_body.get("Code")) is int:
                    api_code = error_body["Code"]
            except (ValueError, UnicodeError, OSError):
                pass
            finally:
                exc.close()
            hints = {400: "パラメータ・API設定を確認してください。",
                     401: "認証情報を確認し、プログラムを再起動してください。",
                     403: "API利用設定・認証情報を確認してください。",
                     429: "API利用上限です。しばらく待って再実行してください。"}
            details = {
                4001007: "kabuステーションにログインしてください。",
                4001008: "API利用設定が完了しているか確認してください。",
                4001009: "APIキーが一致しません。他ツールのトークン再発行を確認し、再実行してください。",
                4001013: "APIパスワードが一致しません。接続環境に対応するAPIパスワードを確認してください。",
                4001017: "kabuステーションが未ログインです。ログイン後に再実行してください。",
            }
            stage = "トークン取得" if path == "/token" else "情報取得・登録"
            code_text = f" / Code {api_code}" if api_code is not None else ""
            hint = details.get(api_code, hints.get(exc.code, "kabuステーションの状態を確認してください。"))
            raise CollectorError(f"{stage}: APIエラー HTTP {exc.code}{code_text}。{hint}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise CollectorError("接続できません。kabuステーションの起動・ログイン・API設定と接続環境を確認してください。") from None
        except (ValueError, UnicodeError):
            raise CollectorError("APIから不正なJSON応答を受信しました") from None
        if not isinstance(result, dict):
            raise CollectorError("API応答の形式が不正です")
        return result

    def authenticate(self, password):
        result = self._request("POST", "/token", {"APIPassword": password})
        token = result.get("Token")
        if result.get("ResultCode") != 0 or not isinstance(token, str) or not token:
            raise CollectorError("トークンを取得できませんでした")
        self.token = token

    def board(self, symbol):
        return self._request("GET", f"/board/{symbol}@1")

    def register(self, symbols):
        result = self._request("PUT", "/register", {
            "Symbols": [{"Symbol": s, "Exchange": 1} for s in symbols]})
        # 登録APIはResultCodeではなくRegistListを返す。
        registered = result.get("RegistList")
        if not isinstance(registered, list):
            raise CollectorError("銘柄登録の応答にRegistListがありません")
        missing = [s for s in symbols if not any(
            isinstance(item, dict) and item.get("Symbol") == s and item.get("Exchange") == 1
            for item in registered)]
        if missing:
            raise CollectorError("登録応答に対象銘柄がありません: " + ", ".join(missing)
                                 + "。kabuステーションのAPI登録銘柄リストを確認してください。")


class Recorder:
    def __init__(self, path, environment):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.environment = environment
        self.db.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, received_at TEXT NOT NULL,
            environment TEXT NOT NULL, source TEXT NOT NULL,
            symbol TEXT, payload TEXT NOT NULL)""")
        self.db.commit()

    def write(self, source, payload):
        # 元の市場時刻をpayloadに残し、受信時刻は別に記録する。
        self.db.execute("INSERT INTO events (received_at, environment, source, symbol, payload) VALUES (?, ?, ?, ?, ?)",
                        (datetime.now(timezone.utc).isoformat(), self.environment,
                         source, payload.get("Symbol"), json.dumps(payload, ensure_ascii=False)))
        self.db.commit()

    def close(self):
        self.db.close()


def print_price(payload):
    print(f"{payload.get('Symbol', '?')} {payload.get('SymbolName', '')} | "
          f"現在値: {payload.get('CurrentPrice')} | "
          f"価格時刻: {payload.get('CurrentPriceTime')} | "
          f"出来高: {payload.get('TradingVolume')}", flush=True)


def watch(client, symbols, recorder, websocket_module, max_retries=5):
    retries = 0
    while True:
        client.register(symbols)
        ws = None
        started = time.monotonic()
        try:
            ws = websocket_module.create_connection(
                client.ws_url, timeout=10,
                http_no_proxy=["localhost", "127.0.0.1"])
            ws.settimeout(1)
            print("リアルタイム受信を開始しました。停止: Ctrl+C", flush=True)
            recorder.write("connected", {})
            while True:
                try:
                    message = ws.recv()
                except websocket_module.WebSocketTimeoutException:
                    continue
                if not message:
                    break
                try:
                    payload = json.loads(message)
                except (ValueError, UnicodeError):
                    raise CollectorError("不正な配信データを受信したため停止しました") from None
                if not isinstance(payload, dict):
                    raise CollectorError("配信データの形式が不正なため停止しました")
                # 同じkabuステーションを使う他ツールの登録銘柄は保存しない。
                if payload.get("Symbol") not in symbols or payload.get("Exchange") != 1:
                    continue
                recorder.write("push", payload)
                print_price(payload)
        except (websocket_module.WebSocketException, ConnectionError, TimeoutError) :
            # 例外全文には認証情報等が含まれる可能性があるため表示しない。
            print("配信通信でエラーが発生しました。", flush=True)
        except KeyboardInterrupt:
            recorder.write("stopped", {})
            raise
        finally:
            if ws is not None:
                ws.close()
        recorder.write("disconnected", {"note": "切断中の配信データは復元できません"})
        if time.monotonic() - started >= 60:
            retries = 0
        if retries >= max_retries:
            raise CollectorError("再接続上限に達しました。kabuステーションを確認し、再起動してください。")
        delay = min(2 ** (retries + 1), 30)
        retries += 1
        print(f"{delay}秒後に再接続します ({retries}/{max_retries})。切断中のデータには欠損があります。", flush=True)
        time.sleep(delay)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="kabuステーションの株価取得・記録専用（東証、注文機能なし）")
    parser.add_argument("--version", action="version", version="kabu_collector 0.1.2 (銘柄登録判定修正)")
    parser.add_argument("command", choices=["quote", "watch"], help="quote: 1回取得、watch: 継続記録")
    parser.add_argument("--symbols", nargs="+", default=["7203"], help="銘柄コード。既定: 7203")
    parser.add_argument("--environment", choices=["production", "verification"], default="production")
    parser.add_argument("--db", type=Path, help="保存先SQLiteファイル")
    args = parser.parse_args(argv)
    args.symbols = list(dict.fromkeys(s.upper() for s in args.symbols))
    if len(args.symbols) > 50 or any(not re.fullmatch(r"[0-9A-Z]{4}", s) for s in args.symbols):
        parser.error("銘柄コードは半角英数字4文字、最大50銘柄で指定してください")
    if args.db is None:
        args.db = Path(__file__).resolve().parent / "data" / f"{args.environment}.sqlite3"
    return args


def main(argv=None):
    args = parse_args(argv)
    websocket_module = None
    if args.command == "watch":
        try:
            import websocket as websocket_module
        except ImportError:
            print("リアルタイム受信には依存ライブラリが必要です。READMEのセットアップを実行してください。", file=sys.stderr)
            return 1
    recorder = None
    try:
        print("kabu_collector 0.1.2（銘柄登録判定修正）")
        print(f"環境: {args.environment} | 東証: {', '.join(args.symbols)} | 注文機能なし")
        print(f"保存先: {args.db.resolve()}")
        client = KabuClient(args.environment)
        password = getpass.getpass("kabuステーションのAPIパスワード（画面には表示されません）: ")
        if not password:
            raise CollectorError("APIパスワードが空です")
        client.authenticate(password)
        del password
        print("認証成功（トークンは表示・保存しません）")
        recorder = Recorder(args.db, args.environment)
        for symbol in args.symbols:
            payload = client.board(symbol)
            recorder.write("snapshot", payload)
            print_price(payload)
            time.sleep(0.2)
        if args.command == "watch":
            watch(client, args.symbols, recorder, websocket_module)
        return 0
    except KeyboardInterrupt:
        print("\n記録を停止しました。保存済みデータは残ります。")
        return 0
    except (CollectorError, sqlite3.Error, OSError, EOFError) as exc:
        message = str(exc) if isinstance(exc, CollectorError) else type(exc).__name__
        print("停止: " + message, file=sys.stderr)
        return 1
    finally:
        if recorder is not None:
            recorder.close()


if __name__ == "__main__":
    sys.exit(main())
