import os
import json
from datetime import datetime, timedelta
import asyncio
import logging
import re
import sqlite3
import pytz
import shutil
import tempfile
import time
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.functions.messages import GetHistoryRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.errors import ChannelPrivateError, UsernameNotOccupiedError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import random
from fpdf import FPDF
from transliterate import translit
import platform
import requests
from PIL import Image
import io
import base64
from ai_service import (
    try_gpt_request, 
    get_available_models,
    get_user_model,
    user_models,
    MONICA_MODELS,
    OPENROUTER_MODELS,
    try_openrouter_request_with_images
)
import aiohttp
from typing import List, Optional, Tuple
import zlib

# Настраиваем логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Загружаем переменные окружения
logger.info("Загружаем .env файл...")
load_dotenv()
token = os.getenv('BOT_TOKEN')
logger.info(f"Токен: {token}")

# Секретный код для самостоятельного получения прав администратора
ADMIN_SECRET_CODE = "super_secure_admin_code"

if not token:
    raise ValueError("BOT_TOKEN не найден в .env файле!")

def get_db_connection(max_attempts=5, retry_delay=1):
    """Получение соединения с базой данных с обработкой блокировки"""
    attempt = 0
    while attempt < max_attempts:
        try:
            conn = sqlite3.connect('bot.db', timeout=20)  # Увеличиваем timeout
            return conn
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e):
                attempt += 1
                if attempt < max_attempts:
                    time.sleep(retry_delay)
                    continue
            raise
    raise sqlite3.OperationalError("Could not acquire database lock after multiple attempts")

def init_db():
    """Инициализация базы данных"""
    conn = get_db_connection()
    c = conn.cursor()
    
    try:
        # Таблица для отчетов
        c.execute('''CREATE TABLE IF NOT EXISTS reports
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER,
                      folder TEXT,
                      content TEXT,
                      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        
        # Таблица для расписания
        c.execute('''CREATE TABLE IF NOT EXISTS schedules
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER,
                      folder TEXT,
                      time TEXT,
                      is_active BOOLEAN DEFAULT 1)''')
        
        # Таблица для управления доступом
        c.execute('''CREATE TABLE IF NOT EXISTS access_control
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER,
                      is_admin BOOLEAN,
                      added_by INTEGER,
                      added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        
        conn.commit()
    finally:
        conn.close()

# Создаем планировщик (но не запускаем)
scheduler = AsyncIOScheduler(timezone=pytz.UTC)

# Декоратор для проверки доступа
def require_access(func):
    async def wrapper(message: types.Message, *args, **kwargs):
        if not is_user_allowed(message.from_user.id):
            await message.answer("⛔️ У вас нет доступа к боту. Обратитесь к администратору.")
            return
        # Удаляем raw_state и command из kwargs если они есть
        kwargs.pop('raw_state', None)
        kwargs.pop('command', None)
        return await func(message, *args, **kwargs)
    return wrapper

# Декоратор для проверки прав администратора
def require_admin(func):
    async def wrapper(message: types.Message, *args, **kwargs):
        if not is_user_admin(message.from_user.id):
            await message.answer("⛔️ Эта функция доступна только администраторам.")
            return
        # Удаляем raw_state и command из kwargs если они есть
        kwargs.pop('raw_state', None)
        kwargs.pop('command', None)
        return await func(message, *args, **kwargs)
    return wrapper

# Функции для управления доступом
def is_user_allowed(user_id: int) -> bool:
    """Проверяем, есть ли у пользователя доступ к боту"""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT 1 FROM access_control WHERE user_id = ?', (user_id,))
        result = c.fetchone() is not None
        return result
    finally:
        conn.close()

def is_user_admin(user_id: int) -> bool:
    """Проверяем, является ли пользователь администратором"""
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT is_admin FROM access_control WHERE user_id = ?', (user_id,))
        result = c.fetchone()
        return result[0] if result else False
    finally:
        conn.close()

# Инициализируем клиенты
bot = Bot(token=token, timeout=20)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Инициализируем клиент Telethon
client = TelegramClient(
    'telegram_session', 
    int(os.getenv('API_ID')), 
    os.getenv('API_HASH'),
    system_version="4.16.30-vxCUSTOM",
    device_model="Desktop",
    app_version="1.0.0"
)

