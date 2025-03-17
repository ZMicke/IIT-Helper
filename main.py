import os
import re
import logging
import asyncio
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# Загрузка переменных окружения
load_dotenv()
API_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
SCHEDULE_DATABASE_URL = os.getenv("SCHEDULE_DATABASE_URL")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()

# ---------- Функции подключения к базам данных ----------
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def get_schedule_db_connection():
    return psycopg2.connect(SCHEDULE_DATABASE_URL, cursor_factory=RealDictCursor)

def get_table_name_by_direction(direction: str) -> str:
    d = direction.upper()
    if d == "ПИ":
        return "schedule_PI"
    elif d == "ПРИ":
        return "schedule_PRI"
    elif d == "БИ":
        return "schedule_BI"
    else:
        return "schedule_other"

def get_schedule_text(direction: str, group_number: str, week_type: str, day_of_week: str):
    table_name = get_table_name_by_direction(direction)
    conn = get_schedule_db_connection()
    cur = conn.cursor()
    query = f"""
        SELECT schedule_text
          FROM {table_name}
         WHERE group_number = %s
           AND week_type = %s
           AND day_of_week = %s
         LIMIT 1
    """
    cur.execute(query, (group_number, week_type, day_of_week))
    row = cur.fetchone()
    conn.close()
    if row:
        # Заменяем <br> на \n, чтобы Telegram корректно обрабатывал переносы строк
        return row["schedule_text"].replace("<br>", "\n")
    return None

# ---------- FSM для просмотра расписания ----------
class ScheduleFSM(StatesGroup):
    waiting_for_week_type = State()
    waiting_for_day = State()

# ---------- Обработчики команды /schedule ----------
@router.message(Command("schedule"))
async def cmd_schedule(message: types.Message, state: FSMContext):
    telegram_id = message.from_user.id
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT direction, group_number FROM students WHERE telegram_id = %s", (telegram_id,))
    student = cur.fetchone()
    conn.close()
    if not student:
        await message.answer("Вы не зарегистрированы. Отправьте данные в формате 'Имя Фамилия Группа'")
        return
    direction = student["direction"]
    group_number = student["group_number"]
    if not direction or not group_number:
        await message.answer("Данные вашей группы указаны некорректно. Обратитесь к администратору.")
        return

    await state.update_data(direction=direction, group_number=group_number)

    # Inline-клавиатура для выбора типа недели
    builder = InlineKeyboardBuilder()
    for wt in ["Четная", "Нечетная"]:
        builder.button(text=wt, callback_data=f"week:{wt}")
    builder.adjust(2)
    await message.answer(
        f"Ваше направление: {direction}, группа: {group_number}\nВыберите тип недели:",
        reply_markup=builder.as_markup()
    )
    await state.set_state(ScheduleFSM.waiting_for_week_type)

@router.callback_query(F.data.startswith("week:"))
async def week_callback(callback: types.CallbackQuery, state: FSMContext):
    week_type = callback.data.split(":")[1]
    await state.update_data(week_type=week_type)
    # Inline-клавиатура для выбора дня недели
    builder = InlineKeyboardBuilder()
    for day in ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]:
        builder.button(text=day, callback_data=f"day:{day}")
    # Кнопка "Назад" для возврата к выбору типа недели
    builder.button(text="Назад", callback_data="back:week")
    builder.adjust(3)
    await callback.message.edit_text(
        f"Вы выбрали {week_type} неделю.\nВыберите день недели:",
        reply_markup=builder.as_markup()
    )
    await state.set_state(ScheduleFSM.waiting_for_day)
    await callback.answer()

@router.callback_query(F.data.startswith("day:"))
async def day_callback(callback: types.CallbackQuery, state: FSMContext):
    day = callback.data.split(":")[1]
    data = await state.get_data()
    direction = data.get("direction")
    group_number = data.get("group_number")
    week_type = data.get("week_type")
    if not all([direction, group_number, week_type]):
        await callback.message.answer("Не удалось определить параметры. Повторите попытку.")
        return
    schedule_text = get_schedule_text(direction, group_number, week_type, day)
    if schedule_text:
        text = (f"<b>Расписание для {direction}-{group_number} ({week_type} неделя) на {day}:</b>\n\n"
                f"{schedule_text}")
    else:
        text = f"Расписание для {direction}-{group_number} ({week_type} неделя) на {day} не найдено."

    # Формируем клавиатуру с кнопками "Назад" и "Завершить"
    builder = InlineKeyboardBuilder()
    builder.button(text="Назад", callback_data="back:day")
    builder.button(text="Завершить", callback_data="done")
    builder.adjust(2)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    # Оставляем состояние, чтобы можно было вернуться назад
    await state.set_state(ScheduleFSM.waiting_for_day)
    await callback.answer()

@router.callback_query(F.data.startswith("back:"))
async def back_callback(callback: types.CallbackQuery, state: FSMContext):
    command = callback.data.split(":")[1]
    data = await state.get_data()
    if command == "week":
        # Возвращаемся к выбору типа недели
        builder = InlineKeyboardBuilder()
        for wt in ["Четная", "Нечетная"]:
            builder.button(text=wt, callback_data=f"week:{wt}")
        builder.adjust(2)
        await callback.message.edit_text("Выберите тип недели:", reply_markup=builder.as_markup())
        await state.set_state(ScheduleFSM.waiting_for_week_type)
    elif command == "day":
        # Возвращаемся к выбору дня недели
        week_type = data.get("week_type")
        builder = InlineKeyboardBuilder()
        for day in ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]:
            builder.button(text=day, callback_data=f"day:{day}")
        builder.button(text="Назад", callback_data="back:week")
        builder.adjust(3)
        await callback.message.edit_text(
            f"Вы выбрали {week_type} неделю.\nВыберите день недели:",
            reply_markup=builder.as_markup()
        )
        await state.set_state(ScheduleFSM.waiting_for_day)
    await callback.answer()

@router.callback_query(F.data == "done")
async def done_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Просмотр расписания завершён. Если нужно ещё раз, введите /schedule.")
    await callback.answer()

# Опционально: регистрация студента через бот
@router.message()
async def register_in_bot(message: types.Message):
    parts = message.text.split()
    if len(parts) == 3:
        first_name, last_name, group_str = parts
        match = re.match(r"([А-ЯЁA-Z]+)-?(\d+)", group_str, re.IGNORECASE)
        if match:
            direction = match.group(1).upper()
            group_number = match.group(2)
        else:
            direction = "OTHER"
            group_number = group_str
        telegram_id = message.from_user.id
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO students (telegram_id, first_name, last_name, group_name, direction, group_number)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE
                SET first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    group_name = EXCLUDED.group_name,
                    direction = EXCLUDED.direction,
                    group_number = EXCLUDED.group_number;
            """, (telegram_id, first_name, last_name, group_str, direction, group_number))
            conn.commit()
            conn.close()
            await message.answer("Регистрация успешно выполнена! Теперь введите /schedule для просмотра расписания.")
        except Exception as e:
            logging.error(f"Ошибка регистрации: {e}")
            await message.answer("Ошибка регистрации. Попробуйте ещё раз.")
    else:
        await message.answer("Неверный формат данных. Введите: Имя Фамилия Группа\nИли /schedule для просмотра расписания.")

router.message.register(register_in_bot)
dp.include_router(router)

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
