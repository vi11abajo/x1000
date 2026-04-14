from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("x1000.hyperliquid")

HL_API_URL = "https://api.hyperliquid.xyz/info"

# Coin mapping: OKX instId -> Hyperliquid coin name
COIN_MAP = {
    "BTC-USDT-SWAP": "BTC",
    "ETH-USDT-SWAP": "ETH",
    "SOL-USDT-SWAP": "SOL",
    "HYPE-USDT-SWAP": "HYPE",
}


@dataclass
class LiquidationCluster:
    """Liquidation cluster data for a single asset."""
    coin: str
    price: float
    # Clusters above price (short-liq zones)
    above_clusters: list[dict] = field(default_factory=list)
    # Clusters below price (long-liq zones)
    below_clusters: list[dict] = field(default_factory=list)
    # Nearest significant cluster distance %
    nearest_above_pct: float | None = None
    nearest_below_pct: float | None = None


@dataclass
class WhaleFlow:
    """Whale flow data for a single asset."""
    coin: str
    price: float
    # Recent large trades
    large_trades: list[dict] = field(default_factory=list)
    # Buy/sell ratio of large trades
    buy_pct: float = 50.0
    sell_pct: float = 50.0
    # Total large trade volume (USD)
    total_large_usd: float = 0.0
    # Signal: bullish/bearish/neutral
    signal: str = "neutral"


class HyperliquidClient:
    """Fetch liquidation clusters and whale flow from Hyperliquid API."""

    def __init__(self):
        self._cache: dict[str, dict] = {}
        self._cache_time: float = 0
        self._cache_ttl = 60  # cache for 60 seconds

    def _post(self, data: dict) -> Any:
        """POST to Hyperliquid info endpoint."""
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            HL_API_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def _get_coin(self, inst_id: str) -> str | None:
        """Map OKX instId to Hyperliquid coin name."""
        return COIN_MAP.get(inst_id)

    def get_liquidation_clusters(self, inst_id: str, price: float) -> LiquidationCluster | None:
        """Get liquidation cluster zones from order book depth.

        Large walls in the order book represent potential liquidation zones:
        - Large bid walls below price = long-liq clusters (price may sweep them)
        - Large ask walls above price = short-liq clusters (price may sweep them)
        """
        coin = self._get_coin(inst_id)
        if not coin:
            return None

        try:
            data = self._post({"type": "l2Book", "coin": coin})
            levels = data.get("levels", [])
            if len(levels) < 2:
                return None

            bids = levels[0]  # bid side
            asks = levels[1]  # ask side

            cluster = LiquidationCluster(coin=coin, price=price)

            # Find significant walls (> 2 BTC for BTC, scaled for others)
            # For smaller coins, scale threshold by price
            wall_threshold = max(2.0, 100000 / price)  # min $100k or 2 BTC

            # Ask walls above price = short-liq clusters
            for a in asks[:20]:
                sz = float(a["sz"])
                px = float(a["px"])
                if sz >= wall_threshold and px > price:
                    dist_pct = (px - price) / price * 100
                    cluster.above_clusters.append({
                        "price": px,
                        "size": sz,
                        "value_usd": sz * px,
                        "dist_pct": round(dist_pct, 3),
                    })

            # Bid walls below price = long-liq clusters
            for b in bids[:20]:
                sz = float(b["sz"])
                px = float(b["px"])
                if sz >= wall_threshold and px < price:
                    dist_pct = (price - px) / price * 100
                    cluster.below_clusters.append({
                        "price": px,
                        "size": sz,
                        "value_usd": sz * px,
                        "dist_pct": round(dist_pct, 3),
                    })

            # Nearest cluster distances
            if cluster.above_clusters:
                cluster.nearest_above_pct = min(c["dist_pct"] for c in cluster.above_clusters)
            if cluster.below_clusters:
                cluster.nearest_below_pct = min(c["dist_pct"] for c in cluster.below_clusters)

            return cluster

        except Exception as e:
            log.warning("Hyperliquid liquidation fetch failed for %s: %s", inst_id, e)
            return None

    def get_whale_flow(self, inst_id: str) -> WhaleFlow | None:
        """Get whale flow from recent large trades.

        Large trades (>$100k) indicate institutional/whale activity.
        Buy/sell ratio shows directional consensus.
        """
        coin = self._get_coin(inst_id)
        if not coin:
            return None

        try:
            data = self._post({"type": "recentTrades", "coin": coin, "limit": 50})
            if not isinstance(data, list):
                return None

            whale = WhaleFlow(coin=coin, price=0.0)
            large_threshold_usd = 100000  # $100k per trade

            prices = []
            for t in data:
                px = float(t.get("px", 0))
                sz = float(t.get("sz", 0))
                prices.append(px)
                usd_value = sz * px
                if usd_value >= large_threshold_usd:
                    whale.large_trades.append({
                        "side": t.get("side", "?"),
                        "size": sz,
                        "price": px,
                        "usd": usd_value,
                        "time": t.get("time", 0),
                    })

            if prices:
                whale.price = prices[0]

            if whale.large_trades:
                buys = [t for t in whale.large_trades if t["side"] == "B"]
                sells = [t for t in whale.large_trades if t["side"] == "A"]
                total = len(whale.large_trades)
                whale.buy_pct = round(len(buys) / total * 100, 1)
                whale.sell_pct = round(len(sells) / total * 100, 1)
                whale.total_large_usd = sum(t["usd"] for t in whale.large_trades)

                # Determine signal
                if whale.buy_pct >= 70:
                    whale.signal = "bullish"
                elif whale.sell_pct >= 70:
                    whale.signal = "bearish"
                elif 40 <= whale.buy_pct <= 60:
                    whale.signal = "split"  # 50/50 = uncertainty
                else:
                    whale.signal = "neutral"

            return whale

        except Exception as e:
            log.warning("Hyperliquid whale flow fetch failed for %s: %s", inst_id, e)
            return None

    def get_all_data(self, assets: list[str], prices: dict[str, float]) -> dict[str, dict]:
        """Fetch liquidation + whale data for all assets. Returns structured dict."""
        result = {}
        for inst_id in assets:
            price = prices.get(inst_id, 0)
            liq = self.get_liquidation_clusters(inst_id, price)
            whale = self.get_whale_flow(inst_id)

            data = {}
            if liq:
                data["liq_nearest_above_pct"] = liq.nearest_above_pct
                data["liq_nearest_below_pct"] = liq.nearest_below_pct
                data["liq_above_count"] = len(liq.above_clusters)
                data["liq_below_count"] = len(liq.below_clusters)
                if liq.above_clusters:
                    data["liq_above_1pct"] = any(c["dist_pct"] < 1.5 for c in liq.above_clusters)
                if liq.below_clusters:
                    data["liq_below_1pct"] = any(c["dist_pct"] < 1.0 for c in liq.below_clusters)

            if whale:
                data["whale_signal"] = whale.signal
                data["whale_buy_pct"] = whale.buy_pct
                data["whale_sell_pct"] = whale.sell_pct
                data["whale_large_count"] = len(whale.large_trades)
                data["whale_total_usd"] = round(whale.total_large_usd, 0)

            result[inst_id] = data

        return result
