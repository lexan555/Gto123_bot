import asyncio
import logging
import os
import sqlite3

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

# Получение токена из секретов Replit
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing from Replit Secrets.")

bot = Bot(token=TOKEN)
dp = Dispatcher()

MAIN_MENU_BUTTON_TEXTS = {
    "📋 Нормативы 6 ступени",
    "🏋️ Комплекс ОФП",
    "📅 Мой план тренировок",
    "💡 Советы по технике",
}

last_bot_msg: dict[int, int] = {}
chat_locks: dict[int, asyncio.Lock] = {}


def get_chat_lock(chat_id: int) -> asyncio.Lock:
    lock = chat_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        chat_locks[chat_id] = lock
    return lock


async def delete_incoming_message(message: types.Message) -> None:
    if message.text:
        command = message.text.split(maxsplit=1)[0].split("@", maxsplit=1)[0]
        if command == "/start" or message.text in MAIN_MENU_BUTTON_TEXTS:
            return

    try:
        await message.delete()
    except Exception as error:
        logging.info(
            "Could not delete incoming message in chat %s: %s",
            message.chat.id,
            error,
        )


async def delete_previous_bot_message(chat_id: int) -> None:
    lock = get_chat_lock(chat_id)
    async with lock:
        await _delete_previous_bot_messages_unlocked(chat_id)


async def _delete_previous_bot_messages_unlocked(chat_id: int) -> None:
    previous_message_id = last_bot_msg.pop(chat_id, None)
    if previous_message_id is not None:
        try:
            await bot.delete_message(chat_id, previous_message_id)
        except Exception as error:
            logging.info(
                "Could not delete bot message %s in chat %s: %s",
                previous_message_id,
                chat_id,
                error,
            )


async def send_clean_message(
    chat_id: int,
    text: str,
    *,
    track_message: bool = True,
    **kwargs,
) -> types.Message:
    lock = get_chat_lock(chat_id)
    async with lock:
        await _delete_previous_bot_messages_unlocked(chat_id)
        sent_message = await bot.send_message(chat_id, text, **kwargs)
        if track_message:
            last_bot_msg[chat_id] = sent_message.message_id
        else:
            last_bot_msg.pop(chat_id, None)
        return sent_message


async def answer_callback_cleanly(
    callback: types.CallbackQuery, text: str, **kwargs
) -> types.Message | None:
    await callback.answer()
    callback_message = callback.message
    if callback_message is None:
        return None

    kwargs.setdefault("reply_markup", InlineKeyboardMarkup(inline_keyboard=[]))
    chat_id = callback_message.chat.id
    lock = get_chat_lock(chat_id)
    async with lock:
        try:
            edited_message = await callback_message.edit_text(text, **kwargs)
        except Exception as error:
            logging.info(
                "Could not edit callback message %s in chat %s: %s",
                callback_message.message_id,
                chat_id,
                error,
            )
            return None
        last_bot_msg[chat_id] = callback_message.message_id
        return edited_message


