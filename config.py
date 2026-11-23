import os
from dotenv import load_dotenv

load_dotenv()

LOLZ_TOKEN = os.getenv("LOLZ_TOKEN", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
SECRET_WORD = os.getenv("SECRET_WORD", "Зенит")

EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS", "")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "")
EMAIL_IMAP_SERVER = os.getenv("EMAIL_IMAP_SERVER", "imap.mail.ru")

ALLOWED_ORIGINS = ["resale", "autoreg", "self_registration"]

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