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
logger = logging.getLogger(__name__)
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
OPENROUTER_MODELS = {
    "anthropic/claude-3-7-sonnet": {
        "name": "Claude 3.7 Sonnet",
        "description": "Мощная модель с модерацией контента и большим контекстом",
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
user_models: Dict[int, str] = {}
user_model_services: Dict[int, str] = {}
def get_available_models():
    all_models = {**MONICA_MODELS, **OPENROUTER_MODELS}
    return all_models
def get_user_model(user_id: int) -> str:
    selected_model = user_models.get(user_id, "gpt-4o")
    if selected_model in MONICA_MODELS:
        pass
    elif selected_model in OPENROUTER_MODELS:
        pass
    else:
        selected_model = "gpt-4o"
        user_models[user_id] = selected_model
    return selected_model
def get_user_model_service(user_id: int) -> str:
    model = get_user_model(user_id)
    service = ""
    if model in MONICA_MODELS:
        service = "monica"
        from main import user_data  
        if user_data:
            try:
                user_settings = user_data.get_user_data(user_id)
                if user_settings['ai_settings'].get('web_search_enabled', False):
                    user_settings['ai_settings']['web_search_enabled'] = False
                    user_data.save()
            except Exception as e:
                logger.error(f"Ошибка при отключении веб-поиска для Monica: {e}")
    elif model in OPENROUTER_MODELS:
        service = "openrouter"
    else:
        service = "monica"
    return service
async def try_gpt_request(prompt: str, posts_text: str, user_id: int, bot: Bot, user_data: dict):
    service = get_user_model_service(user_id)
    selected_model = get_user_model(user_id)
    if service == "monica" and selected_model not in MONICA_MODELS:
        user_models[user_id] = "gpt-4o"
        user_settings = user_data.get_user_data(user_id)
        user_settings['ai_settings']['model'] = "gpt-4o"
        user_data.save()
    elif service == "openrouter" and selected_model not in OPENROUTER_MODELS:
        user_models[user_id] = "anthropic/claude-3-7-sonnet"
        user_settings = user_data.get_user_data(user_id)
        user_settings['ai_settings']['model'] = "anthropic/claude-3-7-sonnet"
        user_data.save()
    service = get_user_model_service(user_id)
    selected_model = get_user_model(user_id)
    if service == "monica":
        return await try_monica_request(prompt, posts_text, user_id, bot, user_data)
    elif service == "openrouter":
        return await try_openrouter_request(prompt, posts_text, user_id, bot, user_data)
    else:
        error_msg = f"❌ Неизвестный сервис модели: {service}"
        logger.error(error_msg)
        raise Exception(error_msg)
async def try_monica_request(prompt: str, posts_text: str, user_id: int, bot: Bot, user_data: dict):
    status_message = None
    try:
        text_length = len(posts_text)
        selected_model = get_user_model(user_id)
        model_info = MONICA_MODELS[selected_model]
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status_message = await bot.send_message(
            user_id,
            f"🔄 Начинаю анализ...\n"
            f"Размер данных: {text_length} символов\n"
            f"Используем: Monica AI - {model_info['name']}\n"
            f"Текущая дата и время: {current_time}"
        )
        api_key = os.getenv("MONICA_API_KEY")
        if not api_key:
            error_msg = "❌ API ключ Monica не найден в .env файле"
            logger.error(error_msg)
            if status_message:
                await status_message.edit_text(error_msg)
            raise Exception(error_msg)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        messages = [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": f"""Ты профессиональный политический аналитик и советник по коммуникациям с глубоким пониманием российской политической системы и региональной специфики. Твои анализы отличаются высоким качеством, глубиной погружения в тему и политической проницательностью.
Особенности твоего стиля работы:
1. Ты умеешь выделять наиболее значимые новости по их реальному политическому и социальному весу
2. Твои комментарии всегда сбалансированы, взвешены, политически корректны, но при этом содержат оригинальную мысль
3. Ты избегаешь банальностей, штампов и очевидных заключений
4. Ты понимаешь конституционные полномочия сенатора РФ и формулируешь предложения строго в рамках этих полномочий
5. Ты отлично знаешь специфику работы Совета Федерации и взаимодействия федерального центра с регионами
6. Ты имеешь глубокие знания в вопросах ЖКХ, поддержки МСП и развития моногородов
7. Ты мастерски адаптируешь свой аналитический материал для использования в социальных сетях и мессенджерах
При анализе новостей для официальных лиц:
• Сохраняешь баланс между критикой и поддержкой государственной политики
• Предлагаешь конкретные, реализуемые инициативы в рамках полномочий
• Учитываешь региональную специфику (в данном случае - Республика Башкортостан)
• Демонстрируешь экспертное понимание обсуждаемых вопросов
• Предлагаешь различные варианты комментариев с разной стилистикой и глубиной
• Никогда не предлагаешь популистских или нереализуемых инициатив
Поддерживаемые форматы вывода:
1. TXT - простой текстовый формат с простым форматированием разделов и абзацев
2. Markdown - форматированный текст с полной поддержкой Markdown синтаксиса (заголовки, списки, ссылки, цитаты, выделение)
3. PDF - высококачественный документ с четкой структурой, где важнее содержание, чем сложное форматирование
Текущая дата и время: {current_time}. Если не системный промт противоречит системному (данный промт), лучше следуй системному промту."""
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
        logger.info(f"Отправляем запрос к Monica API, модель: {selected_model}, размер данных: {text_length}")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://openapi.monica.im/v1/chat/completions",
                    headers=headers,
                    json=data,
                    timeout=None
                ) as response:
                    response_text = await response.text()
                    logger.info(f"Получен ответ от Monica API, статус: {response.status}")
                    if response.status == 200:
                        try:
                            result = json.loads(response_text)
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
        logger.error(f"Трассировка ошибки: {traceback.format_exc()}")
        if status_message:
            await status_message.edit_text(error_msg)
        raise Exception(error_msg)
async def try_openrouter_request(prompt: str, posts_text: str, user_id: int, bot: Bot, user_data: dict):
    status_message = None
    try:
        text_length = len(posts_text)
        selected_model = get_user_model(user_id)
        if selected_model not in OPENROUTER_MODELS:
            selected_model = "anthropic/claude-3-7-sonnet"
            user_models[user_id] = selected_model
            user_settings = user_data.get_user_data(user_id)
            user_settings['ai_settings']['model'] = selected_model
            user_data.save()
        model_info = OPENROUTER_MODELS[selected_model]
        user_settings = user_data.get_user_data(user_id)
        web_search_enabled = user_settings['ai_settings'].get('web_search_enabled', False)
        web_search_results = user_settings['ai_settings'].get('web_search_results', 3)
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        web_info = "🔍 С поиском в интернете" if web_search_enabled else ""
        status_message = await bot.send_message(
            user_id,
            f"🔄 Начинаю анализ...\n"
            f"Размер данных: {text_length} символов\n"
            f"Используем: OpenRouter - {model_info['name']} {web_info}\n"
            f"Текущая дата и время: {current_time}"
        )
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            error_msg = "❌ API ключ OpenRouter не найден в .env файле"
            logger.error(error_msg)
            if status_message:
                await status_message.edit_text(error_msg)
            raise Exception(error_msg)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://t.me",
            "X-Title": "Telegram Bot Analyzer"
        }
        system_message = {
            "role": "system",
            "content": f"""Ты профессиональный политический аналитик и советник по коммуникациям с глубоким пониманием российской политической системы и региональной специфики. Твои анализы отличаются высоким качеством, глубиной погружения в тему и политической проницательностью.
Особенности твоего стиля работы:
1. Ты умеешь выделять наиболее значимые новости по их реальному политическому и социальному весу
2. Твои комментарии всегда сбалансированы, взвешены, политически корректны, но при этом содержат оригинальную мысль
3. Ты избегаешь банальностей, штампов и очевидных заключений
4. Ты понимаешь конституционные полномочия сенатора РФ и формулируешь предложения строго в рамках этих полномочий
5. Ты отлично знаешь специфику работы Совета Федерации и взаимодействия федерального центра с регионами
6. Ты имеешь глубокие знания в вопросах ЖКХ, поддержки МСП и развития моногородов
7. Ты мастерски адаптируешь свой аналитический материал для использования в социальных сетях и мессенджерах
При анализе новостей для официальных лиц:
• Сохраняешь баланс между критикой и поддержкой государственной политики
• Предлагаешь конкретные, реализуемые инициативы в рамках полномочий
• Учитываешь региональную специфику (в данном случае - Республика Башкортостан)
• Демонстрируешь экспертное понимание обсуждаемых вопросов
• Предлагаешь различные варианты комментариев с разной стилистикой и глубиной
• Никогда не предлагаешь популистских или нереализуемых инициатив
Поддерживаемые форматы вывода:
1. TXT - простой текстовый формат с простым форматированием разделов и абзацев
2. Markdown - форматированный текст с полной поддержкой Markdown синтаксиса (заголовки, списки, ссылки, цитаты, выделение)
3. PDF - высококачественный документ с четкой структурой, где важнее содержание, чем сложное форматирование
Текущая дата и время: {current_time}."""
        }
        messages = [
            system_message,
            {
                "role": "user",
                "content": f"{prompt}\n\nДанные для анализа:\n{posts_text}"
            }
        ]
        data = {
            "model": selected_model,
            "messages": messages
        }
        data["models"] = [selected_model]
        logger.info(f"Используем только основную модель без резервных: {selected_model}")
        if web_search_enabled:
            data["plugins"] = [{
                "id": "web",
                "max_results": web_search_results,
                "search_prompt": f"Поиск в интернете был проведен {current_time}. Используй следующие результаты поиска для обоснования своего ответа. ВАЖНО: Цитируй источники, используя формат markdown [домен.com](ссылка)."
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
        logger.info(f"Отправляем запрос к OpenRouter API, модель: {selected_model}, размер данных: {text_length}, веб-поиск: {web_search_enabled}")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=data,
                    timeout=None
                ) as response:
                    response_text = await response.text()
                    logger.info(f"Получен ответ от OpenRouter API, статус: {response.status}")
                    if response.status == 200:
                        try:
                            result = json.loads(response_text)
                            response_text = result['choices'][0]['message']['content']
                            used_model = result.get('model', selected_model)
                            if used_model != selected_model:
                                logger.info(f"Запрос был обработан резервной моделью: {used_model}")
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
                        error_data = json.loads(response_text) if response_text else {}
                        error_message = error_data.get('error', {}).get('message', 'Неизвестная ошибка')
                        error_code = error_data.get('error', {}).get('code', response.status)
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
        logger.error(f"Трассировка ошибки: {traceback.format_exc()}")
        if status_message:
            await status_message.edit_text(error_msg)
        raise Exception(error_msg)
def load_models_from_user_data(user_data_obj):
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
__all__ = [
    'try_gpt_request',
    'get_available_models',
    'get_user_model',
    'user_models',
    'MONICA_MODELS',
    'OPENROUTER_MODELS',
    'get_user_model_service',
    'load_models_from_user_data',
    'try_openrouter_request_with_images',
    'check_monica_credits',
    'check_openrouter_credits'
]
async def try_openrouter_request_with_images(prompt: str, posts: list, user_id: int, bot: Bot, user_data: dict):
    status_message = None
    try:
        selected_model = get_user_model(user_id)
        if selected_model not in OPENROUTER_MODELS:
            selected_model = "anthropic/claude-3-7-sonnet"
            user_models[user_id] = selected_model
            user_settings = user_data.get_user_data(user_id)
            user_settings['ai_settings']['model'] = selected_model
            user_data.save()
        if selected_model in MONICA_MODELS:
            user_settings = user_data.get_user_data(user_id)
            if user_settings['ai_settings'].get('web_search_enabled', False):
                user_settings['ai_settings']['web_search_enabled'] = False
                user_data.save()
        model_info = OPENROUTER_MODELS[selected_model]
        user_settings = user_data.get_user_data(user_id)
        web_search_enabled = user_settings['ai_settings'].get('web_search_enabled', False)
        web_search_results = user_settings['ai_settings'].get('web_search_results', 3)
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text_content = "\n\n---\n\n".join([
            f"[{post['date']}]\n{post['text']}" for post in posts if post.get('has_text', False)
        ])
        image_count = sum(1 for post in posts if post.get('has_photo', False))
        web_info = "🔍 С поиском в интернете" if web_search_enabled else ""
        status_message = await bot.send_message(
            user_id,
            f"🔄 Начинаю анализ...\n"
            f"Размер данных: {len(text_content)} символов, {image_count} изображений\n"
            f"Используем: OpenRouter - {model_info['name']} {web_info}\n"
            f"Текущая дата и время: {current_time}"
        )
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            error_msg = "❌ API ключ OpenRouter не найден в .env файле"
            logger.error(error_msg)
            if status_message:
                await status_message.edit_text(error_msg)
            raise Exception(error_msg)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://t.me",
            "X-Title": "Telegram Bot Analyzer"
        }
        system_message = {
            "role": "system",
            "content": f"""Ты профессиональный политический аналитик и советник по коммуникациям с глубоким пониманием российской политической системы и региональной специфики. Твои анализы отличаются высоким качеством, глубиной погружения в тему и политической проницательностью.
Особенности твоего стиля работы:
1. Ты умеешь выделять наиболее значимые новости по их реальному политическому и социальному весу
2. Твои комментарии всегда сбалансированы, взвешены, политически корректны, но при этом содержат оригинальную мысль
3. Ты избегаешь банальностей, штампов и очевидных заключений
4. Ты понимаешь конституционные полномочия сенатора РФ и формулируешь предложения строго в рамках этих полномочий
5. Ты отлично знаешь специфику работы Совета Федерации и взаимодействия федерального центра с регионами
6. Ты имеешь глубокие знания в вопросах ЖКХ, поддержки МСП и развития моногородов
7. Ты мастерски адаптируешь свой аналитический материал для использования в социальных сетях и мессенджерах
8. Ты умеешь анализировать не только текст, но и визуальный контент, делая выводы на основе фотографий, инфографики и изображений
При анализе новостей для официальных лиц:
• Сохраняешь баланс между критикой и поддержкой государственной политики
• Предлагаешь конкретные, реализуемые инициативы в рамках полномочий
• Учитываешь региональную специфику (в данном случае - Республика Башкортостан)
• Демонстрируешь экспертное понимание обсуждаемых вопросов
• Предлагаешь различные варианты комментариев с разной стилистикой и глубиной
• Никогда не предлагаешь популистских или нереализуемых инициатив
Поддерживаемые форматы вывода:
1. TXT - простой текстовый формат с простым форматированием разделов и абзацев
2. Markdown - форматированный текст с полной поддержкой Markdown синтаксиса (заголовки, списки, ссылки, цитаты, выделение)
3. PDF - высококачественный документ с четкой структурой, где важнее содержание, чем сложное форматирование
При работе с изображениями:
• Описывай ключевое содержание изображений, если это важно для аналитики
• Соотноси текстовую информацию с визуальными материалами
• При необходимости ссылайся на визуальный контент в своих аналитических выводах
Текущая дата и время: {current_time}."""
        }
        user_message_content = []
        user_message_content.append({
            "type": "text",
            "text": f"{prompt}\n\nДанные для анализа:"
        })
        for post in posts:
            post_date = post.get('date', 'Неизвестная дата')
            user_message_content.append({
                "type": "text",
                "text": f"[{post_date}]"
            })
            if post.get('has_text', False) and post.get('text'):
                user_message_content.append({
                    "type": "text",
                    "text": post['text']
                })
            if post.get('has_photo', False) and post.get('photo_path'):
                try:
                    with open(post['photo_path'], 'rb') as img_file:
                        img_data = img_file.read()
                        img_base64 = base64.b64encode(img_data).decode('utf-8')
                        img_type = "jpeg"
                        if post['photo_path'].lower().endswith('.png'):
                            img_type = "png"
                        elif post['photo_path'].lower().endswith('.webp'):
                            img_type = "webp"
                        user_message_content.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/{img_type};base64,{img_base64}"
                            }
                        })
                except Exception as img_error:
                    logger.error(f"Ошибка при обработке изображения {post['photo_path']}: {str(img_error)}")
                    user_message_content.append({
                        "type": "text",
                        "text": f"[Не удалось загрузить изображение: {post['photo_path']}]"
                    })
            user_message_content.append({
                "type": "text",
                "text": "---"
            })
        user_message = {
            "role": "user",
            "content": user_message_content
        }
        data = {
            "model": selected_model,
            "messages": [system_message, user_message]
        }
        data["models"] = [selected_model]
        logger.info(f"Используем только основную модель без резервных: {selected_model}")
        if web_search_enabled:
            data["plugins"] = [{
                "id": "web",
                "max_results": web_search_results,
                "search_prompt": f"Поиск в интернете был проведен {current_time}. Используй следующие результаты поиска для обоснования своего ответа. ВАЖНО: Цитируй источники, используя формат markdown [домен.com](ссылка)."
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
        logger.info(f"Отправляем запрос к OpenRouter API с изображениями, модель: {selected_model}, размер текста: {len(text_content)}, кол-во изображений: {image_count}, веб-поиск: {web_search_enabled}")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=data,
                    timeout=None
                ) as response:
                    response_text = await response.text()
                    logger.info(f"Получен ответ от OpenRouter API, статус: {response.status}")
                    if response.status == 200:
                        try:
                            result = json.loads(response_text)
                            response_text = result['choices'][0]['message']['content']
                            used_model = result.get('model', selected_model)
                            if used_model != selected_model:
                                logger.info(f"Запрос был обработан резервной моделью: {used_model}")
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
                        error_data = json.loads(response_text) if response_text else {}
                        error_message = error_data.get('error', {}).get('message', 'Неизвестная ошибка')
                        error_code = error_data.get('error', {}).get('code', response.status)
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
        logger.error(f"Трассировка ошибки: {traceback.format_exc()}")
        if status_message:
            await status_message.edit_text(error_msg)
        raise Exception(error_msg)

async def check_monica_credits() -> dict:
    return {
        "success": True,
        "total": "Неограничено",
        "used": "—",
        "remaining": "—",
        "info": "Monica API не предоставляет информацию о кредитах"
    }

async def check_openrouter_credits() -> dict:
    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            return {"success": False, "error": "API ключ OpenRouter не найден"}
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://t.me",
            "X-Title": "Telegram Bot Analyzer"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://openrouter.ai/api/v1/credits",
                headers=headers,
                timeout=10
            ) as response:
                response_text = await response.text()
                
                if response.status == 200:
                    try:
                        result = json.loads(response_text)
                        data = result.get("data", {})
                        
                        total_credits = data.get("total_credits", 0)
                        total_usage = data.get("total_usage", 0)
                        
                        # Округляем до двух знаков после запятой
                        if isinstance(total_credits, (int, float)):
                            total_credits = round(total_credits, 2)
                        if isinstance(total_usage, (int, float)):
                            total_usage = round(total_usage, 2)
                        
                        remaining = total_credits - total_usage if isinstance(total_credits, (int, float)) and isinstance(total_usage, (int, float)) else "Неизвестно"
                        if isinstance(remaining, (int, float)):
                            remaining = round(remaining, 2)
                        
                        return {
                            "success": True,
                            "total": total_credits,
                            "used": total_usage,
                            "remaining": remaining
                        }
                    except (json.JSONDecodeError, KeyError) as e:
                        return {"success": False, "error": f"Ошибка обработки ответа: {str(e)}"}
                else:
                    return {"success": False, "error": f"Ошибка API ({response.status}): {response_text[:200]}"}
    except Exception as e:
        logger.error(f"Ошибка при проверке кредитов OpenRouter: {e}")
        return {"success": False, "error": str(e)}
