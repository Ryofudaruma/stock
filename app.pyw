"""株価アラートの起動用スクリプト(ダブルクリック、または start.bat から起動)。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

try:
    from stock_alert.gui import main
except ImportError as exc:  # 依存ライブラリ未インストールなど
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "株価アラート",
        "起動に必要なライブラリが見つかりません。\n"
        "フォルダ内の setup.bat をダブルクリックしてセットアップしてから、start.bat で起動してください。\n\n"
        f"詳細: {exc}",
    )
    sys.exit(1)

sys.exit(main())
