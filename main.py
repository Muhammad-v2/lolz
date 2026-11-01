import asyncio
import logging
import os

from aiohttp import web

import db
from bot import bot, dp, send_log
from worker import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


async def handle_ping(request):
    return web.Response(text="OK")


async def run_web_server():
    """Крошечный веб-сервер — нужен только для того, чтобы Render
    считал сервис рабочим (бесплатный тариф требует открытый порт)."""
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def main():
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
