"""銘柄検索。

- 日本株: 日本取引所グループ(JPX)が公開している「東証上場銘柄一覧」(Excel)をダウンロードし、
  ローカルにキャッシュして会社名・証券コードで検索する。
- 米国株: Yahoo Finance の検索(yfinance.Search)で会社名・ティッカーを検索する。
"""

from __future__ import annotations

import csv
import io
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from . import DATA_DIR

logger = logging.getLogger(__name__)

# 一覧ファイルが置かれているページ。ファイルの URL や形式(xls → xlsx)は JPX 側で変わることがあるため、
# まずこのページからリンクを探し、見つからなければ既知の URL を順に試す。
JPX_PAGE_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
JPX_LIST_URLS = (
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx",
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls",
)
JPX_LINK_PATTERN = re.compile(r"""href=["']([^"']*data_j\.(?:xlsx|xls))["']""", re.IGNORECASE)
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StockAlert"}
JPX_CACHE_PATH = DATA_DIR / "jpx_list.csv"
JPX_REFRESH_DAYS = 30

# Yahoo Finance の取引所コード
US_EXCHANGES = {"NYQ": "NYSE", "NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "ASE": "NYSE American"}
JP_EXCHANGES = {"JPX"}

MAX_RESULTS = 50


@dataclass(frozen=True)
class SearchResult:
    symbol: str  # yfinance のシンボル(例: 7203.T / AAPL)
    name: str
    market: str


# ---------------------------------------------------------------------------
# 文字列の正規化
# ---------------------------------------------------------------------------
def normalize(text: str) -> str:
    """全角/半角・大文字/小文字・ひらがな/カタカナの違いを吸収する。"""
    text = unicodedata.normalize("NFKC", str(text)).casefold()
    # ひらがな → カタカナ
    text = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in text)
    return text.replace(" ", "").replace("　", "")


# ---------------------------------------------------------------------------
# 日本株(JPX 銘柄一覧)
# ---------------------------------------------------------------------------
def _normalize_code(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float):
        if value != value:  # NaN
            return None
        if value.is_integer():
            value = int(value)
    code = unicodedata.normalize("NFKC", str(value)).strip().upper()
    if code.endswith(".0"):
        code = code[:-2]
    return code or None


def _find_column(columns, keyword: str, exclude: tuple[str, ...] = ()) -> str | None:
    """列名に keyword を含む列を探す(列名の表記ゆれ・前後の空白に対応)。"""
    for col in columns:
        name = unicodedata.normalize("NFKC", str(col)).strip()
        if name == keyword:
            return col
    for col in columns:
        name = unicodedata.normalize("NFKC", str(col)).strip()
        if keyword in name and not any(x in name for x in exclude):
            return col
    return None


def parse_jpx_dataframe(df) -> list[SearchResult]:
    """JPX の銘柄一覧(data_j.xlsx / data_j.xls を読み込んだ DataFrame)から株式だけを取り出す。"""
    code_col = _find_column(df.columns, "コード", exclude=("業種", "規模"))
    name_col = _find_column(df.columns, "銘柄名")
    market_col = _find_column(df.columns, "市場")
    if code_col is None or name_col is None or market_col is None:
        raise ValueError(f"銘柄一覧の形式が想定と異なります(列: {list(df.columns)})")

    results = []
    for code, name, market in zip(df[code_col], df[name_col], df[market_col]):
        code = _normalize_code(code)
        market = str(market or "")
        # ETF・REIT・PRO Market などは除外し、株式のみを対象にする
        if not code or "株式" not in market:
            continue
        results.append(SearchResult(symbol=f"{code}.T", name=str(name).strip(), market=market.strip()))
    return results


def save_jpx_cache(listing: list[SearchResult], path: Path = JPX_CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "name", "market"])
        for item in listing:
            writer.writerow([item.symbol, item.name, item.market])
    tmp.replace(path)


def load_jpx_cache(path: Path = JPX_CACHE_PATH) -> list[SearchResult]:
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return [
                SearchResult(row["symbol"], row["name"], row["market"])
                for row in csv.DictReader(f)
                if row.get("symbol")
            ]
    except FileNotFoundError:
        return []
    except (OSError, KeyError, csv.Error, UnicodeDecodeError) as exc:
        logger.warning("東証銘柄一覧のキャッシュを読み込めませんでした: %s", exc)
        return []


def jpx_cache_age_days(path: Path = JPX_CACHE_PATH) -> float | None:
    try:
        return (time.time() - path.stat().st_mtime) / 86400
    except OSError:
        return None


def find_jpx_file_urls(html: str, base_url: str = JPX_PAGE_URL) -> list[str]:
    """JPX のページの HTML から銘柄一覧ファイルへのリンクを探す(xlsx を優先)。"""
    urls = []
    for href in JPX_LINK_PATTERN.findall(html):
        url = urljoin(base_url, href)
        if url not in urls:
            urls.append(url)
    return sorted(urls, key=lambda u: not u.lower().endswith(".xlsx"))


