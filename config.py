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

# Категории лотов на lzt.market, которые разрешено покупать.
# Точные значения — из официальной документации Market API.
# ВАЖНО: категории "stealer" (стиллер) и "phishing" (фишинг) никогда
# не добавлять сюда — это угнанные у реальных людей аккаунты.
ALLOWED_ORIGINS = ["resale", "autoreg", "self_registration"]

# Значения по умолчанию — дальше их можно менять прямо в боте
DEFAULTS = {
    "buy_min_price": 5,
    "buy_max_price": 7,
    "sell_price": 11,
    "min_session_hours": 24,
    "daily_limit": 30,          # лимит трат в рублях на день
    "search_interval_sec": 60,  # как часто искать новые лоты
    "title_template": "+{code} {country} | Чистый | Не трогал",
    "sell_description": "После покупки меняйте почту и 2FA.",
    "sell_buyer_note": "Напишите отзыв.",
}
