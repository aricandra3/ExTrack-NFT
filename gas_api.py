import aiohttp
import asyncio
from typing import Optional, Dict, Any
from config import ETHERSCAN_API_KEY


class GasAPI:
    """Client for Etherscan Gas Price API"""
    
    def __init__(self):
        self.api_key = ETHERSCAN_API_KEY
        self.base_url = "https://api.etherscan.io/api"
        self.timeout = aiohttp.ClientTimeout(total=10, connect=5)
    
    async def get_gas_price(self) -> Optional[Dict[str, Any]]:
        """
        Get current Ethereum gas prices from Etherscan.
        Returns dict with SafeGasPrice, ProposeGasPrice, FastGasPrice in Gwei.
        """
        if not self.api_key:
            return {"error": "Etherscan API key belum dikonfigurasi. Tambahkan ETHERSCAN_API_KEY ke .env"}
        
        url = f"{self.base_url}?module=gastracker&action=gasoracle&apikey={self.api_key}"
        
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("status") == "1":
                            result = data.get("result", {})
                            return {
                                "low": float(result.get("SafeGasPrice", 0)),
                                "average": float(result.get("ProposeGasPrice", 0)),
                                "fast": float(result.get("FastGasPrice", 0)),
                                "base_fee": float(result.get("suggestBaseFee", 0)),
                            }
                        else:
                            return {"error": data.get("message", "API error")}
                    else:
                        return {"error": f"HTTP error: {response.status}"}
        except asyncio.TimeoutError:
            return {"error": "Request timeout - Etherscan API lambat, coba lagi"}
        except aiohttp.ClientError as e:
            return {"error": f"Connection error: {str(e)}"}
        except Exception as e:
            return {"error": f"Error: {str(e)}"}
    
    def format_gas_price(self, gas_data: Dict[str, Any]) -> str:
        """Format gas price data into a readable message"""
        if gas_data is None:
            return "❌ Gagal mengambil data gas"
        
        if "error" in gas_data:
            return f"❌ {gas_data['error']}"
        
        low = gas_data.get("low", 0)
        average = gas_data.get("average", 0)
        fast = gas_data.get("fast", 0)
        base_fee = gas_data.get("base_fee", 0)
        
        # Estimate USD cost for a standard 21000 gas transfer
        # Using approximate ETH price of $2500 (this is just an estimate)
        eth_price_usd = 2500
        gas_limit = 21000
        
        def gwei_to_usd(gwei):
            eth_cost = (gwei * gas_limit) / 1e9
            return eth_cost * eth_price_usd
        
        message = f"""
⛽ **Ethereum Gas Prices**

🐢 **Low:** {low:.1f} gwei (~${gwei_to_usd(low):.2f})
🚶 **Average:** {average:.1f} gwei (~${gwei_to_usd(average):.2f})
🚀 **Fast:** {fast:.1f} gwei (~${gwei_to_usd(fast):.2f})

📊 **Base Fee:** {base_fee:.2f} gwei

_💡 Estimasi untuk transfer standar (21,000 gas)_
_Harga ETH: ~$2,500_
"""
        return message.strip()


# Singleton instance
gas_api = GasAPI()
