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
from aiogram.types import CallbackQuery


from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
import time
import urllib.parse


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
        
#----------- САЙТ СТУДЕНТОВ ----------
# ---------- FSM для авторизации через Selenium ----------
class CreditsAuthFSM(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()
# ---------- Инициализация Selenium-драйвера ----------
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



# Обработчик нажатия на кнопку "Личные зачеты"
@router.callback_query(F.data == "menu:credits")
async def menu_credits_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Обрабатывается...")

    telegram_id = callback.from_user.id

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_login, user_password FROM students WHERE telegram_id = %s;", (telegram_id,))
    row = cur.fetchone()
    conn.close()

    # Если логин/пароль не найдены - запускаем FSM
    if not row or not row["user_login"] or not row["user_password"]:
        await callback.message.answer("Введите ваш логин:")
        await state.set_state(CreditsAuthFSM.waiting_for_login)
        return

    # Если логин/пароль есть - идём дальше
    user_login = row["user_login"]
    user_password = row["user_password"]

    # Пробуем авторизоваться
    driver = get_selenium_driver(user_login, user_password)
    if not driver:
        await callback.message.answer("Не удалось авторизоваться. Проверьте логин/пароль.")
        return

    try:
        driver.get("https://eu.iit.csu.ru/student/credits")
        time.sleep(3)
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

#--------ПЕРЕСДАЧИ-----------
# Функция для парсинга всех таблиц пересдач на странице
def parse_all_retakes_tables(html: str) -> list[str]:
    """
    Ищет все <table> на странице и собирает их содержимое.
    Возвращает список строк, где каждая строка – это красиво
    оформленный текст одной таблицы.
    """
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        return ["На странице нет таблиц."]
    
    result_tables = []
    
    for idx, table in enumerate(tables, start=1):
        rows = table.find_all("tr")
        if not rows:
            result_tables.append(f"<b>Таблица {idx}:</b>\nПустая таблица.")
            continue
        
        # Собираем строки таблицы в список
        table_lines = []
        for row in rows:
            cols = row.find_all(["td", "th"])
            # Обрезаем пробелы у текста каждой ячейки
            cols_text = [col.get_text(strip=True) for col in cols]
            # Если строка не пустая, формируем строку
            if any(cols_text):
                # Разделяем ячейки " | "
                line = " | ".join(cols_text)
                table_lines.append(line)
        
        # Формируем готовый блок для таблицы с использованием <pre> (моноширинный блок)
        if table_lines:
            # Соединяем строки переводами, затем оборачиваем в <pre>
            content = "\n".join(table_lines)
            table_block = f"<b>Таблица {idx}:</b>\n<pre>{content}</pre>"
        else:
            table_block = f"<b>Таблица {idx}:</b>\n(Нет данных)"
        
        result_tables.append(table_block)
    
    return result_tables

def get_all_retakes_tables_html(user_login: str, user_password: str) -> str:
    """
    1) Авторизуется на сайте через Selenium,
    2) Переходит на страницу пересдач,
    3) Возвращает HTML всей страницы (или None в случае ошибки).
    """
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    
    driver = webdriver.Chrome(options=chrome_options)
    try:
        # Авторизация
        driver.get("https://eu.iit.csu.ru/login")
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.NAME, "username")))
        driver.find_element(By.NAME, "username").send_keys(user_login)
        driver.find_element(By.NAME, "password").send_keys(user_password)
        driver.find_element(By.XPATH, "//button[@type='submit']").click()
        WebDriverWait(driver, 10).until(EC.url_changes("https://eu.iit.csu.ru/login"))
        
        # Переход на страницу пересдач (URL может отличаться)
        driver.get("https://eu.iit.csu.ru/mod/page/view.php?id=154874")
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        page_html = driver.page_source
        return page_html
    except Exception as e:
        logging.error(f"Ошибка при получении таблиц пересдач: {e}")
        return None
    finally:
        driver.quit()

# Функция для отправки длинного текста, разбивая его на части, если он превышает лимит
MAX_MESSAGE_LENGTH = 4096
async def send_long_message(message_obj, text: str, **kwargs):
    """
    Разбивает длинный текст на части по MAX_MESSAGE_LENGTH символов и отправляет их.
    message_obj – объект, у которого вызывается .answer() (например, callback.message).
    """
    if len(text) <= MAX_MESSAGE_LENGTH:
        await message_obj.answer(text, **kwargs)
    else:
        parts = text.split("\n")
        current_part = ""
        for part in parts:
            # +1 для учёта перевода строки
            if len(current_part) + len(part) + 1 > MAX_MESSAGE_LENGTH:
                await message_obj.answer(current_part, **kwargs)
                current_part = part
            else:
                if current_part:
                    current_part += "\n" + part
                else:
                    current_part = part
        if current_part:
            await message_obj.answer(current_part, **kwargs)

