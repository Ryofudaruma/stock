"""閾値判定・重複通知防止のテスト。

実行方法: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import main  # noqa: E402
import notifiers  # noqa: E402

TODAY = date(2026, 9, 23)

CONFIG_YAML = """\
notify:
  line: true
  email: true
  email_always: false
stocks:
  - symbol: "7203.T"
    name: "トヨタ自動車"
    threshold: 2500
  - symbol: "AAPL"
    name: "Apple"
    threshold: 180
  - symbol: "MSFT"
    threshold: 400
"""


class FakePrices:
    def __init__(self, prices: dict):
        self.prices = prices

    def __call__(self, symbol):
        value = self.prices.get(symbol)
        if isinstance(value, Exception):
            raise value
        return value


class RecordingNotifier:
    def __init__(self, result: bool = True):
        self.result = result
        self.calls: list[str] = []

    def __call__(self, text, notify_config):
        self.calls.append(text)
        return self.result


class RunTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.config_path = self.dir / "config.yaml"
        self.state_path = self.dir / "state.json"
        self.config_path.write_text(CONFIG_YAML, encoding="utf-8")
        self.state_path.write_text("{}", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self, prices, notifier=None, today=TODAY):
        notifier = notifier or RecordingNotifier()
        alerts = main.run(
            config_path=self.config_path,
            state_path=self.state_path,
            price_getter=FakePrices(prices),
            notify_func=notifier,
            today=today,
        )
        return alerts, notifier

    def read_state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))


class ThresholdTest(RunTestBase):
    def test_price_at_or_below_threshold_is_detected_and_flagged(self):
        alerts, notifier = self.run_once({"7203.T": 2450.0, "AAPL": 180, "MSFT": 500})
        self.assertEqual({a.stock.symbol for a in alerts}, {"7203.T", "AAPL"})
        self.assertEqual(len(notifier.calls), 1)  # 1通にまとめて送る
        self.assertEqual(
            self.read_state(), {"7203.T:2026-09-23": True, "AAPL:2026-09-23": True}
        )

    def test_price_above_threshold_is_not_detected(self):
        alerts, notifier = self.run_once({"7203.T": 2500.01, "AAPL": 200, "MSFT": 500})
        self.assertEqual(alerts, [])
        self.assertEqual(notifier.calls, [])
        self.assertEqual(self.read_state(), {})

    def test_message_format(self):
        _, notifier = self.run_once({"7203.T": 2450.0, "AAPL": 170.123, "MSFT": 500})
        self.assertEqual(
            notifier.calls[0],
            "【株価アラート】トヨタ自動車(7203.T)\n"
            "現在値: 2450.0\n"
            "設定した閾値: 2500 を下回りました。\n"
            "\n"
            "【株価アラート】Apple(AAPL)\n"
            "現在値: 170.12\n"
            "設定した閾値: 180 を下回りました。",
        )

    def test_name_defaults_to_symbol(self):
        _, notifier = self.run_once({"MSFT": 300})
        self.assertIn("【株価アラート】MSFT(MSFT)", notifier.calls[0])


class DuplicateTest(RunTestBase):
    def test_second_run_same_day_does_not_notify(self):  # FR6
        prices = {"7203.T": 2400, "AAPL": 200, "MSFT": 500}
        alerts1, _ = self.run_once(prices)
        alerts2, notifier2 = self.run_once(prices)
        self.assertEqual(len(alerts1), 1)
        self.assertEqual(alerts2, [])
        self.assertEqual(notifier2.calls, [])
        self.assertEqual(self.read_state(), {"7203.T:2026-09-23": True})

    def test_next_day_notifies_again(self):
        prices = {"7203.T": 2400}
        self.run_once(prices)
        alerts, _ = self.run_once(prices, today=date(2026, 9, 24))
        self.assertEqual(len(alerts), 1)

    def test_flag_cleared_when_recovered_and_renotified(self):  # FR7
        self.run_once({"7203.T": 2400})
        self.assertIn("7203.T:2026-09-23", self.read_state())

        alerts, _ = self.run_once({"7203.T": 2600})
        self.assertEqual(alerts, [])
        self.assertNotIn("7203.T:2026-09-23", self.read_state())

        alerts, notifier = self.run_once({"7203.T": 2450})
        self.assertEqual([a.stock.symbol for a in alerts], ["7203.T"])
        self.assertEqual(len(notifier.calls), 1)
        self.assertIn("7203.T:2026-09-23", self.read_state())

    def test_flag_not_set_when_all_notifications_fail(self):
        self.run_once({"7203.T": 2400}, notifier=RecordingNotifier(result=False))
        self.assertEqual(self.read_state(), {})
        # 次回は再度通知対象になる
        alerts, _ = self.run_once({"7203.T": 2400})
        self.assertEqual(len(alerts), 1)

    def test_price_failure_keeps_existing_flag(self):
        self.run_once({"7203.T": 2400})
        self.run_once({"7203.T": None})
        self.assertIn("7203.T:2026-09-23", self.read_state())


class StatePruneTest(RunTestBase):
    def test_old_keys_are_removed(self):
        self.state_path.write_text(
            json.dumps(
                {
                    "7203.T:2026-09-15": True,  # 8日前 -> 削除
                    "7203.T:2026-09-16": True,  # 7日前 -> 保持
                    "AAPL:2026-09-22": True,
                    "broken-key": True,  # 形式不正 -> 削除
                }
            ),
            encoding="utf-8",
        )
        self.run_once({})
        self.assertEqual(
            self.read_state(), {"7203.T:2026-09-16": True, "AAPL:2026-09-22": True}
        )

    def test_missing_or_broken_state_file(self):
        self.state_path.unlink()
        alerts, _ = self.run_once({"7203.T": 2400})
        self.assertEqual(len(alerts), 1)
        self.state_path.write_text("not json", encoding="utf-8")
        alerts, _ = self.run_once({"AAPL": 100})
        self.assertEqual(len(alerts), 1)


class PartialFailureTest(RunTestBase):  # FR8
    def test_other_stocks_continue_when_one_fails(self):
        with self.assertLogs("stock_alert", level="WARNING"):
            alerts, _ = self.run_once(
                {"7203.T": None, "AAPL": RuntimeError("boom"), "MSFT": 300}
            )
        self.assertEqual([a.stock.symbol for a in alerts], ["MSFT"])

    def test_all_fail_does_not_raise(self):
        alerts, notifier = self.run_once({})
        self.assertEqual(alerts, [])
        self.assertEqual(notifier.calls, [])

    def test_get_current_price_returns_none_on_error(self):
        with mock.patch.dict(sys.modules, {"yfinance": None}):  # import 失敗を再現
            self.assertIsNone(main.get_current_price("7203.T"))

    def test_invalid_config_entries_are_skipped(self):
        self.config_path.write_text(
            "stocks:\n"
            "  - symbol: 'AAPL'\n"
            "  - threshold: 100\n"
            "  - symbol: 'MSFT'\n"
            "    threshold: 'abc'\n"
            "  - symbol: 'NVDA'\n"
            "    threshold: 100\n",
            encoding="utf-8",
        )
        alerts, _ = self.run_once({"AAPL": 1, "MSFT": 1, "NVDA": 90})
        self.assertEqual([a.stock.symbol for a in alerts], ["NVDA"])


class NotificationRoutingTest(unittest.TestCase):
    def route(self, notify_config, line_ok=True, email_ok=True):
        line = mock.Mock(return_value=line_ok)
        email = mock.Mock(return_value=email_ok)
        result = main.send_notifications("本文", notify_config, line, email)
        return result, line, email

    def test_line_success_skips_email_by_default(self):
        result, line, email = self.route({"line": True, "email": True})
        self.assertTrue(result)
        line.assert_called_once()
        email.assert_not_called()

    def test_line_failure_falls_back_to_email(self):
        result, line, email = self.route({"line": True, "email": True}, line_ok=False)
        self.assertTrue(result)
        email.assert_called_once()

    def test_email_always(self):
        result, _, email = self.route({"line": True, "email": True, "email_always": True})
        self.assertTrue(result)
        email.assert_called_once()

    def test_line_disabled_uses_email(self):
        result, line, email = self.route({"line": False, "email": True})
        self.assertTrue(result)
        line.assert_not_called()
        email.assert_called_once()

    def test_all_fail(self):
        result, _, _ = self.route({"line": True, "email": True}, line_ok=False, email_ok=False)
        self.assertFalse(result)


class NotifierEnvTest(unittest.TestCase):
    ENV_KEYS = [
        "LINE_CHANNEL_ACCESS_TOKEN",
        "LINE_USER_ID",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "EMAIL_TO",
    ]

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)

    def test_missing_env_skips_with_warning(self):
        with self.assertLogs("notifiers", level="WARNING") as logs:
            self.assertFalse(notifiers.send_line("x"))
            self.assertFalse(notifiers.send_email("x"))
        self.assertEqual(len(logs.records), 2)

    def test_line_request_and_truncation(self):
        os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = "dummy-token"
        os.environ["LINE_USER_ID"] = "Udummy"
        response = mock.Mock(status_code=200, text="{}")
        with mock.patch.object(notifiers.requests, "post", return_value=response) as post:
            self.assertTrue(notifiers.send_line("あ" * 6000))
        _, kwargs = post.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer dummy-token")
        self.assertEqual(kwargs["json"]["to"], "Udummy")
        self.assertEqual(len(kwargs["json"]["messages"][0]["text"]), 4900)

    def test_line_http_error_returns_false(self):
        os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = "dummy-token"
        os.environ["LINE_USER_ID"] = "Udummy"
        response = mock.Mock(status_code=401, text="unauthorized")
        with mock.patch.object(notifiers.requests, "post", return_value=response):
            with self.assertLogs("notifiers", level="ERROR"):
                self.assertFalse(notifiers.send_line("x"))

    def test_line_network_error_returns_false(self):
        os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = "dummy-token"
        os.environ["LINE_USER_ID"] = "Udummy"
        with mock.patch.object(notifiers.requests, "post", side_effect=OSError("down")):
            with self.assertLogs("notifiers", level="ERROR"):
                self.assertFalse(notifiers.send_line("x"))

    def test_email_uses_starttls_and_defaults(self):
        os.environ.update(SMTP_USER="me@example.com", SMTP_PASSWORD="pw", EMAIL_TO="to@example.com")
        with mock.patch.object(notifiers.smtplib, "SMTP") as smtp_cls:
            self.assertTrue(notifiers.send_email("本文"))
        smtp_cls.assert_called_once_with("smtp.gmail.com", 587, timeout=30)
        smtp = smtp_cls.return_value.__enter__.return_value
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("me@example.com", "pw")

    def test_email_failure_returns_false(self):
        os.environ.update(SMTP_USER="me@example.com", SMTP_PASSWORD="pw", EMAIL_TO="to@example.com")
        with mock.patch.object(notifiers.smtplib, "SMTP", side_effect=OSError("refused")):
            with self.assertLogs("notifiers", level="ERROR"):
                self.assertFalse(notifiers.send_email("x"))


class YamlValidityTest(unittest.TestCase):
    def test_repo_yaml_files_parse(self):
        import yaml

        root = Path(__file__).resolve().parent.parent
        config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
        self.assertTrue(main.parse_stocks(config))
        workflow = yaml.safe_load(
            (root / ".github" / "workflows" / "check.yml").read_text(encoding="utf-8")
        )
        self.assertIn("jobs", workflow)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
