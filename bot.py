import asyncio
import glob
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, Message


def load_dotenv(path: str = ".env") -> None:
    """Минимальная загрузка .env без внешних зависимостей."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def get_required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(
            f"Не найдена переменная окружения {name}. "
            f"Создайте .env с {name}=... или экспортируйте переменную перед запуском."
        )
    return value


load_dotenv()

API_ID = int(get_required_env("API_ID"))
API_HASH = get_required_env("API_HASH")
BOT_TOKEN = get_required_env("BOT_TOKEN")
CHANNEL_ID = int(get_required_env("CHANNEL_ID"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "5"))

_admins_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {int(x.strip()) for x in _admins_raw.split(",") if x.strip()}

DB_PATH = os.getenv("DB_PATH", "events.db")
SESSION_NAME = os.getenv("SESSION_NAME", "reminder_bot")

app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
try:
    tz = ZoneInfo(TIMEZONE)
except ZoneInfoNotFoundError:
    print(
        f"[WARN] Таймзона '{TIMEZONE}' не найдена. "
        "Использую UTC. Установите пакет 'tzdata' (pip install tzdata)."
    )
    tz = timezone.utc


@dataclass
class Event:
    id: int
    name: str
    hour: int
    minute: int
    next_run_utc: str
    pending: int
    last_alert_utc: str | None
    active_message_id: int | None


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            hour INTEGER NOT NULL,
            minute INTEGER NOT NULL,
            next_run_utc TEXT NOT NULL,
            pending INTEGER NOT NULL DEFAULT 0,
            last_alert_utc TEXT,
            active_message_id INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_state (
            user_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            payload TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_event_time(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def parse_hhmm(value: str) -> tuple[int, int] | None:
    try:
        hour_str, minute_str = value.strip().split(":")
        hour = int(hour_str)
        minute = int(minute_str)
    except ValueError:
        return None

    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def compute_next_run(hour: int, minute: int, now_utc: datetime | None = None) -> datetime:
    now_utc = now_utc or utc_now()
    now_local = now_utc.astimezone(tz)
    target_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target_local <= now_local:
        target_local += timedelta(days=1)
    return target_local.astimezone(timezone.utc)


def is_admin(user_id: int) -> bool:
    if not ADMIN_IDS:
        return True
    return user_id in ADMIN_IDS


def access_denied_text(user_id: int) -> str:
    admins = ", ".join(str(x) for x in sorted(ADMIN_IDS)) or "<empty>"
    return (
        "⛔ У вас нет доступа к управлению ботом.\n"
        f"Ваш user_id: {user_id}\n"
        f"ADMIN_IDS: {admins}\n\n"
        "Добавьте ваш user_id в ADMIN_IDS или очистите ADMIN_IDS в .env"
    )


def save_user_state(user_id: int, state: str, payload: str = "") -> None:
    conn = db()
    conn.execute(
        """
        INSERT INTO user_state(user_id, state, payload)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, payload=excluded.payload
        """,
        (user_id, state, payload),
    )
    conn.commit()
    conn.close()


def get_user_state(user_id: int) -> tuple[str, str] | None:
    conn = db()
    row = conn.execute("SELECT state, payload FROM user_state WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return row["state"], row["payload"]


def clear_user_state(user_id: int) -> None:
    conn = db()
    conn.execute("DELETE FROM user_state WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def list_events(offset: int = 0, limit: int = PAGE_SIZE) -> list[Event]:
    conn = db()
    rows = conn.execute(
        """
        SELECT id, name, hour, minute, next_run_utc, pending, last_alert_utc, active_message_id
        FROM events
        ORDER BY hour, minute, id
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    conn.close()
    return [Event(**dict(r)) for r in rows]


def count_events() -> int:
    conn = db()
    row = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()
    conn.close()
    return int(row["c"])


def get_event(event_id: int) -> Event | None:
    conn = db()
    row = conn.execute(
        """
        SELECT id, name, hour, minute, next_run_utc, pending, last_alert_utc, active_message_id
        FROM events WHERE id = ?
        """,
        (event_id,),
    ).fetchone()
    conn.close()
    return Event(**dict(row)) if row else None


def create_event(name: str, hour: int, minute: int) -> None:
    next_run = compute_next_run(hour, minute).isoformat()
    conn = db()
    conn.execute(
        "INSERT INTO events(name, hour, minute, next_run_utc) VALUES (?, ?, ?, ?)",
        (name.strip(), hour, minute, next_run),
    )
    conn.commit()
    conn.close()


def delete_event(event_id: int) -> None:
    conn = db()
    conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    conn.commit()
    conn.close()


def rename_event(event_id: int, new_name: str) -> None:
    conn = db()
    conn.execute("UPDATE events SET name = ? WHERE id = ?", (new_name.strip(), event_id))
    conn.commit()
    conn.close()