# --- БАЗА ДАННЫХ ДЛЯ ЖУРНАЛА ТРЕНИРОВОК ---
def init_db():
    conn = sqlite3.connect("workout_stats.db")
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS workout_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                exercise TEXT,
                reps INTEGER
            )
            """
        )

        # Сохраняем старые общие итоги как отдельные помеченные записи.
        cursor.execute("PRAGMA user_version")
        migration_version = cursor.fetchone()[0]
        if migration_version < 1:
            cursor.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'user_stats'
                """
            )
            if cursor.fetchone():
                cursor.execute(
                    "SELECT user_id, total_reps FROM user_stats WHERE total_reps > 0"
                )
                legacy_rows = cursor.fetchall()
                cursor.executemany(
                    """
                    INSERT INTO workout_logs (user_id, exercise, reps)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (
                            user_id,
                            "Ранее записанные повторения (упражнение не указано)",
                            total_reps,
                        )
                        for user_id, total_reps in legacy_rows
                    ],
                )
            cursor.execute("PRAGMA user_version = 1")

        conn.commit()
    finally:
        conn.close()


class WorkoutState(StatesGroup):
    waiting_for_exercise = State()
    waiting_for_reps = State()


# --- ГЛАВНОЕ МЕНЮ (КНОПКИ) ---
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="📋 Нормативы 6 ступени"),
            KeyboardButton(text="🏋️ Комплекс ОФП"),
        ],
        [
            KeyboardButton(text="📅 Мой план тренировок"),
            KeyboardButton(text="💡 Советы по технике"),
        ],
    ],
    resize_keyboard=True,
)


# --- 1. КОМАНДА /START ---
@dp.message(CommandStart())
async def start_handler(message: types.Message):
    first_name = message.from_user.first_name
    chat_id = message.chat.id
    await send_clean_message(
        chat_id,
        f"Привет, {first_name}! 👋\n\n"
        "Я твой официальный помощник для подготовки к сдаче норм ГТО — "
        "6 ступень (16–17 лет).\n\n"
        "Выбери интересующий раздел в меню ниже:",
        track_message=False,
        reply_markup=main_kb,
        parse_mode="Markdown",
    )


# --- 2. РАЗДЕЛ "НОРМАТИВЫ 6 СТУПЕНИ" ---
@dp.message(F.text == "📋 Нормативы 6 ступени")
async def normatives_handler(message: types.Message):
    chat_id = message.chat.id
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👦 Юноши (16–17 лет)", callback_data="normatives_boys"
                )
            ],
            [
                InlineKeyboardButton(
                    text="👧 Девушки (16–17 лет)", callback_data="normatives_girls"
                )
            ],
        ]
    )
    await delete_incoming_message(message)
    await send_clean_message(
        chat_id,
        "🏆 Нормативы ГТО — 6 ступень (16–17 лет).\nВыберите пол:",
        reply_markup=kb,
    )


@dp.callback_query(F.data == "normatives_boys")
async def normatives_boys_handler(callback: types.CallbackQuery):
    text = (
        "🏆 Официальные нормативы ГТО — 6 ступень (16–17 лет), юноши:\n\n"
        "🥇 Золото / 🥈 Серебро / 🥉 Бронза\n\n"
        "⏱ Бег 100 м: 13.8 сек / 14.6 сек / 15.1 сек\n"
        "⏱ Бег 3000 м: 12:30 мин / 13:30 мин / 14:00 мин\n"
        "💪 Подтягивания на высокой перекладине: 15 раз / 11 раз / 9 раз\n"
        "🏋️ Отжимания от пола: 44 раза / 32 раза / 26 раз\n"
        "🧘 Наклон вперед из положения стоя: +13 см / +8 см / +6 см\n"
        "💥 Прыжок в длину с места: 240 см / 220 см / 210 см\n"
        "⚡ Подъем туловища из положения лежа (пресс за 1 мин): "
        "50 раз / 42 раза / 36 раз"
    )
    await answer_callback_cleanly(callback, text)


@dp.callback_query(F.data == "normatives_girls")
async def normatives_girls_handler(callback: types.CallbackQuery):
    text = (
        "🏆 Официальные нормативы ГТО — 6 ступень (16–17 лет), девушки:\n\n"
        "🥇 Золото / 🥈 Серебро / 🥉 Бронза\n\n"
        "⏱ Бег 100 м: 16.5 сек / 17.5 сек / 18.0 сек\n"
        "⏱ Бег 2000 м: 10:15 мин / 11:15 мин / 11:55 мин\n"
        "💪 Отжимания от пола: 17 раз / 12 раз / 10 раз\n"
        "🏋️ Подтягивания на низкой перекладине (90 см): "
        "18 раз / 13 раз / 11 раз\n"
        "🧘 Наклон вперед: +16 см / +11 см / +8 см\n"
        "💥 Прыжок в длину с места: 190 см / 175 см / 165 см\n"
        "⚡ Подъем туловища (пресс за 1 мин): 47 раз / 38 раз / 32 раза"
    )
    await answer_callback_cleanly(callback, text)


# --- 3. РАЗДЕЛ "КОМПЛЕКС ОФП" ---
@dp.message(F.text == "🏋️ Комплекс ОФП")
async def ofp_handler(message: types.Message):
    chat_id = message.chat.id
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👦 Юноши", callback_data="ofp_gender_boys"
                )
            ],
            [
                InlineKeyboardButton(
                    text="👧 Девушки", callback_data="ofp_gender_girls"
                )
            ],
        ]
    )
    await delete_incoming_message(message)
    await send_clean_message(
        chat_id,
        "🏋️ Комплекс ОФП — 6 ступень (16–17 лет).\nВыберите пол:",
        reply_markup=kb,
    )


def ofp_category_keyboard(gender: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💪 Верхний плечевой пояс",
                    callback_data=f"ofp_{gender}_arms",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏃 Ноги и выносливость",
                    callback_data=f"ofp_{gender}_legs",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧘 Пресс и гибкость",
                    callback_data=f"ofp_{gender}_core",
                )
            ],
        ]
    )


async def show_ofp_categories(callback: types.CallbackQuery, gender: str, label: str):
    await answer_callback_cleanly(
        callback,
        f"Комплекс ОФП — 6 ступень (16–17 лет), {label}.\n"
        "Выберите направление тренировки:",
        reply_markup=ofp_category_keyboard(gender),
    )


@dp.callback_query(F.data == "ofp_gender_boys")
async def ofp_boys_handler(callback: types.CallbackQuery):
    await show_ofp_categories(callback, "boys", "юноши")


@dp.callback_query(F.data == "ofp_gender_girls")
async def ofp_girls_handler(callback: types.CallbackQuery):
    await show_ofp_categories(callback, "girls", "девушки")


OFP_WORKOUTS = {
    "ofp_boys_arms": (
        "💪 Верхний плечевой пояс — юноши, 6 ступень (16–17 лет):\n\n"
        "1. Отжимания:\n"
        "   - 4 подхода по 15–20 раз.\n\n"
        "2. Подтягивания на высокой перекладине:\n"
        "   - 4 подхода по 8–12 раз.\n\n"
        "3. Отжимания на брусьях:\n"
        "   - 3 подхода по 10–12 раз."
    ),
    "ofp_girls_arms": (
        "💪 Верхний плечевой пояс — девушки, 6 ступень (16–17 лет):\n\n"
        "1. Отжимания от пола/платформы:\n"
        "   - 4 подхода по 10–12 раз.\n\n"
        "2. Подтягивания на низкой перекладине:\n"
        "   - 4 подхода по 10–14 раз.\n\n"
        "3. Планка с касанием плеч:\n"
        "   - 3 подхода по 16 раз."
    ),
    "ofp_boys_legs": (
        "🏃 Ноги и выносливость — юноши, 6 ступень (16–17 лет):\n\n"
        "1. Приседания — 4 подхода по 25 раз.\n"
        "2. Выпады — 3 подхода по 12 раз на ногу.\n"
        "3. Забеги 60 м — 5 повторений.\n"
        "4. Кросс — 15–20 минут."
    ),
    "ofp_girls_legs": (
        "🏃 Ноги и выносливость — девушки, 6 ступень (16–17 лет):\n\n"
        "1. Приседания — 4 подхода по 20 раз.\n"
        "2. Выпады — 3 подхода по 10 раз на ногу.\n"
        "3. Забеги 60 м — 4 повторения.\n"
        "4. Кросс — 12–15 минут."
    ),
    "ofp_boys_core": (
        "🧘 Пресс и гибкость — юноши, 6 ступень (16–17 лет):\n\n"
        "1. Пресс — 4 подхода по 25–30 раз за 1 мин.\n"
        "2. Планка — 3 подхода по 60 сек.\n"
        "3. Наклоны — 4 подхода по 10 раз."
    ),
    "ofp_girls_core": (
        "🧘 Пресс и гибкость — девушки, 6 ступень (16–17 лет):\n\n"
        "1. Пресс — 4 подхода по 20–25 раз.\n"
        "2. Планка — 3 подхода по 45–60 сек.\n"
        "3. Наклоны — 4 подхода по 10 раз."
    ),
}


@dp.callback_query(F.data.in_(OFP_WORKOUTS))
async def ofp_workout_handler(callback: types.CallbackQuery):
    await answer_callback_cleanly(callback, OFP_WORKOUTS[callback.data])


# --- 4. РАЗДЕЛ "СОВЕТЫ ПО ТЕХНИКЕ" ---
@dp.message(F.text == "💡 Советы по технике")
async def tips_handler(message: types.Message):
    chat_id = message.chat.id
    text = (
        "💡 Методические рекомендации по выполнению упражнений:\n\n"
        "🔹 Отжимания: Следите за тем, чтобы тело составляло прямую линию. "
        "Не прогибайте поясницу и касайтесь грудью пола или платформы.\n\n"
        "🔹 Подтягивания: Выполняйте движение без махов ногами («рыбкой»). "
        "В верхней точке подбородок должен быть выше грифа перекладины.\n\n"
        "🔹 Наклон на гибкость: Ноги в коленях должны быть полностью выпрямлены. "
        "Фиксируйте результат в нижней точке не менее 2 секунд.\n\n"
        "🔹 Дыхание: Вдох делаем при опущении/расслаблении, а основной выдох — "
        "на максимальном усилии."
    )
    await delete_incoming_message(message)
    await send_clean_message(chat_id, text, parse_mode="Markdown")


# --- 5. ДНЕВНИК И ПОДСЧЕТ ПОВТОРЕНИЙ ---
def get_workout_summary(user_id: int):
    conn = sqlite3.connect("workout_stats.db")
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COALESCE(SUM(reps), 0) FROM workout_logs WHERE user_id = ?",
            (user_id,),
        )
        total_reps = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT exercise, reps
            FROM workout_logs
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 10
            """,
            (user_id,),
        )
        recent_entries = cursor.fetchall()
    finally:
        conn.close()

    return total_reps, recent_entries


