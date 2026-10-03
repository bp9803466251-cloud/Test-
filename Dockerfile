# Базовый образ
FROM python:3.12-slim

# Установка рабочей директории
WORKDIR /app

# Копирование зависимостей
COPY requirements.txt .

# Установка зависимостей
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Копирование кода
COPY . /app

# Установка переменных окружения
ENV PYTHONUNBUFFERED=1

# Точка входа
CMD ["python", "main.py"]
