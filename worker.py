"""
Фоновый цикл: поиск → покупка → сброс старых сессий → новая сессия → чистка → смена почты → продажа.
"""
from __future__ import annotations

import asyncio
import logging

import db
from lolz_client import LolzClient, LolzError
from email_login import try_attach_email
from telethon_actions import (
    AccountPipelineError,
    check_spam_block,
    cleanup_account,
    connect_from_hex_dc,
    create_new_session,
    get_auth_hex_and_dc,
    terminate_other_sessions,
    terminate_this_session,
)

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
        for item in items[:5]:
            item_id = item.get("item_id") or item.get("itemId") or item.get("id")
            if item_id is None:
                continue
            item_id = int(item_id)
            price = float(item.get("price", price_max))
            try:
                await self._buy_and_process(item_id, price, item)
                return
            except LolzError as e:
                msg = str(e)
                low = msg.lower()
                if "продан" in low or "sold" in low or "already" in low or "недоступен" in low:
                    await self.send_log(f"ℹ️ Лот #{item_id} уже купили раньше нас, пробую следующий")
                    continue
                log.exception("Ошибка обработки лота %s", item_id)
                try:
                    await db.update_purchase_status(item_id, "error", msg)
                except Exception:
                    pass
                await self.send_log(f"❌ Ошибка обработки аккаунта #{item_id}: {msg}")
                return
            except Exception as e:
                log.exception("Ошибка обработки лота %s", item_id)
                err_text = self._describe_pipeline_error(e)
                try:
                    await db.update_purchase_status(item_id, "error", err_text)
                except Exception:
                    pass
                await self.send_log(f"❌ Ошибка обработки аккаунта #{item_id}: {err_text}")
                return

    @staticmethod
    def _describe_pipeline_error(e: Exception) -> str:
        text = str(e)
        low = text.lower()
        if "not registered" in low or "authkeyunregistered" in low:
            return (
                "мёртвая/невалидная сессия от продавца — аккаунт уже не "
                "авторизован по этим HEX/DC. Пропускаю этот аккаунт."
            )
        if "constructor id" in low or "typenotfound" in low:
            return (
                "повреждённый ответ от Telegram при чтении данных аккаунта "
                "(похоже на нерабочую/битую сессию от продавца, либо устаревшую "
                "библиотеку Telethon). Пропускаю этот аккаунт."
            )
        return text[:500]

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
            if val is not None and str(val).strip():
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
            await self.send_log(f"❌ #{item_id}: не получены HEX/DC, остановлен.\nСырой ответ:\n{raw}")
            raise AccountPipelineError("Нет HEX/DC от Lolz")

        bought_client = await connect_from_hex_dc(hex_key_bought, int(dc_id_bought))
        new_client = None
        try:
            # 1) Сбрасываем старые чужие сессии ДО создания новой,
            #    иначе ResetAuthorizationsRequest убьёт только что созданную сессию!
            await self.send_log(f"🔒 #{item_id}: завершаю старые чужие сессии...")
            await terminate_other_sessions(bought_client)
            await db.update_purchase_status(item_id, "sessions_terminated")

            # 2) Создаём новую сессию с жёстким общим таймаутом 140с.
            await self.send_log(f"🆕 #{item_id}: запрашиваю код и создаю новую сессию...")
            try:
                new_client = await asyncio.wait_for(
                    create_new_session(
                        bought_client,
                        phone,
                        password=cloud_password,
                        lolz_code_fetcher=lambda: self.lolz.get_login_code(item_id),
                    ),
                    timeout=140,
                )
            except asyncio.TimeoutError:
                err_msg = "таймаут создания новой сессии (140с) — сокет Telegram завис"
                await db.update_purchase_status(item_id, "error", err_msg)
                await self.send_log(
                    f"❌ #{item_id}: {err_msg}. "
                    f"Купленная сессия НЕ завершена — доступ не потерян."
                )
                raise AccountPipelineError(err_msg)
            except AccountPipelineError as e:
                await db.update_purchase_status(item_id, "error", str(e))
                await self.send_log(
                    f"❌ #{item_id}: не удалось создать новую сессию ({e}). "
                    f"Купленная сессия НЕ завершена — доступ не потерян."
                )
                raise

            hex_key, dc_id = await get_auth_hex_and_dc(new_client)

            await db.set_purchase_extra(
                item_id,
                hex_key=hex_key,
                dc_id=dc_id,
                buy_url=f"https://lzt.market/{item_id}/",
                telegram_user_id=str(data.get("telegram_id") or data.get("telegramId") or ""),
            )

            # 3) Чистим аккаунт и меняем почту через новую сессию.
            await self.send_log(f"🧹 #{item_id}: чищу чаты и контакты...")
            cleanup_report = await cleanup_account(new_client)
            await db.update_purchase_status(item_id, "cleaned")
            await self.send_log(
                f"🧹 #{item_id}: удалено {cleanup_report['deleted']}, "
                f"не удалось {cleanup_report['failed']}"
            )

            try:
                email_ok, email_info = await try_attach_email(new_client)
                if email_ok:
                    await self.send_log(f"📧 #{item_id}: почта изменена на {email_info}")
                else:
                    await self.send_log(f"ℹ️ #{item_id}: почта не изменена ({email_info})")
            except Exception as e:
                await self.send_log(f"⚠️ #{item_id}: ошибка смены почты ({e})")

            # 4) Проверка спам-блока.
            check_spam = (await db.get_setting("check_spam_block")) != "0"
            if check_spam:
                await self.send_log(f"🔍 #{item_id}: проверяю спам-блок...")
                spam_status = await check_spam_block(new_client)
                await db.set_purchase_extra(item_id, spam_status=spam_status)
            else:
                spam_status = "unknown"

            # 5) Выставляем на продажу.
            sell_price = float(await db.get_setting("sell_price"))
            title_tpl = await db.get_setting("title_template")
            clean_code = (country_code or "?").lstrip("+")
            try:
                title = title_tpl.format(code=clean_code, country=country or "—")
            except Exception:
                title = f"+{clean_code} {country or '—'} | Чистый | Не трогал"

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
                        await asyncio.sleep(5 * attempt)
                        continue
                    raise

            if sell_result is None:
                await db.update_purchase_status(item_id, "cleaned_not_listed", str(last_error))
                await self.send_log(
                    f"⚠️ #{item_id}: аккаунт готов, но выставить не удалось: {last_error}\n"
                    f"Можно повторить вручную."
                )
                return

            await self.send_log(f"🚪 #{item_id}: завершаю купленную сессию...")
            try:
                logout_ok, logout_info = await terminate_this_session(bought_client)
                if not logout_ok:
                    await self.send_log(f"⚠️ #{item_id}: не удалось завершить купленную сессию ({logout_info})")
            except Exception as e:
                await self.send_log(f"⚠️ #{item_id}: не удалось завершить купленную сессию ({e})")
            bought_client = None
            await db.update_purchase_status(item_id, "listed")

            sell_item_id = None
            for key in ("item_id", "itemId", "id"):
                val = sell_result.get(key) if isinstance(sell_result, dict) else None
                if val:
                    sell_item_id = int(val)
                    break

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
                    await asyncio.wait_for(new_client.disconnect(), timeout=5)
                except Exception:
                    pass
            if bought_client is not None:
                try:
                    await asyncio.wait_for(bought_client.disconnect(), timeout=5)
                except Exception:
                    pass