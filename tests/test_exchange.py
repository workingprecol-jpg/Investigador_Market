import copy
import hashlib
import hmac
import io
import json
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from bot.exchange import BinanceDemoClient, DEMO_HOST, ExchangeError, UnknownExecution, _NoRedirect


INFO = {
    "symbol": "ETHUSDT", "status": "TRADING", "quoteAsset": "USDT", "contractType": "PERPETUAL",
    "filters": [
        {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.005"},
        {"filterType": "MIN_NOTIONAL", "notional": "5"},
        {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
    ],
}


def response(payload, status=200):
    mock = MagicMock()
    mock.__enter__.return_value = mock
    mock.status = status
    mock.read.return_value = json.dumps(payload).encode()
    return mock


def http_error(status, code=None, *, retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
    return HTTPError("https://secret-url.invalid/?signature=SECRET", status, "SECRET", headers,
                     io.BytesIO(json.dumps({"code": code, "msg": "SECRET"}).encode()))


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.client = BinanceDemoClient("demo-key", "demo-secret")
        self.client._synced_at = time.monotonic()
        self.client._opener = MagicMock()

    def cache_info(self, info=None):
        self.client._exchange_cache = {"ETHUSDT": copy.deepcopy(info or INFO)}
        self.client._exchange_cached_at = time.monotonic()

    def test_demo_host_hmac_matches_exact_body_and_no_credentials_in_url(self):
        self.client._opener.open.return_value = response({"orderId": 1, "status": "FILLED"})
        with patch("bot.exchange.time.time", return_value=1234.5):
            self.client.place_market("ETHUSDT", "BUY", 0.015, "entry:test/1")
        req = self.client._opener.open.call_args.args[0]
        self.assertEqual(req.full_url, DEMO_HOST + "/fapi/v1/order")
        self.assertEqual(req.get_header("X-mbx-apikey"), "demo-key")
        payload, signature = req.data.decode().rsplit("&signature=", 1)
        self.assertEqual(signature, hmac.new(b"demo-secret", payload.encode(), hashlib.sha256).hexdigest())
        values = parse_qs(payload)
        self.assertEqual(values["timestamp"], ["1234500"])
        self.assertEqual(values["quantity"], ["0.015"])
        self.assertEqual(values["newClientOrderId"], ["entry:test/1"])
        self.assertNotIn("demo-secret", payload)

    def test_private_get_is_signed_and_public_get_has_no_key(self):
        self.client._opener.open.side_effect = [response({"totalWalletBalance": "100"}), response({"serverTime": 999})]
        self.client.account()
        private = self.client._opener.open.call_args.args[0]
        query, signature = urlsplit(private.full_url).query.rsplit("&signature=", 1)
        self.assertEqual(signature, hmac.new(b"demo-secret", query.encode(), hashlib.sha256).hexdigest())
        self.client.sync_time()
        public = self.client._opener.open.call_args.args[0]
        self.assertIsNone(public.get_header("X-mbx-apikey"))
        self.assertEqual(urlsplit(public.full_url).hostname, "demo-fapi.binance.com")

    def test_sync_precedes_first_signed_request(self):
        self.client._synced_at = None
        self.client._opener.open.side_effect = [response({"serverTime": 500000}), response({"totalWalletBalance": "100"})]
        with patch("bot.exchange.time.time", return_value=100.0):
            self.client.account()
        reqs = [call.args[0] for call in self.client._opener.open.call_args_list]
        self.assertEqual(urlsplit(reqs[0].full_url).path, "/fapi/v1/time")
        self.assertEqual(parse_qs(urlsplit(reqs[1].full_url).query)["timestamp"], ["500000"])

    def test_no_host_override_or_redirects(self):
        with self.assertRaises(TypeError):
            BinanceDemoClient(base_url="https://fapi.binance.com")
        with self.assertRaises(ValueError):
            self.client._request("GET", "https://fapi.binance.com/fapi/v1/order", signed=True)
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://fapi.binance.com"))
        self.client._opener.open.side_effect = http_error(302)
        with self.assertRaises(ExchangeError):
            self.client.account()
        self.assertEqual(self.client._opener.open.call_count, 1)

    def test_missing_keys_fail_before_network(self):
        with patch.dict("os.environ", {}, clear=True):
            client = BinanceDemoClient()
        client._opener = MagicMock()
        with self.assertRaises(ExchangeError):
            client.account()
        client._opener.open.assert_not_called()

    def test_uncertain_market_writes_are_never_retried_and_errors_are_sanitized(self):
        for failure in [URLError("SECRET url"), TimeoutError("SECRET"), http_error(503, -1007), http_error(500), http_error(400, -1006)]:
            with self.subTest(failure=type(failure).__name__):
                self.client._opener.open.reset_mock()
                self.client._opener.open.side_effect = failure
                with self.assertRaises(UnknownExecution) as raised:
                    self.client.place_market("ETHUSDT", "SELL", 0.01, "close-1", True)
                self.assertEqual(self.client._opener.open.call_count, 1)
                self.assertNotIn("SECRET", str(raised.exception))
                self.assertNotIn("http://", str(raised.exception))

    def test_rejected_writes_do_not_retry(self):
        self.client._opener.open.side_effect = http_error(400, -2010)
        with self.assertRaises(ExchangeError) as raised:
            self.client.place_market("ETHUSDT", "BUY", 0.01, "entry")
        self.assertNotIsInstance(raised.exception, UnknownExecution)
        self.assertEqual(self.client._opener.open.call_count, 1)

    def test_malformed_write_response_is_unknown(self):
        malformed = response({})
        malformed.read.return_value = b"not json"
        self.client._opener.open.return_value = malformed
        with self.assertRaises(UnknownExecution):
            self.client.place_market("ETHUSDT", "BUY", 0.01, "entry")
        self.assertEqual(self.client._opener.open.call_count, 1)

    def test_empty_order_acknowledgement_is_unknown(self):
        self.client._opener.open.return_value = response({})
        with self.assertRaises(UnknownExecution):
            self.client.place_market("ETHUSDT", "BUY", .01, "entry")
        self.assertEqual(self.client._opener.open.call_count, 1)

    @patch("bot.exchange.time.sleep")
    def test_reads_retry_transient_errors_with_backoff(self, sleep):
        self.client._opener.open.side_effect = [http_error(503), URLError("offline"), response([])]
        self.assertEqual(self.client.open_orders("ETHUSDT"), [])
        self.assertEqual(self.client._opener.open.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.5, 1.0])

    @patch("bot.exchange.time.sleep")
    def test_long_rate_limit_is_not_retried_early(self, sleep):
        self.client._opener.open.side_effect = http_error(429, -1003, retry_after=120)
        with self.assertRaises(ExchangeError):
            self.client.open_orders("ETHUSDT")
        self.assertEqual(self.client._opener.open.call_count, 1)
        sleep.assert_not_called()

    def test_rate_limit_blocks_all_requests_until_monotonic_deadline(self):
        self.client._synced_at = 100
        self.client._opener.open.side_effect = [http_error(429, -1003, retry_after=120), response({"serverTime": 200000})]
        with patch("bot.exchange.time.monotonic", return_value=100):
            with self.assertRaises(ExchangeError) as first:
                self.client.open_orders("ETHUSDT")
        self.assertEqual(first.exception.retry_after, 120)
        with patch("bot.exchange.time.monotonic", return_value=219):
            with self.assertRaises(ExchangeError) as blocked:
                self.client.sync_time()
        self.assertEqual(blocked.exception.status, 429)
        self.assertEqual(blocked.exception.retry_after, 1)
        self.assertEqual(self.client._opener.open.call_count, 1)
        with patch("bot.exchange.time.monotonic", return_value=220):
            self.client.sync_time()
        self.assertEqual(self.client._opener.open.call_count, 2)

    def test_missing_rate_limit_headers_use_conservative_defaults(self):
        for status, expected in [(418, 900), (429, 60)]:
            with self.subTest(status=status):
                self.client._blocked_until = 0
                self.client._synced_at = 100
                self.client._opener.open.side_effect = http_error(status, -1003)
                with patch("bot.exchange.time.monotonic", return_value=100):
                    with self.assertRaises(ExchangeError) as raised:
                        self.client.account()
                self.assertEqual(raised.exception.retry_after, expected)
                self.assertEqual(self.client._blocked_until, 100+expected)

    def test_get_order_none_only_for_documented_not_found(self):
        self.client._opener.open.side_effect = http_error(400, -2013)
        self.assertIsNone(self.client.get_order("ETHUSDT", "entry"))
        self.client._opener.open.side_effect = http_error(401, -2015)
        with self.assertRaises(ExchangeError):
            self.client.get_order("ETHUSDT", "entry")

    def test_quantity_intersection_step_and_no_rounding_up(self):
        self.cache_info()
        self.assertEqual(self.client.normalize_quantity("ETHUSDT", 0.016999999, 2000), 0.015)
        self.assertEqual(self.client.normalize_quantity("ETHUSDT", 101, 2000), 100)
        with self.assertRaises(ValueError):
            self.client.normalize_quantity("ETHUSDT", 0.0049, 2000)
        with self.assertRaises(ValueError):
            self.client.normalize_quantity("ETHUSDT", 0.005, 100)

    def test_decimal_lcm_handles_nondivisible_steps(self):
        info = copy.deepcopy(INFO)
        info["filters"][0]["stepSize"] = "0.002"
        info["filters"][1]["stepSize"] = "0.003"
        self.cache_info(info)
        self.assertEqual(self.client.normalize_quantity("ETHUSDT", 0.011, 2000), 0.006)

    def test_invalid_quantities_fail_closed(self):
        self.cache_info()
        for value in [float("nan"), float("inf"), -1, 0]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.client.normalize_quantity("ETHUSDT", value, 2000)

    def test_wrong_contract_is_rejected(self):
        info = copy.deepcopy(INFO)
        info["contractType"] = "CURRENT_QUARTER"
        self.cache_info(info)
        with self.assertRaises(ExchangeError):
            self.client.exchange_info("ETHUSDT")

    def test_configuration_refuses_hedge_without_changing_it(self):
        self.cache_info()
        with patch.object(self.client, "_request", return_value={"dualSidePosition": True}) as req:
            with self.assertRaises(ExchangeError):
                self.client.configure_symbol("ETHUSDT")
        self.assertEqual(req.call_count, 1)
        self.assertEqual(req.call_args.args[0], "GET")

    def test_configuration_only_accepts_already_isolated_error(self):
        self.cache_info()
        with patch.object(self.client, "_request", side_effect=[{"dualSidePosition": False}, ExchangeError("already", -4046), {"leverage": 5}]) as req:
            self.client.configure_symbol("ETHUSDT")
        self.assertEqual(req.call_args.args[2]["leverage"], 5)
        with patch.object(self.client, "_request", side_effect=[{"dualSidePosition": False}, ExchangeError("open orders", -4047)]) as req:
            with self.assertRaises(ExchangeError):
                self.client.configure_symbol("ETHUSDT")
        self.assertEqual(req.call_count, 2)
        with self.assertRaises(ValueError):
            self.client.configure_symbol("ETHUSDT", leverage=10)

    def test_stop_uses_current_algo_api_close_all_and_tighter_tick(self):
        self.cache_info()
        self.client._opener.open.return_value = response({"algoId": 33, "algoStatus": "NEW"})
        self.client.protection_stop("ETHUSDT", "SELL", 1990.001, "stop-1")
        req = self.client._opener.open.call_args.args[0]
        self.assertEqual(req.full_url, DEMO_HOST + "/fapi/v1/algoOrder")
        values = parse_qs(req.data.decode())
        self.assertEqual(values["triggerPrice"], ["1990.01"])
        self.assertEqual(values["algoType"], ["CONDITIONAL"])
        self.assertEqual(values["type"], ["STOP_MARKET"])
        self.assertEqual(values["workingType"], ["MARK_PRICE"])
        self.assertEqual(values["closePosition"], ["true"])
        self.assertEqual(values["clientAlgoId"], ["stop-1"])
        self.assertNotIn("quantity", values)
        self.assertNotIn("reduceOnly", values)
        self.assertNotIn("stopPrice", values)

    def test_cancel_uncertainty_is_not_retried(self):
        self.client._opener.open.side_effect = TimeoutError()
        with self.assertRaises(UnknownExecution):
            self.client.cancel_stop("ETHUSDT", 22)
        self.assertEqual(self.client._opener.open.call_count, 1)

    def test_snapshot_filters_unclosed_candles_using_server_time(self):
        rows = [[0, "10", "12", "9", "11", "30", 999],
                [1000, "11", "13", "10", "12", "40", 1999],
                [2000, "12", "14", "11", "13", "50", 2999]]
        self.client._opener.open.side_effect = [response({"serverTime": 2500}), response(rows),
                                                response({"bidPrice": "12", "askPrice": "12.1"}), response({"quoteVolume": "20000"})]
        snapshot = self.client.snapshot("ETHUSDT", limit=2)
        self.assertEqual(len(snapshot.candles), 2)
        self.assertEqual(snapshot.candles[-1].close_time, 1999)
        self.assertEqual(snapshot.quote_volume, 20000)

    def test_income_pages_are_frozen_and_deduplicated(self):
        rows = [{"incomeType": "COMMISSION", "tranId": n} for n in range(1000)]
        with patch.object(self.client, "sync_time", return_value=2000), patch.object(self.client, "_request", side_effect=[rows, [{"incomeType": "FUNDING_FEE", "tranId": 1000}]]) as req:
            result = self.client.income(1000)
        self.assertEqual(len(result), 1001)
        self.assertEqual([c.args[2]["page"] for c in req.call_args_list], [1, 2])
        self.assertTrue(all(c.args[2]["endTime"] == 2000 for c in req.call_args_list))

    def test_income_repeated_full_page_fails(self):
        rows = [{"incomeType": "COMMISSION", "tranId": n} for n in range(1000)]
        with patch.object(self.client, "sync_time", return_value=2000), patch.object(self.client, "_request", return_value=rows):
            with self.assertRaises(ExchangeError):
                self.client.income(1000)


if __name__ == "__main__":
    unittest.main()