# Структура для хранения данных
class UserData:
    def __init__(self):
        self.users = {}  # {user_id: {'folders': {}, 'prompts': {}, 'ai_settings': {}}}
        
    def get_user_data(self, user_id: int) -> dict:
        """Получаем или создаем данные пользователя"""
        if str(user_id) not in self.users:
            self.users[str(user_id)] = {
                'folders': {},
                'prompts': {},
                'ai_settings': {
                    'provider_index': 0,
                    'model': get_user_model(user_id),
                    'web_search_enabled': False,
                    'web_search_results': 3
                }
            }
        return self.users[str(user_id)]
        
    def save(self):
        with open('user_data.json', 'w', encoding='utf-8') as f:
            json.dump({'users': self.users}, f, ensure_ascii=False)
    
    @classmethod
    def load(cls):
        instance = cls()
        try:
            with open('user_data.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                instance.users = data.get('users', {})
        except FileNotFoundError:
            pass
        return instance

user_data = UserData.load()

# Состояния для FSM
class BotStates(StatesGroup):
    waiting_for_folder_name = State()
    waiting_for_channels = State()
    waiting_for_prompt = State()
    waiting_for_folder_to_edit = State()
    waiting_for_model_selection = State()
    waiting_for_schedule_folder = State()
    waiting_for_schedule_time = State()
    waiting_for_user_id = State()
    waiting_for_adding_user_type = State()

class AccessControlStates(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_user_id_remove = State()

def save_report(user_id: int, folder: str, content: str):
    """Сохраняем отчет в БД"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT INTO reports (user_id, folder, content) VALUES (?, ?, ?)',
              (user_id, folder, content))
    conn.commit()

def get_user_reports(user_id: int, limit: int = 10) -> list:
    """Получаем последние отчеты пользователя"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT folder, content, created_at FROM reports WHERE user_id = ? ORDER BY created_at DESC LIMIT ?',
              (user_id, limit))
    reports = c.fetchall()
    return reports

def save_schedule(user_id: int, folder: str, time: str):
    """Сохраняем расписание в БД"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT INTO schedules (user_id, folder, time) VALUES (?, ?, ?)',
              (user_id, folder, time))
    conn.commit()

def get_active_schedules() -> list:
    """Получаем все активные расписания"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT user_id, folder, time FROM schedules WHERE is_active = 1')
    schedules = c.fetchall()
    return schedules

def generate_unique_filename(base_name: str, extension: str) -> str:
    """
    Генерирует уникальное имя файла, добавляя (!n) если файл существует
    
    Args:
        base_name: Базовое имя файла без расширения
        extension: Расширение файла (с точкой)
        
    Returns:
        Уникальное имя файла
    """
    # Убедимся, что директория существует
    directory = os.path.dirname(base_name)
    if directory and not os.path.exists(directory):
        os.makedirs(directory)
        
    counter = 0
    while True:
        if counter == 0:
            filename = f"{base_name}{extension}"
        else:
            filename = f"{base_name}(!{counter}){extension}"
            
        if not os.path.exists(filename):
            return filename
        counter += 1

def generate_txt_report(content: str, folder: str) -> str:
    """Генерирует отчет в формате TXT"""
    current_time = datetime.now().strftime("%d%m")
    
    # Создаем директорию analysis если ее нет
    analysis_dir = "analysis"
    if not os.path.exists(analysis_dir):
        os.makedirs(analysis_dir)
        
    base_name = os.path.join(analysis_dir, f"{folder}-{current_time}")
    filename = generate_unique_filename(base_name, ".txt")
    
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(content)
    
    return filename

def generate_pdf_report(content: str, folder: str) -> str:
    """Генерирует отчет в формате PDF"""
    current_time = datetime.now().strftime("%d%m")
    
    # Создаем директорию analysis если ее нет
    analysis_dir = "analysis"
    if not os.path.exists(analysis_dir):
        os.makedirs(analysis_dir)
        
    base_name = os.path.join(analysis_dir, f"{folder}-{current_time}")
    filename = generate_unique_filename(base_name, ".pdf")
    
    pdf = FPDF()
    pdf.add_page()
    
    # Добавляем шрифт с поддержкой русского
    font_path = get_font_path()
    pdf.add_font('DejaVu', '', font_path, uni=True)
    pdf.set_font('DejaVu', '', 12)
    
    # Настраиваем отступы
    margin = 20
    pdf.set_margins(margin, margin, margin)
    pdf.set_auto_page_break(True, margin)
    
    # Пишем заголовок
    pdf.set_font_size(16)
    pdf.cell(0, 10, f'Анализ папки: {folder}', 0, 1, 'L')
    pdf.ln(10)
    
    # Возвращаемся к обычному размеру шрифта
    pdf.set_font_size(12)
    
    # Разбиваем контент на строки и обрабатываем форматирование
    for line in content.split('\n'):
        if not line.strip():  # Пропускаем пустые строки
            pdf.ln(5)
            continue
        
        if line.strip().startswith('###'):  # H3 заголовок
            pdf.set_font_size(14)
            pdf.cell(0, 10, line.strip().replace('###', '').strip(), 0, 1, 'L')
            pdf.set_font_size(12)
            pdf.ln(5)
        elif line.strip().startswith('####'):  # H4 заголовок
            pdf.set_font_size(13)
            pdf.cell(0, 10, line.strip().replace('####', '').strip(), 0, 1, 'L')
            pdf.set_font_size(12)
            pdf.ln(5)
        else:  # Обычный текст
            pdf.multi_cell(0, 10, line.strip())
            pdf.ln(5)
    
    # Сохраняем PDF
    try:
        pdf.output(filename, 'F')
    except Exception as e:
        logger.error(f"Ошибка при сохранении PDF: {str(e)}")
        # Пробуем сохранить с транслитерацией имени файла
        safe_filename = translit(filename, 'ru', reversed=True)
        pdf.output(safe_filename, 'F')
        os.rename(safe_filename, filename)  # Переименовываем обратно
    
    return filename

def generate_md_report(content: str, folder: str) -> str:
    """Генерирует отчет в формате Markdown"""
    current_time = datetime.now().strftime("%d%m")
    
    # Создаем директорию analysis если ее нет
    analysis_dir = "analysis"
    if not os.path.exists(analysis_dir):
        os.makedirs(analysis_dir)
        
    base_name = os.path.join(analysis_dir, f"{folder}-{current_time}")
    filename = generate_unique_filename(base_name, ".md")
    
    # Добавляем заголовок Markdown
    md_content = f"# Анализ папки: {folder}\n\n"
    md_content += f"*Дата создания: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n\n"
    md_content += f"---\n\n"
    
    # Добавляем основной контент
    md_content += content
    
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(md_content)
    
    return filename

# Определяем путь к шрифту в зависимости от ОС
def get_font_path():
    os_type = platform.system().lower()
    if os_type == 'linux':
        paths = [
            "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ]
    elif os_type == 'windows':
        paths = [
            "C:\\Windows\\Fonts\\DejaVuSans.ttf",
            os.path.join(os.getenv('LOCALAPPDATA'), 'Microsoft\\Windows\\Fonts\\DejaVuSans.ttf'),
            "DejaVuSans.ttf"  # В текущей директории
        ]
    else:  # MacOS и другие
        paths = [
            "/Library/Fonts/DejaVuSans.ttf",
            "/System/Library/Fonts/DejaVuSans.ttf",
            "DejaVuSans.ttf"  # В текущей директории
        ]
    
    # Проверяем наличие файла
    for path in paths:
        if os.path.exists(path):
            return path
            
    # Если шрифт не найден - скачиваем
    logger.info("Шрифт не найден, скачиваю...")
    try:
        import requests
        url = "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/ttf/DejaVuSans.ttf"
        response = requests.get(url)
        with open("DejaVuSans.ttf", "wb") as f:
            f.write(response.content)
        return "DejaVuSans.ttf"
    except Exception as e:
        logger.error(f"Не удалось скачать шрифт: {str(e)}")
        raise Exception("Не удалось найти или скачать шрифт DejaVuSans.ttf")

@dp.message_handler(commands=['start'])
@require_access
async def cmd_start(message: types.Message, state: FSMContext = None, **kwargs):
    me = await bot.get_me()
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = [
        "📁 Создать папку",
        "📋 Список папок",
        "✏️ Изменить промпт",
        "⚙️ Настройка ИИ",
        "🔄 Запустить анализ",
        "📊 История отчетов",
        "⏰ Настроить расписание"
    ]
    
    # Добавляем кнопки администратора
    if is_user_admin(message.from_user.id):
        buttons.extend([
            "👥 Управление доступом"
        ])
    
    keyboard.add(*buttons)
    await message.answer(
        f"Привет! Я бот для анализа Telegram каналов.\n"
        f"Мой юзернейм: @{me.username}\n"
        "Что хочешь сделать?",
        reply_markup=keyboard
    )

@dp.message_handler(commands=['init_admin'])
async def cmd_init_admin(message: types.Message):
    """Инициализация первого администратора"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM access_control')
    count = c.fetchone()[0]
    
    if count == 0:
        # Если нет пользователей, добавляем первого админа
        c.execute('INSERT INTO access_control (user_id, is_admin, added_by) VALUES (?, 1, ?)',
                 (message.from_user.id, message.from_user.id))
        conn.commit()
        await message.answer("✅ Вы успешно зарегистрированы как администратор!")
    else:
        await message.answer("❌ Администратор уже инициализирован")
    
    conn.close()

@dp.message_handler(commands=['admint'])
@require_admin
async def cmd_add_admin(message: types.Message, state: FSMContext = None, **kwargs):
    """Быстрое добавление пользователя как администратора"""
    # Извлекаем ID пользователя из текста
    parts = message.text.split()
    
    if len(parts) < 2:
        await message.answer(
            "❌ Необходимо указать ID пользователя.\n"
            "Пример: /admint 123456789"
        )
        return
    
    try:
        user_id = int(parts[1])
        if add_user_access(message.from_user.id, user_id, is_admin=True):
            await message.answer(f"✅ Пользователь с ID {user_id} успешно добавлен как администратор!")
        else:
            await message.answer("❌ Не удалось добавить пользователя. Возможно, он уже добавлен или у вас недостаточно прав.")
    except ValueError:
        await message.answer("❌ Некорректный ID пользователя. Введите числовой ID.")

@dp.message_handler(commands=['selfadmin'])
async def cmd_self_admin(message: types.Message, state: FSMContext = None, **kwargs):
    """Самостоятельное получение прав администратора с использованием секретного кода"""
    # Извлекаем код из текста
    parts = message.text.split()
    
    if len(parts) < 2:
        await message.answer(
            "❌ Необходимо указать секретный код.\n"
            "Пример: /selfadmin your_secret_code"
        )
        return
    
    secret_code = parts[1]
    
    if secret_code == ADMIN_SECRET_CODE:
        conn = get_db_connection()
        c = conn.cursor()
        
        try:
            # Проверяем, есть ли уже пользователь в базе
            c.execute('SELECT is_admin FROM access_control WHERE user_id = ?', (message.from_user.id,))
            existing_user = c.fetchone()
            
            if existing_user:
                if existing_user[0]:
                    await message.answer("✅ Вы уже являетесь администратором!")
                else:
                    # Обновляем права до администратора
                    c.execute('UPDATE access_control SET is_admin = 1 WHERE user_id = ?', (message.from_user.id,))
                    conn.commit()
                    await message.answer("✅ Поздравляем! Вы успешно получили права администратора!")
            else:
                # Добавляем нового пользователя как администратора
                c.execute('INSERT INTO access_control (user_id, is_admin, added_by) VALUES (?, 1, ?)', 
                         (message.from_user.id, message.from_user.id))
                conn.commit()
                await message.answer("✅ Поздравляем! Вы успешно зарегистрированы как администратор!")
                
        except Exception as e:
            logger.error(f"Ошибка при добавлении администратора: {str(e)}")
            await message.answer("❌ Произошла ошибка при обработке запроса.")
        finally:
            conn.close()
    else:
        await message.answer("❌ Неверный секретный код.")

@dp.message_handler(lambda message: message.text == "👥 Управление доступом")
@require_admin
async def access_control_menu(message: types.Message, state: FSMContext = None, **kwargs):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("➕ Добавить пользователя", callback_data="add_user"),
        types.InlineKeyboardButton("➖ Удалить пользователя", callback_data="remove_user"),
        types.InlineKeyboardButton("📋 Список пользователей", callback_data="list_users")
    )
    await message.answer("Управление доступом к боту:", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data == "list_users")
async def list_users(callback_query: types.CallbackQuery):
    users = get_allowed_users(callback_query.from_user.id)
    if not users:
        await callback_query.message.answer("Список пользователей пуст")
        return
        
    text = "📋 Список пользователей:\n\n"
    for user_id, is_admin, added_at in users:
        dt = datetime.fromisoformat(added_at.replace('Z', '+00:00'))
        text += f"{'👑' if is_admin else '👤'} ID: {user_id}\n"
        text += f"Добавлен: {dt.strftime('%Y-%m-%d %H:%M')}\n\n"
    
    await callback_query.message.answer(text)

@dp.message_handler(lambda message: message.text == "📁 Создать папку")
async def create_folder(message: types.Message):
    await BotStates.waiting_for_folder_name.set()
    await message.answer("Введи название папки:")

@dp.message_handler(state=BotStates.waiting_for_folder_name)
async def process_folder_name(message: types.Message, state: FSMContext):
    folder_name = message.text
    await state.update_data(current_folder=folder_name)
    user_data.get_user_data(message.from_user.id)['folders'][folder_name] = []
    user_data.get_user_data(message.from_user.id)['prompts'][folder_name] = "Проанализируй посты и составь краткий отчет"
    user_data.save()
    
    await BotStates.waiting_for_channels.set()
    await message.answer(
        "Отправь ссылки на каналы для этой папки.\n"
        "Каждую ссылку с новой строки.\n"
        "Когда закончишь, напиши 'готово'"
    )

def is_valid_channel(channel_link: str) -> bool:
    """Проверяем, что ссылка похожа на канал"""
    return bool(re.match(r'^@[\w\d_]+$', channel_link))

@dp.message_handler(state=BotStates.waiting_for_channels)
async def process_channels(message: types.Message, state: FSMContext):
    if message.text.lower() == 'готово':
        await state.finish()
        await message.answer("Папка создана! Используй /folders чтобы увидеть список папок")
        return

    data = await state.get_data()
    folder_name = data['current_folder']
    
    channels = [ch.strip() for ch in message.text.split('\n')]
    valid_channels = []
    
    for channel in channels:
        if not is_valid_channel(channel):
            await message.answer(f"❌ Канал {channel} не похож на правильную ссылку. Используй формат @username")
            continue
        valid_channels.append(channel)
    
    if valid_channels:
        user_data.get_user_data(message.from_user.id)['folders'][folder_name].extend(valid_channels)
        user_data.save()
        await message.answer(f"✅ Каналы добавлены в папку {folder_name}")

@dp.message_handler(lambda message: message.text == "📋 Список папок")
@require_access
async def list_folders(message: types.Message, state: FSMContext = None):
    if not user_data.get_user_data(message.from_user.id)['folders']:
        await message.answer("Пока нет созданных папок")
        return

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for folder in user_data.get_user_data(message.from_user.id)['folders']:
        keyboard.add(
            types.InlineKeyboardButton(
                f"📁 {folder}",
                callback_data=f"edit_folder_{folder}"
            )
        )
    
    await message.answer("Выберите папку для редактирования:", reply_markup=keyboard)

@dp.message_handler(commands=['folders'])
@require_access
async def cmd_list_folders(message: types.Message, state: FSMContext = None):
    await list_folders(message)

@dp.callback_query_handler(lambda c: c.data.startswith('edit_folder_'))
async def edit_folder_menu(callback_query: types.CallbackQuery):
    folder = callback_query.data.replace('edit_folder_', '')
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    
    # Добавляем кнопки для каждого канала
    channels = user_data.get_user_data(callback_query.from_user.id)['folders'][folder]
    for channel in channels:
        keyboard.add(
            types.InlineKeyboardButton(
                f"❌ {channel}",
                callback_data=f"remove_channel_{folder}_{channel}"  # Не убираем @ из канала
            )
        )
    
    # Добавляем основные кнопки управления
    keyboard.add(
        types.InlineKeyboardButton("➕ Добавить каналы", callback_data=f"add_channels_{folder}"),
        types.InlineKeyboardButton("❌ Удалить папку", callback_data=f"delete_folder_{folder}")
    )
    keyboard.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_folders"))
    
    await callback_query.message.edit_text(
        f"Редактирование папки {folder}:\n"
        f"Нажми на канал чтобы удалить его:\n" + 
        "\n".join(f"- {channel}" for channel in channels),
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith('add_channels_'))
async def add_channels_start(callback_query: types.CallbackQuery, state: FSMContext):
    folder = callback_query.data.replace('add_channels_', '')
    await state.update_data(current_folder=folder)
    await BotStates.waiting_for_channels.set()
    
    await callback_query.message.answer(
        "Отправь ссылки на каналы для добавления.\n"
        "Каждую ссылку с новой строки.\n"
        "Когда закончишь, напиши 'готово'"
    )

@dp.callback_query_handler(lambda c: c.data.startswith('delete_folder_'))
async def delete_folder(callback_query: types.CallbackQuery):
    folder = callback_query.data.replace('delete_folder_', '')
    user = user_data.get_user_data(callback_query.from_user.id)
    
    if folder in user['folders']:
        del user['folders'][folder]
        del user['prompts'][folder]
        user_data.save()
        
        await callback_query.message.edit_text(f"✅ Папка {folder} удалена")
        
@dp.callback_query_handler(lambda c: c.data == "back_to_folders")
async def back_to_folders(callback_query: types.CallbackQuery):
    await callback_query.message.delete()  # Удаляем сообщение с инлайн клавиатурой
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = [
        "📁 Создать папку",
        "📋 Список папок",
        "✏️ Изменить промпт",
        "⚙️ Настройка ИИ",
        "🔄 Запустить анализ",
        "📊 История отчетов",
        "⏰ Настроить расписание"
    ]
    keyboard.add(*buttons)
    await callback_query.message.answer("Главное меню:", reply_markup=keyboard)

@dp.message_handler(lambda message: message.text == "✏️ Изменить промпт")
async def edit_prompt_start(message: types.Message):
    if not user_data.get_user_data(message.from_user.id)['folders']:
        await message.answer("Сначала создай хотя бы одну папку!")
        return

    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for folder in user_data.get_user_data(message.from_user.id)['folders']:
        keyboard.add(folder)
    keyboard.add("🔙 Назад")
    
    await BotStates.waiting_for_folder_to_edit.set()
    await message.answer("Выбери папку для изменения промпта:", reply_markup=keyboard)

@dp.message_handler(state=BotStates.waiting_for_folder_to_edit)
async def process_folder_selection(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.finish()
        await back_to_main_menu(message, state)
        return

    if message.text not in user_data.get_user_data(message.from_user.id)['folders']:
        await message.answer("Такой папки нет. Попробуй еще раз")
        return

    await state.update_data(selected_folder=message.text)
    await BotStates.waiting_for_prompt.set()
    await message.answer(
        f"Текущий промпт для папки {message.text}:\n"
        f"{user_data.get_user_data(message.from_user.id)['prompts'][message.text]}\n\n"
        "Введи новый промпт:"
    )

@dp.message_handler(state=BotStates.waiting_for_prompt)
async def process_new_prompt(message: types.Message, state: FSMContext):
    data = await state.get_data()
    folder = data['selected_folder']
    
    user_data.get_user_data(message.from_user.id)['prompts'][folder] = message.text
    user_data.save()
    
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = [
        "📁 Создать папку",
        "📋 Список папок",
        "✏️ Изменить промпт",
        "⚙️ Настройка ИИ",
        "🔄 Запустить анализ",
        "📊 История отчетов",
        "⏰ Настроить расписание"
    ]
    keyboard.add(*buttons)
    
    await state.finish()
    await message.answer(
        f"Промпт для папки {folder} обновлен!",
        reply_markup=keyboard
    )

@dp.message_handler(lambda message: message.text == "⚙️ Настройка ИИ")
async def ai_settings(message: types.Message, state: FSMContext = None, **kwargs):
    # Получаем текущую модель пользователя
    current_model = get_user_model(message.from_user.id)
    all_models = get_available_models()
    model_info = all_models[current_model]
    
    # Определяем сервис модели
    service = "Monica AI"
    if current_model in OPENROUTER_MODELS:
        service = "OpenRouter"
    
    # Получаем информацию о настройках веб-поиска
    user_settings = user_data.get_user_data(message.from_user.id)
    web_search_enabled = user_settings['ai_settings'].get('web_search_enabled', False)
    web_search_results = user_settings['ai_settings'].get('web_search_results', 3)
    
    # Создаем клавиатуру
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton("📝 Выбрать модель", callback_data="choose_model"))
    
    # Добавляем кнопку настройки веб-поиска только если выбрана модель OpenRouter
    if service == "OpenRouter":
        web_search_status = "✅ Включен" if web_search_enabled else "❌ Выключен"
        keyboard.add(types.InlineKeyboardButton(
            f"🔍 Веб-поиск: {web_search_status}",
            callback_data="toggle_web_search"
        ))
        if web_search_enabled:
            keyboard.add(types.InlineKeyboardButton(
                f"📊 Количество результатов: {web_search_results}",
                callback_data="change_web_results"
            ))
    
    # Формируем информацию о веб-поиске
    web_search_info = ""
    if service == "OpenRouter":
        web_search_info = f"\n🔍 Веб-поиск: {'Включен' if web_search_enabled else 'Выключен'}"
        if web_search_enabled:
            web_search_info += f"\n📊 Результатов: {web_search_results} (≈${web_search_results*0.004:.3f} за запрос)"
            web_search_info += f"\n💰 <b>Цена = выдуманный кредит, который не исчерпывается, но у него есть перезарядка</b>"
    
    await message.answer(
        f"📊 Текущие настройки ИИ:\n\n"
        f"🔹 Модель: {model_info['name']}\n"
        f"🔧 Сервис: {service}\n"
        f"📝 Описание: {model_info['description']}\n"
        f"📊 Макс. токенов: {model_info['max_tokens']}{web_search_info}\n\n"
        f"ℹ️ Выберите, что хотите настроить:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

@dp.callback_query_handler(lambda c: c.data == "choose_model")
async def show_models(callback_query: types.CallbackQuery, state: FSMContext = None):
    # Получаем текущую модель
    current_model = get_user_model(callback_query.from_user.id)
    all_models = get_available_models()
    
    # Создаем клавиатуру для выбора модели
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    
    # Добавляем заголовок для моделей Monica AI
    keyboard.add(
        types.InlineKeyboardButton(
            "--- MONICA AI МОДЕЛИ ---",
            callback_data="no_action"
        )
    )
    
    # Добавляем модели Monica AI
    for model_id, model_info in MONICA_MODELS.items():
        keyboard.add(
            types.InlineKeyboardButton(
                f"{'✅ ' if model_id == current_model else ''}{model_info['name']}",
                callback_data=f"select_model_{model_id}"
            )
        )
    
    # Добавляем заголовок для моделей OpenRouter
    keyboard.add(
        types.InlineKeyboardButton(
            "--- OPENROUTER МОДЕЛИ ---",
            callback_data="no_action"
        )
    )
    
    # Добавляем модели OpenRouter
    for model_id, model_info in OPENROUTER_MODELS.items():
        keyboard.add(
            types.InlineKeyboardButton(
                f"{'✅ ' if model_id == current_model else ''}{model_info['name']}",
                callback_data=f"select_model_{model_id}"
            )
        )
    
    keyboard.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_settings"))
    
    await callback_query.message.edit_text(
        f"Текущая модель: {all_models[current_model]['name']}\n\n"
        f"Выберите новую модель из списка:",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith("select_model_"))
async def process_model_selection(callback_query: types.CallbackQuery, state: FSMContext = None):
    # Получаем выбранную модель из callback_data
    selected_model = callback_query.data.replace("select_model_", "")
    
    # Обновляем модель пользователя
    user_models[callback_query.from_user.id] = selected_model
    all_models = get_available_models()
    model_info = all_models[selected_model]
    
    # Определяем сервис модели
    service = "Monica AI"
    if selected_model in OPENROUTER_MODELS:
        service = "OpenRouter"
    
    # Получаем информацию о настройках веб-поиска
    user_settings = user_data.get_user_data(callback_query.from_user.id)
    web_search_enabled = user_settings['ai_settings'].get('web_search_enabled', False)
    web_search_results = user_settings['ai_settings'].get('web_search_results', 3)
    
    # Создаем клавиатуру
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton("📝 Выбрать модель", callback_data="choose_model"))
    
    # Добавляем кнопку настройки веб-поиска только если выбрана модель OpenRouter
    if service == "OpenRouter":
        web_search_status = "✅ Включен" if web_search_enabled else "❌ Выключен"
        keyboard.add(types.InlineKeyboardButton(
            f"🔍 Веб-поиск: {web_search_status}",
            callback_data="toggle_web_search"
        ))
        if web_search_enabled:
            keyboard.add(types.InlineKeyboardButton(
                f"📊 Количество результатов: {web_search_results}",
                callback_data="change_web_results"
            ))
    
    # Формируем информацию о веб-поиске
    web_search_info = ""
    if service == "OpenRouter":
        web_search_info = f"\n🔍 Веб-поиск: {'Включен' if web_search_enabled else 'Выключен'}"
        if web_search_enabled:
            web_search_info += f"\n📊 Результатов: {web_search_results} (≈${web_search_results*0.004:.3f} за запрос)"
            web_search_info += f"\n💰 <b>Цена = выдуманный кредит, который не исчерпывается, но у него есть перезарядка</b>"
    
    # Отправляем подтверждение
    await callback_query.message.edit_text(
        f"📊 Текущие настройки ИИ:\n\n"
        f"✅ Модель успешно изменена!\n\n"
        f"🔹 Модель: {model_info['name']}\n"
        f"🔧 Сервис: {service}\n"
        f"📝 Описание: {model_info['description']}\n"
        f"📊 Макс. токенов: {model_info['max_tokens']}{web_search_info}\n\n"
        f"ℹ️ Выберите, что хотите настроить:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    
    await callback_query.answer("✅ Модель успешно изменена!")

@dp.callback_query_handler(lambda c: c.data == "back_to_settings")
async def back_to_settings(callback_query: types.CallbackQuery, state: FSMContext = None):
    await ai_settings(callback_query.message, state)

@dp.callback_query_handler(lambda c: c.data == "toggle_web_search")
async def toggle_web_search(callback_query: types.CallbackQuery, state: FSMContext = None):
    user_settings = user_data.get_user_data(callback_query.from_user.id)
    current_status = user_settings['ai_settings'].get('web_search_enabled', False)
    
    # Переключаем статус
    user_settings['ai_settings']['web_search_enabled'] = not current_status
    user_data.save()
    
    # Получаем новый статус для отображения
    new_status = user_settings['ai_settings']['web_search_enabled']
    
    # Отправляем уведомление
    await callback_query.answer(
        f"Веб-поиск {'включен' if new_status else 'выключен'}."
    )
    
    # Обновляем меню настроек
    await ai_settings(callback_query.message, state)

@dp.callback_query_handler(lambda c: c.data == "change_web_results")
async def change_web_results(callback_query: types.CallbackQuery):
    keyboard = types.InlineKeyboardMarkup(row_width=3)
    
    # Добавляем кнопки с различными вариантами количества результатов
    buttons = []
    for num in [1, 3, 5, 10]:
        buttons.append(types.InlineKeyboardButton(
            f"{num}", callback_data=f"set_web_results_{num}"
        ))
    
    # Размещаем кнопки в ряд
    keyboard.add(*buttons)
    
    # Добавляем кнопку назад
    keyboard.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_settings"))
    
    await callback_query.message.edit_text(
        "Выберите количество результатов веб-поиска:\n\n"
        "Больше результатов даёт более точный анализ, но повышает стоимость запроса.\n"
        "Стоимость: ~$0.004 за результат.\n\n"
        "<b>💰 Цена = выдуманный кредит, который не исчерпывается, но у него есть перезарядка</b>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

@dp.callback_query_handler(lambda c: c.data.startswith("set_web_results_"))
async def set_web_results(callback_query: types.CallbackQuery, state: FSMContext = None):
    # Извлекаем число из callback_data
    num_results = int(callback_query.data.replace("set_web_results_", ""))
    
    # Обновляем настройки пользователя
    user_settings = user_data.get_user_data(callback_query.from_user.id)
    user_settings['ai_settings']['web_search_results'] = num_results
    user_data.save()
    
    # Уведомляем пользователя
    cost = num_results * 0.004
    await callback_query.answer(
        f"Установлено {num_results} результатов (≈${cost:.3f} за запрос)"
    )
    
    # Обновляем меню настроек
    await ai_settings(callback_query.message, state)

@dp.callback_query_handler(lambda c: c.data == "no_action")
async def no_action(callback_query: types.CallbackQuery):
    # Просто отвечаем на callback_query, чтобы убрать часы загрузки
    await callback_query.answer()

async def get_channel_posts(channel_link: str, hours: int = 24) -> list:
    """Получаем посты из канала за последние hours часов"""
    try:
        logger.info(f"Получаю посты из канала {channel_link}")
        
        if not is_valid_channel(channel_link):
            logger.error(f"Невалидная ссылка на канал: {channel_link}")
            return []
            
        try:
            # Пытаемся присоединиться к каналу
            channel = await client.get_entity(channel_link)
            try:
                await client(JoinChannelRequest(channel))
                logger.info(f"Успешно присоединился к каналу {channel_link}")
            except Exception as e:
                logger.warning(f"Не удалось присоединиться к каналу {channel_link}: {str(e)}")
                # Продолжаем работу, возможно мы уже подписаны
        except (ChannelPrivateError, UsernameNotOccupiedError) as e:
            logger.error(f"Не удалось получить доступ к каналу {channel_link}: {str(e)}")
            return []
        
        # Получаем историю сообщений
        posts = []
        time_threshold = datetime.now(channel.date.tzinfo) - timedelta(hours=hours)
        
        async for message in client.iter_messages(channel, limit=None):
            if message.date < time_threshold:
                break
                
            post_data = {
                'date': message.date.strftime('%Y-%m-%d %H:%M:%S'),
                'has_text': bool(message.text and len(message.text.strip()) > 0),
                'text': message.text if message.text else '',
                'has_photo': bool(message.photo),
                'photo_path': None
            }
            
            # Если есть фото, скачиваем его
            if message.photo:
                photo_path = await download_message_photo(message)
                post_data['photo_path'] = photo_path
            
            # Добавляем пост только если есть текст или фото
            if post_data['has_text'] or post_data['has_photo']:
                posts.append(post_data)
        
        logger.info(f"Получено {len(posts)} постов из канала {channel_link}")
        return posts
        
    except Exception as e:
        logger.error(f"Ошибка при получении постов из канала {channel_link}: {str(e)}")
        return []

async def download_message_photo(message, folder_name="temp_photos"):
    """Скачивает фото из сообщения если оно есть и возвращает путь к файлу"""
    if not message.photo:
        return None
    
    # Создаем директорию если её нет
    os.makedirs(folder_name, exist_ok=True)
    
    # Создаем также постоянную директорию для хранения всех фото
    permanent_folder = "photos"
    os.makedirs(permanent_folder, exist_ok=True)
    
    # Генерируем уникальное имя файла на основе даты и ID сообщения
    file_name = f"{message.date.strftime('%Y%m%d_%H%M%S')}_{message.id}.jpg"
    temp_path = os.path.join(folder_name, file_name)
    
    try:
        # Скачиваем фото
        path = await client.download_media(message.photo, temp_path)
        logger.info(f"Скачано фото: {path}")
        
        # Копируем в постоянную директорию
        permanent_path = os.path.join(permanent_folder, file_name)
        shutil.copy2(path, permanent_path)
        logger.info(f"Фото сохранено на постоянное хранение: {permanent_path}")
        
        return path
    except Exception as e:
        logger.error(f"Ошибка при скачивании фото: {str(e)}")
        return None

@dp.message_handler(lambda message: message.text == "📊 История отчетов")
async def show_reports(message: types.Message):
    reports = get_user_reports(message.from_user.id)
    if not reports:
        await message.answer("У вас пока нет сохраненных отчетов")
        return
        
    text = "📊 Последние отчеты:\n\n"
    for folder, content, created_at in reports:
        dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
        text += f"📁 {folder} ({dt.strftime('%Y-%m-%d %H:%M')})\n"
        
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for folder, _, _ in reports:
        keyboard.add(types.InlineKeyboardButton(
            f"📄 Отчет по {folder}",
            callback_data=f"report_{folder}"
        ))
        
    await message.answer(text, reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data.startswith('report_'))
async def show_report_content(callback_query: types.CallbackQuery):
    folder = callback_query.data.replace('report_', '')
    reports = get_user_reports(callback_query.from_user.id)
    
    for rep_folder, content, created_at in reports:
        if rep_folder == folder:
            dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            await callback_query.message.answer(
                f"📊 Отчет по папке {folder}\n"
                f"📅 {dt.strftime('%Y-%m-%d %H:%M')}\n\n"
                f"{content}"
            )
            break

@dp.message_handler(lambda message: message.text == "⏰ Настроить расписание")
async def setup_schedule_start(message: types.Message):
    user = user_data.get_user_data(message.from_user.id)
    if not user['folders']:
        await message.answer("Сначала создайте хотя бы одну папку!")
        return
        
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for folder in user['folders']:
        keyboard.add(folder)
    keyboard.add("🔙 Назад")
    
    await BotStates.waiting_for_schedule_folder.set()
    await message.answer(
        "Выберите папку для настройки расписания:",
        reply_markup=keyboard
    )

@dp.message_handler(state=BotStates.waiting_for_schedule_folder)
async def process_schedule_folder(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.finish()
        await back_to_main_menu(message, state)
        return
        
    user = user_data.get_user_data(message.from_user.id)
    if message.text not in user['folders']:
        await message.answer("Такой папки нет. Попробуйте еще раз")
        return
        
    await state.update_data(schedule_folder=message.text)
    await BotStates.waiting_for_schedule_time.set()
    
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add("🔙 Назад")
    
    await message.answer(
        "Введите время для ежедневного анализа в формате HH:MM (например, 09:00):",
        reply_markup=keyboard
    )

@dp.message_handler(state=BotStates.waiting_for_schedule_time)
async def process_schedule_time(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.finish()
        await back_to_main_menu(message, state)
        return

    if not re.match(r'^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$', message.text):
        await message.answer("Неверный формат времени. Используйте формат HH:MM (например, 09:00)")
        return
        
    data = await state.get_data()
    folder = data['schedule_folder']
    
    # Сохраняем расписание
    save_schedule(message.from_user.id, folder, message.text)
    
    # Добавляем задачу в планировщик
    hour, minute = map(int, message.text.split(':'))
    job_id = f"analysis_{message.from_user.id}_{folder}"
    
    scheduler.add_job(
        run_scheduled_analysis,
        'cron',
        hour=hour,
        minute=minute,
        id=job_id,
        replace_existing=True,
        args=[message.from_user.id, folder]
    )
    
    await state.finish()
    await message.answer(
        f"✅ Расписание установлено! Папка {folder} будет анализироваться ежедневно в {message.text}",
        reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add(*[
            "📁 Создать папку",
            "📋 Список папок",
            "✏️ Изменить промпт",
            "⚙️ Настройка ИИ",
            "🔄 Запустить анализ",
            "📊 История отчетов",
            "⏰ Настроить расписание"
        ])
    )

async def run_scheduled_analysis(user_id: int, folder: str):
    """Запуск анализа по расписанию"""
    try:
        user = user_data.get_user_data(user_id)
        channels = user['folders'][folder]
        
        all_posts = []
        for channel in channels:
            if not is_valid_channel(channel):
                continue
                
            posts = await get_channel_posts(channel)
            if posts:
                all_posts.extend(posts)
                
        if not all_posts:
            logger.error(f"Не удалось получить посты для автоматического анализа папки {folder}")
            return
            
        posts_text = "\n\n---\n\n".join([
            f"[{post['date']}]\n{post['text']}" for post in all_posts
        ])
        prompt = user['prompts'][folder]
        
        response = await try_gpt_request(prompt, posts_text, user_id, bot, user_data)
        
        # Сохраняем отчет
        save_report(user_id, folder, response)
        
        # Логируем успешное завершение отчета
        logger.info("отчет удался")
        
        # Отправляем уведомление пользователю
        await bot.send_message(
            user_id,
            f"✅ Автоматический анализ папки {folder} завершен!\n"
            f"Используйте '📊 История отчетов' чтобы просмотреть результат."
        )
        
    except Exception as e:
        error_msg = f"❌ Ошибка при автоматическом анализе: {str(e)}"
        logger.error(error_msg)
        await bot.send_message(user_id, error_msg)

@dp.message_handler(lambda message: message.text == "🔄 Запустить анализ")
async def start_analysis(message: types.Message):
    user = user_data.get_user_data(message.from_user.id)
    if not user['folders']:
        await message.answer("Сначала создайте хотя бы одну папку!")
        return
        
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    
    # Добавляем кнопки для каждой папки
    for folder in user['folders']:
        keyboard.add(types.InlineKeyboardButton(
            f"📁 {folder}",
            callback_data=f"format_{folder}"
        ))
    
    # Добавляем кнопку "Анализировать все" и "Назад"
    keyboard.add(types.InlineKeyboardButton(
        "📊 Анализировать все папки",
        callback_data="format_all"
    ))
    keyboard.add(types.InlineKeyboardButton(
        "🔙 В главное меню",
        callback_data="back_to_main"
    ))
    
    await message.answer(
        "Выберите папку для анализа:",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith('format_'))
async def choose_format(callback_query: types.CallbackQuery):
    # Проверяем, содержит ли callback_data уже выбранный формат
    if '_txt' in callback_query.data or '_pdf' in callback_query.data:
        # Если формат уже выбран, передаем управление следующему обработчику
        await choose_period(callback_query)
        return
        
    folder = callback_query.data.replace('format_', '')
    
    keyboard = types.InlineKeyboardMarkup(row_width=3)
    # Добавляем кнопки выбора формата
    keyboard.add(
        types.InlineKeyboardButton("📝 TXT", callback_data=f"period_{folder}_txt"),
        types.InlineKeyboardButton("📄 PDF", callback_data=f"period_{folder}_pdf"),
        types.InlineKeyboardButton("📋 MD", callback_data=f"period_{folder}_md")
    )
    keyboard.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_folders"))
    
    await callback_query.message.edit_text(
        f"Выберите формат отчета для {'всех папок' if folder == 'all' else f'папки {folder}'}:",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith('period_'))
async def choose_period(callback_query: types.CallbackQuery):
    # Парсим параметры из callback_data
    parts = callback_query.data.split('_')
    folder = parts[1]
    report_format = parts[2]  # txt или pdf
    
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    
    # Добавляем кнопки выбора периода
    periods = [
        ("24 часа", "24"),
        ("3 дня", "72")
    ]
    
    for period_name, hours in periods:
        if folder == 'all':
            keyboard.add(types.InlineKeyboardButton(
                f"📅 {period_name}",
                callback_data=f"analyze_all_{hours}_{report_format}"
            ))
        else:
            keyboard.add(types.InlineKeyboardButton(
                f"📅 {period_name}",
                callback_data=f"analyze_{folder}_{hours}_{report_format}"
            ))
    
    keyboard.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"format_{folder}"))
    
    await callback_query.message.edit_text(
        f"Выберите период анализа для {'всех папок' if folder == 'all' else f'папки {folder}'}:",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith('analyze_'))
async def process_analysis_choice(callback_query: types.CallbackQuery):
    # Парсим параметры из callback_data
    params = callback_query.data.replace('analyze_', '').split('_')
    if len(params) != 3:  # folder_hours_format
        await callback_query.message.answer("❌ Ошибка в параметрах анализа")
        return
        
    choice, hours, report_format = params
    hours = int(hours)
    user = user_data.get_user_data(callback_query.from_user.id)
    
    # Проверяем, включен ли веб-поиск для уведомления о стоимости
    web_search_enabled = user['ai_settings'].get('web_search_enabled', False)
    web_search_results = user['ai_settings'].get('web_search_results', 3)
    
    # Информация о веб-поиске
    web_search_info = ""
    if web_search_enabled:
        cost = web_search_results * 0.004
        web_search_info = f"\nℹ️ Веб-поиск: активен ({web_search_results} результатов, ≈${cost:.3f} за запрос)"
        web_search_info += f"\n<b>💰 Цена = выдуманный кредит, который не исчерпывается, но у него есть перезарядка</b>"
    
    await callback_query.message.edit_text(
        f"Начинаю анализ... Это может занять некоторое время{web_search_info}",
        parse_mode="HTML"
    )
    
    if choice == 'all':
        folders = user['folders'].items()
    else:
        folders = [(choice, user['folders'][choice])]
    
    # Создаем папку для временного хранения фотографий
    photo_folder = "temp_photos"
    if not os.path.exists(photo_folder):
        os.makedirs(photo_folder)
    
    # Папки для хранения постоянных фото
    permanent_photo_folder = "photo"
    if not os.path.exists(permanent_photo_folder):
        os.makedirs(permanent_photo_folder)
    
    # Дополнительная папка для фото (photos)
    additional_photo_folder = "photos"
    if not os.path.exists(additional_photo_folder):
        os.makedirs(additional_photo_folder)
    
    # Флаг для отслеживания использования фотографий
    photos_used = False
    photo_paths = []
    
    for folder, channels in folders:
        await callback_query.message.answer(f"Анализирую папку {folder}...")
        
        all_posts = []
        for channel in channels:
            if not is_valid_channel(channel):
                continue
                
            posts = await get_channel_posts(channel, hours=hours)
            if posts:
                all_posts.extend(posts)
            else:
                await callback_query.message.answer(f"⚠️ Не удалось получить посты из канала {channel}")
        
        if not all_posts:
            await callback_query.message.answer(f"❌ Не удалось получить посты из каналов в папке {folder}")
            continue
            
        # Сортируем посты по дате
        all_posts.sort(key=lambda x: x['date'], reverse=True)
        
        # Проверяем, есть ли изображения в постах
        has_images = any(post.get('has_photo', False) for post in all_posts)
        if has_images:
            photos_used = True
            # Собираем пути ко всем используемым фотографиям
            for post in all_posts:
                if post.get('has_photo', False) and post.get('photo_path'):
                    photo_paths.append(post['photo_path'])
        
        # Если есть изображения - используем новую функцию для анализа с изображениями
        prompt = user['prompts'][folder]
        
        try:
            if has_images:
                # Используем новую функцию для анализа с изображениями
                response = await try_openrouter_request_with_images(
                    prompt, 
                    all_posts, 
                    callback_query.from_user.id, 
                    bot, 
                    user_data
                )
                
                # После успешного запроса копируем фотографии в постоянную папку
                for post in all_posts:
                    if post.get('has_photo', False) and post.get('photo_path'):
                        try:
                            # Получаем имя файла из пути
                            filename = os.path.basename(post['photo_path'])
                            # Создаем новый путь в постоянной папке
                            new_path = os.path.join(permanent_photo_folder, filename)
                            # Копируем файл в постоянную папку
                            shutil.copy2(post['photo_path'], new_path)
                            # Добавляем путь в список для последующего удаления
                            photo_paths.append(new_path)
                            # Обновляем путь к файлу в посте
                            post['photo_path'] = new_path
                            logger.info(f"Фото скопировано в постоянную папку: {new_path}")
                        except Exception as e:
                            logger.error(f"Ошибка при копировании фото в постоянную папку: {str(e)}")
            else:
                # Используем стандартную функцию для анализа только текста
                posts_text = "\n\n---\n\n".join([
                    f"[{post['date']}]\n{post['text']}" for post in all_posts if post.get('has_text', False)
                ])
                
                response = await try_gpt_request(prompt, posts_text, callback_query.from_user.id, bot, user_data)
            
            # Сохраняем отчет в БД
            save_report(callback_query.from_user.id, folder, response)
            
            # Генерируем отчет в выбранном формате
            if report_format == 'txt':
                filename = generate_txt_report(response, folder)
            elif report_format == 'md':
                filename = generate_md_report(response, folder)
            else:  # pdf
                try:
                    filename = generate_pdf_report(response, folder)
                except Exception as pdf_error:
                    logger.error(f"Ошибка при создании PDF: {str(pdf_error)}")
                    await callback_query.message.answer("⚠️ Не удалось создать PDF версию отчета. Создаю MD версию вместо PDF...")
                    
                    try:
                        filename = generate_md_report(response, folder)
                        report_format = 'md'
                        await callback_query.message.answer("✅ Отчет успешно создан в формате Markdown")
                    except Exception as md_error:
                        logger.error(f"Ошибка при создании MD: {str(md_error)}")
                        await callback_query.message.answer("⚠️ Пробую создать TXT версию...")
                        try:
                            filename = generate_txt_report(response, folder)
                            report_format = 'txt'
                            await callback_query.message.answer("✅ Отчет успешно создан в формате TXT")
                        except Exception as txt_error:
                            logger.error(f"Ошибка при создании TXT: {str(txt_error)}")
                            await callback_query.message.answer("❌ Не удалось создать отчет ни в каком формате")
                            return
            
            # Отправляем файл
            with open(filename, 'rb') as f:
                await callback_query.message.answer_document(
                    f,
                    caption=f"✅ Анализ для папки {folder} ({report_format.upper()})"
                )
            
            # Удаляем временный файл отчета
            os.remove(filename)
            
            # Удаляем фотографии, если они были использованы и получен ответ от API
            if photos_used:
                logger.info("Удаляю все использованные фотографии после получения ответа от API")
                await delete_photos(photo_paths)
                
        except Exception as e:
            error_msg = f"❌ Ошибка при анализе папки {folder}: {str(e)}"
            logger.error(error_msg)
            await callback_query.message.answer(error_msg)
            
    # Удаляем все фотографии из всех папок
    await delete_all_photos([photo_folder, permanent_photo_folder, additional_photo_folder])
            
    await callback_query.message.answer("✅ Анализ завершен!")

async def delete_photos(photo_paths):
    """Удаляет фотографии по указанным путям"""
    for path in photo_paths:
        try:
            if os.path.exists(path) and os.path.isfile(path):
                os.remove(path)
                logger.info(f"Удален файл: {path}")
        except Exception as e:
            logger.error(f"Ошибка при удалении файла {path}: {str(e)}")

async def delete_all_photos(folders):
    """Удаляет все фотографии из указанных папок"""
    for folder in folders:
        try:
            if os.path.exists(folder):
                for file in os.listdir(folder):
                    file_path = os.path.join(folder, file)
                    if os.path.isfile(file_path) and file.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                        try:
                            os.remove(file_path)
                            logger.info(f"Удален файл из папки {folder}: {file}")
                        except Exception as e:
                            logger.error(f"Ошибка при удалении файла {file_path}: {str(e)}")
        except Exception as e:
            logger.error(f"Ошибка при очистке папки {folder}: {str(e)}")

@dp.message_handler(lambda message: message.text == "🔙 Назад", state="*")
async def back_to_main_menu(message: types.Message, state: FSMContext):
    await state.finish()
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = [
        "📁 Создать папку",
        "📋 Список папок",
        "✏️ Изменить промпт",
        "⚙️ Настройка ИИ",
        "🔄 Запустить анализ",
        "📊 История отчетов",
        "⏰ Настроить расписание"
    ]
    await message.answer("Главное меню:", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data.startswith('remove_channel_'))
async def remove_channel(callback_query: types.CallbackQuery):
    try:
        # Парсим данные из callback
        parts = callback_query.data.split('_')
        if len(parts) < 4:  # remove_channel_folder_channelname
            logger.error(f"Неверный формат callback_data: {callback_query.data}")
            await callback_query.answer("❌ Ошибка формата данных")
            return
            
        folder = parts[2]  # Третий элемент - имя папки
        channel = '_'.join(parts[3:])  # Все остальное - имя канала
        
        # Проверяем не является ли это кнопкой отмены
        if "отмена" in folder.lower() or "отмена" in channel.lower():
            await callback_query.answer("Отменено")
            return
            
        user = user_data.get_user_data(callback_query.from_user.id)
        
        logger.info(f"Попытка удаления канала {channel} из папки {folder}")
        logger.info(f"Доступные папки: {list(user['folders'].keys())}")
        logger.info(f"Каналы в папке {folder}: {user['folders'].get(folder, [])}")
        
        if folder not in user['folders']:
            logger.error(f"Папка {folder} не найдена")
            await callback_query.answer("❌ Папка не найдена")
            return
            
        if channel not in user['folders'][folder]:
            logger.error(f"Канал {channel} не найден в папке {folder}")
            await callback_query.answer("❌ Канал не найден в папке")
            return
            
        # Удаляем канал
        user['folders'][folder].remove(channel)
        user_data.save()
        
        logger.info(f"Канал {channel} успешно удален из папки {folder}")
        
        # Обновляем клавиатуру
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        
        # Добавляем оставшиеся каналы
        for ch in user['folders'][folder]:
            keyboard.add(
                types.InlineKeyboardButton(
                    f"❌ {ch}",
                    callback_data=f"remove_channel_{folder}_{ch}"
                )
            )
        
        # Добавляем кнопки управления
        keyboard.add(
            types.InlineKeyboardButton("➕ Добавить каналы", callback_data=f"add_channels_{folder}"),
            types.InlineKeyboardButton("❌ Удалить папку", callback_data=f"delete_folder_{folder}")
        )
        keyboard.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_folders"))
        
        # Обновляем сообщение
        await callback_query.message.edit_text(
            f"Редактирование папки {folder}:\n"
            f"Нажми на канал чтобы удалить его:\n" + 
            "\n".join(f"- {ch}" for ch in user['folders'][folder]),
            reply_markup=keyboard
        )
        
        await callback_query.answer("✅ Канал удален")
        
    except Exception as e:
        logger.error(f"Ошибка при удалении канала: {str(e)}")
        await callback_query.answer("❌ Произошла ошибка при удалении канала")

def add_user_access(admin_id: int, user_id: int, is_admin: bool = False) -> bool:
    """Добавляем пользователя в список разрешенных"""
    if not is_user_admin(admin_id):
        return False
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('INSERT INTO access_control (user_id, is_admin, added_by) VALUES (?, ?, ?)',
                 (user_id, is_admin, admin_id))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False

def remove_user_access(admin_id: int, user_id: int) -> bool:
    """Удаляем пользователя из списка разрешенных"""
    if not is_user_admin(admin_id):
        return False
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('DELETE FROM access_control WHERE user_id = ? AND user_id != ?', (user_id, admin_id))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def get_allowed_users(admin_id: int) -> list:
    """Получаем список разрешенных пользователей"""
    if not is_user_admin(admin_id):
        return []
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT user_id, is_admin, added_at FROM access_control')
    users = c.fetchall()
    conn.close()
    return users

@dp.callback_query_handler(lambda c: c.data == "add_user")
async def add_user_start(callback_query: types.CallbackQuery):
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("👤 Обычный пользователь", callback_data="add_regular_user"),
        types.InlineKeyboardButton("👑 Администратор", callback_data="add_admin_user"),
        types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_access_control")
    )
    await callback_query.message.edit_text(
        "Выберите тип пользователя для добавления:",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data in ["add_regular_user", "add_admin_user"])
async def process_user_type(callback_query: types.CallbackQuery, state: FSMContext):
    user_type = "admin" if callback_query.data == "add_admin_user" else "regular"
    await state.update_data(adding_user_type=user_type)
    await BotStates.waiting_for_user_id.set()
    
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add("🔙 Отмена")
    
    await callback_query.message.edit_text(
        "Введите ID пользователя для добавления.\n"
        "ID можно получить, если пользователь перешлет сообщение от @userinfobot"
    )

@dp.message_handler(state=BotStates.waiting_for_user_id)
async def process_add_user(message: types.Message, state: FSMContext):
    if message.text == "🔙 Отмена":
        await state.finish()
        await back_to_main_menu(message, state)
        return
        
    try:
        user_id = int(message.text)
        data = await state.get_data()
        is_admin = data.get('adding_user_type') == 'admin'
        
        if add_user_access(message.from_user.id, user_id, is_admin):
            keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
            buttons = [
                "📁 Создать папку",
                "📋 Список папок",
                "✏️ Изменить промпт",
                "⚙️ Настройка ИИ",
                "🔄 Запустить анализ",
                "📊 История отчетов",
                "⏰ Настроить расписание",
                "👥 Управление доступом"
            ]
            keyboard.add(*buttons)
            
            await message.answer(
                f"✅ Пользователь {user_id} успешно добавлен как "
                f"{'администратор' if is_admin else 'пользователь'}!",
                reply_markup=keyboard
            )
        else:
            await message.answer("❌ Не удалось добавить пользователя. Возможно, он уже добавлен.")
    except ValueError:
        await message.answer("❌ Неверный формат ID. Введите числовой ID пользователя.")
    finally:
        await state.finish()

@dp.callback_query_handler(lambda c: c.data == "remove_user")
async def remove_user_start(callback_query: types.CallbackQuery):
    users = get_allowed_users(callback_query.from_user.id)
    if not users:
        await callback_query.message.answer("Список пользователей пуст")
        return
        
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for user_id, is_admin, _ in users:
        if user_id != callback_query.from_user.id:  # Не даем удалить самого себя
            keyboard.add(types.InlineKeyboardButton(
                f"{'👑' if is_admin else '👤'} {user_id}",
                callback_data=f"remove_user_{user_id}"
            ))
    keyboard.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_access_control"))
    
    await callback_query.message.edit_text(
        "Выберите пользователя для удаления:",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith("remove_user_"))
async def process_remove_user(callback_query: types.CallbackQuery):
    user_id = int(callback_query.data.replace("remove_user_", ""))
    if remove_user_access(callback_query.from_user.id, user_id):
        await callback_query.message.edit_text(f"✅ Пользователь {user_id} удален")
    else:
        await callback_query.message.edit_text("❌ Не удалось удалить пользователя")

@dp.callback_query_handler(lambda c: c.data == "back_to_access_control")
async def back_to_access_control(callback_query: types.CallbackQuery):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("➕ Добавить пользователя", callback_data="add_user"),
        types.InlineKeyboardButton("➖ Удалить пользователя", callback_data="remove_user"),
        types.InlineKeyboardButton("📋 Список пользователей", callback_data="list_users")
    )
    await callback_query.message.edit_text(
        "Управление доступом к боту:",
        reply_markup=keyboard
    )

async def get_free_proxies() -> List[str]:
    """Получение списка бесплатных прокси"""
    proxies = []
    
    # Список API с бесплатными прокси
    proxy_apis = [
        "https://proxyfreeonly.com/api/free-proxy-list?limit=500&page=1&sortBy=lastChecked&sortType=desc",
        "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
        "https://www.proxy-list.download/api/v1/get?type=http"
    ]
    
    async with aiohttp.ClientSession() as session:
        for api in proxy_apis:
            try:
                async with session.get(api, timeout=10) as response:
                    if response.status == 200:
                        if 'proxyfreeonly.com' in api:
                            # Специальная обработка для proxyfreeonly.com
                            data = await response.json()
                            for proxy in data:
                                if proxy.get('protocols') and proxy.get('ip') and proxy.get('port'):
                                    for protocol in proxy['protocols']:
                                        proxy_str = f"{protocol}://{proxy['ip']}:{proxy['port']}"
                                        if proxy.get('anonymityLevel') == 'elite' and proxy.get('upTime', 0) > 80:
                                            proxies.append(proxy_str)
                        else:
                            # Обработка других API
                            text = await response.text()
                            proxy_list = [
                                f"http://{proxy.strip()}" 
                                for proxy in text.split('\n') 
                                if proxy.strip() and ':' in proxy
                            ]
                            proxies.extend(proxy_list)
                            
            except Exception as e:
                logger.warning(f"Ошибка при получении прокси из {api}: {str(e)}")
                continue
    
    return list(set(proxies))  # Убираем дубликаты

class ProxyManager:
    def __init__(self):
        self.proxies = []
        self.last_update = None
        self.cache_duration = 1800  # 30 минут
        self.working_proxies = {}  # Кэш рабочих прокси
        self.failed_proxies = set()  # Множество неработающих прокси
        
    async def test_proxy(self, proxy: str) -> bool:
        """Проверка работоспособности прокси"""
        if proxy in self.failed_proxies:
            return False
            
        if proxy in self.working_proxies:
            # Проверяем, не устарел ли кэш
            last_check = self.working_proxies[proxy]['last_check']
            if (datetime.now() - last_check).total_seconds() < 300:  # 5 минут
                return True
                
        try:
            start_time = time.time()
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    'https://api.ipify.org?format=json',
                    proxy=proxy,
                    timeout=5
                ) as response:
                    if response.status == 200:
                        response_time = time.time() - start_time
                        self.working_proxies[proxy] = {
                            'last_check': datetime.now(),
                            'response_time': response_time
                        }
                        return True
                    return False
        except Exception as e:
            self.failed_proxies.add(proxy)
            if proxy in self.working_proxies:
                del self.working_proxies[proxy]
            return False

    async def get_proxy(self) -> Optional[str]:
        """Получает рабочий прокси из кэша или обновляет список"""
        if self.should_update_cache():
            await self.update_cache()
            
        # Сначала проверяем уже известные рабочие прокси
        working_proxies = list(self.working_proxies.keys())
        random.shuffle(working_proxies)
        
        for proxy in working_proxies[:5]:  # Проверяем только первые 5
            if await self.test_proxy(proxy):
                return proxy
        
        # Если нет рабочих прокси в кэше, проверяем новые
        random.shuffle(self.proxies)
        for proxy in self.proxies:
            if proxy not in self.failed_proxies and await self.test_proxy(proxy):
                return proxy
        
        # Если все прокси не работают, обновляем кэш
        if self.proxies:
            await self.update_cache()
            # Пробуем еще раз
            random.shuffle(self.proxies)
            for proxy in self.proxies:
                if proxy not in self.failed_proxies and await self.test_proxy(proxy):
                    return proxy
        
        return None

    def should_update_cache(self) -> bool:
        """Проверяет, нужно ли обновить кэш"""
        if not self.last_update:
            return True
        return (datetime.now() - self.last_update).total_seconds() > self.cache_duration

    async def update_cache(self):
        """Обновляет кэш прокси"""
        self.proxies = await get_free_proxies()
        self.last_update = datetime.now()
        # Очищаем устаревшие данные
        self.failed_proxies.clear()
        old_time = datetime.now() - timedelta(minutes=30)
        self.working_proxies = {
            k: v for k, v in self.working_proxies.items() 
            if v['last_check'] > old_time
        }
        logger.info(f"Кэш прокси обновлен. Получено {len(self.proxies)} прокси")

async def convert_mermaid_to_image(mermaid_code: str) -> Optional[bytes]:
    """Конвертирует Mermaid-код в изображение через Kroki"""
    try:
        # Кодируем диаграмму в base64 и сжимаем
        import zlib
        import base64
        
        # Очищаем код от лишних пробелов и переносов строк
        mermaid_code = "\n".join(line.strip() for line in mermaid_code.split("\n") if line.strip())
        
        # Кодируем и сжимаем данные
        deflated = zlib.compress(mermaid_code.encode('utf-8'))
        encoded = base64.urlsafe_b64encode(deflated).decode('ascii')
        
        # Формируем URL для запроса к Kroki
        url = f"https://kroki.io/mermaid/png/{encoded}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    image_data = await response.read()
                    
                    # Улучшаем качество изображения с помощью PIL
                    try:
                        img = Image.open(io.BytesIO(image_data))
                        
                        # Увеличиваем размер изображения
                        new_size = (img.size[0] * 2, img.size[1] * 2)
                        img = img.resize(new_size, Image.Resampling.LANCZOS)
                        
                        # Улучшаем качество
                        output = io.BytesIO()
                        img.save(output, format='PNG', quality=95, optimize=True)
                        return output.getvalue()
                    except Exception as e:
                        logger.warning(f"Ошибка при обработке изображения через PIL: {str(e)}")
                        return image_data
                else:
                    error_text = await response.text()
                    logger.error(f"Ошибка при получении изображения от Kroki: {response.status}, ответ: {error_text}")
                    return None
    except Exception as e:
        logger.error(f"Ошибка при конвертации Mermaid в изображение: {str(e)}")
        return None

async def generate_mermaid_diagram(analysis_text: str, user_id: int) -> Optional[str]:
    """Генерирует Mermaid-диаграмму на основе анализа"""
    try:
        prompt = (
            "На основе следующего анализа создай простую Mermaid-диаграмму. "
            "Следуй этим правилам СТРОГО:\n"
            "1. Начни с 'graph TD'\n"
            "2. Используй только латинские буквы и цифры для ID узлов\n"
            "3. Каждый узел должен иметь уникальный ID\n"
            "4. Максимум 10 узлов\n"
            "5. Используй только простые стрелки '-->' для связей\n"
            "6. Текст узлов должен быть кратким, на русском языке\n"
            "7. Не используй HTML-теги или спецсимволы\n"
            "8. Формат узла: ID[\"Текст узла\"]\n"
            "9. Формат связи: ID1 --> ID2\n\n"
            "Пример правильного кода:\n"
            "graph TD\n"
            "    A[\"Главная тема\"] --> B[\"Подтема 1\"]\n"
            "    A --> C[\"Подтема 2\"]\n"
            "    B --> D[\"Вывод 1\"]\n"
            "    C --> E[\"Вывод 2\"]\n\n"
            f"Анализ:\n{analysis_text}"
        )
        
        mermaid_code = await try_gpt_request(prompt, "", user_id, bot, user_data)
        if not mermaid_code:
            return None
            
        # Очищаем код от markdown обрамления
        mermaid_code = mermaid_code.replace("```mermaid", "").replace("```", "").strip()
        
        # Проверяем, что код начинается с graph TD
        if not mermaid_code.startswith("graph TD"):
            mermaid_code = "graph TD\n" + mermaid_code
            
        # Добавляем отступы для лучшей читаемости
        mermaid_code = "\n".join(
            "    " + line if line.strip() and not line.strip().startswith("graph") else line
            for line in mermaid_code.split("\n")
        )
        
        return mermaid_code
    except Exception as e:
        logger.error(f"Ошибка при генерации Mermaid-диаграммы: {str(e)}")
        return None

async def main():
    try:
        # Инициализируем базу данных
        init_db()
        
        # Запускаем клиент Telethon
        await client.start()
        
        # Запускаем планировщик
        scheduler.start()
        
        # Восстанавливаем сохраненные расписания
        for user_id, folder, time in get_active_schedules():
            hour, minute = map(int, time.split(':'))
            job_id = f"analysis_{user_id}_{folder}"
            scheduler.add_job(
                run_scheduled_analysis,
                'cron',
                hour=hour,
                minute=minute,
                id=job_id,
                replace_existing=True,
                args=[user_id, folder]
            )
            logger.info(f"Восстановлено расписание: {job_id} в {time}")
        
        # Получаем инфо о боте с обработкой таймаута
        try:
            async with asyncio.timeout(10):
                me = await bot.get_me()
                logger.info(f"Бот @{me.username} запущен!")
        except asyncio.TimeoutError:
            logger.error("Таймаут при получении информации о боте")
            raise
        
        # Запускаем поллинг
        await dp.start_polling()
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {str(e)}")
        raise
    finally:
        # Закрываем все соединения
        await dp.storage.close()
        await dp.storage.wait_closed()
        await bot.session.close()
        await client.disconnect()
        scheduler.shutdown()

if __name__ == '__main__':
    # Настраиваем политику событийного цикла
    if platform.system() == 'Windows':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    # Создаем и запускаем событийный цикл
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка: {str(e)}")
    finally:
        # Закрываем все незакрытые таски
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        
        # Запускаем все отмененные таски для корректного завершения
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        
        loop.close()
        logger.info("Бот остановлен") 
