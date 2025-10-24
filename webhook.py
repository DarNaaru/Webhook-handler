from typing import Dict, Deque, List, Any
from aiohttp import ClientResponseError, ClientError
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from cachetools import TTLCache
from dotenv import load_dotenv
import aiohttp
import asyncio
import yaml
import re
import os
import uvicorn
from collections import deque
from dateutil import parser as dtparser
import secrets
import httpx
import psutil
import time
import shutil
import fastapi.responses

from log_config import logger  # Импорт модуля из текущего проекта

# Загрузка переменных из .env
load_dotenv()

# Проверка на None значений переменных
telegram_token = os.getenv('TELEGRAM_TOKEN')
chat_id = os.getenv('CHAT_ID')
access_token = os.getenv('ACCESS_TOKEN')

if not telegram_token or not chat_id or not access_token:
    raise ValueError("Необходимо определить переменные окружения: TELEGRAM_TOKEN, CHAT_ID, ACCESS_TOKEN")

# Загрузка настроек из config.yaml
with open("config.yaml", 'r', encoding='utf-8') as config_file:
    config = yaml.safe_load(config_file)

# Присвоение настроек из config.yaml
TASK_TITLES = config['task_titles']
USER_NAMES = config['user_names']
IMPORTANCE_TRANSLATION = config['importance_translation']

# Загрузка синонимов из config.yaml
INCLUDE_SYNONYMS = config['synonyms']['include']
EXCLUDE_SYNONYMS = config['synonyms']['exclude']

# Создание регулярных выражений на основе синонимов
INCLUDE_PATTERN = r'\b(' + '|'.join(INCLUDE_SYNONYMS) + r')\b'
EXCLUDE_PATTERN = r'\b(' + '|'.join(EXCLUDE_SYNONYMS) + r')\b'

# --- Настройки админ-панели ---
ADMIN_USERNAME = config.get('admin_panel', {}).get('username', 'admin')
ADMIN_PASSWORD = config.get('admin_panel', {}).get('password', 'admin123')
security = HTTPBasic()

