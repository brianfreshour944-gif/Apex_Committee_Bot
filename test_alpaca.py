import asyncio
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

async def main():
    client = CryptoHistoricalDataClient()
    req = CryptoBarsRequest(
        symbol_or_symbols="BTC/USD",
        timeframe=TimeFrame(15, TimeFrameUnit.Minute),
        limit=10,
    )
    res = client.get_crypto_bars(req)
    print("Response keys:", res.data.keys())
    print("Data for BTC/USD:", res.data.get("BTC/USD"))
    print("Data for BTCUSD:", res.data.get("BTCUSD"))

if __name__ == "__main__":
    asyncio.run(main())
