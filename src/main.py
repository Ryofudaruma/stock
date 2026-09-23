"""株価下落通知ツールのエントリポイント。

config.yaml に登録された銘柄の現在値を取得し、閾値以下になった銘柄を
LINE / メールで通知する。通知済みフラグは state.json で管理する。
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import yaml

import notifiers

logger = logging.getLogger("stock_alert")

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "config.yaml"
DEFAULT_STATE_PATH = ROOT_DIR / "state.json"

JST = timezone(timedelta(hours=9))
STATE_RETENTION_DAYS = 7

PriceGetter = Callable[[str], "float | None"]
Notifier = Callable[[str, dict], bool]


@dataclass
class Stock:
    symbol: str
    name: str
    threshold: float


@dataclass
class Alert:
    stock: Stock
    price: float


# ---------------------------------------------------------------------------
# 株価取得(データソースを差し替える場合はこの関数だけを変更する)
# ---------------------------------------------------------------------------
def get_current_price(symbol: str) -> float | None:
    """銘柄の現在値を返す。取得できなかった場合は None を返す(例外は送出しない)。"""
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        price = None
        try:
            price = ticker.fast_info.get("last_price")
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s: fast_info からの取得に失敗しました: %s", symbol, exc)

        if price is None or price != price or price <= 0:  # None / NaN / 不正値
            history = ticker.history(period="5d")
            if history.empty:
                return None
            price = history["Close"].dropna().iloc[-1]

        price = float(price)
        if price != price or price <= 0:
            return None
        return price
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: 価格の取得中にエラーが発生しました: %s", symbol, exc)
        return None


# ---------------------------------------------------------------------------
# 設定・状態ファイル
# ---------------------------------------------------------------------------
def today_jst() -> date:
    return datetime.now(JST).date()


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    if not isinstance(config, dict):
        raise ValueError("config.yaml の形式が不正です")
    return config


def parse_stocks(config: dict) -> list[Stock]:
    """config の stocks を検証して Stock のリストに変換する。不正な項目は警告してスキップ。"""
    stocks: list[Stock] = []
    for i, entry in enumerate(config.get("stocks") or []):
        if not isinstance(entry, dict):
            logger.warning("stocks[%d] の形式が不正なためスキップします: %r", i, entry)
            continue
        symbol = entry.get("symbol")
        threshold = entry.get("threshold")
        if not symbol or threshold is None:
            logger.warning(
                "stocks[%d] に symbol または threshold がないためスキップします: %r", i, entry
            )
            continue
        try:
            threshold = float(threshold) if not isinstance(threshold, (int, float)) else threshold
        except (TypeError, ValueError):
            logger.warning("stocks[%d] の threshold が数値ではないためスキップします: %r", i, entry)
            continue
        symbol = str(symbol).strip()
        name = str(entry.get("name") or symbol)
        stocks.append(Stock(symbol=symbol, name=name, threshold=threshold))
    return stocks


def load_state(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("state.json を読み込めなかったため空の状態から開始します: %s", exc)
        return {}
    if not isinstance(state, dict):
        logger.warning("state.json の形式が不正なため空の状態から開始します")
        return {}
    return state


def save_state(path: Path, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(state.items())), f, ensure_ascii=False, indent=2)
        f.write("\n")


def state_key(symbol: str, day: date) -> str:
    return f"{symbol}:{day.isoformat()}"


def prune_state(state: dict, today: date, days: int = STATE_RETENTION_DAYS) -> dict:
    """days 日より古い日付のキー(および形式不正なキー)を削除した新しい dict を返す。"""
    cutoff = today - timedelta(days=days)
    pruned = {}
    for key, value in state.items():
        _, _, day_str = key.rpartition(":")
        try:
            day = date.fromisoformat(day_str)
        except ValueError:
            continue
        if day >= cutoff:
            pruned[key] = value
    return pruned


# ---------------------------------------------------------------------------
# 判定ロジック
# ---------------------------------------------------------------------------
def evaluate(
    stocks: list[Stock],
    state: dict,
    today: date,
    price_getter: PriceGetter = get_current_price,
) -> list[Alert]:
    """全銘柄の価格を取得し、通知すべき銘柄のリストを返す。

    state はこの関数内で更新される(閾値を上回った銘柄の当日フラグを削除)。
    通知済みフラグの追加は、実際に通知を送れた後に mark_notified で行う。
    """
    alerts: list[Alert] = []
    for stock in stocks:
        try:
            price = price_getter(stock.symbol)
            if price is None:
                logger.warning("%s (%s): 価格を取得できなかったためスキップします", stock.name, stock.symbol)
                continue

            key = state_key(stock.symbol, today)
            logger.info(
                "%s (%s): 現在値 %s / 閾値 %s", stock.name, stock.symbol, price, stock.threshold
            )

            if price <= stock.threshold:
                if state.get(key):
                    logger.info("%s (%s): 本日は通知済みのためスキップします", stock.name, stock.symbol)
                else:
                    alerts.append(Alert(stock=stock, price=price))
            elif key in state:
                # FR7: 閾値を再び上回ったら当日フラグを解除し、再度下回ったら通知できるようにする
                del state[key]
                logger.info(
                    "%s (%s): 閾値を上回ったため本日の通知済みフラグを解除しました",
                    stock.name,
                    stock.symbol,
                )
        except Exception:  # noqa: BLE001 - 1銘柄の失敗で全体を止めない
            logger.exception("%s (%s): 処理中に想定外のエラーが発生しました", stock.name, stock.symbol)
    return alerts


def mark_notified(state: dict, alerts: list[Alert], today: date) -> None:
    for alert in alerts:
        state[state_key(alert.stock.symbol, today)] = True


def _format_number(value: float) -> str:
    if isinstance(value, float):
        return str(round(value, 2))
    return str(value)


def format_message(alerts: list[Alert]) -> str:
    blocks = []
    for alert in alerts:
        stock = alert.stock
        blocks.append(
            f"【株価アラート】{stock.name}({stock.symbol})\n"
            f"現在値: {_format_number(float(alert.price))}\n"
            f"設定した閾値: {_format_number(stock.threshold)} を下回りました。"
        )
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------
def send_notifications(
    text: str,
    notify_config: dict,
    line_sender: Callable[[str], bool] = notifiers.send_line,
    email_sender: Callable[[str], bool] = notifiers.send_email,
) -> bool:
    """設定に従って通知を送る。いずれかの手段で送信できれば True を返す。"""
    use_line = bool(notify_config.get("line", True))
    use_email = bool(notify_config.get("email", True))
    email_always = bool(notify_config.get("email_always", False))

    line_ok = False
    if use_line:
        line_ok = line_sender(text)

    email_ok = False
    if use_email:
        if email_always or not line_ok:
            if use_line and not line_ok:
                logger.info("LINE通知が送信できなかったため、メールで通知します")
            email_ok = email_sender(text)

    if not use_line and not use_email:
        logger.warning("LINE・メールとも無効になっているため、通知は送信されません")

    return line_ok or email_ok


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------
def run(
    config_path: Path = DEFAULT_CONFIG_PATH,
    state_path: Path = DEFAULT_STATE_PATH,
    price_getter: PriceGetter = get_current_price,
    notify_func: Callable[[str, dict], bool] = send_notifications,
    today: date | None = None,
) -> list[Alert]:
    """1回分のチェックを実行し、通知対象となった銘柄のリストを返す。"""
    today = today or today_jst()
    config = load_config(config_path)
    stocks = parse_stocks(config)
    notify_config = config.get("notify") or {}

    state = prune_state(load_state(state_path), today)

    logger.info("監視銘柄数: %d(基準日 %s JST)", len(stocks), today.isoformat())
    alerts = evaluate(stocks, state, today, price_getter)

    if alerts:
        text = format_message(alerts)
        logger.info("通知対象: %s", ", ".join(a.stock.symbol for a in alerts))
        try:
            sent = notify_func(text, notify_config)
        except Exception:  # noqa: BLE001
            logger.exception("通知処理中に想定外のエラーが発生しました")
            sent = False
        if sent:
            mark_notified(state, alerts, today)
        else:
            logger.error("どの手段でも通知を送信できませんでした。次回の実行で再試行します")
    else:
        logger.info("通知対象の銘柄はありません")

    save_state(state_path, state)
    return alerts


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )
    # yfinance 内部の冗長なログを抑制
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    try:
        run()
    except Exception:  # noqa: BLE001
        # 設定ファイルが読めない等の致命的なエラー。ログで気づけるよう出力する
        logger.exception("実行中に致命的なエラーが発生しました")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
