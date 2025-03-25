import os
import json
import logging
import random
import aiohttp
import asyncio
import traceback
from typing import Optional, List, Dict
from datetime import datetime
from aiogram import Bot
import base64

# Настраиваем логирование
logger = logging.getLogger(__name__)

# Доступные модели Monica AI
MONICA_MODELS = {
    "gpt-4o": {
        "name": "GPT-4 Optimized",
        "description": "Оптимизированная версия GPT-4",
        "max_tokens": "8,000"
    },
    "claude-3-5-sonnet-20241022": {
        "name": "Claude 3.5 Sonnet", 
        "description": "Мощная модель с большим контекстом",
        "max_tokens": "200,000"
    },
    "claude-3-haiku-20240307": {
        "name": "Claude 3 Haiku",
        "description": "Быстрая и эффективная модель Claude 3",
        "max_tokens": "4000"
    },
    "o1-mini": {
        "name": "O1 Mini",
        "description": "Компактная и быстрая модель",
        "max_tokens": "2,000"
    }
}

# Доступные модели OpenRouter
OPENROUTER_MODELS = {
    "anthropic/claude-3-7-sonnet": {
        "name": "Claude 3.7 Sonnet",
        "description": "Стандартная версия модели с модерацией контента",
        "max_tokens": "200,000"
    },
    "anthropic/claude-3-7-sonnet:thinking": {
        "name": "Claude 3.7 Sonnet (Thinking)",
        "description": "Версия с расширенным режимом рассуждений для сложных задач",
        "max_tokens": "200,000"
    },
    "anthropic/claude-3-7-sonnet:beta": {
        "name": "Claude 3.7 Sonnet (Beta)",
        "description": "Версия без модерации контента с полным доступом",
        "max_tokens": "200,000"
    }
}

# Хранилище выбранных моделей пользователей
user_models: Dict[int, str] = {}
# Хранилище сервисов моделей (monica или openrouter)
user_model_services: Dict[int, str] = {}

def get_available_models():
    """Получение списка доступных моделей"""
    # Объединяем словари моделей Monica AI и OpenRouter
    all_models = {**MONICA_MODELS, **OPENROUTER_MODELS}
    return all_models

def get_user_model(user_id: int) -> str:
    """Получение модели пользователя или модели по умолчанию"""
    return user_models.get(user_id, "gpt-4o")

def get_user_model_service(user_id: int) -> str:
    """Получение сервиса модели пользователя (monica или openrouter)"""
    # Определяем сервис на основе выбранной модели
    model = get_user_model(user_id)
    if model in MONICA_MODELS:
        return "monica"
    elif model in OPENROUTER_MODELS:
        return "openrouter"
    # По умолчанию используем Monica AI
    return "monica"

async def try_gpt_request(prompt: str, posts_text: str, user_id: int, bot: Bot, user_data: dict):
    """Запрос к Monica AI API или OpenRouter API в зависимости от выбранной модели"""
    service = get_user_model_service(user_id)
    
    if service == "monica":
        return await try_monica_request(prompt, posts_text, user_id, bot, user_data)
    elif service == "openrouter":
        return await try_openrouter_request(prompt, posts_text, user_id, bot, user_data)
    else:
        error_msg = f"❌ Неизвестный сервис модели: {service}"
        logger.error(error_msg)
        raise Exception(error_msg)