def admin_auth(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (correct_username and correct_password):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return credentials.username

app = FastAPI(
    title="Wrike Webhook Handler",
    description="API для обработки вебхуков от Wrike и отправки уведомлений в Telegram.",
    version="1.1.0"
)

# Хранилище для последних ошибок
recent_errors: Deque[str] = deque(maxlen=20)

# --- Мониторинг и статистика ---
service_start_time = time.time()
event_counter = 0
message_counter = 0
error_counter = 0
last_webhooks = deque(maxlen=20)

class WrikeApiError(Exception):
    """Базовое исключение для ошибок API Wrike."""
    pass

class WrikeConnectionError(WrikeApiError):
    """Исключение для ошибок соединения с API Wrike."""
    pass

class WrikeAuthenticationError(WrikeApiError):
    """Исключение для ошибок аутентификации."""
    pass

class WrikeWebhookHandler:
    """
    Обработчик вебхуков от Wrike API.

    Класс предоставляет функциональность для обработки входящих вебхуков от Wrike,
    отправки уведомлений в Telegram и взаимодействия с API Wrike.

    Attributes:
        _wrike_access_token (str): Токен доступа к API Wrike
        _telegram_token (str): Токен бота Telegram
        _chat_id (str): ID чата Telegram для отправки сообщений
        wrike_cache (dict): Кэш для хранения данных от API Wrike
        _error_messages (dict): Словарь с пользовательскими сообщениями об ошибках
        last_errors: Deque[Dict[str, Any]]: Хранилище для последних ошибок

    Example:
        handler = WrikeWebhookHandler(
            wrike_access_token='your-wrike-token',
            tg_token='your-telegram-token',
            tg_chat_id='your-chat-id'
        )
    """

    def __init__(self, wrike_access_token, tg_token, tg_chat_id, config):
        """
        Инициализирует обработчик вебхуков.

        Args:
            wrike_access_token (str): Токен доступа к API Wrike
            tg_token (str): Токен бота Telegram
            tg_chat_id (str): ID чата Telegram для отправки сообщений
        """
        self._wrike_access_token = wrike_access_token
        self._telegram_token = tg_token
        self._chat_id = tg_chat_id
        self.last_errors: Deque[Dict[str, Any]] = deque(maxlen=20)
        self.config = config
        
        # Загружаем настройки кэша из конфига
        cache_settings = config.get('cache_settings', {})
        
        # Инициализация кэша с настройками из конфига
        self.wrike_cache = {
            'projects': TTLCache(
                maxsize=cache_settings.get('max_size', 100),
                ttl=cache_settings.get('projects_ttl', 3600)
            ),
            'users': TTLCache(
                maxsize=cache_settings.get('max_size', 100),
                ttl=cache_settings.get('users_ttl', 3600)
            )
        }
        
        self._error_messages = {
            'Connection closed': 'Потеря соединения с API Wrike',
            'Cannot connect to host': 'Не удается подключиться к серверу Wrike',
            'Connection timeout': 'Превышено время ожидания ответа от Wrike',
            'Client session closed': 'Сессия была закрыта',
            'Invalid status code': 'Некорректный ответ от сервера Wrike',
            'Unauthorized': 'Ошибка авторизации в API Wrike',
            'Rate limit exceeded': 'Превышен лимит запросов к API Wrike'
        }

    def get_user_friendly_error(self, error_message: str) -> str:
        """
        Преобразует технические сообщения об ошибках в понятные пользователю.

        Args:
            error_message (str): Техническое сообщение об ошибке

        Returns:
            str: Пользовательское сообщение об ошибке

        Example:
            >>> handler.get_user_friendly_error("Connection closed")
            "Потеря соединения с API Wrike"
        """
        for tech_msg, user_msg in self._error_messages.items():
            if tech_msg.lower() in error_message.lower():
                return user_msg
        return f"Ошибка при работе с API Wrike: {error_message}"

    def _add_error_log(self, error: Exception, context: str = "General"):
        """Добавляет ошибку в лог и в список последних ошибок."""
        error_time = datetime.now().isoformat()
        error_info = {
            "timestamp": error_time,
            "error": str(error),
            "type": type(error).__name__,
            "context": context
        }
        self.last_errors.append(error_info)
        logger.error("Error occurred: %s", error_info)

    @staticmethod
    def escape_markdown(text):
        """Экранирует специальные символы Markdown."""
        escape_chars = r'\*[]~#+=|{}'
        return ''.join(['\\' + char if char in escape_chars else char for char in text])

    async def send_telegram_message(self, message):
        """
        Асинхронно отправляет сообщение в Telegram.

        Args:
            message (str): Текст сообщения для отправки (поддерживает Markdown)

        Raises:
            aiohttp.ClientResponseError: При ошибке HTTP-запроса
            aiohttp.ClientError: При ошибке соединения с Telegram API

        Note:
            Метод автоматически форматирует сообщение для Markdown и
            обрабатывает ошибки отправки с записью в лог.
        """
        url = f'https://api.telegram.org/bot{self._telegram_token}/sendMessage'
        payload = {
            'chat_id': self._chat_id,
            'text': message,
            'parse_mode': 'Markdown'
        }
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload) as response:
                    response.raise_for_status()  # Проверка на ошибки HTTP
                    response_text = await response.text()
                    logger.info('Message sent to Telegram successfully.')
            except aiohttp.ClientResponseError as e:
                logger.error('HTTP error occurred while sending message to Telegram: %s', e)
                logger.error('Status code: %s, Message: %s', getattr(e, 'status', 'N/A'), str(e))
            except aiohttp.ClientError as e:
                logger.error('Failed to send message to Telegram: %s', e)

    async def send_error_notification(self, error_message):
        """
        Отправляет уведомление об ошибке в Telegram.

        Args:
            error_message (str): Сообщение об ошибке

        Note:
            Добавляет к сообщению специальные маркеры и форматирование
            для лучшей видимости в Telegram.
        """
        message = (
            "❌ *Ошибка при обработке события*\n"
            f"_{error_message}_"
        )
        await self.send_telegram_message(message)

    async def get_wrike_data(self, url, params=None, retries=3, retry_delay=1):
        """
        Асинхронно получает данные из Wrike API с улучшенной обработкой ошибок и повторными попытками.

        Args:
            url (str): URL эндпоинта API Wrike
            params (dict, optional): Параметры запроса. По умолчанию None
            retries (int, optional): Количество попыток повторного запроса. По умолчанию 3
            retry_delay (int, optional): Задержка между попытками в секундах. По умолчанию 1

        Returns:
            list: Список данных из ответа API

        Raises:
            WrikeAuthenticationError: При ошибке авторизации (401)
            WrikeApiError: При ошибках HTTP-запроса
            WrikeConnectionError: При ошибках соединения

        Note:
            При получении кода 429 (превышен лимит запросов) метод автоматически
            делает паузу согласно заголовку Retry-After.
        """
        headers = {
            'Authorization': f'bearer {self._wrike_access_token}',
            'Content-Type': 'application/json'
        }
        
        for attempt in range(retries):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers, params=params) as response:
                        if response.status == 401:
                            raise WrikeAuthenticationError("Unauthorized access to Wrike API")
                        elif response.status == 429:
                            retry_after = int(response.headers.get('Retry-After', retry_delay))
                            await asyncio.sleep(retry_after)
                            continue
                        
                        response.raise_for_status()
                        data = await response.json()
                        return data.get('data', [])
                        
            except aiohttp.ClientResponseError as e:
                if attempt == retries - 1:
                    raise WrikeApiError(f"HTTP error: {e.status} - {e.message}")
                await asyncio.sleep(retry_delay * (attempt + 1))
                
            except aiohttp.ClientError as e:
                if attempt == retries - 1:
                    raise WrikeConnectionError(str(e))
                await asyncio.sleep(retry_delay * (attempt + 1))
                
        return []

    async def get_task_details(self, task_id):
        """
        Получает детальную информацию о задаче из Wrike.

        Args:
            task_id (str): Идентификатор задачи в Wrike

        Returns:
            dict: Детальная информация о задаче или None при ошибке

        Note:
            Результат включает title, authorIds, parentIds и другие поля задачи.
        """
        data = await self.get_wrike_data(f'https://www.wrike.com/api/v4/tasks/{task_id}')
        return data[0] if data else {}

    async def get_task_comments(self, task_id):
        """
        Получает комментарии к задаче из Wrike.

        Args:
            task_id (str): Идентификатор задачи в Wrike

        Returns:
            list: Список комментариев к задаче
        """
        return await self.get_wrike_data(f'https://www.wrike.com/api/v4/tasks/{task_id}/comments')

    async def get_user_name(self, user_id):
        """
        Получает имя пользователя по ID из кэша или API.

        Args:
            user_id (str): Идентификатор пользователя в Wrike

        Returns:
            str: Полное имя пользователя или "Неизвестный пользователь" при ошибке

        Note:
            Результат кэшируется для оптимизации последующих запросов.
        """
        if user_id in self.wrike_cache['users']:
            logger.info('User %s found in cache.', user_id)
            return self.wrike_cache['users'][user_id]
        logger.info('User %s not found in cache, fetching from API.', user_id)
        user_data = await self.get_wrike_data(f'https://www.wrike.com/api/v4/users/{user_id}')
        user_name = f"{user_data[0]['firstName']} {user_data[0]['lastName']}" if user_data else 'Неизвестный пользователь'
        self.wrike_cache['users'][user_id] = user_name
        return user_name

    @staticmethod
    def extract_project_id(title):
        """
        Извлекает ID проекта из его названия.
        """
        match = re.search(r'(идд?\s*\d+)', title, re.IGNORECASE)
        if match:
            project_id = match.group(1).replace(" ", "")
            return f"#{project_id}"
        return "ID проекта не найден"

    async def get_all_projects(self):
        """
        Получает список всех проектов из кэша или API.

        Returns:
            dict: Словарь проектов в формате {id: название}

        Note:
            Результаты кэшируются для оптимизации последующих запросов.
            При отсутствии данных в кэше делается запрос к API.
        """
        if not self.wrike_cache['projects']:
            projects = await self.get_wrike_data('https://www.wrike.com/api/v4/folders')
            if projects:
                self.wrike_cache['projects'].update({project['id']: self.extract_project_id(project['title']) for project in projects})
        return self.wrike_cache['projects']

    async def format_task_message(self, task_details, project_name, comment_text=None):
