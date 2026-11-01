"""
Фоновый цикл: раз в N секунд проверяет очередь покупок, ищет подходящие
лоты, покупает и прогоняет аккаунт через весь конвейер до выставления
на продажу. Все шаги и ошибки отправляются админу через send_log().
"""
import asyncio
import logging
from lolz_client import LolzClient, LolzError
from telethon_actions import (
    connect_from_hex_dc, terminate_other_sessions,
    create_new_session, get_auth_hex_and_dc, terminate_this_session,
    cleanup_account, AccountPipelineError,
)
import db

log = logging.getLogger("worker")


class Worker:
    def __init__(self, send_log):
        self.send_log = send_log  # async fn(text) -> отправка сообщения админу
        self.lolz = LolzClient()
        self._running = False

    async def stop(self):
        self._running = False
        await self.lolz.close()

    async def run_forever(self):
        self._running = True
        while self._running:
            try:
                if await db.is_enabled():
                    await self._tick()
            except Exception as e:
                log.exception("Ошибка в рабочем цикле")
                await self.send_log(f"⚠️ Ошибка в рабочем цикле: {e}")
            interval = int(await db.get_setting("search_interval_sec"))
            await asyncio.sleep(interval)

    async def _tick(self):
        remaining = await db.get_queue()
        if remaining <= 0:
            return  # в очереди пусто — нечего искать, молчим

        daily_limit = float(await db.get_setting("daily_limit"))
        spent_today = await db.get_today_spent()
        if spent_today >= daily_limit:
            return  # лимит на сегодня исчерпан — ждём, пока админ его повысит

        price_min = float(await db.get_setting("buy_min_price"))
        price_max = float(await db.get_setting("buy_max_price"))
        spam = await db.get_setting("filter_spam")
        password = await db.get_setting("filter_password")
        session_age_value = int(await db.get_setting("session_age_value"))
        session_age_period = await db.get_setting("session_age_period")

        max_affordable = daily_limit - spent_today
        price_max = min(price_max, max_affordable)
        if price_max < price_min:
            return

        items = await self.lolz.search_telegram_accounts(
            price_min, price_max, spam=spam, password=password,
            session_age_value=session_age_value, session_age_period=session_age_period,
        )
        if not items:
            return

        item = items[0]
        item_id = item["item_id"]
        price = float(item.get("price", price_max))

        ok = await db.queue_consume_one()
        if not ok:
            return

        try:
            await self._buy_and_process(item_id, price, item)
        except LolzError as e:
            msg = str(e)
            if "продан" in msg.lower() or "sold" in msg.lower():
                await db.add_queue(1)  # вернуть попытку в очередь, лот купили раньше нас
                await self.send_log(f"ℹ️ Лот #{item_id} уже купили раньше нас, пробую другой")
                return
            log.exception("Ошибка обработки лота %s", item_id)
            await db.update_purchase_status(item_id, "error", msg)
            await self.send_log(f"❌ Ошибка обработки аккаунта #{item_id}: {msg}")
        except Exception as e:
            log.exception("Ошибка обработки лота %s", item_id)
            await db.update_purchase_status(item_id, "error", str(e))
            await self.send_log(f"❌ Ошибка обработки аккаунта #{item_id}: {e}")

    async def _buy_and_process(self, item_id: int, price: float, item: dict):
        await self.send_log(f"🛒 Покупаю лот #{item_id} за {price}₽...")
        await self.lolz.buy_item(item_id, price)
        await db.add_spent(price)

        data = await self.lolz.get_item_login_data(item_id)

        hex_key_bought = data.get("login")
        dc_id_bought = data.get("telegram_dc_id")
        phone = data.get("telegram_phone")
        country = data.get("telegram_country") or "—"
        country_code = (data.get("phone") or "").split()[0] if data.get("phone") else ""

        await db.add_purchase(item_id, price, phone, country, country_code)
        await self.send_log(
            f"✅ Куплен аккаунт #{item_id}\n"
            f"Телефон: {phone}\nСтрана: {country}\nЦена: {price}₽\n"
            f"User ID: {data.get('telegram_id', '—')}"
        )

        if not hex_key_bought or not dc_id_bought:
            await db.update_purchase_status(item_id, "error", "Нет HEX/DC от Lolz")
            raw = str(data)[:3500]
            await self.send_log(
                f"❌ #{item_id}: не получены HEX/DC, остановлен.\nСырой ответ:\n{raw}"
            )
            return

        bought_client = await connect_from_hex_dc(hex_key_bought, int(dc_id_bought))
        try:
            await terminate_other_sessions(bought_client)
            await db.update_purchase_status(item_id, "sessions_terminated")

            try:
                new_client = await create_new_session(bought_client, phone)
            except AccountPipelineError as e:
                await db.update_purchase_status(item_id, "error", str(e))
                await self.send_log(
                    f"❌ #{item_id}: не удалось создать новую сессию ({e}). "
                    f"Купленная сессия НЕ завершена — остановлен."
                )
                return

            try:
                hex_key, dc_id = await get_auth_hex_and_dc(new_client)
                await terminate_this_session(bought_client)
                await db.update_purchase_status(item_id, "relogged")

                await cleanup_account(new_client)
                await db.update_purchase_status(item_id, "cleaned")

                sell_price = float(await db.get_setting("sell_price"))
                title_tpl = await db.get_setting("title_template")
                title = title_tpl.format(code=country_code or "?", country=country)

                after_purchase_note = await db.get_setting("sell_description")
                buyer_note = await db.get_setting("sell_buyer_note")
                information = f"{after_purchase_note}\n{buyer_note}"

                await self.lolz.list_for_sale(
                    title=title, price=sell_price, description="",
                    information=information, hex_key=hex_key, dc_id=dc_id,
                )
                await db.update_purchase_status(item_id, "listed")
                await self.send_log(f"💰 Аккаунт #{item_id} выставлен на продажу за {sell_price}₽: {title}")
            finally:
                await new_client.disconnect()
        finally:
            await bought_client.disconnect()