async def try_monica_request(prompt: str, posts_text: str, user_id: int, bot: Bot, user_data: dict):
    """Запрос к Monica AI API"""
    status_message = None
    try:
        text_length = len(posts_text)
        selected_model = get_user_model(user_id)
        model_info = MONICA_MODELS[selected_model]
        
        # Получаем текущую дату и время
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Отправляем сообщение о начале анализа
        status_message = await bot.send_message(
            user_id,
            f"🔄 Начинаю анализ...\n"
            f"Размер данных: {text_length} символов\n"
            f"Используем: Monica AI - {model_info['name']}\n"
            f"Текущая дата и время: {current_time}"
        )
        
        # Загружаем API ключ из .env
        api_key = os.getenv("MONICA_API_KEY")
        if not api_key:
            error_msg = "❌ API ключ Monica не найден в .env файле"
            logger.error(error_msg)
            if status_message:
                await status_message.edit_text(error_msg)
            raise Exception(error_msg)
        
        # Подготавливаем запрос к Monica AI
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        
        # Подготавливаем сообщения
        messages = [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": f"Ты мой личный ассистент для анализа данных. Ты отвечаешь качественно как истинный ИИ-профи, выступаешь как самый лучший аналитик, указываешь на риски и возможности. Текущая дата и время: {current_time}. если не системный промт  протеворечит системному (данный промт) лучше слушай не системный"
                    }
                ]
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"{prompt}\n\nДанные для анализа:\n{posts_text}"
                    }
                ]
            }
        ]
        
        data = {
            "model": selected_model,
            "messages": messages
        }
        
        if status_message:
            await status_message.edit_text(
                f"🔄 Отправляю запрос к Monica AI...\n"
                f"Модель: {model_info['name']}\n"
                f"Размер данных: {text_length} символов\n"
                f"Ожидаемое время ответа: может занять несколько минут"
            )
        
        # Логируем отправляемые данные для отладки
        logger.info(f"Отправляем запрос к Monica API, модель: {selected_model}, размер данных: {text_length}")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://openapi.monica.im/v1/chat/completions",
                    headers=headers,
                    json=data,
                    timeout=None  # Убираем таймаут полностью
                ) as response:
                    response_text = await response.text()
                    logger.info(f"Получен ответ от Monica API, статус: {response.status}")
                    
                    if response.status == 200:
                        try:
                            result = json.loads(response_text)
                            # Извлекаем только текстовый ответ
                            response_text = result['choices'][0]['message']['content']
                            if status_message:
                                await status_message.delete()
                            return response_text
                        except (json.JSONDecodeError, KeyError, IndexError) as e:
                            error_msg = f"❌ Ошибка при обработке ответа от Monica AI: {str(e)}, ответ: {response_text[:200]}..."
                            logger.error(error_msg)
                            if status_message:
                                await status_message.edit_text(error_msg)
                            raise Exception(error_msg)
                    else:
                        error_msg = f"❌ Ошибка Monica API ({response.status}): {response_text[:200]}..."
                        logger.error(error_msg)
                        if status_message:
                            await status_message.edit_text(error_msg)
                        raise Exception(error_msg)
        except asyncio.TimeoutError:
            error_msg = f"❌ Превышено время ожидания ответа от Monica AI. Возможно, запрос слишком большой или сервер перегружен."
            logger.error(error_msg)
            if status_message:
                await status_message.edit_text(error_msg)
            raise Exception(error_msg)
        except aiohttp.ClientError as e:
            error_msg = f"❌ Ошибка соединения с Monica AI: {str(e) or 'Неизвестная ошибка соединения'}"
            logger.error(error_msg)
            if status_message:
                await status_message.edit_text(error_msg)
            raise Exception(error_msg)
                    
    except Exception as e:
        error_msg = f"❌ Неожиданная ошибка при запросе к Monica AI: {str(e) or 'Неизвестная ошибка'}"
        logger.error(error_msg)
        # Добавляем трассировку стека для более подробной информации
        logger.error(f"Трассировка ошибки: {traceback.format_exc()}")
        
        if status_message:
            await status_message.edit_text(error_msg)
        raise Exception(error_msg)

