async def _resolve_dc_address(dc_id: int) -> tuple[str, int]:
    """
    Спрашивает у Telegram актуальный IP нужного DC,
    при ошибке возвращает запасной адрес из DC_ADDRESSES.
    """
    bootstrap = TelegramClient(MemorySession(), API_ID, API_HASH)
    try:
        await bootstrap.connect()
        config = await bootstrap(GetConfigRequest())
        candidates = [
            o
            for o in config.dc_options
            if o.id == dc_id and not o.ipv6 and not o.media_only and not o.tcpo_only
        ]
        if not candidates:
            candidates = [o for o in config.dc_options if o.id == dc_id and not o.ipv6]
        if candidates:
            return candidates[0].ip_address, candidates[0].port
    except Exception as e:
        log.warning("Не удалось получить актуальный адрес DC%s: %s", dc_id, e)
    finally:
        await bootstrap.disconnect()

    ip = DC_ADDRESSES.get(dc_id)
    if not ip:
        raise AccountPipelineError(f"Неизвестный DC ID: {dc_id}")
    return ip, 443


async def connect_from_hex_dc(hex_key: str, dc_id: int) -> TelegramClient:
    if not API_ID or not API_HASH:
        raise AccountPipelineError("Не заданы API_ID / API_HASH в переменных окружения")

    dc_id = int(dc_id)
    ip, port = await _resolve_dc_address(dc_id)

    h = _normalize_hex(hex_key)
    try:
        key_bytes = bytes.fromhex(h)
    except ValueError as e:
        raise AccountPipelineError(f"Не удалось разобрать HEX: {e}") from e

    session = MemorySession()
    session.set_dc(dc_id, ip, port)
    session.auth_key = AuthKey(data=key_bytes)

    client = TelegramClient(
        session,
        API_ID,
        API_HASH,
        device_model=TG_DEVICE_MODEL,
        system_version=TG_SYSTEM_VERSION,
        app_version=TG_APP_VERSION,
        lang_code=TG_LANG_CODE,
        system_lang_code=TG_SYSTEM_LANG_CODE,
    )
    client.session.set_dc(dc_id, ip, port)

    try:
        await client.connect()
    except Exception as e:
        raise AccountPipelineError(f"Не удалось подключиться к DC{dc_id}: {e}") from e

    if not await client.is_user_authorized():
        await client.disconnect()
        raise AccountPipelineError(
            "Купленный аккаунт не авторизован (HEX/DC неверны или сессия уже убита)"
        )

    me = await client.get_me()
    log.info("Подключены как %s (id=%s) через DC%s", me.phone or me.username, me.id, dc_id)
    return client


async def _ensure_service_chat_cached(client: TelegramClient, limit: int = 200) -> bool:
    """
    Ищет чат 777000 среди диалогов, чтобы поместить его в entity cache.
    Останавливается сразу, как только чат найден.
    """
    try:
        async for dialog in client.iter_dialogs(limit=limit):
            if dialog.id == TELEGRAM_SERVICE_ID:
                return True
    except Exception as e:
        log.warning("Не удалось просканировать диалоги для поиска 777000: %s", e)
    return False


async def prepare_client_for_code(client: TelegramClient) -> int:
    """
    Прогревает MemorySession перед запросом кода:
    - инициализирует состояние обновлений (catch_up),
    - переводит клиент в online, чтобы Telegram видел активную сессию,
    - находит 777000 в диалогах и помещает его в entity cache,
    - возвращает ID последнего сообщения в чате 777000 (baseline_msg_id).
    """
    try:
        await client.catch_up()
    except Exception as e:
        log.debug("catch_up пропущен: %s", e)

    try:
        await client(UpdateStatusRequest(offline=False))
    except Exception as e:
        log.debug("UpdateStatusRequest: %s", e)

    await _ensure_service_chat_cached(client, limit=200)
    return await _get_last_service_msg_id(client)


async def _get_last_service_msg_id(client: TelegramClient) -> int:
    try:
        msgs = await client.get_messages(TELEGRAM_SERVICE_ID, limit=1)
        if msgs:
            return int(msgs[0].id)
    except ValueError:
        log.info("Чат 777000 не найден в диалогах (baseline_msg_id=0)")
    except Exception as e:
        log.warning("Не удалось прочитать baseline чата 777000: %s", e)
    return 0


async def terminate_other_sessions(client: TelegramClient) -> None:
    """
    Завершает все сессии кроме текущей (bought_client).
    ВАЖНО: вызывать строго ДО create_new_session, иначе убьёт новую сессию!
    """
    try:
        await client(ResetAuthorizationsRequest())
        log.info("ResetAuthorizations: чужие сессии завершены")
        await asyncio.sleep(2.5)
    except FloodWaitError as e:
        log.warning("FloodWait при ResetAuthorizations: %ss", e.seconds)
        await asyncio.sleep(min(int(e.seconds) + 1, 60))
    except Exception as e:
        log.warning("Не удалось завершить чужие сессии (не фатально): %s", e)


