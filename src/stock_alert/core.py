"""設定・状態ファイルの管理と、閾値判定・重複通知防止のロジック。"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import DATA_DIR

logger = logging.getLogger(__name__)

SETTINGS_PATH = DATA_DIR / "settings.json"
STATE_PATH = DATA_DIR / "state.json"

JST = timezone(timedelta(hours=9))
STATE_RETENTION_DAYS = 7

MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 1440

DEFAULT_SETTINGS: dict = {
    "interval_minutes": 30,
    "email": {
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "from_addr": "",
        "to_addr": "",
    },
    "stocks": [],
}

PriceGetter = Callable[[str], "float | None"]
Notifier = Callable[[str], bool]


@dataclass
class Stock:
    symbol: str
    name: str
    threshold: float


@dataclass
class Alert:
    stock: Stock
    price: float


@dataclass
class CheckResult:
    prices: dict[str, float | None] = field(default_factory=dict)
    alerts: list[Alert] = field(default_factory=list)
    notified: bool = False
    state: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ファイル入出力
# ---------------------------------------------------------------------------
def _atomic_write_json(path: Path, data) -> None:
    """書き込み途中で PC が落ちてもファイルが壊れないよう、一時ファイル経由で保存する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.warning("%s を読み込めませんでした: %s", path.name, exc)
        return None


def load_settings(path: Path = SETTINGS_PATH) -> dict:
    """設定を読み込む。ファイルがない・壊れている場合は既定値を返す。"""
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    data = _read_json(path)
    if not isinstance(data, dict):
        return settings

    try:
        interval = int(data.get("interval_minutes", settings["interval_minutes"]))
        settings["interval_minutes"] = min(max(interval, MIN_INTERVAL_MINUTES), MAX_INTERVAL_MINUTES)
    except (TypeError, ValueError):
        pass

    email = data.get("email")
    if isinstance(email, dict):
        for key in settings["email"]:
            if key in email and email[key] is not None:
                settings["email"][key] = email[key]

    settings["stocks"] = [
        {"symbol": s.symbol, "name": s.name, "threshold": s.threshold}
        for s in parse_stocks(data.get("stocks"))
    ]
    return settings


def save_settings(settings: dict, path: Path = SETTINGS_PATH) -> None:
    _atomic_write_json(path, settings)


def parse_stocks(entries) -> list[Stock]:
    """stocks の各項目を検証して Stock のリストにする。不正な項目は警告してスキップ。"""
    stocks: list[Stock] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries or []):
        if not isinstance(entry, dict):
            logger.warning("stocks[%d] の形式が不正なためスキップします: %r", i, entry)
            continue
        symbol = str(entry.get("symbol") or "").strip().upper()
        threshold = entry.get("threshold")
        if not symbol or threshold is None or isinstance(threshold, bool):
            logger.warning("stocks[%d] に銘柄コードまたは閾値がないためスキップします", i)
            continue
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            logger.warning("stocks[%d] の閾値が数値ではないためスキップします: %r", i, threshold)
            continue
        if threshold <= 0:
            logger.warning("stocks[%d] の閾値が 0 以下のためスキップします", i)
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        stocks.append(Stock(symbol=symbol, name=str(entry.get("name") or symbol), threshold=threshold))
    return stocks


