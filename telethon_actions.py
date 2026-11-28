from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable

from telethon import TelegramClient, events
from telethon.crypto import AuthKey
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import MemorySession
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.functions.auth import LogOutRequest, ResendCodeRequest, ResetAuthorizationsRequest
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.contacts import DeleteContactsRequest, GetContactsRequest
from telethon.tl.functions.help import GetConfigRequest
from telethon.tl.types import InputPeerUser, PeerUser

from config import (
    API_HASH,
    API_ID,
    TG_APP_VERSION,
    TG_DEVICE_MODEL,
    TG_LANG_CODE,
    TG_SYSTEM_LANG_CODE,
    TG_SYSTEM_VERSION,
)

log = logging.getLogger("telethon_actions")

TELEGRAM_SERVICE_ID = 777000
SPAMBOT_USERNAME = "SpamBot"

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
            raise AccountPipelineError(f"Неверная длина HEX auth_key: {len(h)} (ожидается 512)")
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


async def _resolve_dc_address(dc_id: int) -> tuple[str, int]:
    bootstrap = TelegramClient(MemorySession(), API_ID, API_HASH)
    try:
        await asyncio.wait_for(bootstrap.connect(), timeout=10)
        config = await asyncio.wait_for(bootstrap(GetConfigRequest()), timeout=10)
        candidates = [
            o for o in config.dc_options
            if o.id == dc_id and not o.ipv6 and not o.media_only and not o.tcpo_only
        ]
        if not candidates:
            candidates = [o for o in config.dc_options if o.id == dc_id and not o.ipv6]
        if candidates:
            return candidates[0].ip_address, candidates[0].port
    except Exception as e:
        log.warning("Не удалось получить актуальный адрес DC%s: %s", dc_id, e)
    finally:
        try:
            await asyncio.wait_for(bootstrap.disconnect(), timeout=5)
        except Exception:
            pass

    ip = DC_ADDRESSES.get(dc_id)
    if not ip:
        raise AccountPipelineError(f"Неизвестный DC ID: {dc_id}")
    return ip, 443


async def connect_from_hex_dc(hex_key: str, dc_id: int) -> TelegramClient:
    if not API_ID or not API_HASH:
        raise AccountPipelineError("Не заданы API_ID / API_HASH в переменных окружения")

    dc_id = int(dc_id)
    ip, port = await _resolve_dc_address(dc_id)
    h = _normalize_hex(hex_key)
    try:
        key_bytes = bytes.fromhex(h)
    except ValueError as e:
        raise AccountPipelineError(f"Не удалось разобрать HEX: {e}") from e

    session = MemorySession()
    session.set_dc(dc_id, ip, port)
    session.auth_key = AuthKey(data=key_bytes)

    client = TelegramClient(
        session,
        API_ID,
        API_HASH,
        device_model=TG_DEVICE_MODEL,
        system_version=TG_SYSTEM_VERSION,
        app_version=TG_APP_VERSION,
        lang_code=TG_LANG_CODE,
        system_lang_code=TG_SYSTEM_LANG_CODE,
    )
    client.session.set_dc(dc_id, ip, port)

    try:
        await asyncio.wait_for(client.connect(), timeout=15)
    except Exception as e:
        raise AccountPipelineError(f"Не удалось подключиться к DC{dc_id}: {e}") from e

    if not await asyncio.wait_for(client.is_user_authorized(), timeout=15):
        await client.disconnect()
        raise AccountPipelineError("Купленный аккаунт не авторизован (HEX/DC неверны или сессия уже убита)")

    me = await asyncio.wait_for(client.get_me(), timeout=15)
    log.info("Подключены как %s (id=%s) через DC%s", me.phone or me.username, me.id, dc_id)
    return client