def _extract_code(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(
        r"(?:login\s*code|verification\s*code|code|код(?:\s+для\s+входа)?(?:\s+в\s+telegram)?|código|kode)[^\n\d]{0,40}(\d{5,6})\b",
        text,
        flags=re.IGNORECASE,
    )
    if m and m.group(1) != "777000":
        return m.group(1)

    low = text.lower()
    if any(
        marker in low
        for marker in (
            "new login",
            "we detected a login",
            "новый вход",
            "обнаружен вход",
            "terminated",
            "завершены",
        )
    ):
        return None

    for candidate in re.findall(r"(?<!\d)(\d{5,6})(?!\d)", text):
        if candidate != "777000":
            return candidate
    return None


async def _wait_for_code(
    client: TelegramClient,
    baseline_msg_id: int,
    timeout: int = 90,
    lolz_code_fetcher: Callable[[], Awaitable[str | None]] | None = None,
    stale_lolz_code: str | None = None,
    stale_lolz_known: bool = False,
) -> str | None:
    """
    Ждёт новый код входа из чата 777000 (строго msg.id > baseline_msg_id).
    Слушает входящие без привязки к from_users=777000, чтобы не падать при пустом кэше,
    и подгружает диалоги, если 777000 ещё не закэширован.
    """
    code_holder: dict[str, str | None] = {"code": None}
    seen_ids: set[int] = set()

    async def _check_message(msg) -> bool:
        if msg is None or getattr(msg, "id", None) is None:
            return False
        msg_id = int(msg.id)
        if msg_id in seen_ids or msg_id <= baseline_msg_id:
            return False
        seen_ids.add(msg_id)
        if getattr(msg, "out", False):
            return False
        text = (
            getattr(msg, "raw_text", None)
            or getattr(msg, "message", None)
            or getattr(msg, "text", None)
        )
        code = _extract_code(text)
        if code:
            code_holder["code"] = code
            return True
        return False

    @client.on(events.NewMessage(incoming=True))
    async def _on_code(event):
        if code_holder["code"]:
            return
        sender_id = getattr(event, "sender_id", None)
        chat_id = getattr(event, "chat_id", None)
        if sender_id != TELEGRAM_SERVICE_ID and chat_id != TELEGRAM_SERVICE_ID:
            return
        await _check_message(event.message)

    loop = asyncio.get_running_loop()
    start_ts = loop.time()
    deadline = start_ts + timeout
    last_lolz_poll = 0.0
    last_catch_up = 0.0

    try:
        while loop.time() < deadline:
            if code_holder["code"]:
                return code_holder["code"]

            now = loop.time()
            if (now - last_catch_up) >= 4.5:
                last_catch_up = now
                try:
                    await client.catch_up()
                except Exception:
                    pass

            try:
                async for msg in client.iter_messages(TELEGRAM_SERVICE_ID, limit=10):
                    if int(msg.id) <= baseline_msg_id:
                        break
                    if await _check_message(msg):
                        return code_holder["code"]
            except ValueError:
                await _ensure_service_chat_cached(client, limit=20)
            except Exception as e:
                log.warning("Ошибка опроса 777000: %s", e)

            if (
                lolz_code_fetcher is not None
                and stale_lolz_known
                and (now - start_ts) >= 20.0
                and (now - last_lolz_poll) >= 5.0
            ):
                last_lolz_poll = now
                try:
                    lcode = await lolz_code_fetcher()
                    if lcode and lcode != stale_lolz_code:
                        log.info("Код получен через Lolz API после запроса кода")
                        return str(lcode).strip()
                except Exception as e:
                    log.debug("Опрос Lolz API для кода: %s", e)

            await asyncio.sleep(1.5)
    finally:
        client.remove_event_handler(_on_code)

    return code_holder["code"]


async def create_new_session(
    client_for_code_receiving: TelegramClient,
    phone: str,
    password: str | None = None,
    preloaded_code: str | None = None,
    lolz_code_fetcher: Callable[[], Awaitable[str | None]] | None = None,
) -> TelegramClient:
    """
    Создаёт новую сессию через send_code_request + чтение 777000 / Lolz API.
    """
    phone = _normalize_phone(phone)

    baseline_msg_id = 0
    stale_lolz_code = None
    stale_lolz_known = False
    if not preloaded_code:
        baseline_msg_id = await prepare_client_for_code(client_for_code_receiving)
        if lolz_code_fetcher is not None:
            try:
                stale_lolz_code = await lolz_code_fetcher()
                stale_lolz_known = stale_lolz_code is not None
            except Exception:
                stale_lolz_code = None
                stale_lolz_known = False

    new_session = MemorySession()
    src_session = getattr(client_for_code_receiving, "session", None)
    if (
        src_session is not None
        and getattr(src_session, "dc_id", None)
        and getattr(src_session, "server_address", None)
        and getattr(src_session, "port", None)
    ):
        new_session.set_dc(
            int(src_session.dc_id),
            src_session.server_address,
            int(src_session.port),
        )

    new_client = TelegramClient(
        new_session,
        API_ID,
        API_HASH,
        device_model=TG_DEVICE_MODEL,
        system_version=TG_SYSTEM_VERSION,
        app_version=TG_APP_VERSION,
        lang_code=TG_LANG_CODE,
        system_lang_code=TG_SYSTEM_LANG_CODE,
    )
    await new_client.connect()

    try:
        sent = await new_client.send_code_request(phone)
    except FloodWaitError as e:
        await new_client.disconnect()
        raise AccountPipelineError(
            f"FloodWait при запросе кода: подожди {e.seconds}с"
        ) from e
    except Exception as e:
        await new_client.disconnect()
        raise AccountPipelineError(f"Не удалось запросить код: {e}") from e

    sent_type = type(sent.type).__name__
    next_type = type(sent.next_type).__name__ if getattr(sent, "next_type", None) else "none"
    log.info("Код запрошен для %s: sent.type=%s, next_type=%s", phone, sent_type, next_type)

    if sent_type != "SentCodeTypeApp" and "App" in next_type:
        try:
            sent = await new_client(
                ResendCodeRequest(phone_number=phone, phone_code_hash=sent.phone_code_hash)
            )
            sent_type = type(sent.type).__name__
            log.info("ResendCodeRequest: новый sent.type=%s", sent_type)
        except Exception as e:
            log.warning("ResendCodeRequest не удался: %s", e)

    if sent_type != "SentCodeTypeApp" and not preloaded_code:
        await new_client.disconnect()
        raise AccountPipelineError(
            f"Telegram отправил код через {sent_type} (не в чат 777000, next_type={next_type}). "
            f"Проверьте API_ID/API_HASH или параметры устройства."
        )

    if preloaded_code:
        code = preloaded_code
        log.info("Использую preloaded_code")
    else:
        code = await _wait_for_code(
            client_for_code_receiving,
            baseline_msg_id=baseline_msg_id,
            timeout=90,
            lolz_code_fetcher=lolz_code_fetcher,
            stale_lolz_code=stale_lolz_code,
            stale_lolz_known=stale_lolz_known,
        )
        if not code:
            await new_client.disconnect()
            raise AccountPipelineError(
                f"Код входа (sent.type={sent_type}, baseline={baseline_msg_id}) "
                f"не прочитан из чата 777000 за 90 секунд"
            )
        log.info("Код получен: %s", code)

    try:
        await new_client.sign_in(
            phone=phone,
            code=code,
            phone_code_hash=sent.phone_code_hash,
        )
    except SessionPasswordNeededError:
        if not password:
            await new_client.disconnect()
            raise AccountPipelineError(
                "На аккаунте 2FA, пароль неизвестен. Ставь фильтр «Пароль: Нет»."
            )
        try:
            await new_client.sign_in(password=password)
        except PasswordHashInvalidError as e:
            await new_client.disconnect()
            raise AccountPipelineError("Неверный облачный пароль (2FA)") from e
        except Exception as e:
            await new_client.disconnect()
            raise AccountPipelineError(f"Не удалось войти с 2FA: {e}") from e
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        await new_client.disconnect()
        raise AccountPipelineError(f"Полученный код ({code}) не подошёл или истёк: {e}") from e
    except FloodWaitError as e:
        await new_client.disconnect()
        raise AccountPipelineError(f"FloodWait при sign_in: {e.seconds}с") from e
    except Exception as e:
        await new_client.disconnect()
        raise AccountPipelineError(f"Не удалось войти новой сессией: {e}") from e

    if not await new_client.is_user_authorized():
        await new_client.disconnect()
        raise AccountPipelineError("Новая сессия не авторизована после sign_in")

    me = await new_client.get_me()
    log.info("Новая сессия OK: id=%s phone=%s", me.id, me.phone)
    return new_client


async def get_auth_hex_and_dc(client: TelegramClient) -> tuple[str, int]:
    auth_key = client.session.auth_key
    if auth_key is None or not getattr(auth_key, "key", None):
        raise AccountPipelineError("У сессии нет auth_key")
    hex_key = auth_key.key.hex()
    dc_id = int(client.session.dc_id)
    if not (1 <= dc_id <= 5):
        raise AccountPipelineError(f"Странный dc_id у новой сессии: {dc_id}")
    return hex_key, dc_id


async def terminate_this_session(client: TelegramClient) -> None:
    try:
        await client(LogOutRequest())
        log.info("Купленная сессия завершена (LogOut)")
    except Exception as e:
        log.warning("LogOut купленной сессии: %s", e)
    try:
        await client.disconnect()
    except Exception:
        pass


async def cleanup_account(client: TelegramClient) -> dict:
    """
    Удаляет ВСЕ чаты/группы/каналы, кроме служебного чата Telegram (777000),
    и чистит контакты. Возвращает отчёт.
    """
    report = {"deleted": 0, "failed": 0, "errors": []}
    try:
        await client.get_dialogs(limit=200)
    except Exception as e:
        log.warning("Не удалось предварительно загрузить диалоги: %s", e)

    dialogs = []
    try:
        async for dialog in client.iter_dialogs(limit=200):
            if dialog.id != TELEGRAM_SERVICE_ID:
                dialogs.append(dialog)
    except Exception as e:
        report["errors"].append(f"не удалось получить список диалогов: {e}")
        log.warning("Ошибка получения списка диалогов: %s", e)
        dialogs = []

    for dialog in dialogs:
        try:
            if dialog.is_channel or dialog.is_group:
                try:
                    await client(LeaveChannelRequest(dialog.entity))
                except Exception as e:
                    log.info("Leave не удался для %s (%s): %s", dialog.id, dialog.name, e)
            await client.delete_dialog(dialog.entity, revoke=True)
            report["deleted"] += 1
        except Exception as e:
            report["failed"] += 1
            msg = f"{dialog.id} ({dialog.name or '—'}): {e}"
            report["errors"].append(msg)
            log.warning("Не удалось удалить диалог %s", msg)
        await asyncio.sleep(0.3)

    try:
        contacts = await client(GetContactsRequest(hash=0))
        users = getattr(contacts, "users", None) or []
        if users:
            await client(DeleteContactsRequest(id=users))
            log.info("Удалено контактов: %s", len(users))
    except Exception as e:
        report["errors"].append(f"контакты: {e}")
        log.warning("Ошибка при удалении контактов: %s", e)

    log.info("Очистка аккаунта: удалено %s, не удалось %s", report["deleted"], report["failed"])
    return report


# ---------- Проверка спам-блока через официального @SpamBot ----------

SPAMBOT_USERNAME = "SpamBot"


async def check_spam_block(client: TelegramClient, timeout: int = 20) -> str:
    """
    Пишет официальному @SpamBot «/start» и разбирает именно НОВЫЙ ответ
    (msg.id > last_id и входящее сообщение).
    Возвращает 'free', 'limited' или 'unknown'.
    """
    try:
        bot_entity = await client.get_entity(SPAMBOT_USERNAME)
    except Exception as e:
        log.warning("Не удалось найти @%s: %s", SPAMBOT_USERNAME, e)
        return "unknown"

    last_id = 0
    try:
        prev = await client.get_messages(bot_entity, limit=1)
        if prev:
            last_id = int(prev[0].id)
    except Exception:
        last_id = 0

    try:
        sent_msg = await client.send_message(bot_entity, "/start")
        if sent_msg and getattr(sent_msg, "id", None):
            last_id = max(last_id, int(sent_msg.id))
    except FloodWaitError as e:
        log.warning("FloodWait при проверке спам-блока: %ss", e.seconds)
        return "unknown"
    except Exception as e:
        log.warning("Не удалось написать @%s: %s", SPAMBOT_USERNAME, e)
        return "unknown"

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    seen_ids: set[int] = set()

    while loop.time() < deadline:
        try:
            async for msg in client.iter_messages(bot_entity, limit=5):
                if int(msg.id) <= last_id:
                    break
                if msg.id in seen_ids or getattr(msg, "out", False):
                    continue
                seen_ids.add(msg.id)
                text = (msg.raw_text or msg.text or "").lower()
                if not text:
                    continue
                if any(
                    p in text
                    for p in (
                        "good news",
                        "no limits",
                        "не ограничен",
                        "хороших новостей",
                        "свободен",
                    )
                ) and not any(
                    p in text for p in ("temporary", "limited", "ограничен", "restrict")
                ):
                    return "free"
                if any(
                    p in text
                    for p in (
                        "temporarily limited",
                        "limited",
                        "ограничен",
                        "restrict",
                        "read-only",
                    )
                ):
                    return "limited"
        except Exception:
            pass
        await asyncio.sleep(1.5)

    return "unknown"
# --- КОНЕЦ ФАЙЛА telethon_actions.py ---