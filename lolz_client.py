"""
Обёртка над Lolz Market API через lztmarket / lztmarketapi.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from lztmarketapi import (
    AsyncLZTMarket,
    CategoryName,
    SearchParams,
    OrderBy,
    CategoryID,
    APIError,
    RateLimitError,
    StatusResponse,
    EditItemRequest,
)

from config import LOLZ_TOKEN, ALLOWED_ORIGINS, SECRET_WORD

log = logging.getLogger("lolz_client")


class LolzError(Exception):
    pass


# ISO → (русское название, телефонный код без +)
COUNTRY_INFO = {
    "RU": ("Россия", "7"),
    "UA": ("Украина", "380"),
    "BY": ("Беларусь", "375"),
    "KZ": ("Казахстан", "7"),
    "UZ": ("Узбекистан", "998"),
    "KG": ("Кыргызстан", "996"),
    "TJ": ("Таджикистан", "992"),
    "AM": ("Армения", "374"),
    "AZ": ("Азербайджан", "994"),
    "GE": ("Грузия", "995"),
    "MD": ("Молдова", "373"),
    "IN": ("Индия", "91"),
    "ID": ("Индонезия", "62"),
    "PH": ("Филиппины", "63"),
    "VN": ("Вьетнам", "84"),
    "TH": ("Таиланд", "66"),
    "MY": ("Малайзия", "60"),
    "BD": ("Бангладеш", "880"),
    "PK": ("Пакистан", "92"),
    "NP": ("Непал", "977"),
    "LK": ("Шри-Ланка", "94"),
    "CN": ("Китай", "86"),
    "HK": ("Гонконг", "852"),
    "TW": ("Тайвань", "886"),
    "JP": ("Япония", "81"),
    "KR": ("Южная Корея", "82"),
    "US": ("США", "1"),
    "CA": ("Канада", "1"),
    "GB": ("Великобритания", "44"),
    "UK": ("Великобритания", "44"),
    "DE": ("Германия", "49"),
    "FR": ("Франция", "33"),
    "IT": ("Италия", "39"),
    "ES": ("Испания", "34"),
    "PL": ("Польша", "48"),
    "RO": ("Румыния", "40"),
    "CZ": ("Чехия", "420"),
    "NL": ("Нидерланды", "31"),
    "BE": ("Бельгия", "32"),
    "TR": ("Турция", "90"),
    "EG": ("Египет", "20"),
    "NG": ("Нигерия", "234"),
    "ZA": ("ЮАР", "27"),
    "BR": ("Бразилия", "55"),
    "AR": ("Аргентина", "54"),
    "MX": ("Мексика", "52"),
    "CO": ("Колумбия", "57"),
    "PE": ("Перу", "51"),
    "CL": ("Чили", "56"),
    "AE": ("ОАЭ", "971"),
    "SA": ("Саудовская Аравия", "966"),
    "IL": ("Израиль", "972"),
    "IQ": ("Ирак", "964"),
    "IR": ("Иран", "98"),
    "AF": ("Афганистан", "93"),
    "AU": ("Австралия", "61"),
    "NZ": ("Новая Зеландия", "64"),
}

PHONE_PREFIXES = sorted(
    [
        ("998", "UZ"), ("996", "KG"), ("995", "GE"), ("994", "AZ"),
        ("992", "TJ"), ("977", "NP"), ("971", "AE"), ("966", "SA"),
        ("972", "IL"), ("964", "IQ"), ("880", "BD"), ("852", "HK"),
        ("886", "TW"), ("380", "UA"), ("375", "BY"), ("374", "AM"),
        ("373", "MD"), ("420", "CZ"), ("234", "NG"),
        ("98", "IR"), ("93", "AF"), ("92", "PK"), ("91", "IN"),
        ("90", "TR"), ("86", "CN"), ("84", "VN"), ("82", "KR"),
        ("81", "JP"), ("66", "TH"), ("63", "PH"), ("62", "ID"),
        ("61", "AU"), ("60", "MY"), ("55", "BR"), ("54", "AR"),
        ("52", "MX"), ("51", "PE"), ("49", "DE"), ("48", "PL"),
        ("44", "GB"), ("40", "RO"), ("39", "IT"), ("34", "ES"),
        ("33", "FR"), ("32", "BE"), ("31", "NL"), ("27", "ZA"),
        ("20", "EG"), ("7", "RU"), ("1", "US"),
    ],
    key=lambda x: -len(x[0]),
)

NAME_TO_ISO = {
    "india": "IN", "индия": "IN",
    "russia": "RU", "россия": "RU", "рф": "RU",
    "ukraine": "UA", "украина": "UA",
    "indonesia": "ID", "индонезия": "ID",
    "usa": "US", "united states": "US", "сша": "US",
    "kazakhstan": "KZ", "казахстан": "KZ",
    "uzbekistan": "UZ", "узбекистан": "UZ",
    "bangladesh": "BD", "бангладеш": "BD",
    "pakistan": "PK", "пакистан": "PK",
    "philippines": "PH", "филиппины": "PH",
    "vietnam": "VN", "вьетнам": "VN",
    "china": "CN", "китай": "CN",
    "turkey": "TR", "турция": "TR",
    "brazil": "BR", "бразилия": "BR",
    "germany": "DE", "германия": "DE",
    "france": "FR", "франция": "FR",
    "uk": "GB", "united kingdom": "GB", "великобритания": "GB",
}


class LolzClient:
    def __init__(self, token: str = LOLZ_TOKEN):
        self._market = AsyncLZTMarket(token, locale="ru-RU")

    async def close(self):
        await self._market.aclose()

    async def search_telegram_accounts(
        self,
        price_min: float,
        price_max: float,
        spam: str = "nomatter",
        password: str = "no",
        session_age_value: int = 1,
        session_age_period: str = "day",
    ):
        params = SearchParams.model_validate({
            "pmin": str(price_min),
            "pmax": str(price_max),
            "order_by": OrderBy.PRICE_ASC,
            "origin[]": ALLOWED_ORIGINS,
            "spam": spam,
            "password": password,
            "session_age": session_age_value,
            "session_age_period": session_age_period,
        })
        try:
            response = await self._market.categories.search(CategoryName.TELEGRAM, params)
        except (APIError, RateLimitError) as e:
            raise LolzError(f"Поиск не удался: {e}") from e

        return [item.model_dump() for item in response.items]

    async def buy_item(self, item_id: int, price: float):
        """
        Fast-buy по доке Lolz:
        при 403 retry_request — повторять тот же запрос (до 100 раз).
        """
        body: dict = {"price": float(price)}
        if SECRET_WORD:
            body["secret_answer"] = SECRET_WORD

        def _err_blob(e: BaseException) -> str:
            parts = [str(e)]
            for attr in (
                "response", "body", "payload", "data", "content",
                "message", "errors", "error", "args",
            ):
                val = getattr(e, attr, None)
                if val is None:
                    continue
                try:
                    parts.append(str(val))
                except Exception:
                    pass
            resp = getattr(e, "response", None)
            if resp is not None:
                for attr in ("text", "content", "body"):
                    try:
                        v = getattr(resp, attr, None)
                        if v is not None and not callable(v):
                            parts.append(str(v)[:2000])
                    except Exception:
                        pass
            return " ".join(parts).lower()

        last_err = None
        max_attempts = 100  # официальная дока Lolz: до 100 повторов

        for attempt in range(1, max_attempts + 1):
            try:
                result = await self._market.general.request(
                    "POST",
                    f"/{item_id}/fast-buy",
                    StatusResponse,
                    json=body,
                )
                data = result.model_dump()
                extra = getattr(result, "model_extra", None)
                if extra:
                    data.update(extra)
                if attempt > 1:
                    log.info("buy_item #%s OK on attempt %s", item_id, attempt)
                return data
            except (APIError, RateLimitError) as e:
                last_err = e
                combined = _err_blob(e)

                # лот уже купили / удалили — не ретраим
                sold_markers = (
                    "item is sold", "item_sold", "already sold",
                    "продан", "deleted", "удалён", "удален",
                    "not available", "недоступен",
                )
                if any(x in combined for x in sold_markers) and "retry" not in combined:
                    raise LolzError(f"Лот уже недоступен: {e}") from e

                is_retry = (
                    "retry_request" in combined
                    or "retry request" in combined
                    or "retry" in combined
                    or "403" in combined
                    or "forbidden" in combined
                )
                is_rate = (
                    "429" in combined
                    or "rate" in combined
                    or isinstance(e, RateLimitError)
                )

                if is_retry or is_rate:
                    delay = 0.35 if is_retry else min(1.5 + attempt * 0.2, 8)
                    if attempt in (1, 5, 10, 25, 50) or attempt == max_attempts:
                        log.warning(
                            "buy_item #%s attempt %s/%s: %s → sleep %.2fs",
                            item_id, attempt, max_attempts, str(e)[:200], delay,
                        )
                    await asyncio.sleep(delay)
                    continue

                raise LolzError(f"Покупка не удалась: {e}") from e
            except Exception as e:
                last_err = e
                combined = _err_blob(e)
                if "403" in combined or "retry" in combined:
                    await asyncio.sleep(0.4)
                    continue
                raise LolzError(f"Покупка не удалась: {e}") from e

        raise LolzError(
            f"Покупка не удалась после {max_attempts} попыток: {last_err}"
        )

    async def get_item_login_data(self, item_id: int) -> dict:
        try:
            response = await self._market.items.get(item_id)
        except (APIError, RateLimitError) as e:
            raise LolzError(f"Не удалось получить данные лота: {e}") from e

        data = response.item.model_dump()
        extra = getattr(response.item, "model_extra", None)
        if extra:
            data.update(extra)

        for nested_key in ("loginData", "login_data", "telegram", "item"):
            nested = data.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    data.setdefault(k, v)

        return data

    @staticmethod
    def extract_hex_dc(data: dict) -> tuple[str | None, int | None]:
        hex_key = None
        for key in (
            "login",
            "auth_key",
            "authKey",
            "telegram_auth_key",
            "telegramAuthKey",
            "hex",
            "auth_hex",
        ):
            val = data.get(key)
            if val is not None and str(val).strip():
                hex_key = str(val).strip()
                break

        lp = data.get("login_password") or data.get("loginPassword")
        dc_from_lp = None
        if lp and ":" in str(lp):
            a, b = str(lp).split(":", 1)
            a, b = a.strip(), b.strip()
            if a.isdigit() and not b.isdigit():
                dc_from_lp = int(a)
                if not hex_key:
                    hex_key = b
            elif b.isdigit() and not a.isdigit():
                dc_from_lp = int(b)
                if not hex_key:
                    hex_key = a
            elif len(a) > len(b) and not hex_key:
                hex_key = a
                if b.isdigit():
                    dc_from_lp = int(b)

        dc_id = None
        for key in (
            "telegram_dc_id",
            "telegramDcId",
            "dc_id",
            "dcId",
            "password",
        ):
            val = data.get(key)
            if val is None or val == "":
                continue
            try:
                dc_id = int(val)
                if dc_id in (1, 2, 3, 4, 5):
                    break
                dc_id = None
            except (TypeError, ValueError):
                continue

        if dc_id is None and dc_from_lp is not None:
            dc_id = dc_from_lp

        return hex_key, dc_id

    @staticmethod
    def extract_phone_country(data: dict) -> tuple[str | None, str, str]:
        """
        Возвращает (phone, country_ru, code_with_plus)
        Пример: ("9198...", "Индия", "+91")
        """
        phone = None
        for key in ("telegram_phone", "telegramPhone", "phone", "login_phone"):
            val = data.get(key)
            if val is not None and str(val).strip():
                phone = str(val).strip()
                break

        raw_country = None
        for key in (
            "telegram_country",
            "telegramCountry",
            "country",
            "country_name",
            "countryCode",
            "country_code",
        ):
            val = data.get(key)
            if val is not None and str(val).strip():
                raw_country = str(val).strip()
                break

        iso = None
        if raw_country:
            up = raw_country.upper().strip()
            if len(up) == 2 and up.isalpha():
                iso = up
            else:
                iso = NAME_TO_ISO.get(raw_country.lower().strip())

        digits = ""
        if phone:
            digits = "".join(ch for ch in phone if ch.isdigit())

        if not iso and digits:
            for prefix, code in PHONE_PREFIXES:
                if digits.startswith(prefix):
                    iso = code
                    break

        country_ru = "—"
        phone_code = ""

        if iso and iso in COUNTRY_INFO:
            country_ru, phone_code = COUNTRY_INFO[iso]
        elif raw_country and len(raw_country) > 2:
            country_ru = raw_country
        elif iso:
            country_ru = iso

        if not phone_code and digits:
            for prefix, code in PHONE_PREFIXES:
                if digits.startswith(prefix):
                    phone_code = prefix
                    if country_ru == "—" and code in COUNTRY_INFO:
                        country_ru, _ = COUNTRY_INFO[code]
                    break

        for key in ("phone_code", "phoneCode"):
            val = data.get(key)
            if val is not None and str(val).strip():
                pc = "".join(ch for ch in str(val) if ch.isdigit())
                if pc:
                    phone_code = pc
                break

        country_code = f"+{phone_code}" if phone_code else ""
        return phone, country_ru, country_code

    async def list_for_sale(
        self,
        title: str,
        price: float,
        description: str,
        information: str,
        hex_key: str,
        dc_id: int,
    ):
        # Для Telegram на Lolz: login = auth_key, password = dc_id
        payload = {
            "title": title,
            "price": str(price),
            "category_id": int(CategoryID.TELEGRAM),
            "currency": "rub",
            "item_origin": "resale",
            "origin": "resale",
            "description": description or "",
            "information": information or "",
            "login": hex_key,
            "password": str(dc_id),
            "login_password": f"{hex_key}:{dc_id}",
        }

        try:
            result = await self._market.general.request(
                "POST",
                "/item/fast-sell",
                StatusResponse,
                json=payload,
            )
        except (APIError, RateLimitError) as e:
            raise LolzError(f"Не удалось выставить лот: {e}") from e
        return result.model_dump()

    async def edit_item_price(self, item_id: int, new_price: float):
        """Меняет цену уже выставленного на продажу лота (кнопка
        «Изменить цену» в карточке товара в боте)."""
        try:
            result = await self._market.items.edit(
                item_id, EditItemRequest(price=Decimal(str(new_price))),
            )
        except (APIError, RateLimitError) as e:
            raise LolzError(f"Не удалось изменить цену: {e}") from e
        return result.model_dump() if hasattr(result, "model_dump") else result

    async def get_balance(self) -> float:
        try:
            response = await self._market.profile.get()
        except (APIError, RateLimitError) as e:
            raise LolzError(f"Не удалось получить баланс: {e}") from e
        return float(response.user.balance)