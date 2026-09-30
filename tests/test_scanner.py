import copy
import http.client
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.dashboard import DashboardServer
from bot.models import Candle
from bot.scanner import (PublicData, DataError, SPOT, FUTURES, DEX, MAJORS, Scanner,
                         analyze_spot, analyze_market, analyze_dex, bars_from,
                         select_universe, pick_candidates, scanner_snapshot, write_snapshot)

NOW = 1_800_000_000


def bars(interval=900):
    result = []
    opening = int(NOW//interval*interval-100*interval)*1000
    for i in range(100):
        close = 100+i*.08+math.sin(i)*.6
        if i == 99:
            close = max(b.high for b in result[-20:])+.1
        result.append(Candle(opening+i*interval*1000, opening+(i+1)*interval*1000-1,
                             close-.1, close+.2, close-.4, close, 300 if i == 99 else 100))
    return result


def quote():
    last = bars()[-1].close
    return dict(symbol="ABCUSDT", name="ABC", bid=last, ask=last+.001, price=last,
                volume_24h=4_000_000, spread_bps=1, change_24h=8, observed_at=NOW)


def falling_bars(interval=900):
    # Mirror the bullish fixture, including high/low and volume, into a short setup.
    return [Candle(b.open_time, b.close_time, 220-b.open, 220-b.low,
                   220-b.high, 220-b.close, b.volume) for b in bars(interval)]


def dex_pair():
    return dict(chainId="solana", pairAddress="PairAddress", baseToken=dict(address="TokenAddress", symbol="NEW", name="New"),
                priceUsd=".1", liquidity=dict(usd=200_000), volume=dict(h24=500_000),
                priceChange=dict(h24=12, h1=3), pairCreatedAt=(NOW-172800)*1000)


class ScannerTest(unittest.TestCase):
    def test_news_cache_drops_headlines_older_than_six_hours(self):
        from bot.context import Article
        scanner = Scanner()
        articles = [Article("Recent", "https://www.coindesk.com/recent", NOW-100),
                    Article("Expiring", "https://www.coindesk.com/expiring", NOW-21600+20),
                    Article("Old", "https://www.coindesk.com/old", NOW-22000),
                    Article("Future", "https://www.coindesk.com/future", NOW+1)]
        with patch("bot.context._request_bytes", return_value=b"rss"), patch("bot.context.parse_rss", return_value=articles):
            first = scanner.headlines(NOW)
            self.assertEqual([a["title"] for a in first["articles"]], ["Recent", "Expiring"])
            # Cached articles must also expire as time moves inside the TTL.
            later = scanner.headlines(NOW+21)
            self.assertEqual([a["title"] for a in later["articles"]], ["Recent"])

    def test_only_allowlisted_public_get_endpoints(self):
        client = PublicData()
        with patch("bot.scanner.build_opener") as network:
            for base, path in [(SPOT, "/api/v3/order"), (SPOT, "/api/v3/account"),
                               (FUTURES, "/fapi/v1/order"), (FUTURES, "/fapi/v2/account"),
                               ("https://api.binance.com", "/api/v3/klines"),
                               (DEX, "/token-pairs/v1/solana/../../order")]:
                with self.assertRaises(DataError):
                    client.get(base, path)
            network.assert_not_called()

    def test_universe_excludes_inactive_stablecoins_bad_and_old_quotes(self):
        metadata = dict(symbols=[dict(symbol=s+"USDT", baseAsset=s, quoteAsset="USDT", status="HALT" if s=="HALT" else "TRADING", isSpotTradingAllowed=True) for s in ["ABC", "USDC", "HALT", "OLD", "BAD"]])
        tickers = [dict(symbol=s+"USDT", lastPrice="1", quoteVolume="3000000", priceChangePercent="3", closeTime=(NOW-1000 if s=="OLD" else NOW)*1000) for s in ["ABC", "USDC", "HALT", "OLD", "BAD"]]
        books = [dict(symbol=s+"USDT", bidPrice="1", askPrice="nan" if s=="BAD" else "1.001") for s in ["ABC", "USDC", "HALT", "OLD", "BAD"]]
        self.assertEqual([q["symbol"] for q in select_universe(metadata, tickers, books, NOW)], ["ABCUSDT"])

    def test_open_candle_is_excluded_and_gaps_are_rejected(self):
        raw = [[b.open_time, b.open, b.high, b.low, b.close, b.volume, b.close_time] for b in bars()]
        raw.append([NOW*1000, 110, 111, 109, 110, 1, (NOW+900)*1000-1])
        self.assertEqual(len(bars_from(raw, "ABCUSDT", "15m", quote(), NOW)), 100)
        del raw[50]
        with self.assertRaises(ValueError):
            bars_from(raw, "ABCUSDT", "15m", quote(), NOW)

    def test_strong_setup_requires_current_quote_volume_and_context(self):
        q = quote()
        result = analyze_spot(q, bars(), bars(3600), True)
        self.assertEqual(result["decision"], "SPOT_CONDICIONAL")
        self.assertEqual([s["side"] for s in result["scenarios"]], ["spot"])
        self.assertLess(result["levels"]["invalidation"], q["ask"])
        self.assertGreater(result["levels"]["reference_target"], q["ask"])
        self.assertEqual(analyze_spot(q, bars(), bars(3600), False)["decision"], "ESPERAR")
        faded = {**q, "bid": q["bid"]-5, "ask":q["ask"]-5}
        self.assertNotEqual(analyze_spot(faded, bars(), bars(3600), True)["decision"], "SPOT_CONDICIONAL")
        low_volume = bars()
        b = low_volume[-1]
        low_volume[-1] = Candle(b.open_time, b.close_time, b.open, b.high, b.low, b.close, 50)
        self.assertEqual(analyze_spot(q, low_volume, bars(3600), True)["decision"], "ESPERAR")

    def test_majors_are_selected_even_outside_leaders_rotation_and_liquidity(self):
        universe = [{**quote(), "symbol": f"ALT{i:03}USDT", "change_24h": 50-i/10} for i in range(80)]
        universe += [{**quote(), "symbol": symbol, "change_24h": -20, "volume_24h": 1} for symbol in MAJORS[:-1]]
        universe.append({**quote(), "symbol": "1000PEPEUSDT", "volume_24h": 1})
        for market in ["spot", "futures"]:
            selected, _, count = pick_candidates(universe, 32, market)
            self.assertTrue(set(MAJORS[:-1]).issubset(selected))
            self.assertIn("1000PEPEUSDT", selected)
            self.assertEqual(count, 80)
            self.assertLessEqual(len(selected), 45)

    def test_futures_filter_requires_active_usdt_perpetual_contract(self):
        metadata = dict(symbols=[dict(symbol=s, baseAsset=s[:-4], quoteAsset="USDT", status="TRADING",
                                     contractType="PERPETUAL" if s!="DELIVERYUSDT" else "CURRENT_QUARTER",
                                     marginAsset="USDT" if s!="MARGINUSDT" else "USDC")
                                 for s in ["BTCUSDT", "DELIVERYUSDT", "MARGINUSDT"]])
        tickers = [dict(symbol=s["symbol"], lastPrice="1", quoteVolume="3000000", priceChangePercent="3", closeTime=NOW*1000) for s in metadata["symbols"]]
        books = [dict(symbol=s["symbol"], bidPrice="1", askPrice="1.001") for s in metadata["symbols"]]
        self.assertEqual([q["symbol"] for q in select_universe(metadata, tickers, books, NOW, "futures")], ["BTCUSDT"])

    def test_futures_long_and_short_have_separate_checks_and_directional_levels(self):
        for side, candles, hourly in [("long", bars(), bars(3600)), ("short", falling_bars(), falling_bars(3600))]:
            last = candles[-1].close
            q = {**quote(), "price": last, "bid":last-.001, "ask":last+.001,
                 "change_24h": 8 if side=="long" else -8, "funding_rate":0, "funding_fresh":True}
            row = analyze_market(q, candles, hourly, True, "futures")
            self.assertEqual(row["decision"], side.upper()+"_CONDICIONAL")
            self.assertEqual(row["bias"], side)
            self.assertEqual(row["source"], "Binance Futures")
            self.assertEqual([s["side"] for s in row["scenarios"]], ["long", "short"])
            levels = row["levels"]
            sign = 1 if side=="long" else -1
            self.assertGreater(sign*(levels["reference_entry"]-levels["invalidation"]), 0)
            self.assertGreater(sign*(levels["reference_target"]-levels["reference_entry"]), 0)
            self.assertTrue(all(c["passed"] for c in row["checks"]))
            self.assertEqual(row["scenarios"][1 if side=="long" else 0]["decision"], "ESPERAR")
            for funding, fresh in [(None,False), (0,False), (.002*sign,True)]:
                blocked = analyze_market({**q, "funding_rate":funding, "funding_fresh":fresh}, candles, hourly, True, "futures")
                scenario = next(s for s in blocked["scenarios"] if s["side"]==side)
                self.assertEqual(scenario["decision"], "ESPERAR")
                self.assertFalse(next(c for c in scenario["checks"] if c["key"]=="funding")["passed"])
            self.assertEqual(analyze_market(q, candles, hourly, False, "futures")["decision"], "ESPERAR")

    def test_derivative_details_query_own_market_candles(self):
        client = unittest.mock.Mock()
        def candles(base, path, **params):
            self.assertEqual(base, FUTURES)
            self.assertEqual(params["symbol"], "ABCUSDT")
            if path != "/fapi/v1/klines":
                raise DataError("Research feed unavailable")
            return [[b.open_time,b.open,b.high,b.low,b.close,b.volume,b.close_time]
                    for b in bars({"15m":900,"1h":3600,"4h":14400,"1d":86400}[params["interval"]])]
        client.get.side_effect = candles
        with patch("bot.scanner.time.time", return_value=NOW):
            row = Scanner(client).details({**quote(), "funding_rate":0, "funding_fresh":True}, True, "futures")
        self.assertEqual(row["decision"], "LONG_CONDICIONAL")
        self.assertEqual(client.get.call_count, 7)
        self.assertEqual(set(row["chart"]), {"15m","1h","4h","1d"})
        self.assertEqual(row["derivatives"]["status"], "partial")

    def test_dex_never_issues_entry_and_checks_contract_and_liquidity(self):
        p = dex_pair()
        row = analyze_dex(p, "solana", "TokenAddress", NOW)
        self.assertEqual(row["decision"], "INVESTIGAR")
        self.assertIsNone(row["levels"])
        p["liquidity"]["usd"] = 1000
        self.assertEqual(analyze_dex(p, "solana", "TokenAddress", NOW)["decision"], "DESCARTAR")
        with self.assertRaises(DataError):
            analyze_dex(p, "solana", "DifferentAddress", NOW)

    def test_stale_failed_future_and_old_observations_invalidate_entry(self):
        spot = analyze_spot(quote(), bars(), bars(3600), True)
        long = analyze_market({**quote(), "funding_rate":0, "funding_fresh":True}, bars(), bars(3600), True, "futures")
        short = analyze_market({**quote(), "bid":falling_bars()[-1].close-.001,
                                "ask":falling_bars()[-1].close, "funding_rate":0, "funding_fresh":True}, falling_bars(), falling_bars(3600), True, "futures")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"scanner.json"
            for completed, status, observed in [(NOW-901,"ok",NOW), (NOW,"error",NOW),
                                                (NOW+1,"ok",NOW), (NOW,"ok",NOW-901)]:
                payload = dict(mode="analysis", execution_enabled=False, completed_at=completed, status=status,
                               candidates=[{**copy.deepcopy(row), "observed_at":observed} for row in [spot,long,short]])
                write_snapshot(path, payload)
                result = scanner_snapshot(path, NOW)
                for row in result["candidates"]:
                    self.assertEqual(row["decision"], "ESPERAR")
                    self.assertTrue(all(s["decision"]=="ESPERAR" for s in row["scenarios"]))

    def test_failure_of_spot_does_not_invent_market_data(self):
        client = unittest.mock.Mock()
        client.get.side_effect = DataError("offline")
        scanner = Scanner(client)
        with patch.object(scanner, "headlines", return_value=dict(status="error", articles=[])), patch("bot.scanner.time.time", return_value=NOW):
            report = scanner.cycle()
        self.assertEqual(len(report["candidates"]), len(MAJORS)*2)
        for row in report["candidates"]:
            self.assertIsNone(row["price"])
            self.assertIsNone(row["levels"])
            self.assertFalse(row["analysis_ok"])
            self.assertEqual(row["decision"], "ESPERAR")
        self.assertFalse(report["execution_enabled"])
        self.assertEqual(report["status"], "error")

    def test_cli_disables_all_legacy_trading_modes_before_credentials(self):
        root = Path(__file__).resolve().parents[1]
        for mode in ["paper", "demo"]:
            result = subprocess.run([sys.executable, "-m", "bot", "run", "--mode", mode, "--config", "nonexistent.toml"], cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("Trading deshabilitado", result.stderr)

    def test_scan_status_round_trips_unicode_on_legacy_windows_console(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"scanner.json"
            write_snapshot(path, dict(mode="analysis", execution_enabled=False, completed_at=NOW,
                                      status="ok", candidates=[], note="龙虾 → volumen"))
            result = subprocess.run([sys.executable,"-m","bot","scan-status","--output",str(path)],
                                    cwd=root, capture_output=True, env={**os.environ,"PYTHONIOENCODING":"cp1252"})
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)["note"],"龙虾 → volumen")

    def test_analysis_dashboard_reads_scanner_and_rejects_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"scanner.json"
            write_snapshot(path, dict(mode="analysis", execution_enabled=False, completed_at=NOW, status="ok", candidates=[]))
            server = DashboardServer(path, mode="analysis", port=0, html_path=Path(__file__).resolve().parents[1]/"bot/scanner.html")
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                for route, method, expected in [("/api/dashboard", "GET", 200), ("/", "GET", 200), ("/api/dashboard", "POST", 405)]:
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                    connection.request(method, route)
                    response = connection.getresponse()
                    body = response.read()
                    self.assertEqual(response.status, expected)
                    if route == "/api/dashboard" and method == "GET":
                        self.assertIs(json.loads(body)["execution_enabled"], False)
                    connection.close()
            finally:
                server.shutdown()
                server.server_close()
                worker.join(2)


if __name__ == "__main__":
    unittest.main()
