import logging
import logging.handlers
import json
import yaml
from datetime import datetime
from typing import Any, Dict

class JSONFormatter(logging.Formatter):
    """Форматтер для логирования сообщений в формате JSON."""
    def __init__(self, additional_fields: Dict[str, Any] = None):
        super().__init__()
        self.additional_fields = additional_fields or {}

    def format(self, record: logging.LogRecord) -> str:
        """
        Форматирует запись лога в строку JSON.

        :param record: Логируемая запись.
        :return: Строка в формате JSON.
        """
        log_data = {
            'timestamp': datetime.fromtimestamp(record.created).isoformat(),
            'level': record.levelname,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno
        }

        # Добавляем дополнительные поля из конфига
        log_data.update(self.additional_fields)

        # Добавляем информацию об исключении, если оно есть
        if record.exc_info:
            log_data['exception'] = self.formatException(record.exc_info)

        # Добавляем дополнительные поля, если они были переданы через record
        if hasattr(record, 'extra_fields'):
            log_data.update(record.extra_fields)

        return json.dumps(log_data, ensure_ascii=False)


def setup_logging():
    """
    Настраивает систему логирования на основе конфигурации из YAML-файла.

    :return: Настроенный корневой логгер.
    """
    # Загружаем конфигурацию логирования из файла
    with open('config.yaml', 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    logging_config = config.get('logging_settings', {})

    # Получаем уровень логирования (по умолчанию INFO)
    log_level = getattr(logging, logging_config.get('level', 'INFO'))

    # Получаем корневой логгер
    logger = logging.getLogger()
    logger.setLevel(log_level)

    # Очищаем все существующие обработчики
    logger.handlers.clear()

    # Получаем дополнительные поля для логов (если заданы)
    additional_fields = logging_config.get('fields', {})

    # Определяем используемый форматтер: JSON или стандартный
    if logging_config.get('format') == 'json':
        formatter = JSONFormatter(additional_fields)
    else:
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

    # Получаем настройки обработчиков из конфига
    handlers_config = logging_config.get('handlers', {})

    # Настройка консольного обработчика
    if handlers_config.get('console', {}).get('enabled', True):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    # Настройка файлового обработчика (ротация по размеру)
    file_config = handlers_config.get('file', {})
    if file_config.get('enabled', True):
        file_handler = logging.handlers.RotatingFileHandler(
            filename=file_config.get('filename', 'webhook.log'),
            maxBytes=file_config.get('max_size_mb', 10) * 1024 * 1024,
            backupCount=file_config.get('backup_count', 5),
            encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


# Инициализация и настройка логгера
logger = setup_logging()
