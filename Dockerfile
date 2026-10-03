# Базовый образ
FROM python:3.12-slim AS builder

# Установка рабочей директории
WORKDIR /app

# Копируем только requirements для создания слоя зависимостей
COPY requirements.txt .

# Устанавливаем зависимости в отдельном слое
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir --upgrade pip \
    && pip wheel --no-cache-dir -r requirements.txt -w /wheels

# Основной образ
FROM python:3.12-slim

# Установка рабочей директории
WORKDIR /app

# Копируем предварительно собранные колеса
COPY --from=builder /wheels /wheels

# Устанавливаем зависимости из колес
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir /wheels/* \
    && rm -rf /wheels

# Копируем исходный код
COPY . /app

# Установка переменных окружения
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV PYTHONWARNINGS=ignore

# Установка прав доступа
RUN chmod +x /app/entrypoint.sh

# Точка входа
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "main.py"]

# Экспозиция портов
EXPOSE 5000
EXPOSE 9100

# Оптимизации
# 1. Удаление ненужных пакетов
RUN apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 2. Установка только необходимых инструментов
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    build-essential \
    netcat \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
    
