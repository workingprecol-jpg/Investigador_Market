"""Coordinator/shadow integration with local fakes; never contacts an exchange."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot.config import Config
from bot.engine import Coordinator
from bot.exchange import ExchangeError
from bot.models import Advice
from bot.storage import Store
from tests.test_shadow import NOW, premium, reports, snapshot


class ShadowClient:
    def __init__(self, symbols):
        self.now = NOW
        self.symbols = symbols
        self.price = 100
        self.failed_snapshot = None
        self.failed_filters = False
        self.filter_calls = 0

    def snapshot(self, symbol, interval, limit):
        if symbol == self.failed_snapshot:
            raise ExchangeError("Unavailable symbol")
        return snapshot(self.now, symbol, self.price)

    def premium_index(self, symbol):
        return premium(self.now, symbol, self.price)

    def normalize_quantity(self, symbol, quantity, price):
        return quantity

    def exchange_info(self, symbol):
        self.filter_calls += 1
        if self.failed_filters:
            raise ExchangeError("Unavailable filters")
        return premium(self.now, symbol, self.price)["_symbol_filters"]


class ShadowIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = patch("bot.engine.time.time", return_value=NOW).start()
        self.addCleanup(patch.stopall)
        self.stores = []
        self.addCleanup(lambda: [store.close() for store in self.stores])

    def bot(self, symbols=("DOGEUSDT",), active=True):
        config = Config(symbols=symbols, news_enabled=False)
        client = ShadowClient(symbols)
        store = Store(Path(self.directory.name) / (str(len(self.stores))+".sqlite3"))
        self.stores.append(store)
        context = Mock()
        context.analyze.return_value = Advice("market_context", 0, details={"fresh": False})
        bot = Coordinator(config, store, client, context)
        bot.trend.analyze = Mock(return_value=reports()[0])
        bot.momentum.analyze = Mock(return_value=reports()[1])
        if active:
            self.assertTrue(bot.shadow.start(bot.state, dict(id="test", agent="liquidity", patch={"max_spread_bps": 10}), NOW))
            bot.state["experiments"]["test"]["source_version"] = 1
        return bot

    def advance(self, bot, seconds=30, price=97):
        bot.client.now += seconds
        bot.client.price = price
        self.clock.return_value = bot.client.now

    def test_shadow_receives_new_quotes_on_same_closed_signal_candle(self):
        bot = self.bot()
        bot.cycle()
        self.advance(bot)
        bot.cycle()
        experiment = bot.state["experiments"]["test"]
        self.assertEqual(experiment["observations"], 2)
        self.assertEqual(len(experiment["baseline"]["trades"]), 1)
        self.assertEqual(experiment["baseline"]["trades"][0]["reason"], "stop_loss")
        self.assertEqual(bot.trend.analyze.call_count, 1)

    def test_real_portfolio_risk_veto_does_not_disable_independent_paper_research(self):
        bot = self.bot()
        bot.state["halt_reason"] = "Manual paper pause"
        bot.cycle()
        self.assertFalse(bot.state["positions"])
        self.assertIn("DOGEUSDT", bot.state["experiments"]["test"]["baseline"]["positions"])

    def test_active_shadow_exit_survives_unrelated_symbol_data_outage(self):
        bot = self.bot(("DOGEUSDT", "SOLUSDT"))
        bot.cycle()
        self.advance(bot)
        bot.client.failed_snapshot = "SOLUSDT"
        bot.cycle()
        experiment = bot.state["experiments"]["test"]
        self.assertNotIn("DOGEUSDT", experiment["baseline"]["positions"])
        self.assertTrue(any(t["symbol"] == "DOGEUSDT" for t in experiment["baseline"]["trades"]))

    def test_active_shadow_exit_survives_unavailable_quantity_filters(self):
        bot = self.bot()
        bot.cycle()
        self.advance(bot)
        bot.client.failed_filters = True
        bot.cycle()
        experiment = bot.state["experiments"]["test"]
        self.assertFalse(experiment["baseline"]["positions"])
        self.assertEqual(len(experiment["baseline"]["trades"]), 1)

    def test_no_filter_requests_without_experiment(self):
        bot = self.bot(active=False)
        bot.cycle()
        self.assertEqual(bot.client.filter_calls, 0)

    def test_pending_promotion_prevents_next_experiment_on_old_champion(self):
        bot = self.bot()
        bot.state["experiments"]["test"]["status"] = "promote"
        bot.state["pending_promotions"].append(dict(id="test", recommendation="promote"))
        bot.state["proposal_queue"].append(dict(id="next", agent="risk", patch={"risk_per_trade": .004}, base_version=1))
        bot.daily_maintenance()
        self.assertNotIn("next", bot.state["experiments"])
        self.assertEqual(len(bot.state["proposal_queue"]), 1)

    def test_restart_and_experiment_use_effective_version_configuration(self):
        bot = self.bot(active=False)
        bot.state["strategy"].update(patch={"risk_per_trade": .004}, version=2)
        bot.save()
        restarted = Coordinator(bot.base_config, bot.store, bot.client, bot.context)
        self.assertEqual(restarted.c.risk_per_trade, .004)
        restarted.state["proposal_queue"].append(dict(id="v2", agent="liquidity", patch={"max_spread_bps": 10}, base_version=2))
        restarted.daily_maintenance()
        experiment = restarted.state["experiments"]["v2"]
        self.assertEqual(experiment["source_version"], 2)
        self.assertEqual(experiment["frozen_config"]["risk_per_trade"], .004)
        self.assertEqual(experiment["candidate_config"]["risk_per_trade"], .004)


if __name__ == "__main__":
    unittest.main()
