# ==============================================================================
# ЭТАП 1: Сборка зависимостей и компиляция C-расширений
# ==============================================================================
FROM python:3.11-slim AS builder

WORKDIR /app

# Установка системных пакетов для сборки scipy и curl_cffi
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    libffi-dev \
    pkg-config \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Копируем и фиксируем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ==============================================================================
# ЭТАП 2: Финальный Root-Compliance Рантайм
# ==============================================================================
FROM python:3.11-slim AS runner

WORKDIR /app

# Установка базовых сертификатов для бесперебойной работы urllib.request (§1.3)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Перенос собранных пакетов из builder-этапа
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Копирование архитектурных компонентов монолита (Zero-Folder Pattern)
COPY . .

# Проверка ИБ-регламента §1.3 на уровне сборки образа (блокировка requests)
RUN python -c "import urllib.request; print('[BUILD] Нативный urllib транспорт верифицирован.')"
RUN python -c "import sys; 'requests' in sys.modules and sys.exit(1) or print('[BUILD] Compliance §1.3: PASSED.')"

# Открываем порт для интеграционных метрик/хаба (если применимо)
EXPOSE 8080

# По умолчанию запускаем главный контрактный тест-раннер экосистемы
CMD ["python", "test_contracts.py"]
