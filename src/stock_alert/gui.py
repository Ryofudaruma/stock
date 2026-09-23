"""常駐 GUI(tkinter)。

ウィンドウを開いている(最小化を含む)間、一定間隔で株価をチェックし、
閾値以下になった銘柄をメールで通知する。
"""

from __future__ import annotations

import argparse
import copy
import logging
import logging.handlers
import os
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, scrolledtext, simpledialog, ttk

from . import APP_NAME, DATA_DIR, autostart, core, mailer, search
from .prices import get_current_price

logger = logging.getLogger(__name__)

LOCK_PATH = DATA_DIR / "app.lock"
LOG_PATH = DATA_DIR / "app.log"
MAX_LOG_LINES = 500
FIRST_CHECK_DELAY_MS = 3_000
FIRST_CHECK_DELAY_MINIMIZED_MS = 60_000  # ログイン直後はネットワークの準備を待つ

TEST_MAIL_TEXT = "これは株価アラートのテストメールです。\nこのメールが届いていれば、メール設定は正しく完了しています。"


def currency_of(symbol: str) -> str:
    return "円" if symbol.upper().endswith(".T") else "ドル"


# ---------------------------------------------------------------------------
# 二重起動の防止
# ---------------------------------------------------------------------------
class SingleInstance:
    def __init__(self, path: Path = LOCK_PATH):
        self.path = path
        self._file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+")
        f.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._file = f
        return True

    def release(self) -> None:
        if self._file:
            self._file.close()
            self._file = None