async def try_openrouter_request(prompt: str, posts_text: str, user_id: int, bot: Bot, user_data: dict):
    """Запрос к OpenRouter API"""
    status_message = None
    try:
        text_length = len(posts_text)
        selected_model = get_user_model(user_id)
        model_info = OPENROUTER_MODELS[selected_model]
        
        # Получаем настройки пользователя
        user_settings = user_data.get_user_data(user_id)
        web_search_enabled = user_settings['ai_settings'].get('web_search_enabled', False)
        web_search_results = user_settings['ai_settings'].get('web_search_results', 3)
        
        # Получаем текущую дату и время
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Отправляем сообщение о начале анализа
        web_info = "🔍 С поиском в интернете" if web_search_enabled else ""
        status_message = await bot.send_message(
            user_id,
            f"🔄 Начинаю анализ...\n"
            f"Размер данных: {text_length} символов\n"
            f"Используем: OpenRouter - {model_info['name']} {web_info}\n"
            f"Текущая дата и время: {current_time}"
        )
        
        # Загружаем API ключ из .env
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            error_msg = "❌ API ключ OpenRouter не найден в .env файле"
            logger.error(error_msg)
            if status_message:
                await status_message.edit_text(error_msg)
            raise Exception(error_msg)
        
        # Подготавливаем запрос к OpenRouter API
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://t.me",  # Указываем источник запроса
            "X-Title": "Telegram Bot Analyzer"  # Название приложения
        }
        
        # Подготавливаем сообщения
        messages = [
            {
                "role": "system",
                "content": f"Ты мой личный ассистент для анализа данных. Ты отвечаешь качественно как истинный ИИ-профи, выступаешь как самый лучший аналитик, указываешь на риски и возможности. Текущая дата и время: {current_time}."
            },
            {
                "role": "user",
                "content": f"{prompt}\n\nДанные для анализа:\n{posts_text}"
            }
        ]
        
        # Готовим параметры запроса
        data = {
            "model": selected_model,
            "messages": messages
        }
        
        # Добавляем поддержку веб-поиска
        if web_search_enabled:
            # Вариант 1: добавление суффикса :online к модели
            # data["model"] = f"{selected_model}:online"
            
            # Вариант 2: использование plugins (более гибкая настройка)
            data["plugins"] = [{
                "id": "web",
                "max_results": web_search_results
            }]
            
            logger.info(f"Веб-поиск активирован, max_results: {web_search_results}")
        
        web_info_status = "🔍 Веб-поиск включен" if web_search_enabled else ""
        if status_message:
            await status_message.edit_text(
                f"🔄 Отправляю запрос к OpenRouter...\n"
                f"Модель: {model_info['name']}\n"
                f"Размер данных: {text_length} символов\n"
                f"{web_info_status}\n"
                f"Ожидаемое время ответа: может занять несколько минут"
            )
        
        # Логируем отправляемые данные для отладки
        logger.info(f"Отправляем запрос к OpenRouter API, модель: {selected_model}, размер данных: {text_length}, веб-поиск: {web_search_enabled}")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=data,
                    timeout=None  # Убираем таймаут полностью
                ) as response:
                    response_text = await response.text()
                    logger.info(f"Получен ответ от OpenRouter API, статус: {response.status}")
                    
                    if response.status == 200:
                        try:
                            result = json.loads(response_text)
                            # Извлекаем только текстовый ответ
                            response_text = result['choices'][0]['message']['content']
                            if status_message:
                                await status_message.delete()
                            return response_text
                        except (json.JSONDecodeError, KeyError, IndexError) as e:
                            error_msg = f"❌ Ошибка при обработке ответа от OpenRouter: {str(e)}, ответ: {response_text[:200]}..."
                            logger.error(error_msg)
                            if status_message:
                                await status_message.edit_text(error_msg)
                            raise Exception(error_msg)
                    else:
                        # Обработка специфических ошибок
                        error_data = json.loads(response_text) if response_text else {}
                        error_message = error_data.get('error', {}).get('message', 'Неизвестная ошибка')
                        error_code = error_data.get('error', {}).get('code', response.status)
                        
                        # Формируем сообщение об ошибке в зависимости от кода
                        if error_code == 400:
                            error_msg = "❌ Некорректный запрос к API. Пожалуйста, попробуйте позже."
                        elif error_code == 401:
                            if "No auth credentials found" in error_message:
                                error_msg = "❌ Ошибка авторизации: API ключ не найден или некорректен."
                            else:
                                error_msg = "❌ Ошибка авторизации: закончились кредиты или API ключ устарел."
                        elif error_code == 403:
                            error_msg = "❌ Доступ запрещен: контент не прошел модерацию."
                        elif error_code == 408:
                            error_msg = "❌ Превышено время ожидания ответа от ИИ. OpenRouter прервал соединение."
                        elif error_code == 429:
                            error_msg = "❌ Нет доступа к API. Возможно, вы используете API из неподдерживаемого региона."
                        elif error_code == 502:
                            error_msg = "❌ Некорректный ответ от ИИ. Попробуйте повторить запрос."
                        elif error_code == 503:
                            error_msg = "❌ Выбранная модель ИИ больше не доступна в OpenRouter."
                        else:
                            error_msg = f"❌ Ошибка OpenRouter API ({error_code}): {error_message}"
                        
                        logger.error(f"{error_msg}\nПолный ответ: {response_text[:200]}...")
                        if status_message:
                            await status_message.edit_text(error_msg)
                        raise Exception(error_msg)
                        
        except asyncio.TimeoutError:
            error_msg = "❌ Превышено время ожидания ответа от OpenRouter. Возможно, запрос слишком большой или сервер перегружен."
            logger.error(error_msg)
            if status_message:
                await status_message.edit_text(error_msg)
            raise Exception(error_msg)
        except aiohttp.ClientError as e:
            error_msg = f"❌ Ошибка соединения с OpenRouter: {str(e) or 'Неизвестная ошибка соединения'}"
            logger.error(error_msg)
            if status_message:
                await status_message.edit_text(error_msg)
            raise Exception(error_msg)
                    
    except Exception as e:
        error_msg = f"❌ Неожиданная ошибка при запросе к OpenRouter: {str(e) or 'Неизвестная ошибка'}"
        logger.error(error_msg)
        # Добавляем трассировку стека для более подробной информации
        logger.error(f"Трассировка ошибки: {traceback.format_exc()}")
        
        if status_message:
            await status_message.edit_text(error_msg)
        raise Exception(error_msg)