def _candidate_urls(session) -> list[str]:
    urls: list[str] = []
    try:
        response = session.get(JPX_PAGE_URL, timeout=30, headers=HTTP_HEADERS)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
        urls = find_jpx_file_urls(response.text, JPX_PAGE_URL)
        if not urls:
            logger.warning("JPX のページに銘柄一覧ファイルへのリンクが見つかりませんでした")
    except Exception as exc:  # noqa: BLE001
        logger.warning("JPX のページを取得できませんでした: %s", exc)
    return urls + [u for u in JPX_LIST_URLS if u not in urls]


def download_jpx_list(path: Path = JPX_CACHE_PATH, session=None) -> list[SearchResult]:
    """JPX から最新の銘柄一覧をダウンロードしてキャッシュに保存する(失敗時は例外)。"""
    import pandas as pd
    import requests

    session = session or requests.Session()
    errors = []
    for url in _candidate_urls(session):
        try:
            response = session.get(url, timeout=60, headers=HTTP_HEADERS)
            response.raise_for_status()
            # xls / xlsx はファイルの中身から自動判別する
            df = pd.read_excel(io.BytesIO(response.content))
            listing = parse_jpx_dataframe(df)
            if not listing:
                raise ValueError("銘柄一覧に株式が1件も含まれていませんでした")
        except Exception as exc:  # noqa: BLE001
            logger.info("銘柄一覧を取得できませんでした(%s): %s", url, exc)
            errors.append(f"{url.rsplit('/', 1)[-1]}: {exc}")
            continue
        save_jpx_cache(listing, path)
        logger.info("東証の銘柄一覧を更新しました(%d 銘柄)", len(listing))
        return listing
    raise RuntimeError("東証の銘柄一覧をダウンロードできませんでした(" + " / ".join(errors) + ")")


def search_japan(query: str, listing: list[SearchResult]) -> list[SearchResult]:
    """証券コードの前方一致、または銘柄名の部分一致で検索する。"""
    q = normalize(query)
    if not q:
        return []
    q_code = q.upper().removesuffix(".T")
    exact, prefix, partial = [], [], []
    for item in listing:
        code = item.symbol.removesuffix(".T")
        name = normalize(item.name)
        if code == q_code or name == q:
            exact.append(item)
        elif code.startswith(q_code) or name.startswith(q):
            prefix.append(item)
        elif q in name:
            partial.append(item)
    return exact + prefix + partial


# ---------------------------------------------------------------------------
# 米国株(Yahoo Finance)
# ---------------------------------------------------------------------------
def parse_yahoo_quotes(quotes: list[dict]) -> list[SearchResult]:
    results = []
    for quote in quotes or []:
        if quote.get("quoteType") != "EQUITY":
            continue
        exchange = quote.get("exchange")
        symbol = str(quote.get("symbol") or "").strip().upper()
        name = quote.get("longname") or quote.get("shortname") or symbol
        if not symbol:
            continue
        if exchange in US_EXCHANGES:
            results.append(SearchResult(symbol, str(name), US_EXCHANGES[exchange]))
        elif exchange in JP_EXCHANGES and symbol.endswith(".T"):
            results.append(SearchResult(symbol, str(name), "東証"))
    return results


def search_yahoo(query: str, max_results: int = 15) -> list[SearchResult]:
    """Yahoo Finance で検索する(失敗時は例外)。"""
    import yfinance as yf

    search = yf.Search(query, max_results=max_results, news_count=0, lists_count=0,
                       include_cb=False, recommended=0, enable_fuzzy_query=True)
    return parse_yahoo_quotes(search.quotes)


# ---------------------------------------------------------------------------
# まとめて検索
# ---------------------------------------------------------------------------
def search(
    query: str,
    jpx_listing: list[SearchResult],
    yahoo_searcher=search_yahoo,
) -> tuple[list[SearchResult], list[str]]:
    """日本株と米国株をまとめて検索し、(結果, 警告メッセージ) を返す。"""
    warnings: list[str] = []
    results = search_japan(query, jpx_listing)
    if not jpx_listing:
        warnings.append("東証の銘柄一覧がまだ取得できていないため、日本株は英語名でしか検索できません。")

    try:
        yahoo_results = yahoo_searcher(query)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Yahoo Finance の検索に失敗しました: %s", exc)
        warnings.append("Yahoo Finance で検索できませんでした(米国株が表示されない可能性があります)。")
        yahoo_results = []

    seen = {r.symbol for r in results}
    extra = []
    for item in yahoo_results:
        if item.symbol not in seen:
            seen.add(item.symbol)
            extra.append(item)
    extra = extra[:MAX_RESULTS]
    # 日本株の結果が多くても Yahoo の結果が埋もれないよう、その分の枠を残す
    return results[: MAX_RESULTS - len(extra)] + extra, warnings
