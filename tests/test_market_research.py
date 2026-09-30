import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot.models import Candle
from bot.market_research import (technical_series, derivatives, security_result, token_security,
                          discovery, btc_transactions, attributed_transactions,
                          market_valuations, unlock_calendar)
from bot.scanner import PublicData, DataError, SPOT, GOPLUS, COINGECKO
from bot.signal_history import SignalHistory
from tests.test_scanner import NOW, bars, quote, analyze_spot


class ResearchTest(unittest.TestCase):
    def test_chart_ema_seed_and_volume_weighted_window_are_causal(self):
        source=bars()
        result=technical_series(source)
        self.assertIsNone(result[18]["ema20"])
        self.assertAlmostEqual(result[19]["ema20"],sum(b.close for b in source[:20])/20,8)
        expected=sum((b.high+b.low+b.close)/3*b.volume for b in source)/sum(b.volume for b in source)
        self.assertAlmostEqual(result[-1]["vwap"],expected,8)
        self.assertEqual(result[-1]["close_at"],source[-1].close_time/1000)
        self.assertEqual(result[:40],technical_series(source[:40]))

    def test_derivatives_use_units_for_oi_and_only_closed_flow_periods(self):
        client=Mock()
        end=NOW//900*900
        oi=[dict(symbol="BTCUSDT",timestamp=(end-(4-i)*900)*1000,sumOpenInterest=str(100+i),sumOpenInterestValue=str(10000+i*2000)) for i in range(5)]
        flow=[dict(timestamp=(end-(4-i)*900)*1000,buyVol="3",sellVol="1") for i in range(5)]
        def get(base,path,**params):
            if path.endswith("openInterestHist"): return oi
            if path.endswith("takerlongshortRatio"): return flow
            return [dict(symbol="BTCUSDT",fundingTime=(NOW-1000)*1000,fundingRate=".0001")]
        client.get.side_effect=get
        result=derivatives(client,"BTCUSDT",100,NOW)
        self.assertEqual(result["status"],"ok")
        self.assertAlmostEqual(result["open_interest"]["change_1h_pct"],4)
        self.assertEqual(result["flow"]["buy_share_pct"],75)
        self.assertEqual(result["flow"]["delta_usdt_estimate"],800)
        self.assertEqual(result["flow"]["time"],end)
        flow[0]["buyVol"]="-1"
        self.assertIsNone(derivatives(client,"BTCUSDT",100,NOW)["flow"])
        oi[-1]["timestamp"]=(NOW+900)*1000
        self.assertIsNone(derivatives(client,"BTCUSDT",100,NOW)["open_interest"])

    def test_failed_derivatives_remain_unknown_and_never_issue_decisions(self):
        client=Mock();client.get.side_effect=DataError("offline")
        result=derivatives(client,"BTCUSDT",100,NOW)
        self.assertEqual(result["status"],"partial")
        self.assertIsNone(result["open_interest"])
        self.assertIsNone(result["flow"])
        self.assertNotIn("decision",result)

    def test_token_identity_and_unknown_flags_are_not_safe(self):
        address="0x"+"a"*40
        raw=dict(code=1,result={address:dict(is_honeypot="1",sell_tax="",holders=[dict(percent=".7",tag="Exchange"),dict(percent=".1",tag="")])})
        result=security_result(raw,"ethereum",address,NOW)
        checks={c["key"]:c for c in result["checks"]}
        self.assertIs(checks["is_honeypot"]["risk"],True)
        self.assertIsNone(checks["is_mintable"]["risk"])
        self.assertIsNone(checks["sell_tax"]["risk"])
        self.assertIn("10.00%",checks["concentration"]["observed"])
        with self.assertRaises(DataError): security_result(raw,"ethereum","0x"+"b"*40,NOW)
        client=Mock()
        self.assertEqual(token_security(client,"unknown",address,NOW)["status"],"unsupported")
        client.get.assert_not_called()

    def test_solana_authorities_are_decoded_without_claiming_sellability(self):
        address="A"*32
        raw=dict(code=1,result={address:dict(mintable=dict(status="1"),freezable=dict(status="0"))})
        checks={c["key"]:c for c in security_result(raw,"solana",address,NOW)["checks"]}
        self.assertIs(checks["mintable"]["risk"],True)
        self.assertIs(checks["freezable"]["risk"],False)
        self.assertIsNone(checks["selling"]["risk"])

    def test_discovery_compares_returns_and_liquidity_only_with_valid_baseline(self):
        btc={**quote(),"id":"spot:BTCUSDT","source":"Binance Spot","change_24h":2,"analysis_ok":True}
        alt={**btc,"id":"spot:ALTUSDT","change_24h":5,"relative_volume":2,"volume_acceleration":1.5}
        dex=dict(id="dex:x:a",source="DEX Screener",observed_at=NOW,liquidity_usd=200000,age_hours=30,score=60)
        old={dex["id"]:dict(liquidity_usd=100000,observed_at=NOW-300)}
        ranked=discovery([btc,alt,dex],old)
        self.assertEqual(alt["discovery"]["relative_btc_pp"],3)
        self.assertEqual(dex["discovery"]["liquidity_growth_pct"],100)
        self.assertIsNone(dex["discovery"]["relative_btc_pp"])
        self.assertIn(alt["id"],ranked)
        old[dex["id"]]["observed_at"]=NOW+1
        discovery([btc,alt,dex],old)
        self.assertIsNone(dex["discovery"]["liquidity_growth_pct"])

    def test_btc_sample_reports_output_value_without_inventing_wallet_identity(self):
        raw=[dict(txid="a"*64,value=2_000_000_000),dict(txid="not-a-hash",value=10**20),dict(txid="b"*64,value=1)]
        events=btc_transactions(raw,100000,NOW)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]["usd"],2000000)
        self.assertFalse(events[0]["confirmed"])
        self.assertEqual(events[0]["direction"],"Sin atribución")

    def test_known_internal_transfers_are_excluded_and_exchange_inflow_is_not_a_sale(self):
        tx=dict(hash="a"*64,timestamp=NOW-10,sub_transactions=[dict(transaction_type="transfer",symbol="BTC",unit_price_usd="100000",inputs=[dict(owner="Exchange",owner_type="exchange")],outputs=[dict(owner="Exchange",owner_type="exchange",amount="20")])])
        self.assertEqual(attributed_transactions([tx],"bitcoin",NOW),[])
        tx["sub_transactions"][0]["inputs"]=[dict(owner="Investor",owner_type="unknown")]
        events=attributed_transactions([tx],"bitcoin",NOW)
        self.assertEqual(events[0]["direction"],"Hacia exchange")
        self.assertIn("no prueba",events[0]["note"])

    def test_provider_credentials_cannot_be_sent_to_trading_or_untrusted_hosts(self):
        client=PublicData()
        with patch("bot.scanner.build_opener") as network:
            for base,path,headers in [(SPOT,"/api/v3/klines",{"X-MBX-APIKEY":"secret"}),
                                      (GOPLUS,"/api/v1/token_security/1",{"x-cg-demo-api-key":"secret"}),
                                      (COINGECKO,"/api/v3/coins/markets",{"Authorization":"secret"})]:
                with self.assertRaises(DataError): client.get(base,path,_headers=headers)
            network.assert_not_called()

    def test_optional_paid_sources_make_no_calls_without_local_keys(self):
        client=Mock()
        with patch.dict("os.environ",{},clear=True):
            values,status=market_valuations(client,NOW)
            self.assertEqual((values,status["status"]),({},"not_configured"))
            self.assertEqual(unlock_calendar(client,NOW)["status"],"not_configured")
        client.get.assert_not_called()


