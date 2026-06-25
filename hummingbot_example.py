"""Example Hummingbot script: gate orders with the CMC macro pre-flight layer.

Drop this in your Hummingbot `scripts/` folder and start it with
`start --script hummingbot_example.py`. Make sure `cmc_macro.py` is importable
(same folder works) and that the cache is being refreshed in the background:

    python cmc_macro.py refresh --loop 3600 &

This is a deliberately minimal template — it places a single, macro-scaled buy
order per refresh interval. The CMC gate only decides HOW MUCH (or whether) to
trade; Hummingbot still owns execution, and deployment stays user-confirmed.
"""
from decimal import Decimal

from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.order_candidate import OrderCandidate
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase

from cmc_macro import MacroGate


class MacroGatedBuy(ScriptStrategyBase):
    # --- config -------------------------------------------------------------
    exchange = "binance_paper_trade"
    trading_pair = "BTC-USDT"
    base_order_amount = Decimal("0.01")     # size before the macro multiplier
    order_interval = 60 * 60                # seconds between orders (1h)

    markets = {exchange: {trading_pair}}

    # reads cmc_macro.json on each tick — pure local file read, no network
    gate = MacroGate("cmc_macro.json")

    def __init__(self, connectors):
        super().__init__(connectors)
        self._last_order_ts = 0.0

    def on_tick(self):
        now = self.current_timestamp
        if now - self._last_order_ts < self.order_interval:
            return
        self._last_order_ts = now

        macro = self.gate.evaluate(direction="LONG")

        if macro["action"] == "VETO":
            self.logger().info(f"[CMC] VETO ({macro['note']}) — standing down")
            return

        amount = self.base_order_amount * Decimal(str(macro["size_mult"]))
        if amount <= Decimal("0"):
            return

        self.logger().info(
            f"[CMC] regime={macro['regime']} F&G={macro['fear_greed']} "
            f"action={macro['action']} size x{macro['size_mult']} -> {amount}"
            + ("  [STALE cache -> neutral]" if macro["stale"] else "")
        )

        price = self.connectors[self.exchange].get_mid_price(self.trading_pair)
        candidate = OrderCandidate(
            trading_pair=self.trading_pair,
            is_maker=False,
            order_type=OrderType.MARKET,
            order_side=TradeType.BUY,
            amount=amount,
            price=price,
        )
        # respect available balance / exchange rules before sending
        candidate = self.connectors[self.exchange].budget_checker.adjust_candidate(
            candidate, all_or_none=False
        )
        if candidate.amount <= Decimal("0"):
            self.logger().info("[CMC] budget check left nothing to trade")
            return

        self.buy(
            self.exchange,
            self.trading_pair,
            candidate.amount,
            candidate.order_type,
            candidate.price,
        )
