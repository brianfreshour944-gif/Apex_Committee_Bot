import pytest
from unittest.mock import MagicMock, patch
from alpaca.trading.enums import OrderSide
from datetime import datetime, timezone, timedelta

# Import functions to test
from orders import place_order, get_stale_open_orders, cancel_stale_order
import config

@pytest.mark.asyncio
async def test_buy_slippage_buffer_calculation():
    # Mock trading_client.submit_order
    mock_order = MagicMock()
    mock_order.id = "mock_order_123"
    mock_order.filled_avg_price = None
    mock_order.filled_qty = None
    
    with patch("orders.trading_client") as mock_client:
        mock_client.submit_order.return_value = mock_order
        
        # Test BUY order
        # price = 10000.0
        # BUY_SLIPPAGE_BUFFER = 0.003
        # raw_limit = 10000.0 * 1.003 = 10030.0
        res = await place_order(symbol="BTC/USD", side=OrderSide.BUY, qty=0.1, price=10000.0)
        
        assert res is not None
        assert res["success"] is True
        assert res["order_id"] == "mock_order_123"
        
        # Verify the arguments passed to submit_order
        mock_client.submit_order.assert_called_once()
        order_data = mock_client.submit_order.call_args[1]["order_data"]
        
        # Check that limit_price is calculated using the BUY buffer (0.003 -> 10029.99 due to float representation and ROUND_DOWN)
        assert order_data.limit_price == 10029.99

@pytest.mark.asyncio
async def test_sell_slippage_buffer_calculation():
    # Mock trading_client.submit_order
    mock_order = MagicMock()
    mock_order.id = "mock_order_456"
    mock_order.filled_avg_price = None
    mock_order.filled_qty = None
    
    with patch("orders.trading_client") as mock_client:
        mock_client.submit_order.return_value = mock_order
        
        # Test SELL order
        # price = 10000.0
        # SELL_SLIPPAGE_BUFFER = 0.002
        # raw_limit = 10000.0 * 0.998 = 9980.0
        res = await place_order(symbol="BTC/USD", side=OrderSide.SELL, qty=0.1, price=10000.0)
        
        assert res is not None
        assert res["success"] is True
        assert res["order_id"] == "mock_order_456"
        
        # Verify the arguments passed to submit_order
        mock_client.submit_order.assert_called_once()
        order_data = mock_client.submit_order.call_args[1]["order_data"]
        
        # Check limit price
        assert order_data.limit_price == 9980.0

@pytest.mark.asyncio
async def test_get_stale_open_orders():
    # Setup mock orders
    mock_order_fresh = MagicMock()
    mock_order_fresh.id = "fresh_1"
    mock_order_fresh.created_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    
    mock_order_stale = MagicMock()
    mock_order_stale.id = "stale_2"
    mock_order_stale.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
    
    with patch("orders.trading_client") as mock_client:
        mock_client.get_orders.return_value = [mock_order_fresh, mock_order_stale]
        
        # Request orders older than 10 minutes (600 seconds)
        stale_orders = await get_stale_open_orders(symbol="BTC/USD", max_age_seconds=600)
        
        # Should only return the stale one
        assert len(stale_orders) == 1
        assert stale_orders[0].id == "stale_2"
        
        # Verify get_orders was called with the correct symbol
        mock_client.get_orders.assert_called_once()
        filter_data = mock_client.get_orders.call_args[1]["filter"]
        assert filter_data.symbols == ["BTC/USD"]

@pytest.mark.asyncio
async def test_cancel_stale_order_success():
    with patch("orders.trading_client") as mock_client:
        mock_client.cancel_order_by_id.return_value = None
        
        success = await cancel_stale_order(order_id="mock_order_to_cancel")
        
        assert success is True
        mock_client.cancel_order_by_id.assert_called_once_with("mock_order_to_cancel")

@pytest.mark.asyncio
async def test_cancel_stale_order_failure():
    with patch("orders.trading_client") as mock_client:
        mock_client.cancel_order_by_id.side_effect = Exception("API Connection error")
        
        success = await cancel_stale_order(order_id="mock_order_fail")
        
        assert success is False
        mock_client.cancel_order_by_id.assert_called_once_with("mock_order_fail")
