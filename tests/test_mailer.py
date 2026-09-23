"""メール送信とパスワード保存のテスト(実際には送信しない)。"""

import smtplib
import unittest
from unittest import mock

from tests import helpers  # noqa: F401

from stock_alert import mailer

CFG = {"smtp_host": "smtp.gmail.com", "smtp_port": 587, "from_addr": "me@example.com", "to_addr": ""}


class SendEmailTest(unittest.TestCase):
    def test_starttls_and_default_recipient(self):
        with mock.patch.object(mailer.smtplib, "SMTP") as cls:
            self.assertTrue(mailer.send_email("本文", CFG, password="app-pass"))
        cls.assert_called_once_with("smtp.gmail.com", 587, timeout=30)
        smtp = cls.return_value.__enter__.return_value
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("me@example.com", "app-pass")
        from_addr, to_addrs, body = smtp.sendmail.call_args.args
        self.assertEqual(to_addrs, ["me@example.com"])  # 送信先が空なら自分宛て
        self.assertIn("Subject: =?utf-8?", body)

    def test_ssl_port_465(self):
        cfg = dict(CFG, smtp_port=465, to_addr="you@example.com")
        with mock.patch.object(mailer.smtplib, "SMTP_SSL") as cls:
            self.assertTrue(mailer.send_email("本文", cfg, password="pw"))
        smtp = cls.return_value.__enter__.return_value
        smtp.starttls.assert_not_called()
        self.assertEqual(smtp.sendmail.call_args.args[1], ["you@example.com"])

    def test_password_read_from_keyring(self):
        with mock.patch("keyring.get_password", return_value="stored") as get, \
                mock.patch.object(mailer.smtplib, "SMTP") as cls:
            self.assertTrue(mailer.send_email("本文", CFG))
        get.assert_called_once_with(mailer.KEYRING_SERVICE, "me@example.com")
        cls.return_value.__enter__.return_value.login.assert_called_once_with("me@example.com", "stored")

    def test_missing_settings_skip_with_warning(self):
        with mock.patch("keyring.get_password", return_value=None):
            with self.assertLogs("stock_alert.mailer", level="WARNING") as logs:
                self.assertFalse(mailer.send_email("本文", dict(CFG, from_addr="")))
        self.assertIn("送信元メールアドレス", logs.output[0])

    def test_keyring_error_is_handled(self):
        with mock.patch("keyring.get_password", side_effect=RuntimeError("no backend")):
            with self.assertLogs("stock_alert.mailer", level="WARNING"):
                self.assertFalse(mailer.send_email("本文", CFG))

    def test_auth_error(self):
        with mock.patch.object(mailer.smtplib, "SMTP") as cls:
            cls.return_value.__enter__.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad")
            with self.assertLogs("stock_alert.mailer", level="ERROR") as logs:
                self.assertFalse(mailer.send_email("本文", CFG, password="wrong"))
        self.assertIn("アプリパスワード", logs.output[0])

    def test_disconnect_reports_server_and_stage(self):
        with mock.patch.object(mailer.smtplib, "SMTP") as cls:
            cls.return_value.__enter__.return_value.starttls.side_effect = \
                smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
            with self.assertLogs("stock_alert.mailer", level="ERROR") as logs:
                self.assertFalse(mailer.send_email("本文", CFG, password="pw"))
        self.assertIn("smtp.gmail.com:587 への暗号化(STARTTLS)", logs.output[0])
        self.assertIn("接続が途中で切れました", logs.output[0])

    def test_password_whitespace_removed(self):
        self.assertEqual(mailer.normalize_password(" abcd efgh\u00a0ijkl\u3000mnop\n"), "abcdefghijklmnop")
        self.assertEqual(mailer.normalize_password("ＡＢＣＤ"), "ABCD")
        self.assertEqual(mailer.normalize_password(None), "")
        with mock.patch.object(mailer.smtplib, "SMTP") as cls:
            mailer.send_email("本文", CFG, password="abcd efgh ijkl mnop")
        cls.return_value.__enter__.return_value.login.assert_called_once_with("me@example.com", "abcdefghijklmnop")

    def test_network_error(self):
        with mock.patch.object(mailer.smtplib, "SMTP", side_effect=OSError("refused")):
            with self.assertLogs("stock_alert.mailer", level="ERROR") as logs:
                self.assertFalse(mailer.send_email("本文", CFG, password="pw"))
        self.assertIn("smtp.gmail.com:587 への接続", logs.output[0])

    def test_bad_port(self):
        with self.assertLogs("stock_alert.mailer", level="ERROR"):
            self.assertFalse(mailer.send_email("本文", dict(CFG, smtp_port="abc"), password="pw"))


class PasswordStoreTest(unittest.TestCase):
    def test_set_password(self):
        with mock.patch("keyring.set_password") as setter:
            mailer.set_password("me@example.com", "pw")
        setter.assert_called_once_with(mailer.KEYRING_SERVICE, "me@example.com", "pw")

    def test_set_password_error(self):
        with mock.patch("keyring.set_password", side_effect=RuntimeError("locked")):
            with self.assertRaises(mailer.PasswordStoreError):
                mailer.set_password("me@example.com", "pw")

    def test_get_password_empty_user(self):
        self.assertIsNone(mailer.get_password(""))


if __name__ == "__main__":
    unittest.main()
