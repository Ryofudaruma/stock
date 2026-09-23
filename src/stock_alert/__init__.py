"""株価下落通知ツール(Windows 常駐 GUI 版)。"""

from pathlib import Path

APP_NAME = "株価アラート"
APP_ID = "StockAlert"

# リポジトリ(アプリ)のルートフォルダ
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
# 設定・状態・ログを置くフォルダ(Git 管理外)
DATA_DIR = ROOT_DIR / "data"
