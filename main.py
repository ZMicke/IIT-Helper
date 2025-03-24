import os
import re
import logging
import asyncio
import time
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from bs4 import BeautifulSoup

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time

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
        return row["schedule_text"].replace("<br>", "\n")
    return None

# ---------- FSM для просмотра расписания ----------
class ScheduleFSM(StatesGroup):
    waiting_for_week_type = State()
    waiting_for_day = State()

# Новый FSM для авторизации через Selenium на сайте eu.iit.csu.ru
class CreditsAuthFSM(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()

# ---------- Главный /start ----------
@router.message(Command("start"))
async def start_command(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="Расписание", callback_data="menu:schedule")
    builder.button(text="Задать вопрос", callback_data="menu:ask")
    builder.button(text="Создать заявку", callback_data="menu:meeting")
    builder.button(text="Отправить письмо", callback_data="menu:mail")
    builder.button(text="Личные зачеты", callback_data="menu:credits")
    builder.adjust(2)

    telegram_id = message.from_user.id
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT direction, group_number FROM students WHERE telegram_id = %s", (telegram_id,))
    student = cur.fetchone()
    conn.close()

    if not student:
        await message.answer(
            "Привет! Пожалуйста, зарегистрируйтесь. Введите данные в формате:\n"
            "'Имя Фамилия Группа'\nНапример: Иван Иванов PRI-201"
        )
    else:
        await message.answer("Выберите действие:", reply_markup=builder.as_markup())

# ---------- Обработчик меню Расписание ----------
@router.callback_query(F.data == "menu:schedule")
async def menu_schedule_callback(callback: types.CallbackQuery, state: FSMContext):
    telegram_id = callback.from_user.id
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT direction, group_number FROM students WHERE telegram_id = %s", (telegram_id,))
    student = cur.fetchone()
    conn.close()

    if not student:
        await callback.message.answer(
            "Вы не зарегистрированы. Пожалуйста, зарегистрируйтесь, отправив данные в формате 'Имя Фамилия Группа'."
        )
        return

    direction = student["direction"]
    group_number = student["group_number"]

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

@router.callback_query(F.data.startswith("day:"))
async def day_callback(callback: types.CallbackQuery, state: FSMContext):
    day = callback.data.split(":")[1]
    data = await state.get_data()
    direction = data.get("direction")
    group_number = data.get("group_number")
    week_type = data.get("week_type")

    if not all([direction, group_number, week_type]):
        await callback.message.answer("Не удалось определить параметры.\nПовторите попытку.")
        return

    schedule_text = get_schedule_text(direction, group_number, week_type, day)
    if schedule_text:
        text = (f"<b>Расписание для {direction}-{group_number} ({week_type} неделя) на {day}:</b>\n\n"
                f"{schedule_text}")
    else:
        text = f"Расписание для {direction}-{group_number} ({week_type} неделя) на {day} не найдено."

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

# ---------- FSM для авторизации через Selenium ----------
class CreditsAuthFSM(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()

# Пример: функция авторизации через Selenium,
# возвращает driver или None, если авторизация не удалась.
def get_selenium_driver(user_login: str, user_password: str):
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")

    driver = webdriver.Chrome(options=chrome_options)
    try:
        driver.get("https://eu.iit.csu.ru/login")
        time.sleep(2)

        # Заполняем форму логина
        username_input = driver.find_element(By.NAME, "username")
        password_input = driver.find_element(By.NAME, "password")
        username_input.clear()
        username_input.send_keys(user_login)
        password_input.clear()
        password_input.send_keys(user_password)

        # Отправляем форму
        submit_button = driver.find_element(By.XPATH, "//button[@type='submit']")
        submit_button.click()
        time.sleep(3)

        # Проверяем, не отобразилась ли ошибка
        try:
            error_element = driver.find_element(By.CSS_SELECTOR, "div.alert.alert-danger")
            if "Неверный логин или пароль" in error_element.text:
                logging.error("Неверный логин или пароль.")
                driver.quit()
                return None
        except:
            pass

        logging.info("Авторизация прошла успешно.")
        return driver

    except Exception as e:
        logging.error(f"Ошибка в Selenium: {e}")
        driver.quit()
        return None

# Функция для парсинга страницы расписания пересдач
def parse_retakes_table(html: str) -> str:
    """
    Извлекает из HTML таблицу пересдач и формирует строку для вывода.
    Структура таблицы в примере может отличаться от реальной, поэтому
    корректируйте поиск по тегам/классам/атрибутам.
    """
    soup = BeautifulSoup(html, "html.parser")

    table = soup.find("table")
    if not table:
        return "Таблица пересдач не найдена."

    rows = table.find_all("tr")
    if not rows:
        return "Таблица пересдач не найдена или пуста."

    # Формируем заголовок
    result_lines = []
    result_lines.append("Расписание пересдач:\n")

    # Пробегаемся по строкам таблицы
    # Первый <tr> обычно заголовки, можете пропустить/обработать отдельно
    for idx, row in enumerate(rows):
        cols = row.find_all(["td", "th"])
        cols_text = [col.get_text(strip=True) for col in cols]
        line = " | ".join(cols_text)
        result_lines.append(line)

    return "\n".join(result_lines)

# Обработчик нажатия на кнопку "Личные зачеты"
@router.callback_query(F.data == "menu:credits")
async def menu_credits_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Обрабатывается...")

    telegram_id = callback.from_user.id

    # Получаем логин и пароль из базы данных для данного пользователя
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        query = """
            SELECT user_login, user_password
              FROM students
             WHERE telegram_id = %s;
        """
        cur.execute(query, (telegram_id,))
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        logging.error(f"Ошибка при получении логина и пароля: {e}")
        await callback.message.answer("Ошибка при получении данных авторизации.")
        return

    # Если данные не найдены, просим пользователя авторизоваться через кнопку «Личные зачеты»
    if not row or not row["user_login"] or not row["user_password"]:
        await callback.message.answer("Сначала авторизуйтесь в личном кабинете (кнопка 'Личные зачеты').")
        return

    user_login = row["user_login"]
    user_password = row["user_password"]

    # Авторизуемся через Selenium с полученными данными
    driver = get_selenium_driver(user_login, user_password)
    if not driver:
        await callback.message.answer("Не удалось авторизоваться. Проверьте логин/пароль в базе данных.")
        return

    try:
        driver.get("https://eu.iit.csu.ru/student/credits")
        time.sleep(3)
        credits_page_html = driver.page_source
    except Exception as e:
        logging.error(f"Ошибка при загрузке личного кабинета: {e}")
        await callback.message.answer("Ошибка при загрузке личного кабинета.")
        return
    finally:
        driver.quit()

    builder = InlineKeyboardBuilder()
    builder.button(text="Расписание пересдач", callback_data="menu:retakes")
    builder.button(text="Узнать оценки", callback_data="menu:grades")
    builder.adjust(2)

    await callback.message.answer("Авторизация прошла успешно. Выберите действие:", reply_markup=builder.as_markup())


@router.message(CreditsAuthFSM.waiting_for_login)
async def process_credits_login(message: types.Message, state: FSMContext):
    user_login = message.text.strip()
    await state.update_data(user_login=user_login)
    await message.answer("Введите ваш пароль:")
    await state.set_state(CreditsAuthFSM.waiting_for_password)

@router.message(CreditsAuthFSM.waiting_for_password)
async def process_credits_password(message: types.Message, state: FSMContext):
    user_password = message.text.strip()
    data = await state.get_data()
    user_login = data.get("user_login")
    telegram_id = message.from_user.id

    # Записываем логин и пароль в базу данных
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        query = """
            UPDATE students
            SET user_login = %s, user_password = %s
            WHERE telegram_id = %s;
        """
        cur.execute(query, (user_login, user_password, telegram_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Ошибка записи логина и пароля в базу: {e}")
        await message.answer("Ошибка при сохранении логина и пароля. Попробуйте ещё раз.")
        await state.clear()
        return

    # Продолжаем авторизацию через Selenium
    driver = get_selenium_driver(user_login, user_password)
    if driver is None:
        await message.answer("Не удалось авторизоваться. Проверьте логин/пароль.")
        await state.clear()
        return

    try:
        driver.get("https://eu.iit.csu.ru/student/credits")
        time.sleep(2)
    finally:
        driver.quit()

    builder = InlineKeyboardBuilder()
    builder.button(text="Расписание пересдач", callback_data="menu:retakes")
    builder.button(text="Узнать оценки", callback_data="menu:grades")
    builder.adjust(2)
    await message.answer("Авторизация прошла успешно. Выберите действие:", reply_markup=builder.as_markup())
    await state.clear()



def get_retakes_schedule(user_login: str, user_password: str):
    """Авторизуется на сайте и получает расписание пересдач через Selenium"""
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")

    driver = webdriver.Chrome(options=chrome_options)
    try:
        # Авторизация
        driver.get("https://eu.iit.csu.ru/login")
        time.sleep(2)

        username_input = driver.find_element(By.NAME, "username")
        password_input = driver.find_element(By.NAME, "password")
        username_input.send_keys(user_login)
        password_input.send_keys(user_password)

        submit_button = driver.find_element(By.XPATH, "//button[@type='submit']")
        submit_button.click()
        time.sleep(3)

        # Переход на страницу пересдач
        driver.get("https://eu.iit.csu.ru/mod/page/view.php?id=154874")

        container = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.ID, "yui_3_18_1_1_1742779241015_52"))
        )

        table = WebDriverWait(container, 20).until(
            EC.presence_of_element_located((By.TAG_NAME, "table"))
        )

        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.TAG_NAME, "td"))
        )

        html = table.get_attribute("outerHTML")

        schedule_text = parse_table_from_html(html)
        return schedule_text

    except Exception as e:
        print(f"Ошибка при получении расписания пересдач: {e}")
        return "Ошибка при получении расписания пересдач."
    finally:
        driver.quit()


