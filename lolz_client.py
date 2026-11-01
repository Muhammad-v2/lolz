"""
Обёртка над Lolz (lzt.market) Market API через httpx.

ВАЖНО: перед первым запуском свериться с официальной документацией
https://docs.api.zelenka.guru/market/ — названия некоторых параметров
могли поменяться. Места, которые стоит перепроверить, помечены TODO.
"""
import httpx
from config import LOLZ_TOKEN, ALLOWED_ORIGINS, SECRET_WORD

BASE_URL = "https://prod-api.lzt.market"


class LolzError(Exception):
    pass


class LolzClient:
    def __init__(self, token: str = LOLZ_TOKEN):
        self._token = token
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = httpx.AsyncClient(base_url=BASE_URL, headers=self._headers, timeout=30)

    def _with_token(self, params: dict) -> dict:
        """На случай, если заголовок Authorization не сработает — дублируем
        токен как query-параметр oauth_token (второй вариант авторизации по доке)."""
        params = dict(params)
        params["oauth_token"] = self._token
        return params

    async def close(self):
        await self._client.aclose()

    async def search_telegram_accounts(self, price_min: float, price_max: float,
                                        min_session_hours: int = 24):
        """
        Ищет лоты Telegram-аккаунтов по цене и разрешённым категориям
        происхождения (resale / autoreg / selfreg).
        Возвращает список найденных лотов (сырые словари из API).
        """
        params = {
            "pmin": price_min,
            "pmax": price_max,
            "origin[]": ALLOWED_ORIGINS,   # TODO: сверить точное имя параметра
            "order_by": "price_to_up",
            "nsb": 1,   # TODO: "без спам-блока" — если неверно, убрать
        }
        resp = await self._client.get("/telegram", params=self._with_token(params))
        if resp.status_code != 200:
            raise LolzError(f"Поиск не удался ({resp.status_code}): {resp.text}")
        data = resp.json()
        items = data.get("items", [])

        # Доп. фильтрация на нашей стороне — по возрасту последней сессии
        # и отсутствию пароля, на случай если API не поддержит эти фильтры напрямую.
        filtered = []
        for item in items:
            if item.get("telegram_has_password"):
                continue
            session_hours = item.get("telegram_last_online_hours") or item.get("session_hours")
            if session_hours is not None and session_hours < min_session_hours:
                continue
            origin = item.get("origin")
            if origin and origin not in ALLOWED_ORIGINS:
                continue
            filtered.append(item)
        return filtered

    async def buy_item(self, item_id: int, price: float):
        """
        Быстрая покупка лота по фиксированной цене.
        Если на аккаунте стоит секретное слово — передаём его.
        """
        payload = {
            "price": price,
            "secret_answer": SECRET_WORD,  # TODO: сверить имя поля в доке "fast-buy"
        }
        resp = await self._client.post(f"/{item_id}/fast-buy", data=payload)
        if resp.status_code != 200:
            raise LolzError(f"Покупка не удалась ({resp.status_code}): {resp.text}")
        return resp.json()

    async def get_item_login_data(self, item_id: int):
        """Возвращает данные для входа в купленный аккаунт (сессию/tdata/json)."""
        resp = await self._client.get(f"/{item_id}")
        if resp.status_code != 200:
            raise LolzError(f"Не удалось получить данные лота ({resp.status_code}): {resp.text}")
        return resp.json()

    async def list_for_sale(self, title: str, price: float, description: str,
                             buyer_note: str, hex_key: str, dc_id: int):
        """
        Выставляет новый товар (Telegram-аккаунт) на продажу в категории "перепродажа".
        """
        payload = {
            "title": title,
            "price": price,
            "item_origin": "resale",
            "item_description": description,
            "extended_description": buyer_note,   # TODO: сверить имя поля
            "telegram_auth": f"{hex_key}:{dc_id}",  # TODO: сверить формат поля HEX:DC
        }
        resp = await self._client.post("/telegram", data=payload)
        if resp.status_code != 200:
            raise LolzError(f"Не удалось выставить лот ({resp.status_code}): {resp.text}")
        return resp.json()

    async def get_balance(self) -> float:
        resp = await self._client.get("/me")
        if resp.status_code != 200:
            raise LolzError(f"Не удалось получить баланс ({resp.status_code}): {resp.text}")
        data = resp.json()
        return float(data.get("user", {}).get("balance", 0))
