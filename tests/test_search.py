"""銘柄検索のテスト(ネットワークには接続しない)。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from tests import helpers  # noqa: F401

from stock_alert import search
from stock_alert.search import SearchResult


def jpx_df():
    return pd.DataFrame({
        "日付": [20260831] * 7,
        "コード": [7203, 7201, 6758, "130A", 1306, 8951, 9999.0],
        "銘柄名": ["トヨタ自動車", "日産自動車", "ソニーグループ", "ベリサーブ", "ＮＥＸＴ　ＦＵＮＤＳ　ＴＯＰＩＸ連動型上場投信",
                  "日本ビルファンド投資法人", "テスト外国株"],
        "市場・商品区分": ["プライム（内国株式）", "プライム（内国株式）", "プライム（内国株式）", "グロース（内国株式）",
                    "ETF・ETN", "REIT・ベンチャーファンド・カントリーファンド・インフラファンド", "プライム（外国株式）"],
    })


class JpxTest(unittest.TestCase):
    def setUp(self):
        self.listing = search.parse_jpx_dataframe(jpx_df())

    def test_parse_keeps_only_stocks(self):
        self.assertEqual([r.symbol for r in self.listing], ["7203.T", "7201.T", "6758.T", "130A.T", "9999.T"])

    def test_parse_rejects_unexpected_format(self):
        with self.assertRaises(ValueError):
            search.parse_jpx_dataframe(pd.DataFrame({"a": [1]}))

    def test_search_by_name_katakana_hiragana_and_width(self):
        self.assertEqual([r.symbol for r in search.search_japan("トヨタ", self.listing)], ["7203.T"])
        self.assertEqual([r.symbol for r in search.search_japan("とよた", self.listing)], ["7203.T"])
        self.assertEqual([r.symbol for r in search.search_japan("ｿﾆｰ", self.listing)], ["6758.T"])
        self.assertEqual({r.symbol for r in search.search_japan("自動車", self.listing)}, {"7203.T", "7201.T"})

    def test_search_by_code(self):
        self.assertEqual([r.symbol for r in search.search_japan("7203", self.listing)], ["7203.T"])
        self.assertEqual([r.symbol for r in search.search_japan("７２０３", self.listing)], ["7203.T"])
        self.assertEqual([r.symbol for r in search.search_japan("7203.t", self.listing)], ["7203.T"])
        self.assertEqual([r.symbol for r in search.search_japan("130a", self.listing)], ["130A.T"])
        # 前方一致(7201, 7203)
        self.assertEqual({r.symbol for r in search.search_japan("720", self.listing)}, {"7201.T", "7203.T"})

    def test_exact_match_comes_first(self):
        listing = [SearchResult("1111.T", "トヨタ自動車関連", "x"), SearchResult("7203.T", "トヨタ自動車", "x")]
        self.assertEqual(search.search_japan("トヨタ自動車", listing)[0].symbol, "7203.T")

    def test_empty_query(self):
        self.assertEqual(search.search_japan("  ", self.listing), [])

    def test_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "jpx.csv"
            self.assertEqual(search.load_jpx_cache(path), [])
            self.assertIsNone(search.jpx_cache_age_days(path))
            search.save_jpx_cache(self.listing, path)
            self.assertEqual(search.load_jpx_cache(path), self.listing)
            self.assertLess(search.jpx_cache_age_days(path), 1)

    def test_download_parses_and_caches(self):
        response = mock.Mock(content=b"xls-bytes")
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("requests.get", return_value=response) as get, \
                mock.patch("pandas.read_excel", return_value=jpx_df()):
            path = Path(d) / "jpx.csv"
            listing = search.download_jpx_list(path)
            self.assertEqual(len(listing), 5)
            self.assertEqual(search.load_jpx_cache(path), listing)
            self.assertEqual(get.call_args.args[0], search.JPX_LIST_URL)


class YahooTest(unittest.TestCase):
    QUOTES = [
        {"symbol": "AAPL", "longname": "Apple Inc.", "quoteType": "EQUITY", "exchange": "NMS"},
        {"symbol": "APLE", "shortname": "Apple Hospitality", "quoteType": "EQUITY", "exchange": "NYQ"},
        {"symbol": "AAPL.MX", "longname": "Apple Inc.", "quoteType": "EQUITY", "exchange": "MEX"},
        {"symbol": "7203.T", "longname": "Toyota Motor Corporation", "quoteType": "EQUITY", "exchange": "JPX"},
        {"symbol": "AAPL240621C00100000", "quoteType": "OPTION", "exchange": "OPR"},
        {"symbol": "QQQ", "longname": "Invesco QQQ", "quoteType": "ETF", "exchange": "NMS"},
    ]

    def test_filter_quotes(self):
        results = search.parse_yahoo_quotes(self.QUOTES)
        self.assertEqual(
            [(r.symbol, r.name, r.market) for r in results],
            [("AAPL", "Apple Inc.", "NASDAQ"), ("APLE", "Apple Hospitality", "NYSE"),
             ("7203.T", "Toyota Motor Corporation", "東証")],
        )

    def test_search_yahoo_uses_yfinance(self):
        fake = mock.Mock(quotes=self.QUOTES[:1])
        with mock.patch("yfinance.Search", return_value=fake) as cls:
            self.assertEqual([r.symbol for r in search.search_yahoo("apple")], ["AAPL"])
        self.assertEqual(cls.call_args.args[0], "apple")


class CombinedSearchTest(unittest.TestCase):
    def setUp(self):
        self.listing = search.parse_jpx_dataframe(jpx_df())

    def test_merge_and_dedupe(self):
        yahoo = lambda q: [SearchResult("7203.T", "Toyota Motor", "東証"), SearchResult("TM", "Toyota ADR", "NYSE")]
        results, warnings = search.search("トヨタ", self.listing, yahoo)
        self.assertEqual([r.symbol for r in results], ["7203.T", "TM"])
        self.assertEqual(results[0].name, "トヨタ自動車")  # JPX の日本語名を優先
        self.assertEqual(warnings, [])

    def test_yahoo_failure_is_reported_not_raised(self):
        def broken(q):
            raise OSError("network down")

        with self.assertLogs("stock_alert.search", level="WARNING"):
            results, warnings = search.search("トヨタ", self.listing, broken)
        self.assertEqual([r.symbol for r in results], ["7203.T"])
        self.assertEqual(len(warnings), 1)

    def test_missing_jpx_list_warns(self):
        results, warnings = search.search("Apple", [], lambda q: [SearchResult("AAPL", "Apple", "NASDAQ")])
        self.assertEqual([r.symbol for r in results], ["AAPL"])
        self.assertEqual(len(warnings), 1)

    def test_yahoo_results_not_crowded_out(self):
        listing = [SearchResult(f"{1000 + i}.T", f"株式会社テスト{i}", "x") for i in range(100)]
        yahoo = lambda q: [SearchResult("TEST", "Test Inc", "NYSE")]
        results, _ = search.search("テスト", listing, yahoo)
        self.assertEqual(len(results), search.MAX_RESULTS)
        self.assertEqual(results[-1].symbol, "TEST")


if __name__ == "__main__":
    unittest.main()
