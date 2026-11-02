"""
Операции над купленным Telegram-аккаунтом через Telethon.

Пайплайн:
  1) подключиться по HEX+DC
  2) завершить все чужие сессии (кроме текущей купленной)
  3) создать НОВУЮ сессию (код читаем из чата 777000 на купленной)
  4) забрать HEX+DC новой сессии
  5) завершить купленную сессию
  6) почистить чаты/контакты на новой (оставить только 777000)
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    SessionPasswordNeededError,
)
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession
from telethon.tl.functions.auth import LogOutRequest, ResetAuthorizationsRequest
from telethon.tl.functions.contacts import DeleteContactsRequest, GetContactsRequest
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.messages import DeleteHistoryRequest

from config import API_ID, API_HASH

log = logging.getLogger("telethon_actions")

TELEGRAM_SERVICE_ID = 777000

DC_ADDRESSES = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}


class AccountPipelineError(Exception):
    pass


def _normalize_hex(hex_key: str) -> str:
    if hex_key is None:
        raise AccountPipelineError("HEX пустой")
    h = str(hex_key).strip().lower()
    h = h.replace(" ", "").replace("\n", "").replace("\r", "").replace("\t", "")
    if h.startswith("0x"):
        h = h[2:]
    if ":" in h:
        left, right = h.split(":", 1)
        if left.isdigit() and re.fullmatch(r"[0-9a-f]+", right or ""):
            h = right
        elif right.isdigit() and re.fullmatch(r"[0-9a-f]+", left or ""):
            h = left
    if not re.fullmatch(r"[0-9a-f]+", h):
        raise AccountPipelineError(f"HEX содержит недопустимые символы (len={len(h)})")
    if len(h) != 512:
        if len(h) % 2 != 0 or len(h) < 64:
            raise AccountPipelineError(
                f"Неверная длина HEX auth_key: {len(h)} (ожидается 512)"
            )
        log.warning("HEX длина %s вместо 512 — пробуем как есть", len(h))
    return h


def _normalize_phone(phone: str | None) -> str:
    if not phone:
        raise AccountPipelineError("Номер телефона пустой — нельзя запросить код")
    p = re.sub(r"[^\d+]", "", str(phone).strip())
    if p.startswith("00"):
        p = "+" + p[2:]
    if not p.startswith("+") and p.isdigit():
        p = "+" + p
    if len(re.sub(r"\D", "", p)) < 8:
        raise AccountPipelineError(f"Подозрительный номер телефона: {phone}")
    return p


async def connect_from_hex_dc(hex_key: str, dc_id: int) -> TelegramClient:
    dc_id = int(dc_id)
    ip = DC_ADDRESSES.get(dc_id)
    if not ip:
        raise AccountPipelineError(f"Неизвестный DC ID: {dc_id}")

    h = _normalize_hex(hex_key)
    try:
        key_bytes = bytes.fromhex(h)
    except ValueError as e:
        raise AccountPipelineError(f"Не удалось разобрать HEX: {e}") from e

    session = MemorySession()
    session.set_dc(dc_id, ip, 443)
    session.auth_key = AuthKey(data=key_bytes)

    client = TelegramClient(session, API_ID, API_HASH)
    client.session.set_dc(dc_id, ip, 443)

    try:
        await client.connect()
    except Exception as e:
        raise AccountPipelineError(f"Не удалось подключиться к DC{dc_id}: {e}") from e

    if not await client.is_user_authorized():
        await client.disconnect()
        raise AccountPipelineError(
            "Купленный аккаунт не авторизован (HEX/DC неверны или сессия уже убита)"
        )
    me = await client.get_me()
    log.info("Подключены как %s (id=%s) через DC%s", me.phone or me.username, me.id, dc_id)
    return client


async def terminate_other_sessions(client: TelegramClient) -> None:
    """Завершает все сессии кроме текущей. Ошибка «сессия новая» — не фатально."""
    try:
        await client(ResetAuthorizationsRequest())
        log.info("ResetAuthorizations: чужие сессии завершены")
        await asyncio.sleep(1.5)
    except FloodWaitError as e:
        log.warning("FloodWait при ResetAuthorizations: %ss", e.seconds)
        await asyncio.sleep(min(int(e.seconds) + 1, 60))
    except Exception as e:
        log.warning("Не удалось завершить чужие сессии: %s", e)


async def _wait_for_code(
    client: TelegramClient,
    after: datetime,
    timeout: int = 120,
) -> str | None:
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)

    cutoff = after - timedelta(seconds=8)
    code_holder: dict[str, str | None] = {"code": None}
    seen_ids: set[int] = set()

    def _extract(text: str | None) -> str | None:
        if not text:
            return None
        m = re.search(
            r"(?:code|код)[^\d]{0,20}(\d{5,6})\b",
            text,
            flags=re.IGNORECASE,
        )
        if m:
            return m.group(1)
        m = re.search(r"\b(\d{5,6})\b", text)
        return m.group(1) if m else None

    async def _check_message(msg) -> bool:
        if msg is None or msg.id in seen_ids:
            return False
        seen_ids.add(msg.id)
        msg_date = msg.date
        if msg_date is not None:
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            if msg_date < cutoff:
                return False
        code = _extract(msg.text or msg.message)
        if code:
            code_holder["code"] = code
            return True
        return False

    @client.on(events.NewMessage(from_users=TELEGRAM_SERVICE_ID))
    async def _on_code(event):
        if code_holder["code"]:
            return
        await _check_message(event.message)

    deadline = asyncio.get_event_loop().time() + timeout
    try:
        async for msg in client.iter_messages(TELEGRAM_SERVICE_ID, limit=15):
            if await _check_message(msg):
                return code_holder["code"]

        while asyncio.get_event_loop().time() < deadline:
            if code_holder["code"]:
                return code_holder["code"]
            try:
                async for msg in client.iter_messages(TELEGRAM_SERVICE_ID, limit=8):
                    if await _check_message(msg):
                        return code_holder["code"]
            except Exception as e:
                log.debug("poll 777000: %s", e)
            await asyncio.sleep(1.5)
    finally:
        client.remove_event_handler(_on_code)

    return code_holder["code"]


async def create_new_session(
    client_for_code_receiving: TelegramClient,
    phone: str,
    password: str | None = None,
) -> TelegramClient:
    phone = _normalize_phone(phone)
    new_client = TelegramClient(MemorySession(), API_ID, API_HASH)
    await new_client.connect()

    request_time = datetime.now(timezone.utc)
    try:
        sent = await new_client.send_code_request(phone)
    except FloodWaitError as e:
        await new_client.disconnect()
        raise AccountPipelineError(
            f"FloodWait при запросе кода: подожди {e.seconds}с"
        ) from e
    except Exception as e:
        await new_client.disconnect()
        raise AccountPipelineError(f"Не удалось запросить код: {e}") from e

    log.info("Код запрошен для %s", phone)

    code = await _wait_for_code(client_for_code_receiving, after=request_time, timeout=120)
    if not code:
        await new_client.disconnect()
        raise AccountPipelineError(
            "Код входа не пришёл в чат 777000 за 120 секунд"
        )

    log.info("Код получен")

    try:
        await new_client.sign_in(
            phone=phone,
            code=code,
            phone_code_hash=sent.phone_code_hash,
        )
    except SessionPasswordNeededError:
        if not password:
            await new_client.disconnect()
            raise AccountPipelineError(
                "На аккаунте 2FA, пароль неизвестен. Ставь фильтр «Пароль: Нет»."
            )
        try:
            await new_client.sign_in(password=password)
        except PasswordHashInvalidError as e:
            await new_client.disconnect()
            raise AccountPipelineError("Неверный облачный пароль (2FA)") from e
        except Exception as e:
            await new_client.disconnect()
            raise AccountPipelineError(f"Не удалось войти с 2FA: {e}") from e
    except FloodWaitError as e:
        await new_client.disconnect()
        raise AccountPipelineError(f"FloodWait при sign_in: {e.seconds}с") from e
    except Exception as e:
        await new_client.disconnect()
        raise AccountPipelineError(f"Не удалось войти новой сессией: {e}") from e

    if not await new_client.is_user_authorized():
        await new_client.disconnect()
        raise AccountPipelineError("Новая сессия не авторизована после sign_in")

    me = await new_client.get_me()
    log.info("Новая сессия OK: id=%s phone=%s", me.id, me.phone)
    return new_client


async def get_auth_hex_and_dc(client: TelegramClient) -> tuple[str, int]:
    auth_key = client.session.auth_key
    if auth_key is None or not getattr(auth_key, "key", None):
        raise AccountPipelineError("У сессии нет auth_key")
    hex_key = auth_key.key.hex()
    dc_id = int(client.session.dc_id)
    if dc_id not in DC_ADDRESSES:
        raise AccountPipelineError(f"Странный dc_id у новой сессии: {dc_id}")
    return hex_key, dc_id


async def terminate_this_session(client: TelegramClient) -> None:
    try:
        await client(LogOutRequest())
        log.info("Купленная сессия завершена (LogOut)")
    except Exception as e:
        log.warning("LogOut купленной сессии: %s", e)
    try:
        await client.disconnect()
    except Exception:
        pass


async def cleanup_account(client: TelegramClient) -> None:
    try:
        async for dialog in client.iter_dialogs():
            if dialog.id == TELEGRAM_SERVICE_ID:
                continue
            try:
                if dialog.is_channel or dialog.is_group:
                    try:
                        await client(LeaveChannelRequest(dialog.entity))
                    except Exception:
                        pass
                try:
                    await client(
                        DeleteHistoryRequest(
                            peer=dialog.entity,
                            max_id=0,
                            just_clear=False,
                            revoke=True,
                        )
                    )
                except Exception:
                    try:
                        await client.delete_dialog(dialog.entity)
                    except Exception:
                        pass
            except Exception:
                continue
            await asyncio.sleep(0.15)
    except Exception as e:
        log.warning("Ошибка при чистке диалогов: %s", e)

    try:
        contacts = await client(GetContactsRequest(hash=0))
        users = getattr(contacts, "users", None) or []
        if users:
            await client(DeleteContactsRequest(id=users))
            log.info("Удалено контактов: %s", len(users))
    except Exception as e:
        log.warning("Ошибка при удалении контактов: %s", e)

    log.info("Очистка аккаунта завершена")