async def _resolve_service_peer(client: TelegramClient):
    try:
        return await asyncio.wait_for(client.get_input_entity(TELEGRAM_SERVICE_ID), timeout=4)
    except Exception:
        pass

    for folder in (None, 1):
        try:
            async def _scan(f=folder):
                kwargs = {"limit": 200} if f is None else {"limit": 50, "folder": f}
                async for dialog in client.iter_dialogs(**kwargs):
                    if dialog.id == TELEGRAM_SERVICE_ID:
                        return dialog.input_entity
                return None

            peer = await asyncio.wait_for(_scan(), timeout=15)
            if peer is not None:
                return peer
        except Exception as e:
            log.debug("Скан диалогов (folder=%s) для 777000: %s", folder, e)

    try:
        return await asyncio.wait_for(client.get_entity(PeerUser(TELEGRAM_SERVICE_ID)), timeout=6)
    except Exception:
        return InputPeerUser(TELEGRAM_SERVICE_ID, 0)


async def prepare_client_for_code(client: TelegramClient) -> tuple[int, str, object]:
    try:
        await asyncio.wait_for(client.catch_up(), timeout=8)
    except Exception as e:
        log.debug("catch_up пропущен: %s", e)

    try:
        await asyncio.wait_for(client(UpdateStatusRequest(offline=False)), timeout=8)
    except Exception as e:
        log.debug("UpdateStatusRequest: %s", e)

    peer = await _resolve_service_peer(client)
    baseline_id, last_date = await _get_last_service_msg_info(client, peer)
    return baseline_id, last_date, peer


async def _get_last_service_msg_info(client: TelegramClient, peer=None) -> tuple[int, str]:
    target = peer or TELEGRAM_SERVICE_ID
    try:
        msgs = await asyncio.wait_for(client.get_messages(target, limit=1), timeout=10)
        if msgs:
            m = msgs[0]
            dt = m.date.strftime("%Y-%m-%d %H:%M:%S") if getattr(m, "date", None) else "—"
            return int(m.id), dt
    except Exception as e:
        log.warning("Не удалось прочитать baseline чата 777000: %s", e)
    return 0, "—"


async def terminate_other_sessions(client: TelegramClient) -> None:
    try:
        await asyncio.wait_for(client(ResetAuthorizationsRequest()), timeout=15)
        log.info("ResetAuthorizations: чужие сессии завершены")
        await asyncio.sleep(2.5)
    except FloodWaitError as e:
        log.warning("FloodWait при ResetAuthorizations: %ss", e.seconds)
        await asyncio.sleep(min(int(e.seconds) + 1, 60))
    except Exception as e:
        log.warning("Не удалось завершить чужие сессии (не фатально): %s", e)


