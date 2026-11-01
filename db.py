"""
Работа с Postgres (Neon). Хранит:
- settings: текущие настройки бота (цены, лимиты, шаблон)
- purchases: купленные аккаунты и их статус в конвейере
- daily_spend: сколько потрачено за сегодня (для дневного лимита)
- logs: лог событий для истории (дублирует то, что шлём в Telegram)
"""
import asyncpg
import datetime
from config import DATABASE_URL, DEFAULTS

_pool: asyncpg.Pool | None = None


async def init_db():
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with _pool.acquire() as con:
        await con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        await con.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id SERIAL PRIMARY KEY,
                item_id BIGINT UNIQUE NOT NULL,
                price NUMERIC NOT NULL,
                phone TEXT,
                country TEXT,
                country_code TEXT,
                status TEXT NOT NULL DEFAULT 'bought',
                error TEXT,
                bought_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            );
        """)
        await con.execute("""
            CREATE TABLE IF NOT EXISTS daily_spend (
                day DATE PRIMARY KEY,
                spent NUMERIC NOT NULL DEFAULT 0
            );
        """)
        await con.execute("""
            CREATE TABLE IF NOT EXISTS buy_queue (
                id SERIAL PRIMARY KEY,
                remaining INT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            );
        """)
        # дефолтные настройки, если их ещё нет
        for k, v in DEFAULTS.items():
            await con.execute(
                "INSERT INTO settings (key, value) VALUES ($1, $2) "
                "ON CONFLICT (key) DO NOTHING",
                k, str(v)
            )


async def get_setting(key: str) -> str:
    async with _pool.acquire() as con:
        row = await con.fetchrow("SELECT value FROM settings WHERE key=$1", key)
        return row["value"] if row else str(DEFAULTS.get(key, ""))


async def get_settings() -> dict:
    async with _pool.acquire() as con:
        rows = await con.fetch("SELECT key, value FROM settings")
        return {r["key"]: r["value"] for r in rows}


async def set_setting(key: str, value):
    async with _pool.acquire() as con:
        await con.execute(
            "INSERT INTO settings (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value=$2",
            key, str(value)
        )


async def get_today_spent() -> float:
    today = datetime.date.today()
    async with _pool.acquire() as con:
        row = await con.fetchrow("SELECT spent FROM daily_spend WHERE day=$1", today)
        return float(row["spent"]) if row else 0.0


async def add_spent(amount: float):
    today = datetime.date.today()
    async with _pool.acquire() as con:
        await con.execute("""
            INSERT INTO daily_spend (day, spent) VALUES ($1, $2)
            ON CONFLICT (day) DO UPDATE SET spent = daily_spend.spent + $2
        """, today, amount)


async def queue_add(count: int):
    async with _pool.acquire() as con:
        await con.execute("INSERT INTO buy_queue (remaining) VALUES ($1)", count)


async def queue_remaining() -> int:
    async with _pool.acquire() as con:
        row = await con.fetchrow("SELECT COALESCE(SUM(remaining),0) AS total FROM buy_queue")
        return int(row["total"])


async def queue_consume_one():
    """Списывает одну единицу из самой старой незакрытой записи очереди."""
    async with _pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                "SELECT id, remaining FROM buy_queue WHERE remaining > 0 "
                "ORDER BY created_at LIMIT 1 FOR UPDATE"
            )
            if not row:
                return False
            await con.execute(
                "UPDATE buy_queue SET remaining = remaining - 1 WHERE id=$1",
                row["id"]
            )
            return True


async def add_purchase(item_id: int, price: float, phone: str, country: str, country_code: str):
    async with _pool.acquire() as con:
        await con.execute("""
            INSERT INTO purchases (item_id, price, phone, country, country_code, status)
            VALUES ($1, $2, $3, $4, $5, 'bought')
            ON CONFLICT (item_id) DO NOTHING
        """, item_id, price, phone, country, country_code)


async def update_purchase_status(item_id: int, status: str, error: str | None = None):
    async with _pool.acquire() as con:
        await con.execute("""
            UPDATE purchases SET status=$2, error=$3, updated_at=now()
            WHERE item_id=$1
        """, item_id, status, error)


async def recent_purchases(limit: int = 10):
    async with _pool.acquire() as con:
        return await con.fetch(
            "SELECT * FROM purchases ORDER BY bought_at DESC LIMIT $1", limit
        )
