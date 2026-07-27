import asyncio
from datetime import datetime, timedelta
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

async def main():
    client = CryptoHistoricalDataClient()
    start_time = datetime.utcnow() - timedelta(days=5)
    req = CryptoBarsRequest(
        symbol_or_symbols="BTC/USD",
        timeframe=TimeFrame(15, TimeFrameUnit.Minute),
        limit=80,
        start=start_time
    )
    res = client.get_crypto_bars(req)
    print("Response keys:", res.data.keys())
    data = res.data.get("BTC/USD")
    print(f"Number of bars for BTC/USD: {len(data) if data else 0}")

if __name__ == "__main__":
    asyncio.run(main())