# ---------------------------------------------------------------------------
# ログを画面に流すためのハンドラ
# ---------------------------------------------------------------------------
class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__(level=logging.INFO)
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%m/%d %H:%M:%S"))

    def emit(self, record):
        try:
            self.q.put_nowait(self.format(record))
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 銘柄検索ダイアログ
# ---------------------------------------------------------------------------
class SearchDialog(tk.Toplevel):
    def __init__(self, app: "StockAlertApp"):
        super().__init__(app.root)
        self.app = app
        self.title("銘柄を追加")
        self.geometry("700x620")
        self.minsize(600, 520)
        self.transient(app.root)
        self._search_seq = 0
        self._price_seq = 0
        self.selected: search.SearchResult | None = None
        self.selected_price: float | None = None

        pad = {"padx": 10, "pady": 4}

        ttk.Label(self, text="会社名・証券コード・ティッカーで検索してください(例: トヨタ / 7203 / Apple / AAPL)").pack(
            anchor="w", **pad)
        row = ttk.Frame(self)
        row.pack(fill="x", **pad)
        self.query_var = tk.StringVar()
        self.query_entry = ttk.Entry(row, textvariable=self.query_var)
        self.query_entry.pack(side="left", fill="x", expand=True)
        self.query_entry.bind("<Return>", lambda e: self.do_search())
        self.search_button = ttk.Button(row, text="検索", command=self.do_search)
        self.search_button.pack(side="left", padx=(6, 0))

        self.message_var = tk.StringVar()
        ttk.Label(self, textvariable=self.message_var, foreground="#b35900", wraplength=640).pack(anchor="w", **pad)

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, **pad)
        self.results = ttk.Treeview(frame, columns=("symbol", "name", "market"), show="headings", height=6,
                                    selectmode="browse")
        for col, text, width in (("symbol", "コード", 90), ("name", "銘柄名", 330), ("market", "市場", 170)):
            self.results.heading(col, text=text)
            self.results.column(col, width=width, anchor="w", stretch=(col == "name"))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.results.yview)
        self.results.configure(yscrollcommand=scroll.set)
        self.results.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.results.bind("<<TreeviewSelect>>", lambda e: self.on_select())
        self._result_items: dict[str, search.SearchResult] = {}

        buttons = ttk.Frame(self)
        buttons.pack(side="bottom", fill="x", padx=10, pady=(0, 10), before=frame)
        box = ttk.LabelFrame(self, text="選択した銘柄")
        box.pack(side="bottom", fill="x", padx=10, pady=8, before=frame)
        self.selected_var = tk.StringVar(value="(一覧から銘柄を選んでください)")
        self.price_var = tk.StringVar(value="現在値: -")
        ttk.Label(box, textvariable=self.selected_var, font=("", 11, "bold")).grid(
            row=0, column=0, columnspan=6, sticky="w", padx=8, pady=(6, 2))
        ttk.Label(box, textvariable=self.price_var).grid(row=1, column=0, columnspan=6, sticky="w", padx=8)

        ttk.Label(box, text="通知する価格(閾値):").grid(row=2, column=0, sticky="w", padx=8, pady=8)
        self.threshold_var = tk.StringVar()
        self.threshold_entry = ttk.Entry(box, textvariable=self.threshold_var, width=14)
        self.threshold_entry.grid(row=2, column=1, sticky="w")
        self.unit_var = tk.StringVar()
        ttk.Label(box, textvariable=self.unit_var).grid(row=2, column=2, sticky="w", padx=(4, 12))
        self.pct_buttons = []
        for i, pct in enumerate((5, 10, 20)):
            b = ttk.Button(box, text=f"現在値の -{pct}%", command=lambda p=pct: self.fill_percent(p), state="disabled")
            b.grid(row=2, column=3 + i, padx=2)
            self.pct_buttons.append(b)
        ttk.Label(box, text="※ 現在値がこの価格「以下」になったらメールで通知します。",
                  foreground="#555").grid(row=3, column=0, columnspan=6, sticky="w", padx=8, pady=(0, 6))

        ttk.Button(buttons, text="閉じる", command=self.destroy).pack(side="right")
        self.add_button = ttk.Button(buttons, text="この銘柄を追加", command=self.do_add, state="disabled")
        self.add_button.pack(side="right", padx=6)

        if not app.jpx_listing:
            self.message_var.set("東証の銘柄一覧を取得中、または未取得です。日本株は証券コード(例: 7203)か英語名で検索できます。")
        self.query_entry.focus_set()

    # 検索 ------------------------------------------------------------------
    def do_search(self):
        query = self.query_var.get().strip()
        if not query:
            return
        self._search_seq += 1
        seq = self._search_seq
        self.search_button.configure(state="disabled")
        self.message_var.set("検索中…")
        listing = self.app.jpx_listing

        def done(result):
            if seq != self._search_seq or not self.winfo_exists():
                return
            self.search_button.configure(state="normal")
            items, warnings = result
            self.show_results(items)
            msgs = list(warnings)
            if not items:
                msgs.insert(0, "見つかりませんでした。別の書き方(例: 会社名の一部、証券コード)で試してください。")
            self.message_var.set(" ".join(msgs))

        def failed(exc):
            if seq == self._search_seq and self.winfo_exists():
                self.search_button.configure(state="normal")
                self.message_var.set(f"検索に失敗しました: {exc}")

        self.app.run_bg(lambda: search.search(query, listing, self.app.yahoo_searcher), done, failed)

    def show_results(self, items):
        self.results.delete(*self.results.get_children())
        self._result_items = {}
        for item in items:
            iid = self.results.insert("", "end", values=(item.symbol, item.name, item.market))
            self._result_items[iid] = item

    # 選択 ------------------------------------------------------------------
    def on_select(self):
        sel = self.results.selection()
        if not sel:
            return
        item = self._result_items.get(sel[0])
        if item is None:
            return
        self.selected = item
        self.selected_price = None
        self.selected_var.set(f"{item.name}({item.symbol})")
        self.unit_var.set(currency_of(item.symbol))
        self.price_var.set("現在値: 取得中…")
        for b in self.pct_buttons:
            b.configure(state="disabled")
        self.add_button.configure(state="normal")

        self._price_seq += 1
        seq = self._price_seq

        def done(price):
            if seq != self._price_seq or not self.winfo_exists():
                return
            if price is None:
                self.price_var.set("現在値: 取得できませんでした(閾値は手入力できます)")
                return
            self.selected_price = price
            self.price_var.set(f"現在値: {core.format_number(price)} {currency_of(item.symbol)}")
            for b in self.pct_buttons:
                b.configure(state="normal")

        self.app.run_bg(lambda: self.app.price_getter(item.symbol), done)

    def fill_percent(self, pct: int):
        if self.selected_price:
            value = self.selected_price * (100 - pct) / 100
            digits = 0 if self.selected and self.selected.symbol.endswith(".T") else 2
            self.threshold_var.set(f"{round(value, digits):.{digits}f}")

    def do_add(self):
        if not self.selected:
            return
        threshold = parse_threshold(self.threshold_var.get())
        if threshold is None:
            messagebox.showwarning("入力エラー", "通知する価格(閾値)に 0 より大きい数値を入力してください。", parent=self)
            self.threshold_entry.focus_set()
            return
        if self.app.add_stock(self.selected.symbol, self.selected.name, threshold, parent=self):
            self.destroy()