def _extract_code(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(
        r"(?:login\s*code|verification\s*code|code|код(?:\s+для\s+входа)?(?:\s+в\s+telegram)?|código|kode)[^\n\d]{0,40}(\d{5,6})\b",
        text,
        flags=re.IGNORECASE,
    )
    if m and m.group(1) != "777000":
        return m.group(1)

    low = text.lower()
    if any(
        marker in low
        for marker in ("new login", "we detected a login", "новый вход", "обнаружен вход", "terminated", "завершены")
    ):
        return None

    for candidate in re.findall(r"(?<!\d)(\d{5,6})(?!\d)", text):
        if candidate != "777000":
            return candidate
    return None


async def _wait_for_code(
    client: TelegramClient,
    baseline_msg_id: int,
    service_peer=None,
    timeout: int = 80,
    lolz_code_fetcher: Callable[[], Awaitable[str | None]] | None = None,
    stale_lolz_code: str | None = None,
    stale_lolz_known: bool = False,
    on_resend: Callable[[], Awaitable[None]] | None = None,
) -> str | None:
    code_holder: dict[str, str | None] = {"code": None}
    seen_ids: set[int] = set()
    peer_holder = {"peer": service_peer or TELEGRAM_SERVICE_ID}

    async def _check_message(msg) -> bool:
        if msg is None or getattr(msg, "id", None) is None:
            return False
        msg_id = int(msg.id)
        if msg_id in seen_ids or msg_id <= baseline_msg_id:
            return False
        seen_ids.add(msg_id)
        if getattr(msg, "out", False):
            return False
        text = getattr(msg, "raw_text", None) or getattr(msg, "message", None) or getattr(msg, "text", None)
        code = _extract_code(text)
        if code:
            code_holder["code"] = code
            return True
        return False

    @client.on(events.NewMessage(incoming=True))
    async def _on_code(event):
        if code_holder["code"]:
            return
        sender_id = getattr(event, "sender_id", None)
        chat_id = getattr(event, "chat_id", None)
        if sender_id != TELEGRAM_SERVICE_ID and chat_id != TELEGRAM_SERVICE_ID:
            return
        await _check_message(event.message)

    async def _poll_messages() -> str | None:
        async for msg in client.iter_messages(peer_holder["peer"], limit=10):
            if int(msg.id) <= baseline_msg_id:
                break
            if await _check_message(msg):
                return code_holder["code"]
        return None

    async def _poll_top_dialogs() -> str | None:
        dialogs = await client.get_dialogs(limit=15)
        for d in dialogs:
            if d.id == TELEGRAM_SERVICE_ID:
                peer_holder["peer"] = d.input_entity
                if d.message and await _check_message(d.message):
                    return code_holder["code"]
                break
        return None

    loop = asyncio.get_running_loop()
    start_ts = loop.time()
    deadline = start_ts + timeout
    last_lolz_poll = 0.0
    last_catch_up = 0.0
    last_dialogs_poll = 0.0
    resent_once = False

    try:
        while loop.time() < deadline:
            if code_holder["code"]:
                return code_holder["code"]

            now = loop.time()

            if not resent_once and on_resend is not None and (now - start_ts) >= 30.0:
                resent_once = True
                try:
                    await on_resend()
                except Exception as e:
                    log.warning("Повторный запрос кода не удался: %s", e)

            if (now - last_catch_up) >= 4.5:
                last_catch_up = now
                try:
                    await asyncio.wait_for(client.catch_up(), timeout=6)
                except Exception:
                    pass

            try:
                found = await asyncio.wait_for(_poll_messages(), timeout=8)
                if found:
                    return found
            except Exception as e:
                log.debug("Опрос 777000 через iter_messages: %s", e)
                try:
                    found = await asyncio.wait_for(_poll_top_dialogs(), timeout=8)
                    if found:
                        return found
                except Exception:
                    pass

            # Раз в 6 секунд также проверяем верхние диалоги напрямую (на случай удалённого ранее чата)
            if (now - last_dialogs_poll) >= 6.0:
                last_dialogs_poll = now
                try:
                    found = await asyncio.wait_for(_poll_top_dialogs(), timeout=8)
                    if found:
                        return found
                except Exception:
                    pass

            if (
                lolz_code_fetcher is not None
                and stale_lolz_known
                and (now - start_ts) >= 10.0
                and (now - last_lolz_poll) >= 5.0
            ):
                last_lolz_poll = now
                try:
                    lcode = await asyncio.wait_for(lolz_code_fetcher(), timeout=8)
                    if lcode and lcode != stale_lolz_code:
                        log.info("Код получен через Lolz API после запроса кода")
                        return str(lcode).strip()
                except Exception as e:
                    log.debug("Опрос Lolz API для кода: %s", e)

            await asyncio.sleep(1.5)
    finally:
        client.remove_event_handler(_on_code)

    return code_holder["code"]


async def create_new_session(
    client_for_code_receiving: TelegramClient,
    phone: str,
    password: str | None = None,
    preloaded_code: str | None = None,
    lolz_code_fetcher: Callable[[], Awaitable[str | None]] | None = None,
) -> TelegramClient:
    try:
        me_src = await asyncio.wait_for(client_for_code_receiving.get_me(), timeout=8)
        if me_src and getattr(me_src, "phone", None):
            phone = me_src.phone
    except Exception:
        pass
    phone = _normalize_phone(phone)

    baseline_msg_id = 0
    last_msg_date = "—"
    service_peer = None
    stale_lolz_code = None
    stale_lolz_known = False
    if not preloaded_code:
        baseline_msg_id, last_msg_date, service_peer = await prepare_client_for_code(client_for_code_receiving)
        if lolz_code_fetcher is not None:
            try:
                stale_lolz_code = await asyncio.wait_for(lolz_code_fetcher(), timeout=8)
                stale_lolz_known = True
            except Exception:
                stale_lolz_code = None
                stale_lolz_known = False

    new_session = MemorySession()
    src_session = getattr(client_for_code_receiving, "session", None)
    if (
        src_session is not None
        and getattr(src_session, "dc_id", None)
        and getattr(src_session, "server_address", None)
        and getattr(src_session, "port", None)
    ):
        new_session.set_dc(int(src_session.dc_id), src_session.server_address, int(src_session.port))

    new_client = TelegramClient(
        new_session,
        API_ID,
        API_HASH,
        device_model=TG_DEVICE_MODEL,
        system_version=TG_SYSTEM_VERSION,
        app_version=TG_APP_VERSION,
        lang_code=TG_LANG_CODE,
        system_lang_code=TG_SYSTEM_LANG_CODE,
    )

    try:
        await asyncio.wait_for(new_client.connect(), timeout=15)
        sent = await asyncio.wait_for(new_client.send_code_request(phone), timeout=15)
    except FloodWaitError as e:
        try:
            await asyncio.wait_for(new_client.disconnect(), timeout=5)
        except Exception:
            pass
        raise AccountPipelineError(f"FloodWait при запросе кода: подожди {e.seconds}с") from e
    except Exception as e:
        try:
            await asyncio.wait_for(new_client.disconnect(), timeout=5)
        except Exception:
            pass
        raise AccountPipelineError(f"Не удалось запросить код: {e}") from e

    sent_type = type(sent.type).__name__
    next_type = type(sent.next_type).__name__ if getattr(sent, "next_type", None) else "none"
    log.info("Код запрошен для %s: sent.type=%s, next_type=%s", phone, sent_type, next_type)

    if sent_type != "SentCodeTypeApp" and "App" in next_type:
        try:
            sent = await asyncio.wait_for(
                new_client(ResendCodeRequest(phone_number=phone, phone_code_hash=sent.phone_code_hash)),
                timeout=15,
            )
            sent_type = type(sent.type).__name__
            log.info("ResendCodeRequest: новый sent.type=%s", sent_type)
        except Exception as e:
            log.warning("ResendCodeRequest не удался: %s", e)

    if sent_type != "SentCodeTypeApp" and not preloaded_code:
        try:
            await asyncio.wait_for(new_client.disconnect(), timeout=5)
        except Exception:
            pass
        raise AccountPipelineError(
            f"Telegram отправил код через {sent_type} (не в чат 777000, next_type={next_type}). "
            f"Проверьте API_ID/API_HASH или параметры устройства."
        )

    sent_holder = {"sent": sent}

    async def _resend_app_code():
        cur_sent = sent_holder["sent"]
        cur_next = type(cur_sent.next_type).__name__ if getattr(cur_sent, "next_type", None) else "none"
        if "App" in cur_next:
            log.info("Код не пришёл за 30с — ResendCodeRequest для %s", phone)
            new_sent = await asyncio.wait_for(
                new_client(ResendCodeRequest(phone_number=phone, phone_code_hash=cur_sent.phone_code_hash)),
                timeout=15,
            )
        else:
            log.info("Код не пришёл за 30с — повторный send_code_request для %s", phone)
            new_sent = await asyncio.wait_for(new_client.send_code_request(phone), timeout=15)
        new_type = type(new_sent.type).__name__
        if new_type == "SentCodeTypeApp":
            sent_holder["sent"] = new_sent
        log.info("Повторный запрос кода: sent.type=%s", new_type)

    if preloaded_code:
        code = preloaded_code
        log.info("Использую preloaded_code")
    else:
        code = await _wait_for_code(
            client_for_code_receiving,
            baseline_msg_id=baseline_msg_id,
            service_peer=service_peer,
            timeout=80,
            lolz_code_fetcher=lolz_code_fetcher,
            stale_lolz_code=stale_lolz_code,
            stale_lolz_known=stale_lolz_known,
            on_resend=_resend_app_code,
        )
        if not code:
            cur_id, cur_date = await _get_last_service_msg_info(client_for_code_receiving, service_peer)
            try:
                await asyncio.wait_for(new_client.disconnect(), timeout=5)
            except Exception:
                pass
            raise AccountPipelineError(
                f"Код входа (sent.type={sent_type}, baseline={baseline_msg_id} [{last_msg_date}], "
                f"после={cur_id} [{cur_date}]) не прочитан из чата 777000 за 80 секунд"
            )
        log.info("Код получен: %s", code)

    try:
        await asyncio.wait_for(
            new_client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=sent_holder["sent"].phone_code_hash,
            ),
            timeout=15,
        )
    except SessionPasswordNeededError:
        if not password:
            await asyncio.wait_for(new_client.disconnect(), timeout=5)
            raise AccountPipelineError("На аккаунте 2FA, пароль неизвестен. Ставь фильтр «Пароль: Нет».")
        try:
            await asyncio.wait_for(new_client.sign_in(password=password), timeout=15)
        except PasswordHashInvalidError as e:
            await asyncio.wait_for(new_client.disconnect(), timeout=5)
            raise AccountPipelineError("Неверный облачный пароль (2FA)") from e
        except Exception as e:
            await asyncio.wait_for(new_client.disconnect(), timeout=5)
            raise AccountPipelineError(f"Не удалось войти с 2FA: {e}") from e
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        await asyncio.wait_for(new_client.disconnect(), timeout=5)
        raise AccountPipelineError(f"Полученный код ({code}) не подошёл или истёк: {e}") from e
    except FloodWaitError as e:
        await asyncio.wait_for(new_client.disconnect(), timeout=5)
        raise AccountPipelineError(f"FloodWait при sign_in: {e.seconds}с") from e
    except Exception as e:
        await asyncio.wait_for(new_client.disconnect(), timeout=5)
        raise AccountPipelineError(f"Не удалось войти новой сессией: {e}") from e

    if not await asyncio.wait_for(new_client.is_user_authorized(), timeout=15):
        await asyncio.wait_for(new_client.disconnect(), timeout=5)
        raise AccountPipelineError("Новая сессия не авторизована после sign_in")

    me = await asyncio.wait_for(new_client.get_me(), timeout=15)
    log.info("Новая сессия OK: id=%s phone=%s", me.id, me.phone)
    return new_client


