
import os
import psycopg2
from config import logger

# ── Connection pool (lazily initialised) ────────────────────────────────────────
_pool = None
_db_url = None


def _get_pool():
    """Lazily create a thread-safe connection pool on first use."""
    global _pool, _db_url
    if _pool is not None:
        return _pool
    _db_url = os.getenv("DATABASE_URL")
    if not _db_url:
        return None
    try:
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, _db_url)
        logger.info("📘 DB connection pool created (min=1, max=10)")
    except Exception as e:
        logger.warning(f"DB pool creation failed: {e}")
        _pool = None
    return _pool


def _get_conn():
    """Get a connection from the pool, or None if DB is unavailable."""
    p = _get_pool()
    if p is None:
        return None
    try:
        return p.getconn()
    except Exception as e:
        logger.error(f"DB connection checkout failed: {e}")
        return None


def _put_conn(conn):
    """Return a connection to the pool."""
    if conn is not None and _pool is not None:
        try:
            _pool.putconn(conn)
        except Exception:
            pass


def init_db():
    if not _get_pool():
        return
    conn = None
    try:
        conn = _get_conn()
        if conn is None:
            return
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS equity_history (
                id SERIAL PRIMARY KEY, bot_name TEXT, equity NUMERIC, timestamp TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS trades (
                id SERIAL PRIMARY KEY, bot_name TEXT, exchange TEXT DEFAULT 'Alpaca',
                symbol TEXT, side TEXT, price NUMERIC, quantity NUMERIC,
                value NUMERIC, fee NUMERIC DEFAULT 0, fill_price NUMERIC,
                order_id TEXT, timestamp TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS realized_pnl (
                id SERIAL PRIMARY KEY, bot_name TEXT, symbol TEXT,
                side TEXT, entry_price NUMERIC, exit_price NUMERIC,
                qty NUMERIC, realized_pnl NUMERIC,
                gross_pnl NUMERIC, fee_total NUMERIC,
                order_id TEXT, timestamp TIMESTAMP DEFAULT NOW())""")
        conn.commit()
        logger.info("DB initialised")
    except Exception as e:
        logger.warning(f"DB init failed: {e}")
    finally:
        _put_conn(conn)


def record_trade(bot_name, symbol, side, qty, price, fill_price=None, fee=0.0, order_id=None):
    conn = None
    try:
        conn = _get_conn()
        if conn is None:
            return
        with conn.cursor() as cur:
            value = (price or 0) * qty
            cur.execute("""INSERT INTO trades (bot_name,exchange,symbol,side,price,quantity,value,fee,fill_price,order_id,timestamp)
                VALUES (%s,'Alpaca',%s,%s,%s,%s,%s,%s,%s,NOW())""",
                (bot_name, symbol, side, price or 0, qty, value, fee, fill_price, str(order_id) if order_id else None))
        conn.commit()
    except Exception as e:
        logger.error(f"DB trade failed: {e}")
    finally:
        _put_conn(conn)


def record_realized_pnl(bot_name, symbol, side, entry_price, exit_price, qty,
                        realized_pnl, gross_pnl, fee_total, order_id=None):
    conn = None
    try:
        conn = _get_conn()
        if conn is None:
            return
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO realized_pnl
                (bot_name, symbol, side, entry_price, exit_price, qty,
                 realized_pnl, gross_pnl, fee_total, order_id, timestamp)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
                (bot_name, symbol, side, entry_price, exit_price, qty,
                 realized_pnl, gross_pnl, fee_total, str(order_id) if order_id else None))
        conn.commit()
    except Exception as e:
        logger.error(f"DB realized PnL failed: {e}")
    finally:
        _put_conn(conn)


def report_equity(bot_name, equity):
    conn = None
    try:
        conn = _get_conn()
        if conn is None:
            return
        with conn.cursor() as cur:
            cur.execute("INSERT INTO equity_history (bot_name,equity,timestamp) VALUES (%s,%s,NOW())",
                (bot_name, float(equity)))
        conn.commit()
    except Exception as e:
        logger.error(f"DB equity failed: {e}")
    finally:
        _put_conn(conn)