def format_workout_summary(total_reps: int, recent_entries: list) -> str:
    if recent_entries:
        entries_text = "\n".join(
            f"• {exercise}: {reps} повторений"
            for exercise, reps in recent_entries
        )
    else:
        entries_text = "Записей пока нет."

    return (
        "📅 Твой дневник подготовки ГТО\n\n"
        f"📒 Последние записи:\n{entries_text}\n\n"
        f"🔥 Всего повторений: {total_reps}"
    )


def workout_plan_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Добавить упражнение", callback_data="add_exercise"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Очистить журнал", callback_data="request_clear_logs"
                )
            ],
        ]
    )

    return kb


@dp.message(F.text == "📅 Мой план тренировок")
async def plan_handler(message: types.Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    total_reps, recent_entries = get_workout_summary(user_id)
    await delete_incoming_message(message)
    await send_clean_message(
        chat_id,
        format_workout_summary(total_reps, recent_entries),
        reply_markup=workout_plan_keyboard(),
    )


@dp.callback_query(F.data == "add_exercise")
async def add_exercise(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(WorkoutState.waiting_for_exercise)
    await answer_callback_cleanly(
        callback,
        "Введите название упражнения (например, «Отжимания»):",
    )


@dp.message(WorkoutState.waiting_for_exercise)
async def process_exercise(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    exercise = message.text.strip() if message.text else ""
    if not exercise:
        await delete_incoming_message(message)
        await send_clean_message(chat_id, "Введите название упражнения текстом.")
        return

    await state.update_data(exercise=exercise)
    await state.set_state(WorkoutState.waiting_for_reps)
    await delete_incoming_message(message)
    await send_clean_message(
        chat_id,
        f"Сколько повторений упражнения «{exercise}» вы выполнили?"
    )


@dp.message(WorkoutState.waiting_for_reps)
async def process_reps(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    reps_text = message.text.strip() if message.text else ""
    if not reps_text.isascii() or not reps_text.isdecimal():
        await delete_incoming_message(message)
        await send_clean_message(
            chat_id, "Введите целое число повторений, например 50."
        )
        return

    added_reps = int(reps_text)
    if added_reps <= 0:
        await delete_incoming_message(message)
        await send_clean_message(
            chat_id, "Количество повторений должно быть больше нуля."
        )
        return

    state_data = await state.get_data()
    exercise = state_data.get("exercise")
    if not exercise:
        await state.set_state(WorkoutState.waiting_for_exercise)
        await delete_incoming_message(message)
        await send_clean_message(chat_id, "Сначала введите название упражнения.")
        return

    user_id = message.from_user.id
    conn = sqlite3.connect("workout_stats.db")
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO workout_logs (user_id, exercise, reps)
            VALUES (?, ?, ?)
            """,
            (message.from_user.id, exercise, added_reps),
        )
        conn.commit()
    finally:
        conn.close()

    await state.clear()
    await delete_incoming_message(message)
    total_reps, recent_entries = get_workout_summary(user_id)
    await send_clean_message(
        chat_id,
        f"✅ Запись добавлена: {exercise} — {added_reps} повторений.\n\n"
        f"{format_workout_summary(total_reps, recent_entries)}",
        reply_markup=workout_plan_keyboard(),
    )


@dp.callback_query(F.data == "request_clear_logs")
async def request_clear_logs(callback: types.CallbackQuery):
    confirmation_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, удалить записи", callback_data="confirm_clear_logs"
                ),
                InlineKeyboardButton(
                    text="Отмена", callback_data="cancel_clear_logs"
                ),
            ]
        ]
    )
    await answer_callback_cleanly(
        callback,
        "Удалить все записи тренировок? Это также очистит общий счетчик, "
        "и восстановить записи будет нельзя.",
        reply_markup=confirmation_keyboard,
    )


@dp.callback_query(F.data == "confirm_clear_logs")
async def confirm_clear_logs(callback: types.CallbackQuery):
    conn = sqlite3.connect("workout_stats.db")
    try:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM workout_logs WHERE user_id = ?",
            (callback.from_user.id,),
        )
        conn.commit()
    finally:
        conn.close()

    total_reps, recent_entries = get_workout_summary(callback.from_user.id)
    await answer_callback_cleanly(
        callback,
        "Журнал очищен.\n\n"
        + format_workout_summary(total_reps, recent_entries),
        reply_markup=workout_plan_keyboard(),
    )


@dp.callback_query(F.data == "cancel_clear_logs")
async def cancel_clear_logs(callback: types.CallbackQuery):
    total_reps, recent_entries = get_workout_summary(callback.from_user.id)
    await answer_callback_cleanly(
        callback,
        "Очистка журнала отменена.\n\n"
        + format_workout_summary(total_reps, recent_entries),
        reply_markup=workout_plan_keyboard(),
    )


@dp.message()
async def cleanup_unhandled_message(message: types.Message):
    await delete_incoming_message(message)
    await delete_previous_bot_message(message.chat.id)


async def healthcheck(_request: web.Request) -> web.Response:
    return web.Response(text="Bot is alive!", content_type="text/plain")


async def start_health_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", healthcheck)
    app.router.add_get("/health", healthcheck)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=8082)
    await site.start()
    print("Health check server listening at http://0.0.0.0:8082", flush=True)
    return runner


async def main():
    init_db()
    runner = await start_health_server()
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