async def get_auth_hex_and_dc(client: TelegramClient) -> tuple[str, int]:
    auth_key = client.session.auth_key
    if auth_key is None or not getattr(auth_key, "key", None):
        raise AccountPipelineError("У сессии нет auth_key")
    hex_key = auth_key.key.hex()
    dc_id = int(client.session.dc_id)
    if not (1 <= dc_id <= 5):
        raise AccountPipelineError(f"Странный dc_id у новой сессии: {dc_id}")
    return hex_key, dc_id


async def terminate_this_session(client: TelegramClient) -> tuple[bool, str]:
    ok = False
    err_msg = "неизвестная ошибка"
    for attempt in range(2):
        try:
            if not client.is_connected():
                await asyncio.wait_for(client.connect(), timeout=10)
            await asyncio.wait_for(client(LogOutRequest()), timeout=10)
            log.info("Купленная сессия завершена (LogOut)")
            ok = True
            err_msg = "ok"
            break
        except Exception as e:
            err_msg = str(e)
            log.warning("LogOut купленной сессии (попытка %s): %s", attempt + 1, e)
            await asyncio.sleep(1.0)
    try:
        await asyncio.wait_for(client.disconnect(), timeout=5)
    except Exception:
        pass
    return ok, err_msg


async def cleanup_account(client: TelegramClient) -> dict:
    report = {"deleted": 0, "failed": 0, "errors": []}
    try:
        await asyncio.wait_for(client.get_dialogs(limit=200), timeout=15)
    except Exception as e:
        log.warning("Не удалось предварительно загрузить диалоги: %s", e)

    dialogs = []
    try:
        async def _collect():
            async for dialog in client.iter_dialogs(limit=200):
                if dialog.id != TELEGRAM_SERVICE_ID:
                    dialogs.append(dialog)
        await asyncio.wait_for(_collect(), timeout=15)
    except Exception as e:
        report["errors"].append(f"не удалось получить список диалогов: {e}")
        log.warning("Ошибка получения списка диалогов: %s", e)

    for dialog in dialogs:
        try:
            if dialog.is_channel or dialog.is_group:
                try:
                    await asyncio.wait_for(client(LeaveChannelRequest(dialog.entity)), timeout=10)
                except Exception as e:
                    log.info("Leave не удался для %s (%s): %s", dialog.id, dialog.name, e)
            await asyncio.wait_for(client.delete_dialog(dialog.entity, revoke=True), timeout=10)
            report["deleted"] += 1
        except Exception as e:
            report["failed"] += 1
            msg = f"{dialog.id} ({dialog.name or '—'}): {e}"
            report["errors"].append(msg)
            log.warning("Не удалось удалить диалог %s", msg)
        await asyncio.sleep(0.3)

    try:
        contacts = await asyncio.wait_for(client(GetContactsRequest(hash=0)), timeout=10)
        users = getattr(contacts, "users", None) or []
        if users:
            await asyncio.wait_for(client(DeleteContactsRequest(id=users)), timeout=10)
            log.info("Удалено контактов: %s", len(users))
    except Exception as e:
        report["errors"].append(f"контакты: {e}")
        log.warning("Ошибка при удалении контактов: %s", e)

    log.info("Очистка аккаунта: удалено %s, не удалось %s", report["deleted"], report["failed"])
    return report