def update_event_time(event_id: int, hour: int, minute: int) -> None:
    next_run = compute_next_run(hour, minute).isoformat()
    conn = db()
    conn.execute(
        """
        UPDATE events
        SET hour = ?, minute = ?, next_run_utc = ?, pending = 0, last_alert_utc = NULL, active_message_id = NULL
        WHERE id = ?
        """,
        (hour, minute, next_run, event_id),
    )
    conn.commit()
    conn.close()


def build_list_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    total = count_events()
    offset = max(0, page * PAGE_SIZE)
    events = list_events(offset=offset, limit=PAGE_SIZE)

    rows: list[list[InlineKeyboardButton]] = []
    for ev in events:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{ev.name} ({format_event_time(ev.hour, ev.minute)})",
                    callback_data=f"event:{ev.id}:{page}",
                ),
                InlineKeyboardButton(text="🗑", callback_data=f"del:{ev.id}:{page}"),
            ]
        )

    max_page = max((total - 1) // PAGE_SIZE, 0)
    rows.append(
        [
            InlineKeyboardButton(text="⬅️", callback_data=f"page:{max(page - 1, 0)}"),
            InlineKeyboardButton(text=f"{page + 1}/{max_page + 1}", callback_data="noop"),
            InlineKeyboardButton(text="➡️", callback_data=f"page:{min(page + 1, max_page)}"),
        ]
    )
    rows.append([InlineKeyboardButton(text="➕ Добавить событие", callback_data="add")])

    return InlineKeyboardMarkup(rows)


def event_manage_keyboard(event_id: int, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Переименовать", callback_data=f"editname:{event_id}:{page}"),
                InlineKeyboardButton("⏰ Изменить время", callback_data=f"edittime:{event_id}:{page}"),
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"page:{page}")],
        ]
    )


def confirm_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{event_id}")]]
    )