@router.callback_query(F.data == "menu:retakes")
async def menu_retakes_callback(callback: types.CallbackQuery):
    await callback.answer("Обрабатывается...")
    telegram_id = callback.from_user.id

    # Извлекаем данные из БД: логин, пароль и группу (пример)
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT user_login, user_password, group_number FROM students WHERE telegram_id = %s;", (telegram_id,))
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        logging.error(f"Ошибка при получении данных из БД: {e}")
        await callback.message.answer("Ошибка при получении данных авторизации.")
        return

    if not row or not row["user_login"] or not row["user_password"]:
        await callback.message.answer("Сначала авторизуйтесь (Личные зачёты).")
        return

    user_login = row["user_login"]
    user_password = row["user_password"]

    # Получаем HTML со всеми таблицами пересдач
    page_html = get_all_retakes_tables_html(user_login, user_password)
    if not page_html:
        await callback.message.answer("Ошибка при получении расписания пересдач.")
        return

    # Парсим все таблицы -> получаем список сообщений (каждое сообщение = одна таблица)
    tables_texts = parse_all_retakes_tables(page_html)

    # Отправляем каждую таблицу отдельным сообщением
    for table_text in tables_texts:
        await send_long_message(
            callback.message,
            table_text,
            parse_mode="HTML"
        )

#---------ОЦЕНКИ----------
#-------Функция для создания таблицы-------
def format_grades_table(grades: list[dict]) -> str:
    """
    Преобразует список оценок в текст в виде псевдо-таблицы.
    Каждая оценка - словарь с ключами: "assignment", "grade", "range".
    Возвращает строку с HTML-разметкой <pre> для моноширинного отображения.
    """

    if not grades:
        return "Оценки не найдены или таблица оценок пуста."

    # Можно настроить ширину столбцов на свой вкус
    col1_width = 35  # "Задание"
    col2_width = 10  # "Оценка"
    col3_width = 12  # "Диапазон"

    # Заголовок таблицы
    header = (
        f"{'Задание':{col1_width}} | "
        f"{'Оценка':{col2_width}} | "
        f"{'Диапазон':{col3_width}}"
    )
    # Разделитель
    separator = (
        f"{'-'*col1_width}-+-"
        f"{'-'*col2_width}-+-"
        f"{'-'*col3_width}"
    )

    lines = [header, separator]

    for item in grades:
        assignment = item.get("assignment", "")
        grade = item.get("grade", "")
        range_ = item.get("range", "")

        # Если текст слишком длинный, можно обрезать:
        if len(assignment) > col1_width:
            assignment = assignment[:col1_width - 3] + "..."

        line = (
            f"{assignment:{col1_width}} | "
            f"{grade:{col2_width}} | "
            f"{range_:{col3_width}}"
        )
        lines.append(line)

    # Оборачиваем в <pre> для моноширинного отображения в Telegram
    table_text = "\n".join(lines)
    return f"<pre>{table_text}</pre>"
# Функция для получения списка курсов с "(2 сем.) 2024-2025"
def get_courses_list(user_login: str, user_password: str, semester_str="(2 сем.) 2024-2025"):
    """
    Авторизуется на сайте и получает список курсов с подстрокой semester_str.
    Возвращает словарь вида: { course_id: course_name, ... }
    """
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")

    driver = webdriver.Chrome(options=chrome_options)
    courses = {}
    try:
        # Авторизация
        driver.get("https://eu.iit.csu.ru/login")
        time.sleep(2)
        username_input = driver.find_element(By.NAME, "username")
        password_input = driver.find_element(By.NAME, "password")
        username_input.clear()
        username_input.send_keys(user_login)
        password_input.clear()
        password_input.send_keys(user_password)
        submit_button = driver.find_element(By.XPATH, "//button[@type='submit']")
        submit_button.click()
        time.sleep(3)

        # Переход на страницу обзора оценок
        overview_url = "https://eu.iit.csu.ru/grade/report/overview/index.php"
        driver.get(overview_url)
        time.sleep(2)

        soup = BeautifulSoup(driver.page_source, "html.parser")
        table = soup.find("table", {"class": "flexible table table-striped table-hover boxaligncenter generaltable"})
        if not table:
            logging.warning("Таблица с курсами не найдена. Проверьте структуру HTML.")
            return courses

        rows = table.find_all("tr")
        for row in rows:
            course_link_tag = row.find("a", href=True)
            if not course_link_tag:
                continue

            course_name = course_link_tag.get_text(strip=True)
            # Если название курса содержит нужную подстроку, извлекаем параметр id из URL
            if semester_str in course_name:
                parsed_url = urllib.parse.urlparse(course_link_tag["href"])
                query_params = urllib.parse.parse_qs(parsed_url.query)
                if "id" in query_params:
                    course_id = query_params["id"][0]
                    courses[course_id] = course_name
        return courses

    except Exception as e:
        logging.error(f"Ошибка при получении списка курсов: {e}")
        return courses
    finally:
        driver.quit()