class SignalHistoryTest(unittest.TestCase):
    def report(self,row,completed=NOW):
        return dict(status="ok",completed_at=completed,candidates=[row])

    def signal(self,side="spot"):
        row=analyze_spot(quote(),bars(),bars(3600),True)
        if side=="short":
            row["scenarios"][0].update(side="short",decision="SHORT_CONDICIONAL",levels=dict(reference_entry=100,invalidation=102,reference_target=96))
        return row

    def test_observations_persist_and_candle_signal_is_deduplicated_with_frozen_levels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"history.sqlite3";history=SignalHistory(path)
            row=self.signal();first=history.publish(self.report(row))
            self.assertEqual(first["signals"],1)
            original=first["recent"][0]["entry"]
            row=copy.deepcopy(row);row["observed_at"]=NOW+300;row["scenarios"][0]["levels"]["reference_entry"]+=.01
            second=SignalHistory(path).publish(self.report(row,NOW+300))
            self.assertEqual(second["signals"],1)
            self.assertEqual(second["observations"],2)
            self.assertEqual(second["recent"][0]["entry"],original)
            self.assertEqual(len(row["history"]),2)

    def test_only_valid_entry_checklists_are_recorded_and_old_data_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            history=SignalHistory(Path(tmp)/"history.sqlite3")
            row=self.signal();row["scenarios"][0]["checks"][0]["passed"]=False
            self.assertEqual(history.publish(self.report(row))["signals"],0)
            row["observed_at"]=NOW-901
            self.assertEqual(history.publish(self.report(row))["observations"],1)

    def test_directional_horizon_and_same_candle_ambiguity_are_not_simulated_fills(self):
        with tempfile.TemporaryDirectory() as tmp:
            history=SignalHistory(Path(tmp)/"history.sqlite3")
            row=self.signal("short");row["price"]=100
            history.publish(self.report(row))
            later=copy.deepcopy(row);later.update(observed_at=NOW+3600,price=98,decision="ESPERAR",scenarios=[])
            later["chart"]={"15m":[dict(time=NOW+900,close_at=NOW+1800,high=103,low=95)]}
            result=history.publish(self.report(later,NOW+3600))
            progress=result["recent"][0]["progress"]
            self.assertEqual(progress["horizons"]["1h"]["move_pct"],2)
            self.assertEqual(progress["levels_state"],"Ambos niveles en una vela; orden desconocido")
            self.assertAlmostEqual(progress["max_adverse_pct"],3)
            self.assertAlmostEqual(progress["max_favorable_pct"],5)
            self.assertNotIn("pnl",progress)

    def test_missing_horizon_is_not_replaced_with_a_late_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            history=SignalHistory(Path(tmp)/"history.sqlite3")
            row=self.signal();history.publish(self.report(row))
            later=copy.deepcopy(row);later.update(observed_at=NOW+7200,scenarios=[],decision="ESPERAR")
            result=history.publish(self.report(later,NOW+7200))
            self.assertNotIn("1h",result["recent"][0]["progress"]["horizons"])