@app.on_message(filters.command("start") & filters.private)
async def cmd_start(_: Client, message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.reply(access_denied_text(message.from_user.id))
        return

    await message.reply(
        "Привет! Я бот-напоминалка.\n"
        "Команды:\n"
        "/add Название | HH:MM — быстро добавить событие\n"
        "/events — список событий",
        reply_markup=build_list_keyboard(0),
    )


@app.on_message(filters.command("events") & filters.private)
async def cmd_events(_: Client, message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.reply(access_denied_text(message.from_user.id))
        return
    await message.reply("Список событий:", reply_markup=build_list_keyboard(0))


@app.on_message(filters.command("add") & filters.private)
async def cmd_add(_: Client, message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.reply(access_denied_text(message.from_user.id))
        return
    text = message.text or ""
    payload = text.removeprefix("/add").strip()
    if "|" not in payload:
        await message.reply("Формат: /add Название | HH:MM")
        return

    name, time_part = [p.strip() for p in payload.split("|", 1)]
    hm = parse_hhmm(time_part)
    if not name or not hm:
        await message.reply("Ошибка. Используйте формат: /add Название | HH:MM")
        return

    create_event(name, hm[0], hm[1])
    await message.reply(f"✅ Добавлено: {name} ({time_part})")


@app.on_message(filters.private & filters.text)
async def text_state_handler(_: Client, message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        if message.from_user and message.text and message.text.startswith("/"):
            await message.reply(access_denied_text(message.from_user.id))
        return

    state = get_user_state(message.from_user.id)
    if not state:
        return

    action, payload = state
    if action == "await_add":
        if "|" not in message.text:
            await message.reply("Неверно. Формат: Название | HH:MM")
            return
        name, time_part = [x.strip() for x in message.text.split("|", 1)]
        hm = parse_hhmm(time_part)
        if not name or not hm:
            await message.reply("Неверно. Формат: Название | HH:MM")
            return
        create_event(name, hm[0], hm[1])
        clear_user_state(message.from_user.id)
        await message.reply("✅ Событие добавлено.")
        return

    if action.startswith("await_rename:"):
        event_id = int(action.split(":", 1)[1])
        rename_event(event_id, message.text.strip())
        clear_user_state(message.from_user.id)
        await message.reply("✅ Название обновлено.")
        return

    if action.startswith("await_time:"):
        event_id = int(action.split(":", 1)[1])
        hm = parse_hhmm(message.text.strip())
        if not hm:
            await message.reply("Введите время в формате HH:MM")
            return
        update_event_time(event_id, hm[0], hm[1])
        clear_user_state(message.from_user.id)
        await message.reply("✅ Время обновлено.")
        return


@app.on_callback_query()
async def callbacks(_: Client, cq: CallbackQuery) -> None:
    if not cq.from_user or not is_admin(cq.from_user.id):
        await cq.answer("Нет доступа", show_alert=True)
        return

    data = cq.data or ""

    if data == "noop":
        await cq.answer()
        return

    if data == "add":
        save_user_state(cq.from_user.id, "await_add")
        await cq.answer()
        await cq.message.reply("Введите: Название | HH:MM")
        return

    if data.startswith("page:"):
        page = int(data.split(":", 1)[1])
        await cq.message.edit_reply_markup(build_list_keyboard(page))
        await cq.answer()
        return

    if data.startswith("del:"):
        _, event_id, page = data.split(":")
        delete_event(int(event_id))
        await cq.message.edit_reply_markup(build_list_keyboard(int(page)))
        await cq.answer("Удалено")
        return

    if data.startswith("event:"):
        _, event_id, page = data.split(":")
        ev = get_event(int(event_id))
        if not ev:
            await cq.answer("Событие не найдено", show_alert=True)
            return
        await cq.message.reply(
            f"Событие: {ev.name}\nВремя: {format_event_time(ev.hour, ev.minute)}",
            reply_markup=event_manage_keyboard(ev.id, int(page)),
        )
        await cq.answer()
        return

    if data.startswith("editname:"):
        _, event_id, _page = data.split(":")
        save_user_state(cq.from_user.id, f"await_rename:{event_id}")
        await cq.answer()
        await cq.message.reply("Введите новое название:")
        return

    if data.startswith("edittime:"):
        _, event_id, _page = data.split(":")
        save_user_state(cq.from_user.id, f"await_time:{event_id}")
        await cq.answer()
        await cq.message.reply("Введите новое время в формате HH:MM:")
        return

    if data.startswith("confirm:"):
        event_id = int(data.split(":", 1)[1])
        ev = get_event(event_id)
        if not ev:
            await cq.answer("Событие удалено", show_alert=True)
            return

        next_run = compute_next_run(ev.hour, ev.minute).isoformat()
        conn = db()
        conn.execute(
            """
            UPDATE events
            SET pending = 0, last_alert_utc = NULL, active_message_id = NULL, next_run_utc = ?
            WHERE id = ?
            """,
            (next_run, event_id),
        )
        conn.commit()
        conn.close()

        await cq.answer("Подтверждено ✅")
        try:
            await cq.message.edit_reply_markup(None)
        except Exception:
            pass
        return

    await cq.answer()


async def reminder_worker() -> None:
    while True:
        now = utc_now()
        conn = db()
        due_rows = conn.execute(
            """
            SELECT id, name, hour, minute, next_run_utc, pending, last_alert_utc, active_message_id
            FROM events
            WHERE datetime(next_run_utc) <= datetime(?)
            """,
            (now.isoformat(),),
        ).fetchall()

        for row in due_rows:
            ev = Event(**dict(row))
            if ev.pending == 0:
                text = (
                    f"🔔 Напоминание: {ev.name}\n"
                    f"Время: {format_event_time(ev.hour, ev.minute)} ({TIMEZONE})\n\n"
                    "Подтвердите выполнение."
                )
                msg = await app.send_message(
                    chat_id=CHANNEL_ID,
                    text=text,
                    reply_markup=confirm_keyboard(ev.id),
                )
                conn.execute(
                    """
                    UPDATE events
                    SET pending = 1, last_alert_utc = ?, active_message_id = ?
                    WHERE id = ?
                    """,
                    (now.isoformat(), msg.id, ev.id),
                )
            else:
                should_spam = True
                if ev.last_alert_utc:
                    last = datetime.fromisoformat(ev.last_alert_utc)
                    should_spam = (now - last) >= timedelta(minutes=1)
                if should_spam:
                    msg = await app.send_message(
                        chat_id=CHANNEL_ID,
                        text=f"⏰ Повтор: подтвердите событие «{ev.name}».",
                        reply_markup=confirm_keyboard(ev.id),
                    )
                    conn.execute(
                        "UPDATE events SET last_alert_utc = ?, active_message_id = ? WHERE id = ?",
                        (now.isoformat(), msg.id, ev.id),
                    )

        conn.commit()
        conn.close()
        await asyncio.sleep(20)


async def main() -> None:
    init_db()
    await app.start()
    me = await app.get_me()
    if not me.is_bot:
        await app.stop()
        session_pattern = f"{SESSION_NAME}.session*"
        session_files = ", ".join(sorted(glob.glob(session_pattern))) or session_pattern
        raise RuntimeError(
            "Pyrogram запустился как пользователь, а не как бот. "
            f"Удалите файлы сессии ({session_files}) и запустите снова с BOT_TOKEN."
        )

    asyncio.create_task(reminder_worker())
    print(f"Bot started: @{me.username} (id={me.id})")
    print(f"ADMIN_IDS={sorted(ADMIN_IDS) if ADMIN_IDS else 'not set (open access)'}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
