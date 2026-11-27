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

PRIORITY_CODE_RE = re.compile(
    r"(?:code|код|confirmation|подтверждения)[^\n\d]{0,40}(\d{5,8})\b",
    re.IGNORECASE,
)
FALLBACK_CODE_RE = re.compile(r"\b(\d{5,8})\b")


def _extract_text(msg) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            text = payload.decode(errors="ignore")
            if ctype == "text/plain":
                plain_parts.append(text)
            elif ctype == "text/html":
                clean = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
                clean = re.sub(r"<[^>]+>", " ", clean)
                html_parts.append(clean)
        return " ".join(plain_parts) if plain_parts else " ".join(html_parts)

    payload = msg.get_payload(decode=True)
    text = payload.decode(errors="ignore") if payload else str(msg.get_payload())
    if msg.get_content_type() == "text/html":
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
    return text


def _extract_email_code(body: str) -> str | None:
    if not body:
        return None
    m = PRIORITY_CODE_RE.search(body)
    if m and m.group(1) != "777000":
        return m.group(1)
    for candidate in FALLBACK_CODE_RE.findall(body):
        if candidate != "777000":
            return candidate
    return None


def _fetch_latest_code_sync(after_ts: float) -> str | None:
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        return None
    imap = None
    try:
        imap = imaplib.IMAP4_SSL(EMAIL_IMAP_SERVER, timeout=15)
        imap.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        imap.select("INBOX")

        status, data = imap.search(None, '(FROM "telegram")')
        ids = data[0].split() if status == "OK" and data and data[0] else []
        if not ids:
            status, data = imap.search(None, "UNSEEN")
            ids = data[0].split() if status == "OK" and data and data[0] else []

        for msg_id in reversed(ids[-10:]):
            status, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw_bytes = msg_data[0][1] if isinstance(msg_data[0], tuple) else None
            if not raw_bytes:
                continue
            msg = email.message_from_bytes(raw_bytes)
            try:
                parsed = email.utils.parsedate_tz(msg.get("Date"))
                msg_ts = email.utils.mktime_tz(parsed) if parsed else None
            except Exception:
                msg_ts = None
            if msg_ts is not None and msg_ts < (after_ts - 60):
                continue
            body = _extract_text(msg)
            code = _extract_email_code(body)
            if code:
                return code
    except Exception as e:
        log.warning("Ошибка чтения почты (%s): %s", EMAIL_IMAP_SERVER, e)
        return None
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass
    return None


async def wait_for_email_code(after_ts: float, timeout: int = 90, interval: int = 5) -> str | None:
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        return None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            code = await asyncio.wait_for(
                asyncio.to_thread(_fetch_latest_code_sync, after_ts),
                timeout=20,
            )
            if code:
                return code
        except asyncio.TimeoutError:
            log.warning("Таймаут подключения к IMAP %s", EMAIL_IMAP_SERVER)
        await asyncio.sleep(interval)
    return None