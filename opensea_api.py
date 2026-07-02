import aiohttp
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from urllib.parse import quote
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

    async def get_recent_sales(self, collection_slug: str, limit: int = 5) -> Optional[Dict[str, Any]]:
        """Get recent sale events for a collection."""
        safe_slug = quote(collection_slug, safe="")
        safe_limit = max(1, min(limit, 10))
        url = (
            f"{self.base_url}/events/collection/{safe_slug}"
            f"?event_type=sale&limit={safe_limit}"
        )

        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            return await self._make_request(url, session)

    async def get_collection_overview(self, collection_slug: str, sales_limit: int = 5):
        """Get stats, info, and recent sales in parallel for the richer Telegram UI."""
        safe_slug = quote(collection_slug, safe="")
        stats_url = f"{self.base_url}/collections/{safe_slug}/stats"
        info_url = f"{self.base_url}/collections/{safe_slug}"
        sales_url = (
            f"{self.base_url}/events/collection/{safe_slug}"
            f"?event_type=sale&limit={max(1, min(sales_limit, 10))}"
        )

        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            stats_task = self._make_request(stats_url, session)
            info_task = self._make_request(info_url, session)
            sales_task = self._make_request(sales_url, session)

            stats, info, sales = await asyncio.gather(stats_task, info_task, sales_task)
            return stats, info, sales
    
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

    def _escape_md(self, value: Any) -> str:
        """Escape Telegram legacy Markdown control characters in dynamic text."""
        text = str(value) if value is not None else ""
        return (
            text.replace("\\", "\\\\")
                .replace("_", "\\_")
                .replace("*", "\\*")
                .replace("`", "\\`")
                .replace("[", "\\[")
        )

    def _format_number(self, value: Any, decimals: int = 0) -> str:
        try:
            number = float(value)
            if number == 0:
                return "0"
            if decimals > 0:
                return f"{number:,.{decimals}f}".rstrip("0").rstrip(".")
            return f"{number:,.0f}"
        except (TypeError, ValueError):
            return "N/A"

    def _format_price(self, eth_amount: Any, symbol: str = "ETH",
                      eth_usd: float = 0, eth_idr: float = 0) -> str:
        try:
            amount = float(eth_amount or 0)
        except (TypeError, ValueError):
            amount = 0

        price = f"{amount:,.4f} {self._escape_md(symbol)}"
        fiat_parts = []
        if eth_usd:
            fiat_parts.append(f"${amount * eth_usd:,.2f}")
        if eth_idr:
            fiat_parts.append(f"Rp {amount * eth_idr:,.0f}")
        if fiat_parts:
            price += f" ({' / '.join(fiat_parts)})"
        return price

    def _get_interval(self, stats: Dict[str, Any], interval_name: str) -> Dict[str, Any]:
        for interval in stats.get("intervals", []) or []:
            if interval.get("interval") == interval_name:
                return interval
        return {}

    def _collection_links(self, collection_slug: str, collection_info: Optional[Dict[str, Any]]) -> List[str]:
        info = collection_info or {}
        links = []

        opensea_url = info.get("opensea_url") or f"https://opensea.io/collection/{collection_slug}"
        links.append(f"[OpenSea]({opensea_url})")

        website = info.get("project_url") or info.get("external_url")
        if website:
            links.append(f"[Website]({website})")

        twitter = info.get("twitter_username")
        if twitter:
            links.append(f"[X](https://x.com/{twitter.lstrip('@')})")

        discord = info.get("discord_url")
        if discord:
            links.append(f"[Discord]({discord})")

        return links

    def _relative_time(self, value: Any) -> str:
        if not value:
            return ""

        dt = None
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        else:
            text = str(value).replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(text)
            except ValueError:
                return ""

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        seconds = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
        if seconds < 60:
            return f"{seconds}s ago"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"

    def _extract_sale_events(self, sales_data: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not sales_data or "error" in sales_data:
            return []
        events = sales_data.get("asset_events") or sales_data.get("events") or []
        return events if isinstance(events, list) else []

    def _event_payment(self, event: Dict[str, Any]) -> tuple[float, str]:
        payment = event.get("payment") or event.get("payment_token") or {}
        symbol = payment.get("symbol") or event.get("payment_symbol") or "ETH"
        raw_quantity = payment.get("quantity") or event.get("quantity") or event.get("price") or 0
        decimals = payment.get("decimals")

        try:
            amount = float(raw_quantity)
            if decimals is not None and amount > 10_000:
                amount = amount / (10 ** int(decimals))
        except (TypeError, ValueError):
            amount = 0

        return amount, symbol

    def _format_recent_sales(self, sales_data: Optional[Dict[str, Any]],
                             eth_usd: float = 0, limit: int = 5) -> str:
        events = self._extract_sale_events(sales_data)
        if not events:
            return "🔥 *Recent Sales*\nBelum ada data sale terbaru dari OpenSea."

        lines = ["🔥 *Recent Sales*"]
        for event in events[:limit]:
            nft = event.get("nft") or event.get("asset") or {}
            token_id = (
                nft.get("identifier")
                or nft.get("token_id")
                or event.get("token_id")
                or "?"
            )
            amount, symbol = self._event_payment(event)
            timestamp = (
                event.get("event_timestamp")
                or event.get("created_date")
                or event.get("created_at")
                or event.get("timestamp")
            )
            time_text = self._relative_time(timestamp)
            usd_text = f" (~${amount * eth_usd:,.2f})" if eth_usd else ""
            suffix = f" — {time_text}" if time_text else ""
            lines.append(
                f"• `#{self._escape_md(token_id)}` — "
                f"*{amount:,.4f} {self._escape_md(symbol)}*{usd_text}{suffix}"
            )
        return "\n".join(lines)
    
    def format_floor_price(
        self,
        stats: Dict[str, Any],
        collection_info: Optional[Dict[str, Any]] = None,
        sales_data: Optional[Dict[str, Any]] = None,
        eth_price: Optional[Dict[str, Any]] = None,
        collection_slug: str = "",
    ) -> str:
        """Format collection data into a richer Telegram message inspired by embed UIs."""
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
        one_day = self._get_interval(stats, "one_day")
        volume_24h = one_day.get("volume", 0) or 0
        sales_24h = one_day.get("sales", 0) or 0
        avg_price_24h = one_day.get("average_price", 0) or 0
        eth_usd = eth_price.get("usd", 0) if eth_price and "error" not in eth_price else 0
        eth_idr = eth_price.get("idr", 0) if eth_price and "error" not in eth_price else 0

        # Collection name from info if available
        name = "Collection"
        if collection_info and "name" in collection_info:
            name = collection_info["name"]

        contracts = (collection_info or {}).get("primary_asset_contracts") or []
        first_contract = contracts[0] if contracts else {}
        chain = (
            (collection_info or {}).get("chain")
            or first_contract.get("chain_identifier")
            or first_contract.get("chain")
            or "ethereum"
        )
        created_date = (collection_info or {}).get("created_date") or (collection_info or {}).get("created_at")

        # Format numbers safely
        try:
            num_owners_str = f"{num_owners:,}" if isinstance(num_owners, (int, float)) else str(num_owners)
        except:
            num_owners_str = str(num_owners)
        
        try:
            total_supply_str = f"{total_supply:,}" if isinstance(total_supply, (int, float)) else str(total_supply)
        except:
            total_supply_str = str(total_supply)

        avg_vs_floor = ""
        if floor_price and avg_price_24h:
            diff_pct = ((avg_price_24h - floor_price) / floor_price) * 100
            relation = "above" if diff_pct > 0 else "below"
            avg_vs_floor = f" — {abs(diff_pct):.1f}% {relation} floor"

        lines = [
            f"🖼 *{self._escape_md(name)}*",
            "",
            f"⛓ `{self._escape_md(chain or 'ethereum')}` • 🧬 Supply *{total_supply_str}* • 👥 Holders *{num_owners_str}*",
        ]
        if created_date:
            lines.append(f"🗓 Listed: `{self._escape_md(str(created_date)[:10])}`")

        lines.extend([
            "",
            "📊 *Market*",
            f"💰 Floor: *{self._format_price(floor_price, floor_price_symbol, eth_usd, eth_idr)}*",
            f"💎 24h Volume: *{self._format_price(volume_24h, floor_price_symbol, eth_usd, eth_idr)}*",
            f"📈 Total Volume: *{self._format_price(total_volume, floor_price_symbol, eth_usd, eth_idr)}*",
            f"🧾 24h Sales: *{self._format_number(sales_24h)}*",
            f"💵 Avg Sale: *{self._format_price(avg_price_24h, floor_price_symbol, eth_usd, eth_idr)}*{avg_vs_floor}",
            "",
            self._format_recent_sales(sales_data, eth_usd=eth_usd),
            "",
            "🔗 *Links*",
            " • ".join(self._collection_links(collection_slug, collection_info)),
        ])

        return "\n".join(lines).strip()
    
    def format_volume_stats(self, stats: Dict[str, Any], collection_info: Optional[Dict[str, Any]] = None,
                            previous_volume: Optional[float] = None) -> str:
        """Format volume and sales stats into a readable message"""
        if stats is None:
            return "❌ Error: Gagal mengambil data"
        
        if "error" in stats:
            return f"❌ Error: {stats['error']}"
        
        total = stats.get("total", {})
        intervals = stats.get("intervals", [])
        
        # Get 24h data from intervals if available
        volume_24h = 0
        sales_24h = 0
        avg_price_24h = 0
        
        for interval in intervals:
            if interval.get("interval") == "one_day":
                volume_24h = interval.get("volume", 0) or 0
                sales_24h = interval.get("sales", 0) or 0
                avg_price_24h = interval.get("average_price", 0) or 0
                break
        
        # If no interval data, use total data
        if volume_24h == 0:
            volume_24h = total.get("volume", 0) or 0
            sales_24h = total.get("sales", 0) or 0
        
        floor_price_symbol = total.get("floor_price_symbol", "ETH")
        
        # Collection name from info if available
        name = "Collection"
        if collection_info and "name" in collection_info:
            name = collection_info["name"]
        
        # Calculate volume change if previous volume is provided
        volume_change_str = ""
        if previous_volume and previous_volume > 0:
            change_pct = ((volume_24h - previous_volume) / previous_volume) * 100
            emoji = "📈" if change_pct > 0 else "📉" if change_pct < 0 else "➡️"
            sign = "+" if change_pct > 0 else ""
            volume_change_str = f"\n📊 *Volume Change:* {sign}{change_pct:.1f}% {emoji}"
        
        message = f"""
📊 *Volume Stats: {name}*

💎 *Volume 24h:* {volume_24h:,.4f} {floor_price_symbol}
🛒 *Sales 24h:* {sales_24h}
💵 *Avg Price:* {avg_price_24h:,.4f} {floor_price_symbol}{volume_change_str}
"""
        return message.strip()


# Singleton instance
opensea_api = OpenSeaAPI()