def parse_table_from_html(html):
    """Парсит таблицу пересдач из HTML"""
    soup = BeautifulSoup(html, "html.parser")

    table = soup.find("table")
    if not table:
        return "Таблица пересдач не найдена."

    rows = table.find_all("tr")
    if not rows:
        return "Таблица пересдач пуста."

    result_lines = ["<b>Расписание пересдач:</b>\n"]

    for row in rows:
        cols = row.find_all("td")
        cols_text = [col.get_text(strip=True) for col in cols]
        if any(cols_text):  # Пропускаем пустые строки
            result_lines.append(" | ".join(cols_text))

    return "\n".join(result_lines) if len(result_lines) > 1 else "Нет данных в таблице."


# Обработчик кнопки "Расписание пересдач"
@router.callback_query(F.data == "menu:retakes")
async def menu_retakes_callback(callback: types.CallbackQuery):
    await callback.answer("Обрабатывается...")

    telegram_id = callback.from_user.id

    # Получаем логин и пароль из базы данных
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT user_login, user_password FROM students WHERE telegram_id = %s;", (telegram_id,))
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        logging.error(f"Ошибка при получении логина и пароля: {e}")
        await callback.message.answer("Ошибка при получении данных авторизации.")
        return

    # Если у пользователя нет сохранённого логина и пароля
    if not row or not row["user_login"] or not row["user_password"]:
        await callback.message.answer("Сначала авторизуйтесь в личном кабинете (кнопка 'Личные зачеты').")
        return

    user_login = row["user_login"]
    user_password = row["user_password"]

    # Получаем расписание пересдач
    schedule_text = get_retakes_schedule(user_login, user_password)

    await callback.message.answer(schedule_text)


# ---------- Регистрация пользователя через бот ----------
@router.message()
async def register_in_bot(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        return

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
            cur.execute(
                """
                INSERT INTO students (telegram_id, first_name, last_name, direction, group_number)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE
                SET first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    direction = EXCLUDED.direction,
                    group_number = EXCLUDED.group_number;
                """,
                (telegram_id, first_name, last_name, direction, group_number)
            )
            conn.commit()
            conn.close()

            builder = InlineKeyboardBuilder()
            builder.button(text="Перейти к расписанию", callback_data="menu:schedule")
            builder.button(text="Перейти к личному кабинету", callback_data="menu:credits")
            builder.adjust(1)

            await message.answer(
                "Регистрация успешно выполнена! Теперь вы можете перейти к просмотру расписания.",
                reply_markup=builder.as_markup()
            )
        except Exception as e:
            logging.error(f"Ошибка регистрации: {e}")
            await message.answer("Ошибка регистрации. Попробуйте ещё раз.")
    else:
        await message.answer("Неверный формат данных. Введите данные в формате:\n'Имя Фамилия Группа'")

router.message.register(register_in_bot)
dp.include_router(router)

# ---------- Запуск бота ----------
async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
