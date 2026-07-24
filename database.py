# database.py — PostgreSQL equity + trade logging.
import os, psycopg2
from config import logger


def init_db():
    db_url = os.getenv("DATABASE_URL")
    if not db_url: return
    try:
        with psycopg2.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS equity_history (
                    id SERIAL PRIMARY KEY, bot_name TEXT, equity NUMERIC, timestamp TIMESTAMP DEFAULT NOW())""")
                cur.execute("""CREATE TABLE IF NOT EXISTS trades (
                    id SERIAL PRIMARY KEY, bot_name TEXT, exchange TEXT DEFAULT 'Alpaca',
                    symbol TEXT, side TEXT, price NUMERIC, quantity NUMERIC,
                    value NUMERIC, fee NUMERIC DEFAULT 0, order_id TEXT, timestamp TIMESTAMP DEFAULT NOW())""")
            conn.commit()
        logger.info("📘 DB initialised")
    except Exception as e:
        logger.warning(f"DB init failed: {e}")


def record_trade(bot_name, symbol, side, qty, price, pnl_pct=None, order_id=None):
    db_url = os.getenv("DATABASE_URL")
    if not db_url: return
    try:
        with psycopg2.connect(db_url) as conn:
            with conn.cursor() as cur:
                value = (price or 0) * qty
                cur.execute("""INSERT INTO trades (bot_name,exchange,symbol,side,price,quantity,value,fee,order_id,timestamp)
                    VALUES (%s,'Alpaca',%s,%s,%s,%s,%s,0,%s,NOW())""",
                    (bot_name, symbol, side, price or 0, qty, value, str(order_id) if order_id else None))
            conn.commit()
    except Exception as e:
        logger.error(f"DB trade failed: {e}")


def report_equity(bot_name, equity):
    db_url = os.getenv("DATABASE_URL")
    if not db_url: return
    try:
        with psycopg2.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO equity_history (bot_name,equity,timestamp) VALUES (%s,%s,NOW())",
                    (bot_name, float(equity)))
            conn.commit()
    except Exception as e:
        logger.error(f"DB equity failed: {e}")
