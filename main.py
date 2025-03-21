import os
import re
import logging
import asyncio
import psycopg2
import requests  # Для работы с OLLAMA API
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup

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
        # Если в базе сохранены <br>, заменяем их на \n для корректного отображения в Telegram
        return row["schedule_text"].replace("<br>", "\n")
    return None

# ---------- FSM для просмотра расписания ----------
class ScheduleFSM(StatesGroup):
    waiting_for_week_type = State()
    waiting_for_day = State()

    

## ---------- Главный меню ----------

@router.message(Command("start"))
async def start_command(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="Расписание", callback_data="menu:schedule")
    builder.button(text="Задать вопрос", callback_data="menu:ask")
    builder.button(text="Создать заявку", callback_data="menu:meeting")
    builder.button(text="Отправить письмо", callback_data="menu:mail")
    builder.button(text="Личные зачеты", callback_data="menu:credits")
    builder.adjust(2)
    
    # Проверка, зарегистрирован ли пользователь
    telegram_id = message.from_user.id
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT direction, group_number FROM students WHERE telegram_id = %s", (telegram_id,))
    student = cur.fetchone()
    conn.close()
    
    # Если пользователь не зарегистрирован, отправляем сообщение с инструкцией
    if not student:
        await message.answer("Привет! Пожалуйста, зарегистрируйтесь. Введите данные в формате:\n'Имя Фамилия Группа'\nНапример: Иван Иванов PRI-201")
    else:
        # Если пользователь уже зарегистрирован, показываем меню
        await message.answer("Выберите действие:", reply_markup=builder.as_markup())
        
        
# ---------- Обработчик выбора расписания ----------

@router.callback_query(F.data == "menu:schedule")
async def menu_schedule_callback(callback: types.CallbackQuery, state: FSMContext):
    telegram_id = callback.from_user.id
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT direction, group_number FROM students WHERE telegram_id = %s", (telegram_id,))
    student = cur.fetchone()
    conn.close()

    # Проверка наличия данных студента в БД
    if not student:
        await callback.message.answer("Вы не зарегистрированы. Пожалуйста, зарегистрируйтесь, отправив данные в формате 'Имя Фамилия Группа'.")
        return
    
    direction = student["direction"]
    group_number = student["group_number"]
    
    # Отправка сообщения с выбором типа недели
    builder = InlineKeyboardBuilder()
    for week_type in ["Четная", "Нечетная"]:
        builder.button(text=week_type, callback_data=f"week:{week_type}")
    builder.adjust(2)
    
    await state.update_data(direction=direction, group_number=group_number)
    
    await callback.message.answer(
        f"Ваше направление: {direction}, группа: {group_number}\nВыберите тип недели:",
        reply_markup=builder.as_markup()
    )
    await state.set_state(ScheduleFSM.waiting_for_week_type)
    await callback.answer()


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
    
    # Добавляем отладочное сообщение для вывода состояния
    print(f"State data: {data}")
    
    direction = data.get("direction")
    group_number = data.get("group_number")
    week_type = data.get("week_type")
    
    print(f"State: {data}, day: {day}, direction: {direction}, group_number: {group_number}, week_type: {week_type}")
    
    if not all([direction, group_number, week_type]):
        await callback.message.answer("Не удалось определить параметры.\nПовторите попытку.")
        return
    schedule_text = get_schedule_text(direction, group_number, week_type, day)
    if schedule_text:
        text = (f"<b>Расписание для {direction}-{group_number} ({week_type} неделя) на {day}:</b>\n\n"
                f"{schedule_text}")
    else:
        text = f"Расписание для {direction}-{group_number} ({week_type} неделя) на {day} не найдено."
    # Клавиатура с кнопками "Назад" и "Завершить"
    builder = InlineKeyboardBuilder()
    builder.button(text="Назад", callback_data="back:day")
    builder.button(text="Завершить", callback_data="done")
    builder.adjust(2)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await state.set_state(ScheduleFSM.waiting_for_day)
    await callback.answer()

@router.callback_query(F.data.startswith("back:"))
async def back_callback(callback: types.CallbackQuery, state: FSMContext):
    command = callback.data.split(":")[1]
    data = await state.get_data()
    if command == "week":
        builder = InlineKeyboardBuilder()
        for wt in ["Четная", "Нечетная"]:
            builder.button(text=wt, callback_data=f"week:{wt}")
        builder.adjust(2)
        await callback.message.edit_text("Выберите тип недели:", reply_markup=builder.as_markup())
        await state.set_state(ScheduleFSM.waiting_for_week_type)
    elif command == "day":
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

# ---------- Команда /ask для отправки вопроса к OLLAMA (RAG + LLM) ----------
@router.message(Command("ask"))
async def ask_command(message: types.Message):
    query = message.get_args()
    if not query:
        await message.answer("Пожалуйста, введите вопрос после команды /ask")
        return
    prompt = f"Вопрос: {query}\nОтвет:"
    answer = ollama_generate(prompt)
    await message.answer(answer)

# ---------- Функция для генерации ответа через OLLAMA ----------
def ollama_generate(prompt: str) -> str:
    url = "http://localhost:11434/api/generate"  # Проверьте, что этот URL корректен
    payload = {
        "model": "llama2-7b",
        "prompt": prompt,
        "max_tokens": 150,
        "temperature": 0.7
    }
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        return data.get("generated_text", "").strip()
    except Exception as e:
        return f"Ошибка генерации ответа: {e}"
    


# ---------- Регистрация студента через бот ----------

@router.message()
async def register_in_bot(message: types.Message):
    parts = message.text.split()
    
    # Проверка на правильность введенных данных
    if len(parts) == 3:
        first_name, last_name, group_str = parts
        match = re.match(r"([А-ЯЁA-Z]+)-?(\d+)", group_str, re.IGNORECASE)
        
        # Обработка данных направления и группы
        if match:
            direction = match.group(1).upper()
            group_number = match.group(2)
        else:
            direction = "OTHER"
            group_number = group_str

        telegram_id = message.from_user.id
        try:
            # Добавляем данные пользователя в базу данных
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO students (telegram_id, first_name, last_name, direction, group_number)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE
                SET first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    direction = EXCLUDED.direction,
                    group_number = EXCLUDED.group_number;
            """, (telegram_id, first_name, last_name, direction, group_number))
            conn.commit()
            conn.close()
            
            # Подтверждение успешной регистрации
            builder = InlineKeyboardBuilder()
            builder.button(text="Перейти к расписанию", callback_data="menu:schedule")
            builder.adjust(1)
            await message.answer(
                "Регистрация успешно выполнена! Теперь вы можете перейти к просмотру расписания.",
                reply_markup=builder.as_markup()
            )
        except Exception as e:
            logging.error(f"Ошибка регистрации: {e}")
            await message.answer("Ошибка регистрации. Попробуйте ещё раз.")
    else:
        # Инструкция по правильному вводу данных
        await message.answer("Неверный формат данных. Введите данные в формате:\n'Имя Фамилия Группа'")

router.message.register(register_in_bot)
dp.include_router(router)

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())