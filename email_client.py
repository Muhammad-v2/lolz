"""
НЕЗАВИСИМЫЙ модуль — не импортируется нигде, пока сам не подключишь.
Читает код подтверждения email от Telegram из почты по IMAP (Mail.ru и т.д.).
"""
import asyncio
import email
import email.utils
import imaplib
import logging
import os
import re

log = logging.getLogger("email_client")

EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS", "").strip()
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "").strip()
EMAIL_IMAP_SERVER = os.getenv("EMAIL_IMAP_SERVER", "imap.mail.ru").strip()

CODE_RE = re.compile(r"\b(\d{5,8})\b")


def _fetch_latest_code_sync(after_ts: float):
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        return None
    try:
        imap = imaplib.IMAP4_SSL(EMAIL_IMAP_SERVER)
        imap.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        imap.select("INBOX")

        status, data = imap.search(None, '(FROM "telegram")')
        ids = data[0].split() if status == "OK" else []
        if not ids:
            status, data = imap.search(None, "UNSEEN")
            ids = data[0].split() if status == "OK" else []

        for msg_id in reversed(ids[-10:]):
            status, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            try:
                msg_ts = email.utils.mktime_tz(email.utils.parsedate_tz(msg.get("Date")))
            except Exception:
                msg_ts = None
            if msg_ts is not None and msg_ts < after_ts:
                continue
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


async def wait_for_email_code(after_ts: float, timeout: int = 90, interval: int = 5):
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        return None
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        code = await asyncio.to_thread(_fetch_latest_code_sync, after_ts)
        if code:
            return code
        await asyncio.sleep(interval)
    return None
