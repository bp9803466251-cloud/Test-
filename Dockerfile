# ==============================================================================
# Gatekeeper-AI Ecosystem
# Build Manifest: Dockerfile
# Version: 9.6.8-patched
# ==============================================================================

# ЭТАП 1: Сборка зависимостей и компиляция C-расширений
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    libffi-dev \
    pkg-config \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Системная установка вместо --user: /usr/local читается всеми пользователями
RUN pip install --no-cache-dir -r requirements.txt

# ЭТАП 2: Финальный Root-Compliance Рантайм
FROM python:3.12-slim AS runner

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY . .

# Реальная проверка §1.3: find_spec видит пакет в site-packages,
# в отличие от пустого sys.modules свежего интерпретатора
RUN python -c "import importlib.util, sys; s = importlib.util.find_spec('requests'); sys.exit('[BUILD] FAIL: библиотека requests обнаружена в site-packages') if s else print('[BUILD] Compliance §1.3: PASSED - requests отсутствует.')"

# Непривилегированный рантайм
RUN useradd --create-home --shell /usr/sbin/nologin gatekeeper \
    && chown -R gatekeeper:gatekeeper /app
USER gatekeeper

EXPOSE 8080

# Прод-энтрипоинт: конвейер, а не тест-раннер. Переопределяется через env для CI.
CMD ["sh", "-c", "exec python -u ${GATEKEEPER_ENTRYPOINT:-main.py}"]