def load_state(path: Path = STATE_PATH) -> dict:
    data = _read_json(path)
    return data if isinstance(data, dict) else {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    _atomic_write_json(path, dict(sorted(state.items())))


# ---------------------------------------------------------------------------
# 状態(通知済みフラグ)
# ---------------------------------------------------------------------------
def today_jst() -> date:
    return datetime.now(JST).date()


def state_key(symbol: str, day: date) -> str:
    return f"{symbol}:{day.isoformat()}"


def prune_state(state: dict, today: date, days: int = STATE_RETENTION_DAYS) -> dict:
    """days 日より古い日付のキー(および形式不正なキー)を除いた新しい dict を返す。"""
    cutoff = today - timedelta(days=days)
    pruned = {}
    for key, value in state.items():
        _, _, day_str = str(key).rpartition(":")
        try:
            day = date.fromisoformat(day_str)
        except ValueError:
            continue
        if day >= cutoff:
            pruned[key] = value
    return pruned


def is_notified(state: dict, symbol: str, today: date) -> bool:
    return bool(state.get(state_key(symbol, today)))


# ---------------------------------------------------------------------------
# 判定・通知
# ---------------------------------------------------------------------------
def evaluate(
    stocks: list[Stock],
    state: dict,
    today: date,
    price_getter: PriceGetter,
) -> tuple[list[Alert], dict[str, float | None]]:
    """全銘柄の価格を取得し、(通知すべき銘柄, 取得した価格) を返す。

    state はこの関数内で更新される(閾値を上回った銘柄の当日フラグを削除)。
    通知済みフラグの追加は、実際に通知を送れた後に mark_notified で行う。
    """
    alerts: list[Alert] = []
    prices: dict[str, float | None] = {}
    for stock in stocks:
        prices[stock.symbol] = None
        try:
            price = price_getter(stock.symbol)
            if price is None:
                logger.warning("%s (%s): 価格を取得できなかったためスキップします", stock.name, stock.symbol)
                continue
            prices[stock.symbol] = price

            logger.info("%s (%s): 現在値 %s / 閾値 %s", stock.name, stock.symbol,
                        format_number(price), format_number(stock.threshold))
            key = state_key(stock.symbol, today)
            if price <= stock.threshold:
                if state.get(key):
                    logger.info("%s (%s): 本日は通知済みです", stock.name, stock.symbol)
                else:
                    alerts.append(Alert(stock=stock, price=price))
            elif key in state:
                # 閾値を再び上回ったら当日フラグを解除し、再度下回ったら通知できるようにする
                del state[key]
                logger.info("%s (%s): 閾値を上回ったため本日の通知済みフラグを解除しました",
                            stock.name, stock.symbol)
        except Exception:  # noqa: BLE001 - 1銘柄の失敗で全体を止めない
            logger.exception("%s (%s): 処理中に想定外のエラーが発生しました", stock.name, stock.symbol)
    return alerts, prices


def mark_notified(state: dict, alerts: list[Alert], today: date) -> None:
    for alert in alerts:
        state[state_key(alert.stock.symbol, today)] = True


def format_number(value: float) -> str:
    """表示用の数値。整数ならそのまま、小数は小数第2位まで。"""
    text = f"{float(value):,.2f}".rstrip("0").rstrip(".")
    return text


def format_message(alerts: list[Alert]) -> str:
    blocks = []
    for alert in alerts:
        stock = alert.stock
        blocks.append(
            f"【株価アラート】{stock.name}({stock.symbol})\n"
            f"現在値: {format_number(alert.price)}\n"
            f"設定した閾値: {format_number(stock.threshold)} を下回りました。"
        )
    return "\n\n".join(blocks)


def run_check(
    settings: dict,
    price_getter: PriceGetter,
    notifier: Notifier,
    state_path: Path = STATE_PATH,
    today: date | None = None,
) -> CheckResult:
    """全銘柄を1回チェックし、閾値以下の銘柄があればまとめて1通通知する。"""
    today = today or today_jst()
    stocks = parse_stocks(settings.get("stocks"))
    state = prune_state(load_state(state_path), today)

    logger.info("チェック開始: %d 銘柄(基準日 %s JST)", len(stocks), today.isoformat())
    alerts, prices = evaluate(stocks, state, today, price_getter)
    result = CheckResult(prices=prices, alerts=alerts)

    if alerts:
        logger.info("通知対象: %s", ", ".join(a.stock.symbol for a in alerts))
        try:
            result.notified = bool(notifier(format_message(alerts)))
        except Exception:  # noqa: BLE001
            logger.exception("通知処理中に想定外のエラーが発生しました")
        if result.notified:
            mark_notified(state, alerts, today)
        else:
            logger.error("通知を送信できませんでした。次回のチェックで再送します")
    else:
        logger.info("通知対象の銘柄はありません")

    save_state(state, state_path)
    result.state = state
    return result
