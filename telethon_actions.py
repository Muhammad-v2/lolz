"""
Операции над купленным Telegram-аккаунтом через Telethon:
1. Подключение по купленной сессии.
2. Завершение всех остальных активных сессий (возможно, т.к. сессии > 24ч).
3. Создание новой сессии (вход по номеру + код, который приходит в чат 777000).
4. Извлечение auth_key (HEX) и DC ID новой сессии.
5. Завершение купленной сессии (остаётся только новая).
6. Очистка аккаунта: все чаты, контакты, группы/каналы, избранное — кроме 777000.
"""
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.auth import ResetAuthorizationsRequest, LogOutRequest
from telethon.tl.functions.account import GetAuthorizationsRequest
from telethon.tl.functions.contacts import DeleteContactsRequest, GetContactsRequest
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.types import InputPeerChannel, InputPeerChat
from config import API_ID, API_HASH

TELEGRAM_SERVICE_ID = 777000


class AccountPipelineError(Exception):
    pass


async def connect_from_string_session(session_str: str) -> TelegramClient:
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        raise AccountPipelineError("Купленная сессия не авторизована")
    return client


async def terminate_other_sessions(client: TelegramClient):
    """Завершает все сессии, кроме текущей (возможно только если текущей > 24ч)."""
    await client(ResetAuthorizationsRequest())


async def create_new_session(client_for_code_receiving: TelegramClient, phone: str,
                              password: str | None = None) -> TelegramClient:
    """
    Запрашивает код на номер и создаёт новую сессию, читая код
    из чата 777000 на СТАРОЙ (купленной) сессии.
    """
    new_client = TelegramClient(StringSession(), API_ID, API_HASH)
    await new_client.connect()

    sent = await new_client.send_code_request(phone)
    code = await _wait_for_code(client_for_code_receiving)
    if not code:
        raise AccountPipelineError("Код входа не пришёл вовремя")

    try:
        await new_client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
    except Exception as e:
        if password:
            await new_client.sign_in(password=password)
        else:
            raise AccountPipelineError(f"Не удалось войти новой сессией: {e}")

    return new_client


async def _wait_for_code(client: TelegramClient, timeout: int = 60) -> str | None:
    """Опрашивает чат 777000 в поисках свежего кода входа."""
    import re
    deadline = asyncio.get_event_loop().time() + timeout
    seen_ids = set()
    while asyncio.get_event_loop().time() < deadline:
        async for msg in client.iter_messages(TELEGRAM_SERVICE_ID, limit=5):
            if msg.id in seen_ids:
                continue
            seen_ids.add(msg.id)
            if msg.text:
                m = re.search(r"\b(\d{5,6})\b", msg.text)
                if m:
                    return m.group(1)
        await asyncio.sleep(2)
    return None


async def get_auth_hex_and_dc(client: TelegramClient) -> tuple[str, int]:
    """Возвращает (auth_key в HEX, dc_id) активной сессии client'а."""
    auth_key = client.session.auth_key.key
    hex_key = auth_key.hex()
    dc_id = client.session.dc_id
    return hex_key, dc_id


async def terminate_this_session(client: TelegramClient):
    """Завершает сессию, которой принадлежит client (используется для купленной сессии)."""
    await client(LogOutRequest())


async def cleanup_account(client: TelegramClient):
    """Удаляет все чаты, выходит из групп/каналов, чистит контакты и избранное,
    оставляя только диалог с Telegram (777000)."""
    async for dialog in client.iter_dialogs():
        if dialog.id == TELEGRAM_SERVICE_ID:
            continue
        try:
            entity = dialog.entity
            if dialog.is_channel or dialog.is_group:
                await client(LeaveChannelRequest(entity))
            await client.delete_dialog(dialog)
        except Exception:
            continue

    try:
        contacts = await client(GetContactsRequest(hash=0))
        if hasattr(contacts, "users") and contacts.users:
            await client(DeleteContactsRequest(id=contacts.users))
    except Exception:
        pass

    try:
        await client.delete_dialog(TELEGRAM_SERVICE_ID.__class__ and "me")  # очистка "Избранного"
    except Exception:
        pass