#        logger.info(f"DEBUG: project_name in format_task_message: '{project_name}'")
        author_id = task_details.get('authorIds', [''])[0]
        responsible_ids = task_details.get('responsibleIds', [])

        # Сбор всех уникальных user_id
        user_ids = set([author_id] + responsible_ids)

        # Получение всех имен пользователей
        user_names = {}
        for user_id in user_ids:
            if user_id not in user_names:
                user_names[user_id] = await self.get_user_name(user_id)

        # Формирование строки с именами
        author_name = user_names.get(author_id, 'Неизвестный пользователь')
        responsible_names = ', '.join([user_names.get(rid, 'Неизвестный пользователь') for rid in responsible_ids])

        description = task_details.get('description', 'Описание отсутствует').replace('<br />', '\n').strip()

        created_date_utc = None
        try:
            created_date_utc = dtparser.parse(task_details.get('createdDate', datetime.utcnow().isoformat()))
        except Exception:
            created_date_utc = datetime.utcnow()
        created_date_utc_plus_5 = created_date_utc + timedelta(hours=5)
        created_date = created_date_utc_plus_5.strftime('%Y-%m-%d %H:%M:%S')

        importance = IMPORTANCE_TRANSLATION.get(task_details.get('importance', 'Важность не указана'),'Важность не указана')

        # Определение эмоджи на основе комментария
        project_emoji = ""
        if comment_text:
            # Ищем и сохраняем все эмоджи в порядке их появления в тексте
            emojis = []

            # Найдем все вхождения для отключения (🔥) и включения (✅) в том порядке, как они встречаются в тексте
            for match in re.finditer(rf'{EXCLUDE_PATTERN}|{INCLUDE_PATTERN}', comment_text, re.IGNORECASE):
                if re.match(EXCLUDE_PATTERN, match.group(), re.IGNORECASE):
                    emojis.append("🔥")
                elif re.match(INCLUDE_PATTERN, match.group(), re.IGNORECASE):
                    emojis.append("✅")

            project_emoji = " ".join(emojis)

        extracted_project_id = self.extract_project_id(project_name)