async def check_spam_block(client: TelegramClient, timeout: int = 20) -> str:
    try:
        bot_entity = await asyncio.wait_for(client.get_entity(SPAMBOT_USERNAME), timeout=10)
    except Exception as e:
        log.warning("Не удалось найти @%s: %s", SPAMBOT_USERNAME, e)
        return "unknown"

    last_id = 0
    try:
        prev = await asyncio.wait_for(client.get_messages(bot_entity, limit=1), timeout=10)
        if prev:
            last_id = int(prev[0].id)
    except Exception:
        last_id = 0

    try:
        sent_msg = await asyncio.wait_for(client.send_message(bot_entity, "/start"), timeout=10)
        if sent_msg and getattr(sent_msg, "id", None):
            last_id = max(last_id, int(sent_msg.id))
    except FloodWaitError as e:
        log.warning("FloodWait при проверке спам-блока: %ss", e.seconds)
        return "unknown"
    except Exception as e:
        log.warning("Не удалось написать @%s: %s", SPAMBOT_USERNAME, e)
        return "unknown"

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    seen_ids: set[int] = set()

    async def _poll_spambot() -> str | None:
        async for msg in client.iter_messages(bot_entity, limit=5):
            if int(msg.id) <= last_id:
                break
            if msg.id in seen_ids or getattr(msg, "out", False):
                continue
            seen_ids.add(msg.id)
            text = (msg.raw_text or msg.text or "").lower()
            if not text:
                continue
            if any(
                p in text
                for p in ("good news", "no limits", "не ограничен", "хороших новостей", "свободен")
            ) and not any(
                p in text for p in ("temporary", "limited", "ограничен", "restrict")
            ):
                return "free"
            if any(
                p in text
                for p in ("temporarily limited", "limited", "ограничен", "restrict", "read-only")
            ):
                return "limited"
        return None

    while loop.time() < deadline:
        try:
            res = await asyncio.wait_for(_poll_spambot(), timeout=8)
            if res:
                return res
        except Exception:
            pass
        await asyncio.sleep(1.5)

    return "unknown"