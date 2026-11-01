"""
Обёртка над Lolz (lzt.market) Market API через httpx.

Сверено с официальной документацией:
https://lzt-market.readme.io/reference/information

Места, которые всё ещё стоит проверить на реальных данных (TODO),
касаются только структуры ответа API при покупке/продаже — сами
параметры запросов уже сверены с документацией.
"""
import httpx
from config import LOLZ_TOKEN, ALLOWED_ORIGINS

BASE_URL = "https://api.lzt.market"


class LolzError(Exception):
    pass


class LolzClient:
    def __init__(self, token: str = LOLZ_TOKEN):
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = httpx.AsyncClient(base_url=BASE_URL, headers=self._headers, timeout=300)

    async def close(self):
        await self._client.aclose()

    async def search_telegram_accounts(self, price_min: float, price_max: float,
                                        spam: str = "nomatter", password: str = "no",
                                        session_age_value: int = 1, session_age_period: str = "day"):
        """
        Ищет лоты Telegram-аккаунтов по цене, разрешённым категориям
        происхождения и остальным фильтрам (спам-блок, пароль, возраст сессии) —
        как на сайте lzt.market.
        """
        params = {
            "pmin": price_min,
            "pmax": price_max,
            "origin[]": ALLOWED_ORIGINS,
            "order_by": "price_to_up",
            "spam": spam,
            "password": password,
            "session_age": session_age_value,
            "session_age_period": session_age_period,
        }
        resp = await self._client.get("/telegram", params=params)
        if resp.status_code != 200:
            raise LolzError(f"Поиск не удался ({resp.status_code}): {resp.text}")
        data = resp.json()
        return data.get("items", [])

    async def buy_item(self, item_id: int, price: float):
        """Быстрая покупка лота по фиксированной цене (эндпоинт /{item_id}/fast-buy)."""
        payload = {"price": price}
        resp = await self._client.post(f"/{item_id}/fast-buy", data=payload)
        if resp.status_code != 200:
            raise LolzError(f"Покупка не удалась ({resp.status_code}): {resp.text}")
        return resp.json()

    async def get_item_login_data(self, item_id: int):
        """Возвращает данные для входа в купленный аккаунт."""
        resp = await self._client.get(f"/{item_id}")
        if resp.status_code != 200:
            raise LolzError(f"Не удалось получить данные лота ({resp.status_code}): {resp.text}")
        return resp.json()

    async def list_for_sale(self, title: str, price: float, description: str,
                             information: str, hex_key: str, dc_id: int):
        """
        Выставляет новый товар (Telegram-аккаунт) на продажу через
        эндпоинт /item/fast-sell. category_id=24 — это Telegram
        (список категорий из документации).

        TODO: поле extra для передачи HEX/DC не описано в общей
        схеме (видимо, зависит от категории) — нужно свериться по
        реальному ответу от /item/fast-sell либо по разделу
        "Check Account Details" для категории Telegram.
        """
        payload = {
            "title": title,
            "price": price,
            "category_id": 24,  # Telegram
            "currency": "rub",
            "item_origin": "resale",
            "description": description,
            "information": information,
            "extra[auth_key]": hex_key,
            "extra[dc_id]": dc_id,
        }
        resp = await self._client.post("/item/fast-sell", data=payload)
        if resp.status_code != 200:
            raise LolzError(f"Не удалось выставить лот ({resp.status_code}): {resp.text}")
        return resp.json()

    async def get_balance(self) -> float:
        resp = await self._client.get("/me")
        if resp.status_code != 200:
            raise LolzError(f"Не удалось получить баланс ({resp.status_code}): {resp.text}")
        data = resp.json()
        return float(data.get("user", {}).get("balance", 0))
