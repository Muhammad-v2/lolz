import os
from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int = 0) -> int:
    val = os.getenv(name, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


LOLZ_TOKEN = os.getenv("LOLZ_TOKEN", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = _int_env("ADMIN_ID", 0)

API_ID = _int_env("API_ID", 0)
API_HASH = os.getenv("API_HASH", "").strip()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SECRET_WORD = os.getenv("SECRET_WORD", "Зенит").strip()

# Параметры устройства для Telethon (помогают получать код в 777000 вместо SMS)
TG_DEVICE_MODEL = os.getenv("TG_DEVICE_MODEL", "Desktop").strip()
TG_SYSTEM_VERSION = os.getenv("TG_SYSTEM_VERSION", "Windows 10").strip()
TG_APP_VERSION = os.getenv("TG_APP_VERSION", "5.10.3 x64").strip()
TG_LANG_CODE = os.getenv("TG_LANG_CODE", "en").strip()
TG_SYSTEM_LANG_CODE = os.getenv("TG_SYSTEM_LANG_CODE", "en-US").strip()

ALLOWED_ORIGINS = [
    "resale",
    "autoreg",
    "self_registration",
    "personal",
    "dummy",
    "retrieve_via_support",
]

DEFAULTS = {
    "check_spam_block": 1,
    "buy_min_price": 5,
    "buy_max_price": 7,
    "sell_price": 11,
    "search_interval_sec": 60,
    "filter_spam": "nomatter",
    "filter_password": "no",
    "session_age_value": 1,
    "session_age_period": "day",
    "title_template": "+{code} {country} | Чистый | Не трогал",
    "sell_description": "После покупки меняйте почту и 2FA.",
    "sell_buyer_note": "Напишите отзыв.",
}
