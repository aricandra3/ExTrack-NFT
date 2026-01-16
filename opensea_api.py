import aiohttp
import asyncio
from typing import Optional, Dict, Any
from config import OPENSEA_API_KEY, OPENSEA_API_BASE_URL


class OpenSeaAPI:
    """Client for OpenSea API v2 - Optimized for speed"""
    
    def __init__(self):
        self.base_url = OPENSEA_API_BASE_URL
        self.headers = {
            "Accept": "application/json",
        }
        if OPENSEA_API_KEY:
            self.headers["X-API-KEY"] = OPENSEA_API_KEY
        
        # Timeout settings for faster response
        self.timeout = aiohttp.ClientTimeout(total=10, connect=5)
    
    async def _make_request(self, url: str, session: aiohttp.ClientSession) -> Optional[Dict[str, Any]]:
        """Make a single API request with error handling"""
        try:
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 401:
                    return {"error": "Unauthorized - API key tidak valid atau tidak ada"}
                elif response.status == 404:
                    return {"error": "Collection not found"}
                elif response.status == 429:
                    return {"error": "Rate limit exceeded. Please try again later."}
                else:
                    return {"error": f"API error: {response.status}"}
        except asyncio.TimeoutError:
            return {"error": "Request timeout - OpenSea API lambat, coba lagi"}
        except aiohttp.ClientError as e:
            return {"error": f"Connection error: {str(e)}"}
    
    async def get_collection_stats(self, collection_slug: str) -> Optional[Dict[str, Any]]:
        """Get collection statistics including floor price"""
        url = f"{self.base_url}/collections/{collection_slug}/stats"
        
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            return await self._make_request(url, session)
    
    async def get_collection_info(self, collection_slug: str) -> Optional[Dict[str, Any]]:
        """Get collection information"""
        url = f"{self.base_url}/collections/{collection_slug}"
        
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            return await self._make_request(url, session)
    
    async def get_floor_price_fast(self, collection_slug: str) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        """
        Get stats and info in parallel for faster response
        Returns (stats, info) tuple
        """
        stats_url = f"{self.base_url}/collections/{collection_slug}/stats"
        info_url = f"{self.base_url}/collections/{collection_slug}"
        
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            # Run both requests in parallel
            stats_task = self._make_request(stats_url, session)
            info_task = self._make_request(info_url, session)
            
            stats, info = await asyncio.gather(stats_task, info_task)
            return stats, info
    
    def format_floor_price(self, stats: Dict[str, Any], collection_info: Optional[Dict[str, Any]] = None) -> str:
        """Format floor price data into a readable message"""
        if stats is None:
            return "❌ Error: Gagal mengambil data"
        
        if "error" in stats:
            return f"❌ Error: {stats['error']}"
        
        total = stats.get("total", {})
        floor_price = total.get("floor_price", 0) or 0
        floor_price_symbol = total.get("floor_price_symbol", "ETH")
        num_owners = total.get("num_owners", "N/A")
        total_supply = total.get("total_supply", "N/A")
        total_volume = total.get("volume", 0) or 0
        
        # Collection name from info if available
        name = "Collection"
        if collection_info and "name" in collection_info:
            name = collection_info["name"]
        
        # Format numbers safely
        try:
            num_owners_str = f"{num_owners:,}" if isinstance(num_owners, (int, float)) else str(num_owners)
        except:
            num_owners_str = str(num_owners)
        
        try:
            total_supply_str = f"{total_supply:,}" if isinstance(total_supply, (int, float)) else str(total_supply)
        except:
            total_supply_str = str(total_supply)
        
        message = f"""
📊 **{name}**

💰 **Floor Price:** {floor_price:.4f} {floor_price_symbol}
👥 **Owners:** {num_owners_str}
📦 **Total Supply:** {total_supply_str}
📈 **Total Volume:** {total_volume:,.2f} {floor_price_symbol}
"""
        return message.strip()


# Singleton instance
opensea_api = OpenSeaAPI()