# Функция для получения оценок по конкретному курсу (по его id)
def get_course_grades(user_login: str, user_password: str, course_id: str):
    """
    Авторизуется на сайте и переходит на страницу оценок по конкретному курсу.
    Возвращает список оценок вида:
      [
         {
            "Элемент оценки": ...,
            "Оценка": ...,
            "Диапазон": ...,
            "Отзыв": ...
         },
         ...
      ]
    Если таблица оценок не найдена, возвращается пустой список.
    """
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")

    driver = webdriver.Chrome(options=chrome_options)
    result = []
    try:
        # Авторизация
        driver.get("https://eu.iit.csu.ru/login")
        time.sleep(2)
        driver.find_element(By.NAME, "username").send_keys(user_login)
        driver.find_element(By.NAME, "password").send_keys(user_password)
        driver.find_element(By.XPATH, "//button[@type='submit']").click()
        time.sleep(3)

        # Переход на страницу оценок по курсу
        course_url = f"https://eu.iit.csu.ru/grade/report/user/index.php?id={course_id}"
        driver.get(course_url)
        time.sleep(3)

        # Получение HTML-кода страницы
        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")

        # Ищем таблицу оценок
        grades_table = soup.find("table", class_="user-grade")
        if not grades_table:
            return result

        tbody = grades_table.find("tbody")
        if not tbody:
            return result

        rows = tbody.find_all("tr")
        total = len(rows)
        if total == 0:
            return result

        for idx, row in enumerate(rows):
            assignment_cell = row.find("th", class_="column-itemname")
            if assignment_cell:
                # Пробуем найти ссылку <a class="gradeitemheader">
                link = assignment_cell.find("a", class_="gradeitemheader")
                if link:
                    assignment = link.get_text(strip=True)
                else:
                    # Если ссылки нет, берём общий текст ячейки
                    assignment = assignment_cell.get_text(strip=True)
            else:
                assignment = "Неизвестное задание"


            # Извлекаем остальные столбцы
            cols = row.find_all("td")
            if len(cols) < 2:
                continue

            grade = cols[0].get_text(strip=True)  # Оценка
            range_ = cols[1].get_text(strip=True)  # Диапазон

            if idx == total - 1:
                # Последняя строка — это итоговая оценка за курс
                result.append({
                    "assignment": "Итоговая оценка за курс",
                    "grade": grade,
                    "range": range_
                })
            else:
                result.append({
                    "assignment": assignment,
                    "grade": grade,
                    "range": range_
                })
        return result

    except Exception as e:
        logging.error(f"Ошибка при получении оценок для курса id {course_id}: {e}")
        return result
    finally:
        driver.quit()

@router.callback_query(F.data == "menu:grades")
async def menu_grades_callback(callback: types.CallbackQuery):
    await callback.answer("Получаю список курсов...")
    telegram_id = callback.from_user.id

    # Получаем логин и пароль из базы данных для текущего пользователя
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        query = "SELECT user_login, user_password FROM students WHERE telegram_id = %s;"
        cur.execute(query, (telegram_id,))
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        logging.error(f"Ошибка при получении данных авторизации: {e}")
        await callback.message.answer("Ошибка при получении данных авторизации.")
        return

    if not row or not row["user_login"] or not row["user_password"]:
        await callback.message.answer("Сначала авторизуйтесь в личном кабинете (кнопка 'Личные зачеты').")
        return

    user_login = row["user_login"]
    user_password = row["user_password"]

    # Получаем список курсов
    courses = get_courses_list(user_login, user_password)
    if not courses:
        await callback.message.answer("Курсы с оценками не найдены.")
        return

    # Формируем клавиатуру с кнопками для каждого курса
    keyboard = InlineKeyboardBuilder()
    for course_id, course_name in courses.items():
        # callback_data: "course_grade:{course_id}"
        keyboard.button(text=course_name, callback_data=f"course_grade:{course_id}")
    keyboard.adjust(1)

    await callback.message.answer("Выберите курс:", reply_markup=keyboard.as_markup())


# Обработчик для кнопок с курсами
@router.callback_query(F.data.startswith("course_grade:"))
async def course_grade_callback(callback: types.CallbackQuery):
    await callback.answer("Получаю оценки...")
    telegram_id = callback.from_user.id

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        query = "SELECT user_login, user_password FROM students WHERE telegram_id = %s;"
        cur.execute(query, (telegram_id,))
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        logging.error(f"Ошибка при получении данных авторизации: {e}")
        await callback.message.answer("Ошибка при получении данных авторизации.")
        return

    if not row or not row["user_login"] or not row["user_password"]:
        await callback.message.answer("Сначала авторизуйтесь в личном кабинете (кнопка 'Личные зачеты').")
        return

    user_login = row["user_login"]
    user_password = row["user_password"]

    course_id = callback.data.split("course_grade:")[1]

    # Получаем оценки для выбранного курса
    grades = get_course_grades(user_login, user_password, course_id)

    # Вызываем нашу новую функцию для красивого форматирования
    response_text = format_grades_table(grades)

    # Отправляем пользователю
    await callback.message.answer(response_text, parse_mode="HTML")

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

#---------OLLAMA ---------



# ---------- Регистрация студента через бот ----------

@router.message(Command("schedule"), F.state == None)  # только если пользователь НЕ в каком-либо состоянии
async def register_in_bot(message: types.Message):
    parts = message.text.split()
    
    # Проверка на правильность введенных данных
    if len(parts) == 3:
        first_name, last_name, group_str =  parts
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

    
#---router.message.register(register_in_bot)
dp.include_router(router)

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())