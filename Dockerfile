# Базовый образ
FROM python:3.12-slim

# Установка рабочей директории
WORKDIR /app

# Установка дополнительных утилит
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Копирование зависимостей
COPY requirements.txt .

# Установка зависимостей
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Копирование кода
COPY . /app

# Установка переменных окружения
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Настройка таймаутов
ENV PYTHONWARNINGS=ignore

# Установка прав доступа
RUN chmod +x /app/entrypoint.sh

# Точка входа
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "main.py"]

# Экспозиция портов
EXPOSE 5000
EXPOSE 9100  # Порт для метрик Prometheus