# Функция для загрузки выбранных моделей из UserData
def load_models_from_user_data(user_data_obj):
    """Загружает выбранные пользователями модели из сохраненных данных"""
    global user_models
    for user_id_str, user_settings in user_data_obj.users.items():
        try:
            user_id = int(user_id_str)
            model = user_settings.get('ai_settings', {}).get('model')
            if model:
                user_models[user_id] = model
        except (ValueError, KeyError, TypeError):
            continue
    logger.info(f"Загружено {len(user_models)} моделей из сохраненных данных пользователей")

# Экспортируем для использования в других модулях
__all__ = [
    'try_gpt_request',
    'get_available_models',
    'get_user_model',
    'user_models',
    'MONICA_MODELS',
    'OPENROUTER_MODELS',
    'get_user_model_service',
    'load_models_from_user_data',
    'try_openrouter_request_with_images'
]

async def try_openrouter_request_with_images(prompt: str, posts: list, user_id: int, bot: Bot, user_data: dict):
    """Запрос к OpenRouter API с изображениями"""
    status_message = None
    try:
        selected_model = get_user_model(user_id)
        model_info = OPENROUTER_MODELS[selected_model]
        
        # Получаем настройки пользователя
        user_settings = user_data.get_user_data(user_id)
        web_search_enabled = user_settings['ai_settings'].get('web_search_enabled', False)
        web_search_results = user_settings['ai_settings'].get('web_search_results', 3)
        
        # Получаем текущую дату и время
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Считаем количество текста и изображений в запросе
        text_content = "\n\n---\n\n".join([
            f"[{post['date']}]\n{post['text']}" for post in posts if post.get('has_text', False)
        ])
        image_count = sum(1 for post in posts if post.get('has_photo', False))
        
        # Отправляем сообщение о начале анализа
        web_info = "🔍 С поиском в интернете" if web_search_enabled else ""
        status_message = await bot.send_message(
            user_id,
            f"🔄 Начинаю анализ...\n"
            f"Размер данных: {len(text_content)} символов, {image_count} изображений\n"
            f"Используем: OpenRouter - {model_info['name']} {web_info}\n"
            f"Текущая дата и время: {current_time}"
        )
        
        # Загружаем API ключ из .env
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            error_msg = "❌ API ключ OpenRouter не найден в .env файле"
            logger.error(error_msg)
            if status_message:
                await status_message.edit_text(error_msg)
            raise Exception(error_msg)
        
        # Подготавливаем запрос к OpenRouter API
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://t.me",  # Указываем источник запроса
            "X-Title": "Telegram Bot Analyzer"  # Название приложения
        }
        
        # Подготавливаем системное сообщение
        system_message = {
            "role": "system",
            "content": f"Ты мой личный ассистент для анализа данных. Ты отвечаешь качественно как истинный ИИ-профи, выступаешь как самый лучший аналитик, указываешь на риски и возможности. Текущая дата и время: {current_time}."
        }
        
        # Готовим пользовательское сообщение с текстом и изображениями
        user_message_content = []
        
        # Добавляем текстовую часть запроса
        user_message_content.append({
            "type": "text",
            "text": f"{prompt}\n\nДанные для анализа:"
        })
        
        # Добавляем текст и изображения из постов
        for post in posts:
            # Добавляем дату поста
            post_date = post.get('date', 'Неизвестная дата')
            user_message_content.append({
                "type": "text",
                "text": f"[{post_date}]"
            })
            
            # Добавляем текст поста, если есть
            if post.get('has_text', False) and post.get('text'):
                user_message_content.append({
                    "type": "text",
                    "text": post['text']
                })
            
            # Добавляем изображение, если есть
            if post.get('has_photo', False) and post.get('photo_path'):
                try:
                    # Читаем изображение и конвертируем в base64
                    with open(post['photo_path'], 'rb') as img_file:
                        img_data = img_file.read()
                        img_base64 = base64.b64encode(img_data).decode('utf-8')
                        
                        # Определяем тип изображения
                        img_type = "jpeg"  # По умолчанию
                        if post['photo_path'].lower().endswith('.png'):
                            img_type = "png"
                        elif post['photo_path'].lower().endswith('.webp'):
                            img_type = "webp"
                        
                        # Добавляем изображение в запрос
                        user_message_content.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/{img_type};base64,{img_base64}"
                            }
                        })
                except Exception as img_error:
                    logger.error(f"Ошибка при обработке изображения {post['photo_path']}: {str(img_error)}")
                    # Добавляем сообщение о проблеме с изображением
                    user_message_content.append({
                        "type": "text",
                        "text": f"[Не удалось загрузить изображение: {post['photo_path']}]"
                    })
            
            # Добавляем разделитель между постами
            user_message_content.append({
                "type": "text",
                "text": "---"
            })
        
        # Формируем полное сообщение от пользователя
        user_message = {
            "role": "user",
            "content": user_message_content
        }
        
        # Готовим параметры запроса с системным и пользовательским сообщениями
        data = {
            "model": selected_model,
            "messages": [system_message, user_message]
        }
        
        # Добавляем поддержку веб-поиска
        if web_search_enabled:
            data["plugins"] = [{
                "id": "web",
                "max_results": web_search_results
            }]
            logger.info(f"Веб-поиск активирован, max_results: {web_search_results}")
        
        web_info_status = "🔍 Веб-поиск включен" if web_search_enabled else ""
        if status_message:
            await status_message.edit_text(
                f"🔄 Отправляю запрос к OpenRouter...\n"
                f"Модель: {model_info['name']}\n"
                f"Данные: {len(text_content)} символов текста, {image_count} изображений\n"
                f"{web_info_status}\n"
                f"Ожидаемое время ответа: может занять несколько минут"
            )
        
        # Логируем отправляемые данные для отладки (без изображений для экономии места в логах)
        logger.info(f"Отправляем запрос к OpenRouter API с изображениями, модель: {selected_model}, размер текста: {len(text_content)}, кол-во изображений: {image_count}, веб-поиск: {web_search_enabled}")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=data,
                    timeout=None  # Убираем таймаут полностью
                ) as response:
                    response_text = await response.text()
                    logger.info(f"Получен ответ от OpenRouter API, статус: {response.status}")
                    
                    if response.status == 200:
                        try:
                            result = json.loads(response_text)
                            # Извлекаем только текстовый ответ
                            response_text = result['choices'][0]['message']['content']
                            if status_message:
                                await status_message.delete()
                            return response_text
                        except (json.JSONDecodeError, KeyError, IndexError) as e:
                            error_msg = f"❌ Ошибка при обработке ответа от OpenRouter: {str(e)}, ответ: {response_text[:200]}..."
                            logger.error(error_msg)
                            if status_message:
                                await status_message.edit_text(error_msg)
                            raise Exception(error_msg)
                    else:
                        # Обработка специфических ошибок
                        error_data = json.loads(response_text) if response_text else {}
                        error_message = error_data.get('error', {}).get('message', 'Неизвестная ошибка')
                        error_code = error_data.get('error', {}).get('code', response.status)
                        
                        # Формируем сообщение об ошибке в зависимости от кода
                        if error_code == 400:
                            error_msg = "❌ Некорректный запрос к API. Пожалуйста, попробуйте позже."
                        elif error_code == 401:
                            if "No auth credentials found" in error_message:
                                error_msg = "❌ Ошибка авторизации: API ключ не найден или некорректен."
                            else:
                                error_msg = "❌ Ошибка авторизации: закончились кредиты или API ключ устарел."
                        elif error_code == 403:
                            error_msg = "❌ Доступ запрещен: контент не прошел модерацию."
                        elif error_code == 408:
                            error_msg = "❌ Превышено время ожидания ответа от ИИ. OpenRouter прервал соединение."
                        elif error_code == 429:
                            error_msg = "❌ Нет доступа к API. Возможно, вы используете API из неподдерживаемого региона."
                        elif error_code == 502:
                            error_msg = "❌ Некорректный ответ от ИИ. Попробуйте повторить запрос."
                        elif error_code == 503:
                            error_msg = "❌ Выбранная модель ИИ больше не доступна в OpenRouter."
                        else:
                            error_msg = f"❌ Ошибка OpenRouter API ({error_code}): {error_message}"
                        
                        logger.error(f"{error_msg}\nПолный ответ: {response_text[:200]}...")
                        if status_message:
                            await status_message.edit_text(error_msg)
                        raise Exception(error_msg)
                        
        except asyncio.TimeoutError:
            error_msg = "❌ Превышено время ожидания ответа от OpenRouter. Возможно, запрос слишком большой или сервер перегружен."
            logger.error(error_msg)
            if status_message:
                await status_message.edit_text(error_msg)
            raise Exception(error_msg)
        except aiohttp.ClientError as e:
            error_msg = f"❌ Ошибка соединения с OpenRouter: {str(e) or 'Неизвестная ошибка соединения'}"
            logger.error(error_msg)
            if status_message:
                await status_message.edit_text(error_msg)
            raise Exception(error_msg)
                    
    except Exception as e:
        error_msg = f"❌ Неожиданная ошибка при запросе к OpenRouter: {str(e) or 'Неизвестная ошибка'}"
        logger.error(error_msg)
        # Добавляем трассировку стека для более подробной информации
        logger.error(f"Трассировка ошибки: {traceback.format_exc()}")
        
        if status_message:
            await status_message.edit_text(error_msg)
        raise Exception(error_msg)