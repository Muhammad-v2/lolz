"""
Читает код подтверждения email от Telegram из почтового ящика по IMAP.

Используется для автоматической привязки email входа на купленных
аккаунтах — после запроса кода (account.sendVerifyEmailCode) Telegram
присылает письмо с кодом на EMAIL_ADDRESS, а этот модуль его читает
и возвращает, без участия человека.

Важно: почти все почтовые сервисы (Gmail, Яндекс, Mail.ru и т.д.) требуют
для IMAP не обычный пароль от аккаунта, а отдельный "пароль приложения" —
обычный пароль просто не даст подключиться.
"""
import asyncio
import email
import imaplib
import logging
import re
from datetime import datetime, timezone

from config import EMAIL_ADDRESS, EMAIL_APP_PASSWORD, EMAIL_IMAP_SERVER

log = logging.getLogger("email_client")

CODE_RE = re.compile(r"\b(\d{5,8})\b")


class EmailError(Exception):
    pass


def _fetch_latest_code_sync(after_ts: float) -> str | None:
    """Синхронная часть (imaplib не умеет в asyncio) — вызывается через
    asyncio.to_thread, чтобы не блокировать остальной бот."""
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        return None

    try:
        imap = imaplib.IMAP4_SSL(EMAIL_IMAP_SERVER)
        imap.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        imap.select("INBOX")

        # Ищем недавние письма — и от Telegram, и просто непрочитанные,
        # т.к. тема/отправитель у разных писем Telegram могут отличаться.
        status, data = imap.search(None, '(FROM "telegram")')
        ids = data[0].split() if status == "OK" else []
        if not ids:
            status, data = imap.search(None, "UNSEEN")
            ids = data[0].split() if status == "OK" else []

        for msg_id in reversed(ids[-10:]):  # смотрим последние 10, с конца
            status, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])

            msg_date = msg.get("Date")
            try:
                msg_ts = email.utils.mktime_tz(email.utils.parsedate_tz(msg_date))
            except Exception:
                msg_ts = None
            if msg_ts is not None and msg_ts < after_ts:
                continue  # письмо пришло ДО нашего запроса кода — не то

            body = _extract_text(msg)
            m = CODE_RE.search(body)
            if m:
                imap.logout()
                return m.group(1)

        imap.logout()
    except Exception as e:
        log.warning("Ошибка чтения почты: %s", e)
        return None
    return None


def _extract_text(msg) -> str:
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    parts.append(part.get_payload(decode=True).decode(errors="ignore"))
                except Exception:
                    pass
        return " ".join(parts)
    try:
        return msg.get_payload(decode=True).decode(errors="ignore")
    except Exception:
        return str(msg.get_payload())


async def wait_for_email_code(after_ts: float, timeout: int = 90, interval: int = 5) -> str | None:
    """Опрашивает почту, пока не найдёт код (пришедший после after_ts) или не истечёт timeout."""
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        return None
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        code = await asyncio.to_thread(_fetch_latest_code_sync, after_ts)
        if code:
            return code
        await asyncio.sleep(interval)
    return None
