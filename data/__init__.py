"""
QDE V1 - data package

Single source of truth for all Binance Futures market data. No
indicator or engine module talks to the exchange or cache directly -
everything consumes DataCollector.fetch() output (models.MarketData).
"""

from data.exchange import ExchangeClient
from data.cache import MarketCache
from data.collector import DataCollector

__all__ = ["ExchangeClient", "MarketCache", "DataCollector"]
