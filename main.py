import asyncio
import logging

import db
from bot import bot, dp, send_log
from worker import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


async def main():
    await db.init_db()
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
