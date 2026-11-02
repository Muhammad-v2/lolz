"""
Обёртка над Lolz Market API.

Использует стороннюю типизированную библиотеку lztmarket (пакет на PyPI
"lztmarket", модуль "lztmarketapi"), которая сверена с официальной
OpenAPI-схемой Market API — то есть названия полей в ответах берутся
из реальной схемы API, а не угадываются вручную.

Если какого-то метода в библиотеке ещё нет (новый/редкий эндпоинт),
используется типизированный "escape hatch" market.general.request(...).
"""
import logging
from decimal import Decimal

from lztmarketapi import (
    AsyncLZTMarket, CategoryName, SearchParams, OrderBy,
    AddItemRequest, CategoryID, Currency, ItemOrigin,
    APIError, RateLimitError,
)

from config import LOLZ_TOKEN, ALLOWED_ORIGINS

log = logging.getLogger("lolz_client")


class LolzError(Exception):
    pass


class LolzClient:
    def __init__(self, token: str = LOLZ_TOKEN):
        self._market = AsyncLZTMarket(token, locale="ru-RU")

    async def close(self):
        await self._market.aclose()

    async def search_telegram_accounts(self, price_min: float, price_max: float,
                                        spam: str = "nomatter", password: str = "no",
                                        session_age_value: int = 1, session_age_period: str = "day"):
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

        # response.items — список pydantic-моделей; превращаем в простые словари,
        # чтобы остальной код (worker.py) мог работать с ними как раньше.
        return [item.model_dump() for item in response.items]

    async def buy_item(self, item_id: int, price: float):
        """Быстрая покупка лота (через escape hatch — метод fast-buy)."""
        try:
            result = await self._market.general.request(
                "POST", f"/{item_id}/fast-buy", dict,
                json={"price": str(price)},
            )
        except (APIError, RateLimitError) as e:
            raise LolzError(f"Покупка не удалась: {e}") from e
        return result

    async def get_item_login_data(self, item_id: int):
        """
        Возвращает ПОЛНЫЕ данные по купленному лоту, включая всё, что вернула
        pydantic-модель (включая нестандартные/новые поля — через model_extra),
        чтобы не терять данные для входа, даже если их имя нам неизвестно заранее.
        """
        try:
            response = await self._market.items.get(item_id)
        except (APIError, RateLimitError) as e:
            raise LolzError(f"Не удалось получить данные лота: {e}") from e

        data = response.item.model_dump()
        # добавляем "сырые" дополнительные поля, если pydantic их не распознал
        extra = getattr(response.item, "model_extra", None)
        if extra:
            data.update(extra)
        return data

    async def list_for_sale(self, title: str, price: float, description: str,
                             information: str, hex_key: str, dc_id: int):
        req = AddItemRequest(
            title=title,
            price=Decimal(str(price)),
            category_id=CategoryID.TELEGRAM,
            currency=Currency.RUB,
            item_origin=ItemOrigin.RESALE,
            description=description,
            information=information,
        )
        # Поля для HEX/DC специфичны для категории Telegram — pydantic
        # в lztmarket пропускает неизвестные ему доп. поля через model_extra,
        # поэтому передаём их через словарь перед отправкой.
        payload = req.model_dump(exclude_none=True)
        payload["extra[auth_key]"] = hex_key
        payload["extra[dc_id]"] = dc_id

        try:
            result = await self._market.general.request(
                "POST", "/item/fast-sell", dict, json=payload,
            )
        except (APIError, RateLimitError) as e:
            raise LolzError(f"Не удалось выставить лот: {e}") from e
        return result

    async def get_balance(self) -> float:
        try:
            response = await self._market.profile.get()
        except (APIError, RateLimitError) as e:
            raise LolzError(f"Не удалось получить баланс: {e}") from e
        return float(response.user.balance)
