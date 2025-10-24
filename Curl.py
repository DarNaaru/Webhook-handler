
"""
 CURL запрос для создания webhook wrike

TaskResponsiblesAdded - Срабатывает при добавлении любого нового адресата в задачу, включая всех
                        пользователей Wrike (и соавторов), а также пользователей с ожидающими приглашениями

TaskResponsiblesRemoved - Срабатывает, когда кто-то отстраняется от выполнения задания

TaskStatusChanged - Выполняется при изменении статуса задачи

TaskDescriptionChanged - Срабатывает при изменении описания задачи. Примечание: Уведомления, связанные с
                         изменением поля описания, отправляются примерно с 5-минутной задержкой

TaskImportanceChanged - Выполняется при изменении важности задачи

CommentAdded - Выполняется при добавлении нового комментария. Не выполняется
                для комментариев без текста (то есть комментариев только с вложениями).

"""


import requests

# Параметры
ACCESS_TOKEN_WRIKE = (
    ''
)

WEBHOOK_URL = '/webhooks'

EVENTS = [
    "TaskResponsiblesAdded", "TaskResponsiblesRemoved", "TaskStatusChanged",
    "TaskDescriptionChanged", "TaskImportanceChanged", "CommentAdded"
]

# Заголовки
HEADERS = {
    'Authorization': f'bearer {ACCESS_TOKEN_WRIKE}',
    'Content-Type': 'application/json'
}

# Данные
data = {
    "hookUrl": WEBHOOK_URL,
    "events": EVENTS
}

# Отправка POST запроса
response = requests.post(
    "https://www.wrike.com/api/v4/webhooks",
    headers=HEADERS,
    json=data
)

# Проверка ответа
if response.status_code == 200:
    print('Webhook created successfully:', response.json())
else:
    print('Failed to create webhook:', response.json())
