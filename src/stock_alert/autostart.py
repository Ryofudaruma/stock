"""Windows のログイン時に自動起動させる設定。

HKEY_CURRENT_USER の Run キーに登録する(管理者権限は不要)。
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import APP_ID, ROOT_DIR

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = APP_ID
APP_SCRIPT = ROOT_DIR / "app.pyw"


def is_supported() -> bool:
    return sys.platform == "win32"


def pythonw_path(python_exe: str | None = None) -> Path:
    """コンソール画面を出さずに起動できる pythonw.exe のパスを返す。"""
    exe = Path(python_exe or sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def build_command(python_exe: str | None = None, script: Path = APP_SCRIPT) -> str:
    return f'"{pythonw_path(python_exe)}" "{script}" --minimized'


def _winreg():
    import winreg

    return winreg


def is_enabled() -> bool:
    if not is_supported():
        return False
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
            return True
    except FileNotFoundError:
        return False


def registered_command() -> str | None:
    if not is_supported():
        return None
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return value
    except FileNotFoundError:
        return None


def enable() -> None:
    winreg = _winreg()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, build_command())


def disable() -> None:
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass
