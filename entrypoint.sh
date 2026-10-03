#!/bin/bash
set -e  # Остановка при первой ошибке

# Ожидание доступности Redis
wait_for_service() {
    local service=$1
    local port=$2
    local ret=1
    for ((i = 0; i < 60; i++)); do
        echo "Ожидание подключения к ${service}:${port}..."
        nc -z ${service} ${port} && ret=0 && break
        echo "Сервис ${service} пока недоступен..."
        sleep 2
    done
    if [[ ${ret} -ne 0 ]]; then
        echo "Не удалось подключиться к ${service}:${port}"
        exit 1
    fi
}

# Проверка зависимостей
wait_for_service redis 6379

# Загрузка конфигурации
function load_config() {
    if [ -f .env ]; then
        echo "Загрузка переменных окружения из .env"
        set -a
        source .env
        set +a
    fi
}

# Проверка обязательных переменных
function check_env() {
    if [ -z "$SHARPAPI_API_KEY" ]; then
        echo "Ошибка: SHARPAPI_API_KEY не установлен"
        exit 1
    fi
    
    if [ -z "$ODDS_API_KEY" ]; then
        echo "Ошибка: ODDS_API_KEY не установлен"
        exit 1
    fi
}

# Инициализация приложения
function init_app() {
    # Здесь можно добавить специфичную для приложения инициализацию
    echo "Инициализация приложения..."
}

# Запуск
function start_app() {
    echo "Запуск основного процесса..."
    exec python main.py
}

# Основная логика
load_config
check_env
init_app
start_app
