"""株価の取得。

データソースを差し替える場合は get_current_price だけを変更すればよい。
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)


def _valid(price) -> bool:
    try:
        value = float(price)
    except (TypeError, ValueError):
        return False
    return not math.isnan(value) and value > 0


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

        if not _valid(price):
            history = ticker.history(period="5d")
            if history.empty:
                return None
            price = history["Close"].dropna().iloc[-1]

        return float(price) if _valid(price) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: 価格の取得中にエラーが発生しました: %s", symbol, exc)
        return None
