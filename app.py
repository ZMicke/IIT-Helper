import os
import re
import asyncio
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, redirect, url_for, session, flash
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
SCHEDULE_DATABASE_URL = os.getenv("SCHEDULE_DATABASE_URL")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "your_secret_key")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY


# -------------------- Функции подключения к базам данных --------------------

def get_db_connection():
    """Подключение к основной базе (students, deans)."""
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def get_schedule_db_connection():
    """Подключение к базе расписания."""
    return psycopg2.connect(SCHEDULE_DATABASE_URL, cursor_factory=RealDictCursor)

# -------------------- Инициализация таблиц --------------------

def init_db():
    """
    Создаем таблицы для студентов и деканата.
    Поля direction и group_number выделяются отдельно.
    """
    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS students (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE,
                first_name TEXT,
                last_name TEXT,
                group_name TEXT,
                direction TEXT,
                group_number TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS deans (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE,
                password TEXT
            )
        ''')
    conn.commit()
    conn.close()

def init_schedule_db():
    """
    Создаем таблицы расписания для направлений.
    Для каждого направления создаем уникальный индекс, чтобы не дублировать запись по 
    (group_number, week_type, day_of_week) или (direction, group_number, week_type, day_of_week) для schedule_other.
    """
    conn = get_schedule_db_connection()
    with conn.cursor() as cursor:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS schedule_PI (
                id SERIAL PRIMARY KEY,
                group_number TEXT,
                week_type TEXT,
                day_of_week TEXT,
                schedule_text TEXT
            )
        ''')
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS schedule_pi_unique_idx ON schedule_PI (group_number, week_type, day_of_week)")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS schedule_PRI (
                id SERIAL PRIMARY KEY,
                group_number TEXT,
                week_type TEXT,
                day_of_week TEXT,
                schedule_text TEXT
            )
        ''')
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS schedule_pri_unique_idx ON schedule_PRI (group_number, week_type, day_of_week)")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS schedule_BI (
                id SERIAL PRIMARY KEY,
                group_number TEXT,
                week_type TEXT,
                day_of_week TEXT,
                schedule_text TEXT
            )
        ''')
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS schedule_bi_unique_idx ON schedule_BI (group_number, week_type, day_of_week)")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS schedule_other (
                id SERIAL PRIMARY KEY,
                direction TEXT,
                group_number TEXT,
                week_type TEXT,
                day_of_week TEXT,
                schedule_text TEXT
            )
        ''')
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS schedule_other_unique_idx ON schedule_other (direction, group_number, week_type, day_of_week)")
    conn.commit()
    conn.close()

# -------------------- Маршруты веб-интерфейса --------------------

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM deans WHERE username = %s", (username,))
        dean = cur.fetchone()
        conn.close()
        if dean and dean['password'] == password:
            session['user'] = username
            return redirect(url_for('dashboard'))
        else:
            flash('Неверный логин или пароль', 'error')
    return render_template('login.html')

@app.route('/register_dean', methods=['GET', 'POST'])
def register_dean():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO deans (username, password) VALUES (%s, %s)", (username, password))
            conn.commit()
            flash('Регистрация прошла успешно. Теперь авторизуйтесь.', 'success')
            return redirect(url_for('login'))
        except psycopg2.IntegrityError:
            conn.rollback()
            flash('Пользователь с таким логином уже существует', 'error')
        finally:
            conn.close()
    return render_template('register_dean.html')

@app.route('/dashboard')
def dashboard():
    if 'user' not in session:
        return redirect(url_for('login'))
    return render_template('dashboard.html')

@app.route('/create_event', methods=['GET', 'POST'])
def create_event():
    if 'user' not in session:
        return redirect(url_for('login'))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM students")
    students = cur.fetchall()
    conn.close()
    if request.method == 'POST':
        selected_ids = request.form.getlist('student')
        message_text = request.form.get('message')
        async def send_notifications(chat_ids, text):
            from aiogram import Bot
            bot = Bot(token=TELEGRAM_BOT_TOKEN)
            for chat_id in chat_ids:
                try:
                    await bot.send_message(chat_id=chat_id, text=text)
                except Exception as e:
                    print(f"Ошибка при отправке уведомления пользователю {chat_id}: {e}")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(send_notifications(selected_ids, message_text))
        loop.close()
        flash("Уведомления отправлены!", "success")
        return render_template('create_event.html', students=students)
    return render_template('create_event.html', students=students)

@app.route('/add_schedule', methods=['GET', 'POST'])
def add_schedule():
    if 'user' not in session:
        return redirect(url_for('login'))
    
    # Определяем временные интервалы для пар
    pairs_info = [
        ("08:00", "09:30"),
        ("09:40", "11:10"),
        ("11:20", "12:50"),
        ("13:20", "14:50"),
        ("15:00", "16:30"),
        ("16:40", "18:10"),
        ("18:20", "19:50"),
        ("19:55", "21:25"),
    ]
    
    if request.method == 'POST':
        direction = request.form.get('direction', '').strip().upper()
        group_number = request.form.get('group_number', '').strip()
        day_of_week = request.form.get('day_of_week', '').strip()
        week_type = request.form.get('week_type', '').strip()
        
        # Собираем данные для каждой пары (4 поля: предмет, тип занятия, преподаватель, аудитория)
        schedule_lines = []
        for i, (start_time, end_time) in enumerate(pairs_info):
            subject = request.form.get(f"subject_{i}", "").strip()
            lesson_type = request.form.get(f"type_{i}", "").strip()
            teacher = request.form.get(f"teacher_{i}", "").strip()
            room = request.form.get(f"room_{i}", "").strip()
            
            # Если все поля пустые, считаем, что пары нет; иначе формируем красиво отформатированную строку
            if not any([subject, lesson_type, teacher, room]):
                schedule_lines.append(f"{i+1}) {start_time}-{end_time}: Пары нет.")
            else:
                # Форматирование:
                # Предмет – жирным, преподаватель – курсивом, аудитория – верхним регистром.
                line = f"{i+1}) {start_time}-{end_time}: "
                line += f"<b>{subject or '-'}</b>"
                if lesson_type:
                    line += f" ({lesson_type})"
                if teacher:
                    line += f"<br><i>{teacher}</i>"
                if room:
                    line += f", ауд. {room.upper()}"
                schedule_lines.append(line)
        
        schedule_text = "<br>".join(schedule_lines)  # Используем <br> для переноса строк в HTML

        if not all([direction, group_number, day_of_week, week_type]):
            flash("Направление, номер группы, тип недели и день недели обязательны для заполнения!", "error")
            return render_template('add_schedule.html', pairs_info=pairs_info, enumerate=enumerate)
        
        # Определяем таблицу расписания по направлению
        if direction == "ПИ":
            table_name = "schedule_PI"
        elif direction == "ПРИ":
            table_name = "schedule_PRI"
        elif direction == "БИ":
            table_name = "schedule_BI"
        else:
            table_name = "schedule_other"
        
        try:
            conn = get_schedule_db_connection()
            cur = conn.cursor()
            if table_name == "schedule_other":
                query = f"""
                    INSERT INTO {table_name} (direction, group_number, week_type, day_of_week, schedule_text)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (direction, group_number, week_type, day_of_week)
                    DO UPDATE SET schedule_text = EXCLUDED.schedule_text
                """
                cur.execute(query, (direction, group_number, week_type, day_of_week, schedule_text))
            else:
                query = f"""
                    INSERT INTO {table_name} (group_number, week_type, day_of_week, schedule_text)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (group_number, week_type, day_of_week)
                    DO UPDATE SET schedule_text = EXCLUDED.schedule_text
                """
                cur.execute(query, (group_number, week_type, day_of_week, schedule_text))
            conn.commit()
            conn.close()
            flash("Расписание успешно добавлено/обновлено", "success")
            return render_template('add_schedule.html', pairs_info=pairs_info, enumerate=enumerate)
        except Exception as e:
            flash(f"Ошибка при добавлении расписания: {e}", "error")
            return render_template('add_schedule.html', pairs_info=pairs_info, enumerate=enumerate)
    
    return render_template('add_schedule.html', pairs_info=pairs_info, enumerate=enumerate)

@app.route('/register', methods=['GET', 'POST'])
def register_handler():
    if request.method == 'POST':
        data = request.form.get('data', '').strip()
        parts = data.split()
        if len(parts) != 3:
            flash("Неверный формат. Введите данные как: Имя Фамилия Группа", "error")
            return redirect(url_for('register_handler'))
        first_name, last_name, group = parts
        match = re.match(r"([А-ЯЁA-Z]+)-?(\d+)", group, re.IGNORECASE)
        if match:
            direction = match.group(1).upper()
            group_number = match.group(2)
        else:
            direction = "OTHER"
            group_number = group
        telegram_id = request.form.get('telegram_id', '')
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO students (telegram_id, first_name, last_name, group_name, direction, group_number)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE
                SET first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    group_name = EXCLUDED.group_name,
                    direction = EXCLUDED.direction,
                    group_number = EXCLUDED.group_number;
                """,
                (telegram_id, first_name, last_name, group, direction, group_number)
            )
        conn.commit()
        conn.close()
        flash("Вы успешно зарегистрированы!", "success")
        return redirect(url_for('index'))
    return render_template('register.html')

@app.route('/index')
def start_page():
    return render_template('index.html')

if __name__ == '__main__':
    init_db()
    init_schedule_db()
    app.run(debug=True, host='0.0.0.0', port=5000)
