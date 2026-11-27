"""
НЕЗАВИСИМЫЙ модуль — отдельный файл, не меняет telethon_actions.py.
Привязка email для входа на уже авторизованной сессии (EmailVerifyPurposeLoginChange).

ВАЖНО, честно: LoginChange работает только если на аккаунте email для
входа УЖЕ был настроен раньше (это смена существующего, не первая
настройка). Для свежекупленных аккаунтов, где email для входа ещё
никогда не было, Telegram ответит ошибкой EMAIL_NOT_SETUP — в этом
случае функция просто вернёт False, ничего не сломав.
"""
import logging
import time

from telethon import TelegramClient
from telethon.tl.functions.account import SendVerifyEmailCodeRequest, VerifyEmailRequest
from telethon.tl.types import EmailVerifyPurposeLoginChange, EmailVerificationCode

from email_client import wait_for_email_code, EMAIL_ADDRESS

log = logging.getLogger("email_login")


async def try_attach_email(client: TelegramClient) -> bool:
    """
    Пытается привязать EMAIL_ADDRESS как email для входа на аккаунте,
    к которому уже подключён client. Возвращает True/False — никогда
    не поднимает исключение наружу, безопасно вызывать откуда угодно.
    """
    if not EMAIL_ADDRESS:
        return False
    try:
        request_time = time.time()
        await client(SendVerifyEmailCodeRequest(
            purpose=EmailVerifyPurposeLoginChange(),
            email=EMAIL_ADDRESS,
        ))
    except Exception as e:
        log.info("Email не привязан (запрос кода не удался): %s", e)
        return False

    code = await wait_for_email_code(after_ts=request_time, timeout=90)
    if not code:
        log.info("Email не привязан: код с почты не пришёл за 90 сек")
        return False

    try:
        await client(VerifyEmailRequest(
            purpose=EmailVerifyPurposeLoginChange(),
            verification=EmailVerificationCode(code=code),
        ))
    except Exception as e:
        log.info("Email не привязан (подтверждение не удалось): %s", e)
        return False

    log.info("Email успешно привязан")
    return True
