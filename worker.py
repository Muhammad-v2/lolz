"""
Фоновый цикл: поиск → покупка → перелогин → чистка → продажа.
"""
from __future__ import annotations

import asyncio
import logging

from lolz_client import LolzClient, LolzError
from telethon_actions import (
    connect_from_hex_dc,
    terminate_other_sessions,
    create_new_session,
    get_auth_hex_and_dc,
    terminate_this_session,
    cleanup_account,
    check_spam_block,
    request_email_change,
    confirm_email_change,
    EmailChangeError,
    AccountPipelineError,
)
from email_client import wait_for_email_code
from config import EMAIL_ADDRESS
import db
import time

log = logging.getLogger("worker")


class Worker:
    def __init__(self, send_log):
        self.send_log = send_log
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
            try:
                interval = int(await db.get_setting("search_interval_sec"))
            except Exception:
                interval = 60
            await asyncio.sleep(max(15, interval))

    async def _tick(self):
        remaining = await db.get_queue()
        if remaining <= 0:
            return

        price_min = float(await db.get_setting("buy_min_price"))
        price_max = float(await db.get_setting("buy_max_price"))
        spam = await db.get_setting("filter_spam")
        password = await db.get_setting("filter_password")
        session_age_value = int(await db.get_setting("session_age_value"))
        session_age_period = await db.get_setting("session_age_period")

        items = await self.lolz.search_telegram_accounts(
            price_min,
            price_max,
            spam=spam,
            password=password,
            session_age_value=session_age_value,
            session_age_period=session_age_period,
        )
        if not items:
            return

        item = items[0]
        item_id = item.get("item_id") or item.get("itemId") or item.get("id")
        if item_id is None:
            await self.send_log(f"⚠️ Странный лот без id: {str(item)[:500]}")
            return
        item_id = int(item_id)
        price = float(item.get("price", price_max))

        try:
            await self._buy_and_process(item_id, price, item)
        except LolzError as e:
            msg = str(e)
            low = msg.lower()
            if "продан" in low or "sold" in low or "already" in low or "недоступен" in low:
                await self.send_log(f"ℹ️ Лот #{item_id} уже купили раньше нас, пробую другой")
                return
            log.exception("Ошибка обработки лота %s", item_id)
            try:
                await db.update_purchase_status(item_id, "error", msg)
            except Exception:
                pass
            await self.send_log(f"❌ Ошибка обработки аккаунта #{item_id}: {msg}")
        except Exception as e:
            log.exception("Ошибка обработки лота %s", item_id)
            try:
                await db.update_purchase_status(item_id, "error", str(e))
            except Exception:
                pass
            await self.send_log(f"❌ Ошибка обработки аккаунта #{item_id}: {e}")

    async def _attach_email(self, item_id: int, client):
        """Привязывает EMAIL_ADDRESS как email для входа на аккаунте.
        Код подтверждения Telegram пришлёт на эту почту — читаем его
        сами через IMAP (email_client.py), без участия человека.
        Если что-то не получится — просто пропускаем этот шаг и идём
        дальше (не ломаем остальной пайплайн)."""
        try:
            await self.send_log(f"✉️ #{item_id}: привязываю email {EMAIL_ADDRESS}...")
            request_time = time.time()
            await request_email_change(client, EMAIL_ADDRESS)

            code = await wait_for_email_code(after_ts=request_time, timeout=90)
            if not code:
                await self.send_log(
                    f"⚠️ #{item_id}: код подтверждения email не пришёл за 90 сек, "
                    f"пропускаю привязку почты (остальное продолжаю)."
                )
                return

            await confirm_email_change(client, code)
            await self.send_log(f"✅ #{item_id}: email успешно привязан")
        except EmailChangeError as e:
            await self.send_log(f"⚠️ #{item_id}: не удалось привязать email ({e}), продолжаю без этого")
        except Exception as e:
            log.exception("Ошибка привязки email для %s", item_id)
            await self.send_log(f"⚠️ #{item_id}: ошибка при привязке email ({e}), продолжаю без этого")

    async def _buy_and_process(self, item_id: int, price: float, item: dict):
        await self.send_log(f"🛒 Покупаю лот #{item_id} за {price}₽...")
        await self.lolz.buy_item(item_id, price)
        await db.queue_consume_one()
        await db.add_spent(price)

        data = await self.lolz.get_item_login_data(item_id)

        hex_key_bought, dc_id_bought = LolzClient.extract_hex_dc(data)
        phone, country, country_code = LolzClient.extract_phone_country(data)

        cloud_password = None
        for key in ("telegram_password", "cloud_password", "password_2fa", "tfa_password"):
            val = data.get(key)
            if val and str(val).strip() and not str(val).strip().isdigit():
                cloud_password = str(val).strip()
                break

        await db.add_purchase(item_id, price, phone or "", country, country_code)
        await self.send_log(
            f"✅ Куплен аккаунт #{item_id}\n"
            f"Телефон: {phone}\nСтрана: {country}\nКод: {country_code or '—'}\n"
            f"Цена: {price}₽\n"
            f"User ID: {data.get('telegram_id') or data.get('telegramId') or '—'}\n"
            f"HEX: {'есть' if hex_key_bought else 'нет'}, DC: {dc_id_bought or 'нет'}"
        )

        if not hex_key_bought or not dc_id_bought:
            await db.update_purchase_status(item_id, "error", "Нет HEX/DC от Lolz")
            raw = str(data)[:3500]
            await self.send_log(
                f"❌ #{item_id}: не получены HEX/DC, остановлен.\nСырой ответ:\n{raw}"
            )
            raise AccountPipelineError("Нет HEX/DC от Lolz")

        bought_client = await connect_from_hex_dc(hex_key_bought, int(dc_id_bought))
        new_client = None
        try:
            await self.send_log(f"🔒 #{item_id}: завершаю чужие сессии...")
            await terminate_other_sessions(bought_client)
            await db.update_purchase_status(item_id, "sessions_terminated")

            await self.send_log(f"🆕 #{item_id}: создаю новую сессию (жду код в 777000)...")
            try:
                new_client = await create_new_session(
                    bought_client,
                    phone,
                    password=cloud_password,
                )
            except AccountPipelineError as e:
                await db.update_purchase_status(item_id, "error", str(e))
                await self.send_log(
                    f"❌ #{item_id}: не удалось создать новую сессию ({e}). "
                    f"Купленная сессия НЕ завершена."
                )
                raise

            hex_key, dc_id = await get_auth_hex_and_dc(new_client)

            # Сохраняем HEX/DC новой сессии СРАЗУ, до попытки продажи —
            # если выставление на продажу упадёт (например, временная
            # ошибка API Lolz), доступ к аккаунту всё равно не потеряется,
            # его можно будет посмотреть в карточке товара в боте.
            await db.set_purchase_extra(
                item_id,
                hex_key=hex_key,
                dc_id=dc_id,
                buy_url=f"https://lzt.market/{item_id}/",
                telegram_user_id=str(data.get("telegram_id") or data.get("telegramId") or ""),
            )

            await self.send_log(f"🔍 #{item_id}: проверяю спам-блок через @SpamBot...")
            spam_status = await check_spam_block(new_client)
            await db.set_purchase_extra(item_id, spam_status=spam_status)

            await self.send_log(f"🚪 #{item_id}: завершаю купленную сессию...")
            await terminate_this_session(bought_client)
            bought_client = None
            await db.update_purchase_status(item_id, "relogged")

            await self.send_log(f"🧹 #{item_id}: чищу чаты и контакты...")
            await cleanup_account(new_client)
            await db.update_purchase_status(item_id, "cleaned")

            if EMAIL_ADDRESS:
                await self._attach_email(item_id, new_client)

            sell_price = float(await db.get_setting("sell_price"))
            title_tpl = await db.get_setting("title_template")
            try:
                title = title_tpl.format(
                    code=country_code or "?",
                    country=country or "—",
                )
            except Exception:
                title = f"{country_code or '?'} {country or '—'} | Чистый | Не трогал"

            after_purchase_note = await db.get_setting("sell_description")
            buyer_note = await db.get_setting("sell_buyer_note")
            information = f"{after_purchase_note}\n{buyer_note}".strip()

            await self.send_log(f"📤 #{item_id}: выставляю на продажу за {sell_price}₽...")

            sell_result = None
            last_error = None
            for attempt in range(1, 4):
                try:
                    sell_result = await self.lolz.list_for_sale(
                        title=title,
                        price=sell_price,
                        description="",
                        information=information,
                        hex_key=hex_key,
                        dc_id=dc_id,
                    )
                    last_error = None
                    break
                except LolzError as e:
                    last_error = e
                    if "503" in str(e) or "502" in str(e) or "504" in str(e):
                        # временная ошибка сервера Lolz — пробуем ещё раз
                        await asyncio.sleep(5 * attempt)
                        continue
                    raise

            if sell_result is None:
                # Продажа не удалась даже после повторов, но аккаунт жив,
                # почищен и HEX/DC уже сохранены — доступ не потерян.
                await db.update_purchase_status(item_id, "cleaned_not_listed", str(last_error))
                await self.send_log(
                    f"⚠️ #{item_id}: аккаунт готов (HEX/DC сохранены, доступен в "
                    f"боте через «📦 На продаже» → но он там не появится, т.к. "
                    f"не выставлен), выставить на продажу не удалось: {last_error}\n"
                    f"Попробуй выставить вручную на сайте с этими HEX/DC, "
                    f"или подожди и перезапусти обработку этого лота."
                )
                return

            await db.update_purchase_status(item_id, "listed")

            # id нового выставленного лота может называться по-разному —
            # пробуем несколько вариантов, как и для других полей API.
            sell_item_id = None
            for key in ("item_id", "itemId", "id"):
                val = sell_result.get(key) if isinstance(sell_result, dict) else None
                if val:
                    sell_item_id = int(val)
                    break
            if sell_item_id is None and isinstance(sell_result, dict):
                nested = sell_result.get("item")
                if isinstance(nested, dict):
                    sell_item_id = nested.get("item_id") or nested.get("itemId")
                    if sell_item_id:
                        sell_item_id = int(sell_item_id)

            await db.set_purchase_extra(
                item_id,
                sell_item_id=sell_item_id,
                sell_price=sell_price,
                buy_url=f"https://lzt.market/{item_id}/",
                sell_url=f"https://lzt.market/{sell_item_id}/" if sell_item_id else None,
                hex_key=hex_key,
                dc_id=dc_id,
                telegram_user_id=str(data.get("telegram_id") or data.get("telegramId") or ""),
                spam_status=spam_status,
            )

            await self.send_log(
                f"💰 Аккаунт #{item_id} выставлен на продажу за {sell_price}₽:\n{title}"
            )
        finally:
            if new_client is not None:
                try:
                    await new_client.disconnect()
                except Exception:
                    pass
            if bought_client is not None:
                try:
                    await bought_client.disconnect()
                except Exception:
                    pass