#        logger.info(f"DEBUG: extracted_project_id: '{extracted_project_id}' from project_name: '{project_name}'")
        project_id = f" ***{extracted_project_id}*** {project_emoji}"  # Добавляем эмоджи к ИД проекта

        message = (
            f"Проект: {project_id}\n"
            f"```\nЗаголовок: {task_details.get('title', 'Нет названия')}\n```"
            f"Статус: {task_details.get('status', 'Статус не указан')}\n"
            f"Важность: {importance}\n"
            f"Автор: {author_name}\n"
            f"Исполнитель(и): {responsible_names or 'Нет исполнителей'}\n"
            f"Дата создания: {created_date}\n"
            f"Описание: {description or 'Нет описания'}\n"
            f"Ссылка на задачу: {task_details.get('permalink', 'Ссылка отсутствует')}\n"
        )
        return message

    async def handle_task_event(self, task_details, project_name):
        """
        Обрабатывает события изменения задачи.

        Args:
            task_details (dict): Детали задачи из Wrike
            project_name (str): Название проекта

        Note:
            Обрабатывает изменения статуса, важности и ответственных.
            Отправляет уведомление в Telegram при необходимости.
        """
        message = await self.format_task_message(task_details, project_name)
        await self.send_telegram_message(message)

    async def handle_comment_added(self, event, task_details, project_name):
        """
        Обрабатывает события добавления комментария.

        Args:
            event (dict): Данные события от вебхука
            task_details (dict): Детали задачи из Wrike
            project_name (str): Название проекта

        Note:
            Форматирует и отправляет уведомление о новом комментарии в Telegram.
        """
        comment_text = event['comment'].get('text', 'Нет комментариев')

        # Заменяем с добавлением эмоджи на основе синонимов
        comment_text = re.sub(EXCLUDE_PATTERN, '🔥 \\1', comment_text, flags=re.IGNORECASE)
        comment_text = re.sub(INCLUDE_PATTERN, '✅ \\1', comment_text, flags=re.IGNORECASE)

        message = await self.format_task_message(task_details, project_name, comment_text)

        comment_date_utc = None
        try:
            comment_date_utc = dtparser.parse(event.get('lastUpdatedDate', datetime.utcnow().isoformat()))
        except Exception:
            comment_date_utc = datetime.utcnow()
        comment_date_utc_plus_5 = comment_date_utc + timedelta(hours=5)
        comment_date = comment_date_utc_plus_5.strftime('%Y-%m-%d %H:%M:%S')

        for wrike_user, telegram_user in USER_NAMES.items():
            comment_text = comment_text.replace(f"@{wrike_user}", f" {telegram_user}")

        comment_text = self.escape_markdown(comment_text)

        comment_author_id = event.get('eventAuthorId', '')
        comment_author_name = await self.get_user_name(comment_author_id)

        message += (
            "\nДобавлен комментарий:\n"
            f"Автор: {comment_author_name}\n"
            f"Дата: {comment_date}\n"
            f"***Текст: {comment_text}\n***"
        )
        await self.send_telegram_message(message)

    async def get_project_name_by_id(self, project_id):
        # Если проект уже есть в кэше — вернуть его
        if project_id in self.wrike_cache['projects']:
            logger.info(f"PROJECT CACHE HIT: {project_id}")
            return self.wrike_cache['projects'][project_id]
        # Иначе — запросить только этот проект из Wrike
        logger.info(f"PROJECT API REQUEST: {project_id}")
        data = await self.get_wrike_data(f'https://www.wrike.com/api/v4/folders/{project_id}')
        if data:
            project_title = data[0].get('title', '')
            extracted_id = self.extract_project_id(project_title)
            self.wrike_cache['projects'][project_id] = extracted_id
            return extracted_id
        return "ID проекта не найден"

    async def process_webhook_event(self, event):
        try:
            task_id = event.get('taskId')
            if not task_id:
                raise ValueError("Task ID is missing in the event data.")

            task_details = await self.get_task_details(task_id)
            if not task_details:
                raise ValueError(f"Failed to get task details for task ID: {task_id}")

            title = task_details.get('title')
            if not title:
                raise ValueError(f"Task title is missing for task ID: {task_id}")

            author_ids = task_details.get('authorIds')
            if not author_ids or not author_ids[0]:
                raise ValueError(f"Author ID is missing for task ID: {task_id}")

            created_date = task_details.get('createdDate')
            if not created_date:
                raise ValueError(f"Created date is missing for task ID: {task_id}")

            # Проверки успешны, выполняется обработка события
            task_title = title.lower()
            if any(task_title == t.lower() for t in TASK_TITLES):
                parent_ids = task_details.get('parentIds', [''])
