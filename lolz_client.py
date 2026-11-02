"""
Обёртка над Lolz Market API через lztmarket / lztmarketapi.
"""
from __future__ import annotations

import logging

from lztmarketapi import (
    AsyncLZTMarket,
    CategoryName,
    SearchParams,
    OrderBy,
    CategoryID,
    APIError,
    RateLimitError,
    StatusResponse,
)

from config import LOLZ_TOKEN, ALLOWED_ORIGINS, SECRET_WORD

log = logging.getLogger("lolz_client")


class LolzError(Exception):
    pass


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
        body: dict = {"price": str(price)}
        if SECRET_WORD:
            body["secret_answer"] = SECRET_WORD

        try:
            result = await self._market.general.request(
                "POST",
                f"/{item_id}/fast-buy",
                StatusResponse,
                json=body,
            )
        except (APIError, RateLimitError) as e:
            raise LolzError(f"Покупка не удалась: {e}") from e
        data = result.model_dump()
        extra = getattr(result, "model_extra", None)
        if extra:
            data.update(extra)
        return data

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
        phone = None
        for key in ("telegram_phone", "telegramPhone", "phone", "login_phone"):
            val = data.get(key)
            if val is not None and str(val).strip():
                phone = str(val).strip()
                break

        country = None
        for key in ("telegram_country", "telegramCountry", "country", "country_name"):
            val = data.get(key)
            if val is not None and str(val).strip():
                country = str(val).strip()
                break
        country = country or "—"

        country_code = ""
        if phone:
            parts = phone.replace("(", " ").replace(")", " ").split()
            if parts and parts[0].startswith("+"):
                country_code = parts[0]
            else:
                digits = "".join(ch for ch in phone if ch.isdigit())
                if digits.startswith("7") and len(digits) >= 11:
                    country_code = "+7"
                elif digits.startswith("380"):
                    country_code = "+380"
                elif digits.startswith("1") and len(digits) >= 11:
                    country_code = "+1"

        for key in ("phone_code", "phoneCode", "country_code", "countryCode"):
            val = data.get(key)
            if val is not None and str(val).strip():
                country_code = str(val).strip()
                if not country_code.startswith("+") and country_code.isdigit():
                    country_code = "+" + country_code
                break

        return phone, country, country_code

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

    async def get_balance(self) -> float:
        try:
            response = await self._market.profile.get()
        except (APIError, RateLimitError) as e:
            raise LolzError(f"Не удалось получить баланс: {e}") from e
        return float(response.user.balance)