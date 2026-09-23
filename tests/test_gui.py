"""GUI の動作テスト。画面(ディスプレイ)がない環境では自動的にスキップする。

Linux では xvfb-run を使うと画面なしでも実行できる:
    xvfb-run -a python -m unittest tests.test_gui
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: F401

try:
    import tkinter as tk

    _root = tk.Tk()
    _root.destroy()
    HAS_DISPLAY = True
except Exception:  # noqa: BLE001
    HAS_DISPLAY = False

if HAS_DISPLAY:
    from stock_alert import core, gui
    from stock_alert.search import SearchResult


@unittest.skipUnless(HAS_DISPLAY, "ディスプレイがないため GUI テストをスキップ")
class GuiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.prices = {"7203.T": 2650.5, "AAPL": 195.25}
        self.sent = []
        self.passwords = {}
        self.jpx = [SearchResult("7203.T", "トヨタ自動車", "プライム（内国株式）"),
                    SearchResult("7201.T", "日産自動車", "プライム（内国株式）")]
        self.root = tk.Tk()
        self.app = self.make_app()

    def make_app(self):
        search_path = self.dir / "jpx.csv"
        from stock_alert import search

        search.save_jpx_cache(self.jpx, search_path)
        return gui.StockAlertApp(
            self.root,
            settings_path=self.dir / "settings.json",
            state_path=self.dir / "state.json",
            jpx_path=search_path,
            price_getter=lambda s: self.prices.get(s),
            yahoo_searcher=lambda q: [SearchResult("AAPL", "Apple Inc.", "NASDAQ")] if "apple" in q.lower() else [],
            jpx_downloader=lambda p: self.jpx,
            email_sender=self.fake_send,
            password_getter=lambda u: self.passwords.get(u),
            password_setter=lambda u, p: self.passwords.__setitem__(u, p),
            auto_check=False,
        )

    def fake_send(self, text, cfg, password=None, subject="株価アラート"):
        self.sent.append((subject, text, dict(cfg)))
        return True

    def tearDown(self):
        self.app.shutdown()
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        self.tmp.cleanup()

    def pump(self, condition, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            self.root.update()
            if condition():
                return
            time.sleep(0.02)
        self.fail("タイムアウトしました")

    def saved_settings(self):
        return json.loads((self.dir / "settings.json").read_text(encoding="utf-8"))

    def row(self, symbol):
        return self.app.tree.item(symbol, "values")

    # -------------------------------------------------------------------
    def test_add_edit_delete_stock(self):
        self.assertTrue(self.app.add_stock("7203.T", "トヨタ自動車", 2500))
        self.assertEqual(self.saved_settings()["stocks"],
                         [{"symbol": "7203.T", "name": "トヨタ自動車", "threshold": 2500}])
        self.assertEqual(self.row("7203.T")[:3], ("7203.T", "トヨタ自動車", "2,500 円"))

        with mock.patch.object(gui.messagebox, "showinfo") as info:
            self.assertFalse(self.app.add_stock("7203.T", "トヨタ自動車", 2000))
        info.assert_called_once()

        self.app.tree.selection_set("7203.T")
        with mock.patch.object(gui.simpledialog, "askstring", return_value="２，４００"):
            self.app.edit_threshold()
        self.assertEqual(self.saved_settings()["stocks"][0]["threshold"], 2400)

        with mock.patch.object(gui.messagebox, "askyesno", return_value=True):
            self.app.delete_stock()
        self.assertEqual(self.saved_settings()["stocks"], [])
        self.assertEqual(self.app.tree.get_children(), ())

    def test_check_notifies_and_updates_table(self):
        self.app.add_stock("7203.T", "トヨタ自動車", 2700)
        self.app.add_stock("AAPL", "Apple", 180)
        self.app.add_stock("ZZZZ", "取得失敗する銘柄", 10)
        self.app.start_check(manual=True)
        self.pump(lambda: not self.app._checking and self.app.last_check is not None)

        self.assertEqual(len(self.sent), 1)
        self.assertIn("トヨタ自動車(7203.T)", self.sent[0][1])
        self.assertEqual(self.row("7203.T")[3:], ("2,650.5 円", "閾値以下(通知済み)"))
        self.assertEqual(self.row("AAPL")[3:], ("195.25 ドル", "監視中"))
        self.assertEqual(self.row("ZZZZ")[3:], ("-", "取得失敗"))
        self.assertIsNotNone(self.app.next_check)

        # 2回目は通知しない
        self.app.start_check(manual=True)
        self.pump(lambda: not self.app._checking)
        self.pump(lambda: self.app.next_check is not None)
        self.assertEqual(len(self.sent), 1)

    def test_search_dialog_add_flow(self):
        dialog = gui.SearchDialog(self.app)
        dialog.query_var.set("とよた")
        dialog.do_search()
        self.pump(lambda: len(dialog.results.get_children()) > 0)
        first = dialog.results.get_children()[0]
        self.assertEqual(dialog.results.item(first, "values")[0], "7203.T")

        dialog.results.selection_set(first)
        self.root.update()
        self.pump(lambda: dialog.selected_price is not None)
        self.assertIn("2,650.5 円", dialog.price_var.get())
        dialog.fill_percent(10)
        self.assertEqual(dialog.threshold_var.get(), "2385")

        dialog.do_add()
        self.assertEqual(self.saved_settings()["stocks"][0]["symbol"], "7203.T")
        self.assertEqual(self.saved_settings()["stocks"][0]["threshold"], 2385)

    def test_search_dialog_us_stock_and_invalid_threshold(self):
        dialog = gui.SearchDialog(self.app)
        dialog.query_var.set("apple")
        dialog.do_search()
        self.pump(lambda: len(dialog.results.get_children()) > 0)
        item = dialog.results.get_children()[0]
        self.assertEqual(dialog.results.item(item, "values")[0], "AAPL")
        dialog.results.selection_set(item)
        self.pump(lambda: dialog.selected_price is not None)
        dialog.fill_percent(5)
        self.assertEqual(dialog.threshold_var.get(), "185.49")

        dialog.threshold_var.set("abc")
        with mock.patch.object(gui.messagebox, "showwarning") as warn:
            dialog.do_add()
        warn.assert_called_once()
        self.assertEqual(self.app.settings["stocks"], [])

    def test_email_settings_and_test_mail(self):
        self.app.email_vars["from_addr"].set("me@example.com")
        self.app.password_var.set("abcd efgh ijkl mnop")
        with mock.patch.object(gui.messagebox, "showinfo"):
            self.app.save_email()
        self.assertEqual(self.passwords, {"me@example.com": "abcdefghijklmnop"})
        self.assertEqual(self.app.password_var.get(), "")
        self.assertEqual(self.app.password_status_var.get(), "保存済み")
        saved = self.saved_settings()["email"]
        self.assertEqual(saved["from_addr"], "me@example.com")
        self.assertNotIn("abcdefghijklmnop", (self.dir / "settings.json").read_text(encoding="utf-8"))

        with mock.patch.object(gui.messagebox, "showinfo") as info:
            self.app.send_test_mail()
            self.pump(lambda: info.called)
        self.assertEqual(self.sent[0][0], "株価アラート(テスト)")

    def test_test_mail_requires_settings(self):
        with mock.patch.object(gui.messagebox, "showwarning") as warn:
            self.app.send_test_mail()
        warn.assert_called_once()
        self.assertEqual(self.sent, [])

    def test_interval(self):
        self.app.interval_var.set("15")
        self.app.save_interval()
        self.assertEqual(self.saved_settings()["interval_minutes"], 15)
        with mock.patch.object(gui.messagebox, "showwarning") as warn:
            self.app.interval_var.set("1")
            self.app.save_interval()
        warn.assert_called_once()

    def test_background_failure_is_reported(self):
        def broken(path):
            raise OSError("network down")

        self.app.jpx_downloader = broken
        with mock.patch.object(gui.messagebox, "showerror") as err:
            with self.assertLogs("stock_alert.gui", level="WARNING"):
                self.app.update_jpx(manual=True)
                self.pump(lambda: err.called)
        self.assertFalse(self.app._jpx_updating)
        self.assertEqual(str(self.app.jpx_button.cget("state")), "normal")

    def test_check_failure_reschedules(self):
        self.app.add_stock("7203.T", "トヨタ自動車", 2500)
        with mock.patch.object(gui.core, "run_check", side_effect=RuntimeError("boom")):
            with self.assertLogs("stock_alert.gui", level="ERROR"):
                self.app.start_check(manual=True)
                self.pump(lambda: not self.app._checking)
        self.assertIsNotNone(self.app.next_check)

    def test_parse_threshold(self):
        self.assertEqual(gui.parse_threshold("1,234.5"), 1234.5)
        self.assertEqual(gui.parse_threshold("１２３"), 123)
        for bad in ("", "0", "-5", "abc", "nan", "inf"):
            self.assertIsNone(gui.parse_threshold(bad), bad)


@unittest.skipUnless(HAS_DISPLAY, "ディスプレイがないため GUI テストをスキップ")
class SingleInstanceTest(unittest.TestCase):
    def test_second_lock_fails(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "app.lock"
            first, second = gui.SingleInstance(path), gui.SingleInstance(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()


if __name__ == "__main__":
    unittest.main()
