"""閾値判定・重複通知防止・設定ファイルのテスト。"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests import helpers  # noqa: F401

from stock_alert import core

TODAY = date(2026, 9, 23)

SETTINGS = {
    "interval_minutes": 30,
    "email": {"smtp_host": "smtp.gmail.com", "smtp_port": 587, "from_addr": "", "to_addr": ""},
    "stocks": [
        {"symbol": "7203.T", "name": "トヨタ自動車", "threshold": 2500},
        {"symbol": "AAPL", "name": "Apple", "threshold": 180},
        {"symbol": "MSFT", "name": "", "threshold": 400},
    ],
}


class FakePrices:
    def __init__(self, prices):
        self.prices = prices

    def __call__(self, symbol):
        value = self.prices.get(symbol)
        if isinstance(value, Exception):
            raise value
        return value


class RecordingNotifier:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def __call__(self, text):
        self.calls.append(text)
        return self.result


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state_path = self.dir / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self, prices, notifier=None, today=TODAY, settings=SETTINGS):
        notifier = notifier or RecordingNotifier()
        result = core.run_check(settings, FakePrices(prices), notifier, self.state_path, today)
        return result, notifier

    def read_state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))


class ThresholdTest(Base):
    def test_at_or_below_threshold_is_detected_and_flagged(self):
        result, notifier = self.run_once({"7203.T": 2450.0, "AAPL": 180, "MSFT": 500})
        self.assertEqual({a.stock.symbol for a in result.alerts}, {"7203.T", "AAPL"})
        self.assertTrue(result.notified)
        self.assertEqual(len(notifier.calls), 1)  # 1通にまとめる
        self.assertEqual(self.read_state(), {"7203.T:2026-09-23": True, "AAPL:2026-09-23": True})
        self.assertEqual(result.prices, {"7203.T": 2450.0, "AAPL": 180, "MSFT": 500})

    def test_above_threshold_is_not_detected(self):
        result, notifier = self.run_once({"7203.T": 2500.01, "AAPL": 200, "MSFT": 500})
        self.assertEqual(result.alerts, [])
        self.assertEqual(notifier.calls, [])
        self.assertEqual(self.read_state(), {})

    def test_message_format(self):
        _, notifier = self.run_once({"7203.T": 2450.0, "AAPL": 170.123, "MSFT": 399})
        self.assertEqual(
            notifier.calls[0],
            "【株価アラート】トヨタ自動車(7203.T)\n現在値: 2,450\n設定した閾値: 2,500 を下回りました。\n\n"
            "【株価アラート】Apple(AAPL)\n現在値: 170.12\n設定した閾値: 180 を下回りました。\n\n"
            "【株価アラート】MSFT(MSFT)\n現在値: 399\n設定した閾値: 400 を下回りました。",
        )

    def test_format_number(self):
        self.assertEqual(core.format_number(2450.0), "2,450")
        self.assertEqual(core.format_number(12345.678), "12,345.68")
        self.assertEqual(core.format_number(179.5), "179.5")


class DuplicateTest(Base):
    def test_second_run_same_day_does_not_notify(self):
        prices = {"7203.T": 2400, "AAPL": 200, "MSFT": 500}
        first, _ = self.run_once(prices)
        second, notifier = self.run_once(prices)
        self.assertEqual(len(first.alerts), 1)
        self.assertEqual(second.alerts, [])
        self.assertEqual(notifier.calls, [])

    def test_next_day_notifies_again(self):
        self.run_once({"7203.T": 2400})
        result, _ = self.run_once({"7203.T": 2400}, today=date(2026, 9, 24))
        self.assertEqual(len(result.alerts), 1)

    def test_flag_cleared_on_recovery_and_renotified(self):
        self.run_once({"7203.T": 2400})
        result, _ = self.run_once({"7203.T": 2600})
        self.assertEqual(result.alerts, [])
        self.assertNotIn("7203.T:2026-09-23", self.read_state())
        result, notifier = self.run_once({"7203.T": 2450})
        self.assertEqual([a.stock.symbol for a in result.alerts], ["7203.T"])
        self.assertEqual(len(notifier.calls), 1)

    def test_flag_not_set_when_notification_fails(self):
        result, _ = self.run_once({"7203.T": 2400}, notifier=RecordingNotifier(False))
        self.assertFalse(result.notified)
        self.assertEqual(self.read_state(), {})
        result, _ = self.run_once({"7203.T": 2400})
        self.assertEqual(len(result.alerts), 1)

    def test_notifier_exception_is_handled(self):
        def boom(text):
            raise RuntimeError("smtp down")

        with self.assertLogs("stock_alert.core", level="ERROR"):
            result, _ = self.run_once({"7203.T": 2400}, notifier=boom)
        self.assertFalse(result.notified)

    def test_price_failure_keeps_flag(self):
        self.run_once({"7203.T": 2400})
        self.run_once({"7203.T": None})
        self.assertIn("7203.T:2026-09-23", self.read_state())

    def test_old_keys_pruned(self):
        self.state_path.write_text(json.dumps({
            "7203.T:2026-09-15": True, "7203.T:2026-09-16": True, "AAPL:2026-09-22": True, "broken": True,
        }), encoding="utf-8")
        self.run_once({})
        self.assertEqual(self.read_state(), {"7203.T:2026-09-16": True, "AAPL:2026-09-22": True})


class PartialFailureTest(Base):
    def test_other_stocks_continue(self):
        with self.assertLogs("stock_alert.core", level="WARNING"):
            result, _ = self.run_once({"7203.T": None, "AAPL": RuntimeError("boom"), "MSFT": 300})
        self.assertEqual([a.stock.symbol for a in result.alerts], ["MSFT"])
        self.assertEqual(result.prices, {"7203.T": None, "AAPL": None, "MSFT": 300})

    def test_broken_state_file(self):
        self.state_path.write_text("not json", encoding="utf-8")
        result, _ = self.run_once({"7203.T": 2400})
        self.assertEqual(len(result.alerts), 1)


class SettingsTest(Base):
    def test_missing_file_returns_defaults(self):
        settings = core.load_settings(self.dir / "none.json")
        self.assertEqual(settings, core.DEFAULT_SETTINGS)
        self.assertIsNot(settings["email"], core.DEFAULT_SETTINGS["email"])

    def test_roundtrip(self):
        path = self.dir / "sub" / "settings.json"
        core.save_settings(SETTINGS, path)
        loaded = core.load_settings(path)
        self.assertEqual([s["symbol"] for s in loaded["stocks"]], ["7203.T", "AAPL", "MSFT"])
        self.assertEqual(loaded["stocks"][2]["name"], "MSFT")  # 空の名前はコードで補完
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_invalid_values_are_sanitized(self):
        path = self.dir / "settings.json"
        path.write_text(json.dumps({
            "interval_minutes": 1,
            "email": {"from_addr": "me@example.com"},
            "stocks": [
                {"symbol": "aapl", "threshold": "150"},
                {"symbol": "AAPL", "threshold": 1},        # 重複
                {"symbol": "X", "threshold": "abc"},
                {"symbol": "Y", "threshold": -1},
                {"threshold": 100},
                "garbage",
            ],
        }), encoding="utf-8")
        settings = core.load_settings(path)
        self.assertEqual(settings["interval_minutes"], core.MIN_INTERVAL_MINUTES)
        self.assertEqual(settings["email"]["from_addr"], "me@example.com")
        self.assertEqual(settings["email"]["smtp_host"], "smtp.gmail.com")
        self.assertEqual(settings["stocks"], [{"symbol": "AAPL", "name": "AAPL", "threshold": 150.0}])

    def test_broken_settings_file(self):
        path = self.dir / "settings.json"
        path.write_text("{broken", encoding="utf-8")
        self.assertEqual(core.load_settings(path), core.DEFAULT_SETTINGS)


if __name__ == "__main__":
    unittest.main()
