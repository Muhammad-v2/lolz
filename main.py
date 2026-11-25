import asyncio
import logging
import os
from aiohttp import web

from config import ADMIN_ID, API_HASH, API_ID, BOT_TOKEN, DATABASE_URL, LOLZ_TOKEN
import db
from bot import bot, dp, send_log
from worker import Worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


async def handle_ping(request):
    return web.Response(text="OK")


async def run_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


def _check_env():
    missing = []
    if not LOLZ_TOKEN:
        missing.append("LOLZ_TOKEN")
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not ADMIN_ID:
        missing.append("ADMIN_ID")
    if not API_ID or not API_HASH:
        missing.append("API_ID / API_HASH")
    if not DATABASE_URL:
        missing.append("DATABASE_URL")
    if missing:
        raise RuntimeError(f"Не заполнены обязательные переменные окружения: {', '.join(missing)}")


async def main():
    _check_env()
    await db.init_db()
    await run_web_server()
    worker = Worker(send_log)
    worker_task = asyncio.create_task(worker.run_forever())
    try:
        await send_log("🤖 Бот запущен")
        await dp.start_polling(bot)
    finally:
        await worker.stop()
        worker_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
