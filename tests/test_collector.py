import io
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

from kabu_collector import CollectorError, KabuClient, Recorder, parse_args, watch


class CollectorTests(unittest.TestCase):
    def test_register_accepts_official_response_without_result_code(self):
        client = KabuClient("production")
        client.token = "test-token"
        client.opener = Mock()
        client.opener.open.return_value = io.BytesIO(
            b'{"RegistList":[{"Symbol":"7203","Exchange":1},{"Symbol":"8306","Exchange":1}]}')
        client.register(["7203"])
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.method, "PUT")
        self.assertEqual(json.loads(request.data), {"Symbols": [{"Symbol": "7203", "Exchange": 1}]})

    def test_register_rejects_missing_symbol_or_wrong_exchange(self):
        for response in [{}, {"RegistList": []}, {"RegistList": None},
                         {"RegistList": [{"Symbol": "7203", "Exchange": 3}]},
                         {"RegistList": [{"Symbol": "8306", "Exchange": 1}]}]:
            with self.subTest(response=response):
                client = KabuClient("production")
                client._request = Mock(return_value=response)
                with self.assertRaises(CollectorError):
                    client.register(["7203"])

    def test_auth_error_shows_code_without_response_secrets(self):
        for code, expected in [(4001013, "APIパスワードが一致"), (4001017, "未ログイン")]:
            client = KabuClient("production")
            client.opener = Mock()
            body = io.BytesIO(json.dumps({"Code": code, "Message": "private-secret"}).encode())
            client.opener.open.side_effect = urllib.error.HTTPError(client.base, 401, "Unauthorized", {}, body)
            with self.assertRaises(CollectorError) as raised:
                client.authenticate("password-secret")
            message = str(raised.exception)
            self.assertIn(str(code), message)
            self.assertIn(expected, message)
            self.assertNotIn("secret", message)

    def test_order_api_is_blocked_before_network(self):
        client = KabuClient("production")
        client.opener = Mock()
        for method, path in [("POST", "/sendorder"), ("PUT", "/cancelorder"),
                             ("GET", "/board/7203@1/../../sendorder")]:
            with self.assertRaises(CollectorError):
                client._request(method, path)
        client.opener.open.assert_not_called()

    def test_auth_and_header(self):
        client = KabuClient("verification")
        client.opener = Mock()
        client.opener.open.side_effect = [io.BytesIO(b'{"ResultCode":0,"Token":"test-secret"}'),
                                         io.BytesIO(b'{"Symbol":"7203"}')]
        client.authenticate("example")
        self.assertEqual(client.board("7203")["Symbol"], "7203")
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:18081/kabusapi/board/7203@1")
        self.assertEqual(request.get_header("X-api-key"), "test-secret")

    def test_record_preserves_null_market_time_and_book(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            data = {"Symbol": "7203", "CurrentPrice": None,
                    "CurrentPriceTime": "2026-09-11T15:30:00+09:00",
                    "Sell1": {"Price": 3000, "Qty": 100}}
            recorder = Recorder(path, "verification")
            recorder.write("push", data)
            recorder.close()
            with closing(sqlite3.connect(path)) as db:
                row = db.execute("SELECT received_at, environment, payload FROM events").fetchone()
            self.assertTrue(row[0].endswith("+00:00"))
            self.assertEqual(row[1], "verification")
            self.assertEqual(json.loads(row[2]), data)

    def test_arguments(self):
        args = parse_args(["quote", "--symbols", "7203", "130a", "7203"])
        self.assertEqual(args.symbols, ["7203", "130A"])
        with self.assertRaises(SystemExit), patch("sys.stderr", new=io.StringIO()):
            parse_args(["quote", "--symbols", "../x"])

    def fake_websocket(self, messages):
        module = Mock()
        module.WebSocketException = type("WebSocketException", (Exception,), {})
        module.WebSocketTimeoutException = type("WebSocketTimeoutException", (module.WebSocketException,), {})
        connection = module.create_connection.return_value
        connection.recv.side_effect = messages
        return module, connection

    def test_stream_filters_other_symbols_and_stops_on_ctrl_c(self):
        module, connection = self.fake_websocket([
            json.dumps({"Symbol": "8306", "Exchange": 1}),
            json.dumps({"Symbol": "7203", "Exchange": 1, "CurrentPrice": 3000}), KeyboardInterrupt()])
        recorder = Mock()
        with self.assertRaises(KeyboardInterrupt):
            watch(Mock(), ["7203"], recorder, module)
        self.assertEqual([c.args[0] for c in recorder.write.call_args_list], ["connected", "push", "stopped"])
        connection.close.assert_called_once()

    def test_clean_remote_close_reconnects_with_limit(self):
        module, connection = self.fake_websocket(["", ""])
        client = Mock()
        with patch("kabu_collector.time.sleep") as sleep, self.assertRaises(CollectorError):
            watch(client, ["7203"], Mock(), module, max_retries=1)
        self.assertEqual(client.register.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_storage_failure_does_not_continue(self):
        module, connection = self.fake_websocket([json.dumps({"Symbol": "7203", "Exchange": 1})])
        recorder = Mock()
        recorder.write.side_effect = [None, sqlite3.OperationalError("disk full")]
        with self.assertRaises(sqlite3.OperationalError):
            watch(Mock(), ["7203"], recorder, module)
        connection.close.assert_called_once()
        self.assertEqual(module.create_connection.call_count, 1)


if __name__ == "__main__":
    unittest.main()
