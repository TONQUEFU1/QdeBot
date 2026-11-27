"""
QDE Liquidity Scanner Plugin - models.py

Pure data classes. Zero imports from the existing QDE codebase
(models/, engines/, storage/, etc.) - this plugin is fully isolated,
per the explicit requirement that it must never touch or depend on
QDE's own scoring/confidence/ranking internals.

TERMINOLOGY NOTE (important, per explicit instruction)
    Everything here is derived from Binance Futures ORDER BOOK depth -
    resting limit orders - not actual liquidation data (Binance does
    not expose a free, real-time liquidation feed; that would require
    a paid provider like CoinGlass, explicitly out of scope for this
    version). "LONG LIQUIDITY" / "SHORT LIQUIDITY" here mean "order
    book depth clustered below/above the current price", NOT "positions
    that will be forcibly liquidated". This distinction is preserved
    in every label and docstring so it's never misrepresented as real
    liquidation data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class OrderBookLevel:
    """One raw price/quantity level from the order book."""

    price: float
    quantity: float

    @property
    def notional(self) -> float:
        """Approximate USD value resting at this level (price * qty)."""
        return self.price * self.quantity


@dataclass(frozen=True)
class LiquidityPool:
    """A single significant price level ("wall") on one side of the
    book - one of the largest resting-order clusters found in the
    fetched depth, not a claim about liquidations.
    """

    price: float
    notional: float
    distance_pct: float  # signed: positive = above current price, negative = below

    def format_price(self) -> str:
        return f"{self.price:,.6g}"


@dataclass
class LiquiditySnapshot:
    """Everything computed for one symbol. This is the plugin's only
    output unit - formatter.py renders this, nothing else.
    """

    symbol: str
    current_price: float | None
    long_pools: list[LiquidityPool] = field(default_factory=list)  # below price (bid side)
    short_pools: list[LiquidityPool] = field(default_factory=list)  # above price (ask side)
    long_notional_total: float = 0.0  # sum of ALL bid notional in the fetched depth window
    short_notional_total: float = 0.0  # sum of ALL ask notional in the fetched depth window
    depth_levels_fetched: int = 0
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    error: str | None = None  # set instead of raising, per the isolation/never-crash requirement

    @property
    def ok(self) -> bool:
        return self.error is None and self.current_price is not None

    @property
    def nearest_above(self) -> LiquidityPool | None:
        """Nearest major SHORT-side pool (resting sell orders) above
        current price - the closest wall by distance, not necessarily
        the largest.
        """
        if not self.short_pools:
            return None
        return min(self.short_pools, key=lambda p: p.distance_pct)

    @property
    def nearest_below(self) -> LiquidityPool | None:
        """Nearest major LONG-side pool (resting buy orders) below
        current price.
        """
        if not self.long_pools:
            return None
        return max(self.long_pools, key=lambda p: p.distance_pct)  # least negative = closest

    @property
    def imbalance_ratio(self) -> float | None:
        """long_notional / short_notional across the whole fetched
        depth window. >1 means more resting buy-side depth than
        sell-side in this window, and vice versa. None if either side
        has zero depth (avoids a divide-by-zero misrepresenting the
        data).
        """
        if self.short_notional_total <= 0 or self.long_notional_total <= 0:
            return None
        return round(self.long_notional_total / self.short_notional_total, 3)

    @property
    def bias(self) -> str:
        """Simple, honest directional read: whichever side (nearest
        above vs nearest below) has the larger single pool AND is
        closer is reported as the more likely draw. Deliberately not a
        composite score - per instruction, V1 produces clear metrics,
        not an invented scoring system.
        """
        above = self.nearest_above
        below = self.nearest_below
        if above is None and below is None:
            return "UNKNOWN"
        if above is None:
            return "LONG"
        if below is None:
            return "SHORT"
        # Closer wall wins; ties broken by larger notional.
        if abs(above.distance_pct) < abs(below.distance_pct):
            return "SHORT"
        if abs(below.distance_pct) < abs(above.distance_pct):
            return "LONG"
        return "SHORT" if above.notional >= below.notional else "LONG"

    def eta_class(self) -> str:
        """Coarse proximity class for the NEAREST wall on the bias
        side, in percentage-distance terms only - explicitly a
        proximity estimate, not a time prediction (no historical
        velocity data is used to estimate real ETA in V1).
        """
        pool = self.nearest_above if self.bias == "SHORT" else self.nearest_below
        if pool is None:
            return "UNKNOWN"
        dist = abs(pool.distance_pct)
        if dist <= 1.0:
            return "Immediate"
        if dist <= 3.0:
            return "Near"
        if dist <= 6.0:
            return "Medium"
        return "Late"
