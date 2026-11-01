"""
Работа с Postgres (Neon). Хранит:
- settings: текущие настройки бота (цены, лимиты, шаблон, очередь, вкл/выкл)
- purchases: купленные аккаунты и их статус в конвейере
- daily_spend: сколько потрачено за сегодня (для дневного лимита)
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
        for k, v in DEFAULTS.items():
            await con.execute(
                "INSERT INTO settings (key, value) VALUES ($1, $2) "
                "ON CONFLICT (key) DO NOTHING",
                k, str(v)
            )
        # очередь и переключатель работы — тоже просто настройки
        await con.execute(
            "INSERT INTO settings (key, value) VALUES ('queue_remaining', '0') "
            "ON CONFLICT (key) DO NOTHING"
        )
        await con.execute(
            "INSERT INTO settings (key, value) VALUES ('worker_enabled', '1') "
            "ON CONFLICT (key) DO NOTHING"
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


# ---------- Очередь на покупку (одно число, можно прибавлять/убавлять) ----------

async def get_queue() -> int:
    val = await get_setting("queue_remaining")
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


async def add_queue(delta: int):
    """delta может быть отрицательным — убавляет очередь, но не ниже 0."""
    async with _pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                "SELECT value FROM settings WHERE key='queue_remaining' FOR UPDATE"
            )
            current = int(row["value"]) if row else 0
            new_val = max(0, current + delta)
            await con.execute(
                "INSERT INTO settings (key, value) VALUES ('queue_remaining', $1) "
                "ON CONFLICT (key) DO UPDATE SET value=$1",
                str(new_val)
            )
    return new_val


async def set_queue(value: int):
    value = max(0, value)
    await set_setting("queue_remaining", value)


async def queue_consume_one() -> bool:
    async with _pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                "SELECT value FROM settings WHERE key='queue_remaining' FOR UPDATE"
            )
            current = int(row["value"]) if row else 0
            if current <= 0:
                return False
            await con.execute(
                "UPDATE settings SET value=$1 WHERE key='queue_remaining'",
                str(current - 1)
            )
            return True


# ---------- Вкл/выкл рабочего цикла поиска ----------

async def is_enabled() -> bool:
    val = await get_setting("worker_enabled")
    return val == "1"


async def set_enabled(enabled: bool):
    await set_setting("worker_enabled", "1" if enabled else "0")


# ---------- Покупки ----------

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
