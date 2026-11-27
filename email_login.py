import asyncio
import logging
import time

from telethon import TelegramClient
from telethon.tl.functions.account import SendVerifyEmailCodeRequest, VerifyEmailRequest
from telethon.tl.types import EmailVerificationCode, EmailVerifyPurposeLoginChange

from email_client import EMAIL_ADDRESS, EMAIL_APP_PASSWORD, wait_for_email_code

log = logging.getLogger("email_login")


async def try_attach_email(client: TelegramClient) -> tuple[bool, str]:
    """
    Меняет email для входа на EMAIL_ADDRESS на уже авторизованной сессии.
    Возвращает (успех, описание).
    """
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        return False, "EMAIL_ADDRESS или EMAIL_APP_PASSWORD не заданы"

    request_time = time.time()
    try:
        await asyncio.wait_for(
            client(
                SendVerifyEmailCodeRequest(
                    purpose=EmailVerifyPurposeLoginChange(),
                    email=EMAIL_ADDRESS,
                )
            ),
            timeout=20,
        )
    except Exception as e:
        log.info("Email не изменён (запрос кода): %s", e)
        return False, f"запрос кода отклонён: {e}"

    code = await wait_for_email_code(after_ts=request_time, timeout=90, interval=5)
    if not code:
        log.info("Email не изменён: код на %s не пришёл за 90 сек", EMAIL_ADDRESS)
        return False, "код не пришёл на почту за 90 сек"

    try:
        await asyncio.wait_for(
            client(
                VerifyEmailRequest(
                    purpose=EmailVerifyPurposeLoginChange(),
                    verification=EmailVerificationCode(code=code),
                )
            ),
            timeout=20,
        )
    except Exception as e:
        log.info("Email не изменён (подтверждение кода %s): %s", code, e)
        return False, f"подтверждение кода не удалось: {e}"

    log.info("Email для входа изменён на %s", EMAIL_ADDRESS)
    return True, EMAIL_ADDRESS