#                logger.info(f"DEBUG: task_details parentIds: {parent_ids}")
                project_id = parent_ids[0]
                project_name = await self.get_project_name_by_id(project_id)

                if event['eventType'] in ('TaskStatusChanged', 'TaskImportanceChanged', 'TaskResponsiblesAdded', 'TaskResponsiblesRemoved'):
                    await self.handle_task_event(task_details, project_name)
                elif event['eventType'] == 'CommentAdded' and 'text' in event['comment']:
                    await self.handle_comment_added(event, task_details, project_name)

        except (ValueError, KeyError) as e:
            self._add_error_log(e, context="Webhook Event Processing")
            await self.send_error_notification(str(e))
        except (WrikeApiError, ClientResponseError, ClientError) as e:
            friendly_error = self.get_user_friendly_error(str(e))
            self._add_error_log(e, context="API Interaction")
            await self.send_error_notification(friendly_error)
        except Exception as e:
            friendly_error = self.get_user_friendly_error(str(e))
            self._add_error_log(e, context="Unexpected Error")
            await self.send_error_notification(friendly_error)

# Создание экземпляра обработчика вебхуков
webhook_handler = WrikeWebhookHandler(access_token, telegram_token, chat_id, config)

# --- Admin Panel Endpoints ---

@app.get("/admin/cache", tags=["Admin Panel"])
async def get_cache_status(username: str = Depends(admin_auth)):
    """Возвращает текущее состояние кэша Wrike."""
    return {
        "projects_cache": {
            "count": len(webhook_handler.wrike_cache['projects']),
            "ttl": webhook_handler.wrike_cache['projects'].ttl,
            "maxsize": webhook_handler.wrike_cache['projects'].maxsize,
            "items": dict(webhook_handler.wrike_cache['projects'].items()),
        },
        "users_cache": {
            "count": len(webhook_handler.wrike_cache['users']),
            "ttl": webhook_handler.wrike_cache['users'].ttl,
            "maxsize": webhook_handler.wrike_cache['users'].maxsize,
            "items": dict(webhook_handler.wrike_cache['users'].items()),
        }
    }

@app.get("/admin/errors", tags=["Admin Panel"])
async def get_recent_errors(username: str = Depends(admin_auth)) -> List[Dict[str, Any]]:
    """Возвращает список последних 20 ошибок, произошедших в обработчике."""
    return list(webhook_handler.last_errors)

