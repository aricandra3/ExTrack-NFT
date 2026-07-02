import aiohttp
import asyncio
import time
from typing import Optional, Dict, Any


class PriceAPI:
    """Client for CoinGecko API - ETH/IDR price converter"""

    def __init__(self):
        self.base_url = "https://api.coingecko.com/api/v3"
        self.timeout = aiohttp.ClientTimeout(total=10, connect=5)
        # Cache to avoid rate limiting (CoinGecko free tier: 10-30 req/min)
        self._cache: Dict[str, Any] = {}
        self._cache_ttl = 60  # Cache for 60 seconds

    def _get_cached(self, key: str) -> Optional[Dict[str, Any]]:
        """Get cached data if still valid."""
        if key in self._cache:
            data, timestamp = self._cache[key]
            if time.time() - timestamp < self._cache_ttl:
                return data
        return None

    def _set_cache(self, key: str, data: Dict[str, Any]):
        """Cache data with timestamp."""
        self._cache[key] = (data, time.time())

    async def get_eth_price(self) -> Optional[Dict[str, Any]]:
        """
        Get current ETH price in multiple currencies (USD, IDR).
        Returns dict with prices and 24h change.
        """
        cache_key = "eth_price"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        url = (
            f"{self.base_url}/simple/price"
            f"?ids=ethereum"
            f"&vs_currencies=usd,idr"
            f"&include_24hr_change=true"
            f"&include_24hr_vol=true"
            f"&include_market_cap=true"
        )

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        eth_data = data.get("ethereum", {})
                        result = {
                            "usd": eth_data.get("usd", 0),
                            "idr": eth_data.get("idr", 0),
                            "usd_24h_change": eth_data.get("usd_24h_change", 0),
                            "idr_24h_change": eth_data.get("idr_24h_change", 0),
                            "usd_24h_vol": eth_data.get("usd_24h_vol", 0),
                            "usd_market_cap": eth_data.get("usd_market_cap", 0),
                        }
                        self._set_cache(cache_key, result)
                        return result
                    elif response.status == 429:
                        return {"error": "Rate limit exceeded. Coba lagi dalam 1 menit."}
                    else:
                        return {"error": f"API error: {response.status}"}
        except asyncio.TimeoutError:
            return {"error": "Request timeout - CoinGecko API lambat, coba lagi"}
        except aiohttp.ClientError as e:
            return {"error": f"Connection error: {str(e)}"}
        except Exception as e:
            return {"error": f"Error: {str(e)}"}

    def format_eth_price(self, price_data: Dict[str, Any]) -> str:
        """Format ETH price data into a readable message."""
        if price_data is None:
            return "❌ Gagal mengambil data harga ETH"

        if "error" in price_data:
            return f"❌ {price_data['error']}"

        usd = price_data.get("usd", 0)
        idr = price_data.get("idr", 0)
        usd_change = price_data.get("usd_24h_change", 0)
        idr_change = price_data.get("idr_24h_change", 0)
        market_cap = price_data.get("usd_market_cap", 0)

        # Format change emoji
        usd_emoji = "🟢" if usd_change >= 0 else "🔴"
        idr_emoji = "🟢" if idr_change >= 0 else "🔴"
        usd_sign = "+" if usd_change >= 0 else ""
        idr_sign = "+" if idr_change >= 0 else ""

        message = (
            "💱 *Ethereum Price*\n"
            "Live market rate\n\n"
            "📊 *Market*\n"
            f"🇺🇸 USD: *${usd:,.2f}* ({usd_sign}{usd_change:.2f}% {usd_emoji})\n"
            f"🇮🇩 IDR: *Rp {idr:,.0f}* ({idr_sign}{idr_change:.2f}% {idr_emoji})\n"
            f"🏦 Market Cap: *${market_cap:,.0f}*\n\n"
            "🔁 *Quick Convert*\n"
            "Gunakan `/convert 0.5` untuk konversi ETH."
        )

        return message

    def convert_eth_to_idr(self, eth_amount: float, idr_rate: float) -> str:
        """Convert ETH amount to IDR string."""
        idr_value = eth_amount * idr_rate
        return f"Rp {idr_value:,.0f}"

    def convert_eth_to_usd(self, eth_amount: float, usd_rate: float) -> str:
        """Convert ETH amount to USD string."""
        usd_value = eth_amount * usd_rate
        return f"${usd_value:,.2f}"


# Singleton instance
price_api = PriceAPI()
