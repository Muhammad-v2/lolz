# Lolz Market Bot

## Установка в Termux

```
pkg install git python -y
git clone <ссылка на твой репозиторий>
cd <папка проекта>
pip install -r requirements.txt
```

## Настройка

```
nano .env
```
Заполни по образцу из `.env.example` (токен Lolz, токен бота, свой Telegram ID,
api_id/api_hash, строку подключения Neon).

## Запуск

```
python main.py
```

## Важно перед первым реальным запуском

В `lolz_client.py` несколько мест помечены `TODO` — названия некоторых
параметров API (происхождение лота, формат HEX:DC и т.п.) нужно свериться
с официальной документацией Lolz Market API:
https://docs.api.zelenka.guru/?market

Лучше всего: сначала вызвать `search_telegram_accounts` и
`get_item_login_data` на тестовом запросе и посмотреть, что реально
возвращает API, затем поправить имена полей при необходимости.

## Команды бота

- `/status` — текущее состояние (баланс, очередь, последние покупки)
- `/buy N` — поставить в очередь покупку N аккаунтов
- `/limit N` — дневной лимит трат, ₽
- `/buyprice MIN MAX` — диапазон цены покупки
- `/sellprice N` — цена продажи
- `/title шаблон` — шаблон заголовка, напр. `+{code} {country} | Чистый | Не трогал`
- `/interval сек` — как часто искать новые лоты
