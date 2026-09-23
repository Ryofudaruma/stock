"""自動起動(レジストリ登録)のテスト。winreg を偽物に差し替えて検証する。"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: F401

from stock_alert import autostart


class FakeKey:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def OpenKey(self, root, path, reserved, access):
        assert (root, path) == ("HKCU", autostart.RUN_KEY)
        return FakeKey(self.values)

    CreateKeyEx = OpenKey

    def QueryValueEx(self, key, name):
        if name not in key.store:
            raise FileNotFoundError(name)
        return key.store[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        key.store[name] = value

    def DeleteValue(self, key, name):
        if name not in key.store:
            raise FileNotFoundError(name)
        del key.store[name]


class AutostartTest(unittest.TestCase):
    def setUp(self):
        self.reg = FakeWinreg()
        patches = [
            mock.patch.object(autostart, "_winreg", return_value=self.reg),
            mock.patch.object(sys, "platform", "win32"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_enable_disable(self):
        self.assertFalse(autostart.is_enabled())
        autostart.enable()
        self.assertTrue(autostart.is_enabled())
        self.assertEqual(autostart.registered_command(), autostart.build_command())
        autostart.disable()
        self.assertFalse(autostart.is_enabled())
        autostart.disable()  # 2回目もエラーにならない

    def test_command_prefers_pythonw(self):
        with tempfile.TemporaryDirectory() as d:
            python = Path(d) / "python.exe"
            python.touch()
            self.assertEqual(autostart.pythonw_path(str(python)), python)
            (Path(d) / "pythonw.exe").touch()
            cmd = autostart.build_command(str(python), Path("C:/app/app.pyw"))
        self.assertTrue(cmd.startswith(f'"{Path(d) / "pythonw.exe"}"'))
        self.assertTrue(cmd.endswith('app.pyw" --minimized'))


class NonWindowsTest(unittest.TestCase):
    def test_not_supported(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertFalse(autostart.is_supported())
            self.assertFalse(autostart.is_enabled())
            self.assertIsNone(autostart.registered_command())


if __name__ == "__main__":
    unittest.main()