def parse_threshold(text: str) -> float | None:
    import unicodedata

    text = unicodedata.normalize("NFKC", str(text)).replace(",", "").strip()
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 and value == value and value != float("inf") else None


# ---------------------------------------------------------------------------
# メインウィンドウ
# ---------------------------------------------------------------------------
class StockAlertApp:
    def __init__(
        self,
        root: tk.Tk,
        *,
        settings_path: Path = core.SETTINGS_PATH,
        state_path: Path = core.STATE_PATH,
        jpx_path: Path = search.JPX_CACHE_PATH,
        price_getter=get_current_price,
        yahoo_searcher=search.search_yahoo,
        jpx_downloader=search.download_jpx_list,
        email_sender=mailer.send_email,
        password_getter=mailer.get_password,
        password_setter=mailer.set_password,
        start_minimized: bool = False,
        auto_check: bool = True,
    ):
        self.root = root
        self.settings_path = settings_path
        self.state_path = state_path
        self.jpx_path = jpx_path
        self.price_getter = price_getter
        self.yahoo_searcher = yahoo_searcher
        self.jpx_downloader = jpx_downloader
        self.email_sender = email_sender
        self.password_getter = password_getter
        self.password_setter = password_setter

        self.settings = core.load_settings(settings_path)
        self.jpx_listing = search.load_jpx_cache(jpx_path)
        self.last_prices: dict[str, float | None] = {}
        self.last_check: datetime | None = None
        self.next_check: datetime | None = None
        self._checking = False
        self._timer_id = None
        self._poll_id = None
        self._jpx_updating = False

        self._callbacks: queue.Queue = queue.Queue()
        self._log_queue: queue.Queue = queue.Queue()
        self._log_handler = QueueLogHandler(self._log_queue)
        logging.getLogger().addHandler(self._log_handler)

        root.title(APP_NAME)
        root.geometry("860x640")
        root.minsize(720, 520)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = self._report_callback_exception
        self._setup_style()
        self._build()
        self.refresh_table()
        self._poll_id = self.root.after(100, self._poll)

        if start_minimized:
            root.iconify()
        self._maybe_update_jpx()
        self._sync_autostart_path()
        if auto_check:
            delay = FIRST_CHECK_DELAY_MINIMIZED_MS if start_minimized else FIRST_CHECK_DELAY_MS
            self._schedule(delay)
        logger.info("%s を起動しました(チェック間隔: %d 分)", APP_NAME, self.settings["interval_minutes"])

    # 基本 ------------------------------------------------------------------
    def _setup_style(self):
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=26)
        style.configure("Big.TButton", padding=(10, 4))

    def _report_callback_exception(self, exc_type, exc, tb):
        logger.error("画面の処理中にエラーが発生しました", exc_info=(exc_type, exc, tb))

    def run_bg(self, func, on_success=None, on_error=None):
        """func を別スレッドで実行し、結果のコールバックは画面のスレッドで呼ぶ。"""

        def worker():
            try:
                result = func()
            except Exception as exc:  # noqa: BLE001
                if on_error:
                    # 想定内の失敗(通信エラーなど)は on_error 側で分かりやすく伝える
                    logger.debug("バックグラウンド処理でエラーが発生しました", exc_info=True)
                    self._callbacks.put(lambda e=exc: on_error(e))
                else:
                    logger.exception("バックグラウンド処理でエラーが発生しました")
                return
            if on_success:
                self._callbacks.put(lambda: on_success(result))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        while True:
            try:
                callback = self._callbacks.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception:  # noqa: BLE001
                logger.exception("画面の更新中にエラーが発生しました")
        lines = []
        while True:
            try:
                lines.append(self._log_queue.get_nowait())
            except queue.Empty:
                break
        if lines:
            self._append_log(lines)
        self._update_status()
        self._poll_id = self.root.after(100, self._poll)

    def _append_log(self, lines):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", "\n".join(lines) + "\n")
        excess = int(self.log_text.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if excess > 0:
            self.log_text.delete("1.0", f"{excess + 1}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # 画面の組み立て ----------------------------------------------------------
    def _build(self):
        paned = ttk.PanedWindow(self.root, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8, pady=8)

        notebook = ttk.Notebook(paned)
        self.notebook = notebook
        paned.add(notebook, weight=4)
        notebook.add(self._build_stocks_tab(notebook), text=" 監視銘柄 ")
        notebook.add(self._build_email_tab(notebook), text=" メール設定 ")
        notebook.add(self._build_options_tab(notebook), text=" その他の設定 ")

        log_frame = ttk.LabelFrame(paned, text="ログ")
        paned.add(log_frame, weight=1)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=7, state="disabled", wrap="none",
                                                  font=("", 9))
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

        self.status_var = tk.StringVar()
        ttk.Label(self.root, textvariable=self.status_var, anchor="w", relief="sunken", padding=(8, 2)).pack(
            fill="x", side="bottom")

    def _build_stocks_tab(self, parent):
        tab = ttk.Frame(parent, padding=8)
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="+ 銘柄を追加…", style="Big.TButton", command=self.open_search).pack(side="left")
        self.edit_button = ttk.Button(bar, text="閾値を変更…", command=self.edit_threshold)
        self.edit_button.pack(side="left", padx=4)
        self.delete_button = ttk.Button(bar, text="削除", command=self.delete_stock)
        self.delete_button.pack(side="left")
        self.check_button = ttk.Button(bar, text="今すぐチェック", style="Big.TButton",
                                       command=lambda: self.start_check(manual=True))
        self.check_button.pack(side="right")

        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True)
        columns = ("symbol", "name", "threshold", "price", "status")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for col, text, width, anchor in (
            ("symbol", "コード", 90, "w"),
            ("name", "銘柄名", 260, "w"),
            ("threshold", "閾値", 110, "e"),
            ("price", "現在値", 110, "e"),
            ("status", "状態", 150, "w"),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "name"))
        self.tree.tag_configure("alert", background="#ffe3e3")
        self.tree.tag_configure("error", foreground="#999999")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.edit_threshold())
        self.tree.bind("<Delete>", lambda e: self.delete_stock())

        self.empty_label = ttk.Label(tab, foreground="#555",
                                     text="まだ銘柄が登録されていません。「+ 銘柄を追加…」から追加してください。")
        return tab

    def _build_email_tab(self, parent):
        tab = ttk.Frame(parent, padding=16)
        email = self.settings["email"]
        self.email_vars = {
            "from_addr": tk.StringVar(value=email.get("from_addr", "")),
            "to_addr": tk.StringVar(value=email.get("to_addr", "")),
            "smtp_host": tk.StringVar(value=email.get("smtp_host", "")),
            "smtp_port": tk.StringVar(value=str(email.get("smtp_port", ""))),
        }
        self.password_var = tk.StringVar()

        rows = (
            ("送信元メールアドレス", "from_addr", "通知に使う Gmail アドレス(例: yourname@gmail.com)"),
            ("アプリパスワード", None, "Google で発行した16文字のアプリパスワード(普段のパスワードではありません)"),
            ("送信先メールアドレス", "to_addr", "空欄なら送信元アドレス(自分自身)に送ります"),
            ("SMTPサーバー", "smtp_host", "Gmail の場合は smtp.gmail.com のままで OK"),
            ("SMTPポート", "smtp_port", "Gmail の場合は 587 のままで OK"),
        )
        self.password_status_var = tk.StringVar()
        for i, (label, key, hint) in enumerate(rows):
            ttk.Label(tab, text=label).grid(row=i * 2, column=0, sticky="w", pady=(8, 0))
            cell = ttk.Frame(tab)
            cell.grid(row=i * 2, column=1, sticky="w", padx=8, pady=(8, 0))
            if key is None:
                ttk.Entry(cell, textvariable=self.password_var, show="●", width=40).pack(side="left")
                ttk.Label(cell, textvariable=self.password_status_var, foreground="#1a7f37").pack(
                    side="left", padx=8)
            else:
                ttk.Entry(cell, textvariable=self.email_vars[key], width=40).pack(side="left")
            ttk.Label(tab, text=hint, foreground="#555").grid(row=i * 2 + 1, column=1, sticky="w", padx=8)
        self._update_password_status()

        buttons = ttk.Frame(tab)
        buttons.grid(row=len(rows) * 2, column=0, columnspan=2, sticky="w", pady=16)
        ttk.Button(buttons, text="保存", style="Big.TButton", command=self.save_email).pack(side="left")
        self.test_mail_button = ttk.Button(buttons, text="テストメールを送信", command=self.send_test_mail)
        self.test_mail_button.pack(side="left", padx=8)
        ttk.Label(tab, foreground="#555", justify="left",
                  text="※ アプリパスワードは Windows の「資格情報マネージャー」に安全に保存され、設定ファイルには書き込まれません。\n"
                       "    保存済みの場合、欄は空のままで大丈夫です(変更するときだけ入力してください)。").grid(
            row=len(rows) * 2 + 1, column=0, columnspan=2, sticky="w")
        return tab

    def _build_options_tab(self, parent):
        tab = ttk.Frame(parent, padding=16)

        box = ttk.LabelFrame(tab, text="チェック間隔", padding=10)
        box.pack(fill="x", pady=(0, 10))
        self.interval_var = tk.StringVar(value=str(self.settings["interval_minutes"]))
        ttk.Spinbox(box, from_=core.MIN_INTERVAL_MINUTES, to=core.MAX_INTERVAL_MINUTES, increment=5,
                    textvariable=self.interval_var, width=6).pack(side="left")
        ttk.Label(box, text="分ごと").pack(side="left", padx=(4, 12))
        ttk.Button(box, text="保存", command=self.save_interval).pack(side="left")
        ttk.Label(box, text=f"({core.MIN_INTERVAL_MINUTES}〜{core.MAX_INTERVAL_MINUTES} 分)",
                  foreground="#555").pack(side="left", padx=8)

        box = ttk.LabelFrame(tab, text="自動起動", padding=10)
        box.pack(fill="x", pady=(0, 10))
        self.autostart_var = tk.BooleanVar(value=autostart.is_enabled())
        cb = ttk.Checkbutton(box, text="Windows にサインインしたときに自動で起動する(最小化した状態で起動します)",
                             variable=self.autostart_var, command=self.toggle_autostart)
        cb.pack(anchor="w")
        if not autostart.is_supported():
            cb.configure(state="disabled")
            ttk.Label(box, text="※ この設定は Windows でのみ使えます。", foreground="#555").pack(anchor="w")

        box = ttk.LabelFrame(tab, text="東証の銘柄一覧(日本株の会社名検索に使用)", padding=10)
        box.pack(fill="x", pady=(0, 10))
        self.jpx_status_var = tk.StringVar()
        ttk.Label(box, textvariable=self.jpx_status_var).pack(side="left")
        self.jpx_button = ttk.Button(box, text="今すぐ更新", command=lambda: self.update_jpx(manual=True))
        self.jpx_button.pack(side="right")
        self._update_jpx_status()

        box = ttk.LabelFrame(tab, text="データの保存場所", padding=10)
        box.pack(fill="x")
        ttk.Label(box, text=str(DATA_DIR)).pack(side="left")
        if sys.platform == "win32":
            ttk.Button(box, text="フォルダを開く", command=self.open_data_dir).pack(side="right")
        return tab

    # 監視銘柄 --------------------------------------------------------------
    def stocks(self) -> list[core.Stock]:
        return core.parse_stocks(self.settings["stocks"])

    def _status_of(self, stock: core.Stock, state: dict, today) -> tuple[str, str]:
        if stock.symbol not in self.last_prices:
            return "未チェック", ""
        price = self.last_prices[stock.symbol]
        if price is None:
            return "取得失敗", "error"
        if price <= stock.threshold:
            if core.is_notified(state, stock.symbol, today):
                return "閾値以下(通知済み)", "alert"
            return "閾値以下(未通知)", "alert"
        return "監視中", ""

    def refresh_table(self):
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        state = core.load_state(self.state_path)
        today = core.today_jst()
        stocks = self.stocks()
        for stock in stocks:
            price = self.last_prices.get(stock.symbol)
            unit = currency_of(stock.symbol)
            status, tag = self._status_of(stock, state, today)
            self.tree.insert("", "end", iid=stock.symbol, tags=(tag,) if tag else (), values=(
                stock.symbol,
                stock.name,
                f"{core.format_number(stock.threshold)} {unit}",
                f"{core.format_number(price)} {unit}" if price is not None else "-",
                status,
            ))
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
        if stocks:
            self.empty_label.pack_forget()
        else:
            self.empty_label.pack(pady=8)

    def _save_settings(self, parent=None) -> bool:
        try:
            core.save_settings(self.settings, self.settings_path)
            return True
        except OSError as exc:
            logger.error("設定を保存できませんでした: %s", exc)
            messagebox.showerror("保存エラー", f"設定を保存できませんでした。\n{exc}", parent=parent or self.root)
            return False

    def open_search(self):
        SearchDialog(self)

    def add_stock(self, symbol: str, name: str, threshold: float, parent=None) -> bool:
        symbol = symbol.strip().upper()
        if any(s["symbol"] == symbol for s in self.settings["stocks"]):
            messagebox.showinfo("登録済み", f"{name}({symbol})はすでに登録されています。\n"
                                            "閾値を変えるときは一覧で「閾値を変更…」を使ってください。",
                                parent=parent or self.root)
            return False
        self.settings["stocks"].append({"symbol": symbol, "name": name, "threshold": threshold})
        if not self._save_settings(parent):
            self.settings["stocks"].pop()
            return False
        logger.info("銘柄を追加しました: %s(%s)閾値 %s", name, symbol, core.format_number(threshold))
        self.refresh_table()
        self.tree.selection_set(symbol)
        return True

    def _selected_entry(self) -> dict | None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("銘柄を選択", "一覧から銘柄を選んでください。", parent=self.root)
            return None
        return next((s for s in self.settings["stocks"] if s["symbol"] == sel[0]), None)

    def edit_threshold(self):
        entry = self._selected_entry()
        if entry is None:
            return
        value = simpledialog.askstring(
            "閾値を変更",
            f"{entry['name']}({entry['symbol']})\n通知する価格(閾値)を入力してください"
            f"(単位: {currency_of(entry['symbol'])})",
            initialvalue=core.format_number(entry["threshold"]).replace(",", ""),
            parent=self.root,
        )
        if value is None:
            return
        threshold = parse_threshold(value)
        if threshold is None:
            messagebox.showwarning("入力エラー", "0 より大きい数値を入力してください。", parent=self.root)
            return
        old = entry["threshold"]
        entry["threshold"] = threshold
        if not self._save_settings():
            entry["threshold"] = old
            return
        logger.info("閾値を変更しました: %s(%s)%s → %s", entry["name"], entry["symbol"],
                    core.format_number(old), core.format_number(threshold))
        self.refresh_table()

    def delete_stock(self):
        entry = self._selected_entry()
        if entry is None:
            return
        if not messagebox.askyesno("削除の確認", f"{entry['name']}({entry['symbol']})を監視対象から削除しますか?",
                                   parent=self.root):
            return
        index = self.settings["stocks"].index(entry)
        self.settings["stocks"].pop(index)
        if not self._save_settings():
            self.settings["stocks"].insert(index, entry)
            return
        self.last_prices.pop(entry["symbol"], None)
        logger.info("銘柄を削除しました: %s(%s)", entry["name"], entry["symbol"])
        self.refresh_table()

    # 定期チェック ------------------------------------------------------------
    def _schedule(self, delay_ms: int):
        if self._timer_id is not None:
            self.root.after_cancel(self._timer_id)
        self.next_check = datetime.now() + timedelta(milliseconds=delay_ms)
        self._timer_id = self.root.after(delay_ms, self.start_check)

    def _schedule_next(self):
        self._schedule(self.settings["interval_minutes"] * 60 * 1000)

    def start_check(self, manual: bool = False):
        if self._checking:
            return
        if self._timer_id is not None:
            self.root.after_cancel(self._timer_id)
            self._timer_id = None
        if not self.settings["stocks"]:
            if manual:
                messagebox.showinfo("銘柄がありません", "先に「+ 銘柄を追加…」から銘柄を登録してください。",
                                    parent=self.root)
            self._schedule_next()
            return

        self._checking = True
        self.next_check = None
        self.check_button.configure(state="disabled")
        snapshot = copy.deepcopy(self.settings)

        def notifier(text):
            return self.email_sender(text, snapshot["email"])

        def job():
            return core.run_check(snapshot, self.price_getter, notifier, self.state_path)

        def done(result: core.CheckResult):
            self.last_prices.update(result.prices)
            if result.alerts and not result.notified:
                self.notebook.select(0)
            self._finish_check()

        def failed(exc):
            logger.error("チェック中にエラーが発生しました: %s", exc)
            self._finish_check()

        self.run_bg(job, done, failed)

    def _finish_check(self):
        self._checking = False
        self.last_check = datetime.now()
        self.check_button.configure(state="normal")
        self.refresh_table()
        self._schedule_next()

    def _update_status(self):
        parts = []
        if self._checking:
            parts.append("チェック中…")
        if self.last_check:
            parts.append(f"最終チェック: {self.last_check:%m/%d %H:%M}")
        if self.next_check and not self._checking:
            parts.append(f"次回チェック: {self.next_check:%H:%M} ごろ")
        parts.append(f"間隔: {self.settings['interval_minutes']} 分")
        text = "   |   ".join(parts)
        if self.status_var.get() != text:
            self.status_var.set(text)

    # メール設定 ------------------------------------------------------------
    def _email_form(self) -> dict | None:
        cfg = {key: var.get().strip() for key, var in self.email_vars.items()}
        try:
            port = int(cfg["smtp_port"] or 587)
            if not 0 < port < 65536:
                raise ValueError
        except ValueError:
            messagebox.showwarning("入力エラー", "SMTPポートには数値(Gmail なら 587)を入力してください。", parent=self.root)
            return None
        cfg["smtp_port"] = port
        return cfg

    def _update_password_status(self):
        user = self.email_vars["from_addr"].get().strip()
        saved = bool(user) and bool(self.password_getter(user))
        self.password_status_var.set("保存済み" if saved else "未保存")

    def save_email(self):
        cfg = self._email_form()
        if cfg is None:
            return
        password = self.password_var.get().replace(" ", "").strip()
        if password:
            if not cfg["from_addr"]:
                messagebox.showwarning("入力エラー", "送信元メールアドレスを入力してください。", parent=self.root)
                return
            try:
                self.password_setter(cfg["from_addr"], password)
            except mailer.PasswordStoreError as exc:
                messagebox.showerror("保存エラー", f"アプリパスワードを保存できませんでした。\n{exc}", parent=self.root)
                return
            self.password_var.set("")
        self.settings["email"] = cfg
        if self._save_settings():
            logger.info("メール設定を保存しました")
            self._update_password_status()
            messagebox.showinfo("保存しました", "メール設定を保存しました。\n「テストメールを送信」で届くか確認できます。",
                                parent=self.root)

    def send_test_mail(self):
        cfg = self._email_form()
        if cfg is None:
            return
        password = self.password_var.get().replace(" ", "").strip() or self.password_getter(cfg["from_addr"])
        missing = mailer.missing_fields(cfg, password)
        if missing:
            messagebox.showwarning("設定が足りません", "次の項目を入力してください:\n・" + "\n・".join(missing),
                                   parent=self.root)
            return
        self.test_mail_button.configure(state="disabled")

        def done(ok):
            self.test_mail_button.configure(state="normal")
            if ok:
                to = cfg["to_addr"] or cfg["from_addr"]
                messagebox.showinfo("送信しました", f"{to} にテストメールを送信しました。\n届いているか確認してください。",
                                    parent=self.root)
            else:
                messagebox.showerror("送信できませんでした",
                                     "テストメールを送信できませんでした。\n画面下のログで原因を確認してください。",
                                     parent=self.root)

        self.run_bg(
            lambda: self.email_sender(TEST_MAIL_TEXT, cfg, password=password, subject="株価アラート(テスト)"),
            done,
            lambda exc: done(False),
        )

    # その他の設定 ------------------------------------------------------------
    def save_interval(self):
        try:
            minutes = int(self.interval_var.get())
        except ValueError:
            minutes = -1
        if not core.MIN_INTERVAL_MINUTES <= minutes <= core.MAX_INTERVAL_MINUTES:
            messagebox.showwarning(
                "入力エラー",
                f"{core.MIN_INTERVAL_MINUTES}〜{core.MAX_INTERVAL_MINUTES} の整数を入力してください。",
                parent=self.root)
            return
        self.settings["interval_minutes"] = minutes
        if self._save_settings():
            logger.info("チェック間隔を %d 分に変更しました", minutes)
            if not self._checking:
                self._schedule_next()

    def toggle_autostart(self):
        try:
            if self.autostart_var.get():
                autostart.enable()
                logger.info("Windows サインイン時の自動起動を有効にしました")
            else:
                autostart.disable()
                logger.info("Windows サインイン時の自動起動を無効にしました")
        except OSError as exc:
            self.autostart_var.set(autostart.is_enabled())
            messagebox.showerror("エラー", f"自動起動の設定を変更できませんでした。\n{exc}", parent=self.root)

    def _sync_autostart_path(self):
        """アプリのフォルダを移動した場合に、自動起動の登録内容を新しい場所に合わせる。"""
        try:
            registered = autostart.registered_command()
            if registered and registered != autostart.build_command():
                autostart.enable()
                logger.info("自動起動の登録を現在のフォルダに合わせて更新しました")
        except OSError as exc:
            logger.warning("自動起動の登録を確認できませんでした: %s", exc)

    def _update_jpx_status(self):
        age = search.jpx_cache_age_days(self.jpx_path)
        if self._jpx_updating:
            text = "ダウンロード中…"
        elif not self.jpx_listing or age is None:
            text = "未取得です。「今すぐ更新」を押してください。"
        else:
            updated = datetime.fromtimestamp(self.jpx_path.stat().st_mtime)
            text = f"取得日: {updated:%Y/%m/%d}({len(self.jpx_listing):,} 銘柄)"
        self.jpx_status_var.set(text)

    def _maybe_update_jpx(self):
        age = search.jpx_cache_age_days(self.jpx_path)
        if not self.jpx_listing or age is None or age > search.JPX_REFRESH_DAYS:
            self.update_jpx(manual=False)

    def update_jpx(self, manual: bool):
        if self._jpx_updating:
            return
        self._jpx_updating = True
        self.jpx_button.configure(state="disabled")
        self._update_jpx_status()

        def done(listing):
            self.jpx_listing = listing
            self._jpx_updating = False
            self.jpx_button.configure(state="normal")
            self._update_jpx_status()
            if manual:
                messagebox.showinfo("更新しました", f"東証の銘柄一覧を更新しました({len(listing):,} 銘柄)。",
                                    parent=self.root)

        def failed(exc):
            self._jpx_updating = False
            self.jpx_button.configure(state="normal")
            self._update_jpx_status()
            logger.warning("東証の銘柄一覧を取得できませんでした: %s", exc)
            if manual:
                messagebox.showerror("更新できませんでした",
                                     f"東証の銘柄一覧を取得できませんでした。\n時間をおいて再度お試しください。\n\n{exc}",
                                     parent=self.root)

        self.run_bg(lambda: self.jpx_downloader(self.jpx_path), done, failed)

    def open_data_dir(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(DATA_DIR)  # type: ignore[attr-defined]  # Windows のみ

    # 終了 ------------------------------------------------------------------
    def on_close(self):
        answer = messagebox.askyesnocancel(
            "終了の確認",
            "株価の監視を終了しますか?\n\n"
            "「はい」… アプリを終了します(通知も止まります)\n"
            "「いいえ」… ウィンドウを最小化して、監視を続けます",
            parent=self.root,
        )
        if answer is None:
            return
        if answer:
            self.quit()
        else:
            self.root.iconify()

    def shutdown(self):
        """予約済みのタイマーを止め、ログの画面出力を解除する。"""
        for timer in (self._poll_id, self._timer_id):
            if timer is not None:
                try:
                    self.root.after_cancel(timer)
                except tk.TclError:
                    pass
        self._poll_id = self._timer_id = None
        logging.getLogger().removeHandler(self._log_handler)

    def quit(self):
        logger.info("%s を終了します", APP_NAME)
        self.shutdown()
        self.root.destroy()


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------
def setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(handler)
    if sys.stderr is not None:  # pythonw では stderr がない
        root.addHandler(logging.StreamHandler())
    for noisy in ("yfinance", "peewee", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)


def enable_high_dpi() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--minimized", action="store_true", help="最小化した状態で起動する")
    args = parser.parse_args(argv)

    setup_logging()
    enable_high_dpi()

    lock = SingleInstance()
    if not lock.acquire():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_NAME, f"{APP_NAME}はすでに起動しています。\nタスクバーのアイコンから画面を開いてください。")
        root.destroy()
        return 0

    try:
        root = tk.Tk()
        StockAlertApp(root, start_minimized=args.minimized)
        root.mainloop()
    except Exception:  # noqa: BLE001
        logger.exception("アプリの実行中に致命的なエラーが発生しました")
        return 1
    finally:
        lock.release()
    return 0
