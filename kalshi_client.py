import asyncio
import base64
import json
import time
import uuid
import os
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Any
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

class KalshiClient:
    """
    Standalone Kalshi Client (No external dependencies).
    Place this file in the same directory as your bot.
    """
    def __init__(self, email=None, password=None, api_key=None, private_key_path=None):
        # 1. LOAD CREDENTIALS (Prioritize arguments, then Environment Variables)
        self.api_key = api_key or os.getenv("KALSHI_API_KEY")
        self.base_url = "https://api.elections.kalshi.com"  # V2 API URL
        self.private_key_path = private_key_path or os.getenv("KALSHI_PRIVATE_KEY_PATH")
        
        # 2. LOAD KEY
        if not self.private_key_path:
            raise ValueError("Missing Private Key Path. Set KALSHI_PRIVATE_KEY_PATH env var or pass in init.")
            
        self.private_key = self._load_private_key(self.private_key_path)
        
        # 3. HTTP CLIENT
        self.client = httpx.AsyncClient(timeout=30.0)
        print("✅ Kalshi Client Initialized (Standalone Mode)")

    def _load_private_key(self, path_str):
        path = Path(path_str)
        if not path.exists():
            raise FileNotFoundError(f"❌ Private Key not found at: {path}")
        with open(path, "rb") as key_file:
            return serialization.load_pem_private_key(
                key_file.read(),
                password=None
            )

    def _sign_request(self, timestamp: str, method: str, path: str) -> str:
        msg = timestamp + method.upper() + path
        signature = self.private_key.sign(
            msg.encode('utf-8'),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size
            ),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode('utf-8')

    async def _request(self, method, endpoint, params=None, data=None):
        url = f"{self.base_url}{endpoint}"
        ts = str(int(time.time() * 1000))
        
        headers = {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._sign_request(ts, method, endpoint)
        }
        
        if params:
            url += "?" + urlencode(params)
            
        json_body = json.dumps(data) if data else None

        for attempt in range(5):
            try:
                resp = await self.client.request(method, url, headers=headers, content=json_body)
                if resp.status_code == 429:
                    await asyncio.sleep(0.5 * (2 ** attempt) + random.random())
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                if attempt == 4:
                    print(f"❌ API Error: {e}")
                await asyncio.sleep(0.5 * (2 ** attempt) + random.random())
        return {}

    # --- PUBLIC METHODS ---
    async def get_balance(self):
        return await self._request("GET", "/trade-api/v2/portfolio/balance")

    async def get_markets(self, **kwargs):
        return await self._request("GET", "/trade-api/v2/markets", params=kwargs)

    async def get_market(self, ticker):
        """Fetches a single market by ticker. Used for settlement verification."""
        return await self._request("GET", f"/trade-api/v2/markets/{ticker}")

    async def get_orderbook(self, ticker, depth=25):
        return await self._request("GET", f"/trade-api/v2/markets/{ticker}/orderbook", params={"depth": depth})

    async def create_order(self, ticker, action, type, count, price=None, side="yes"):
        payload = {
            "action": action, "count": count, "side": side, "ticker": ticker, 
            "type": type, "client_order_id": str(uuid.uuid4())
        }
        if type == 'limit':
            if not price: raise ValueError("Price required for limit orders")
            payload["yes_price"] = price if side == 'yes' else None
            payload["no_price"] = price if side == 'no' else None
            
        return await self._request("POST", "/trade-api/v2/portfolio/orders", data=payload)

    async def cancel_order(self, order_id: str):
        """
        Cancels an open order on Kalshi.
        Required for the dynamic 'Maker' replace logic.
        """
        endpoint = f"/trade-api/v2/portfolio/orders/{order_id}"
        return await self._request("DELETE", endpoint)
    
    async def get_order(self, order_id: str):
        """Fetches the current status and fill details of an order."""
        endpoint = f"/trade-api/v2/portfolio/orders/{order_id}"
        return await self._request("GET", endpoint)