@app.get("/admin/logs", tags=["Admin Panel"])
async def get_logs(lines: int = 50, username: str = Depends(admin_auth)):
    """
    Возвращает последние N строк из лог-файла.
    По умолчанию возвращает 50 строк.
    """
    try:
        log_filename = config.get('logging_settings', {}).get('handlers', {}).get('file', {}).get('filename', 'webhook.log')
        with open(log_filename, "r", encoding="utf-8") as f:
            log_lines = deque(f, maxlen=lines)
        return {"logs": list(log_lines)}
    except FileNotFoundError:
        return {"error": "Log file not found."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/clear_cache", tags=["Admin Panel"])
async def clear_cache(username: str = Depends(admin_auth)):
    """Очищает кэш проектов и пользователей."""
    projects_cleared = len(webhook_handler.wrike_cache['projects'])
    users_cleared = len(webhook_handler.wrike_cache['users'])
    webhook_handler.wrike_cache['projects'].clear()
    webhook_handler.wrike_cache['users'].clear()
    return {
        "status": "ok",
        "projects_cleared": projects_cleared,
        "users_cleared": users_cleared
    }

@app.get("/admin/ngrok_tunnels", tags=["Admin Panel"])
async def get_ngrok_tunnels(username: str = Depends(admin_auth)):
    """Возвращает информацию о всех активных туннелях ngrok (через http://localhost:4040/api/tunnels)."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get("http://localhost:4040/api/tunnels")
            response.raise_for_status()
            return response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка при обращении к ngrok API: {e}")

# --- Мониторинг и статистика ---
@app.get("/admin/health", tags=["Admin Panel"])
async def health(username: str = Depends(admin_auth)):
    """Проверка статуса сервиса и аптайм."""
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - service_start_time)
    }

@app.get("/admin/stats", tags=["Admin Panel"])
async def stats(username: str = Depends(admin_auth)):
    """Статистика по событиям, сообщениям, ошибкам."""
    return {
        "webhooks_processed": event_counter,
        "messages_sent": message_counter,
        "errors": error_counter
    }

@app.get("/admin/last_webhooks", tags=["Admin Panel"])
async def last_webhooks_view(username: str = Depends(admin_auth)):
    """Последние 20 обработанных вебхуков."""
    return list(last_webhooks)

# --- Управление конфигом ---
@app.get("/admin/config", tags=["Admin Panel"])
async def get_config(username: str = Depends(admin_auth)):
    """Просмотр текущих настроек (без чувствительных данных)."""
    safe_config = {k: v for k, v in config.items() if k not in ["admin_panel", "telegram_token", "access_token"]}
    return safe_config

# --- Диагностика интеграций ---
@app.get("/admin/wrike_ping", tags=["Admin Panel"])
async def wrike_ping(username: str = Depends(admin_auth)):
    """Тестовый запрос к Wrike API."""
    try:
        async with httpx.AsyncClient() as client:
            headers = {'Authorization': f'bearer {access_token}'}
            response = await client.get("https://www.wrike.com/api/v4/contacts", headers=headers)
            response.raise_for_status()
            return {"status": "ok", "data": response.json()}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.post("/admin/telegram_ping", tags=["Admin Panel"])
async def telegram_ping(username: str = Depends(admin_auth)):
    """Тестовая отправка сообщения в Telegram."""
    try:
        await webhook_handler.send_telegram_message("Проверка связи с Telegram (admin ping)")
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

# --- Управление логами ---
@app.get("/admin/download_logs", tags=["Admin Panel"])
async def download_logs(username: str = Depends(admin_auth)):
    """Скачать лог-файл целиком."""
    log_filename = config.get('logging_settings', {}).get('handlers', {}).get('file', {}).get('filename', 'webhook.log')
    try:
        return fastapi.responses.FileResponse(log_filename, filename=log_filename)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка при скачивании лог-файла: {e}")

# --- Webhook Endpoint ---

@app.post("/webhooks", tags=["Webhook"])
async def wrike_webhook(request: Request):
    """Асинхронно обрабатывает вебхук от Wrike."""
    logger.info('Received webhook POST request')
    data = await request.json()
    logger.info('Webhook data: %s', data)

    if not data:
        logger.warning("Webhook request contains empty data.")
        return {"status": "OK"}

    # Параллельная обработка всех событий
    await asyncio.gather(*(webhook_handler.process_webhook_event(event) for event in data))

    return {"status": "OK"}

if __name__ == "__main__":
    uvicorn.run(
        "webhook:app",
        host="0.0.0.0",
        port=5555,
        reload=True
    )
