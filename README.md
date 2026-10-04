# GatekeeperAI Ecosystem

Распределённая экосистема для сбора, нормализации и анализа футбольных данных.

## Архитектура

```
Модуль (collector_sharpapi.py, ...)
    |
    +-- gatekeeper_hub.py    <- Единый шлюз для матчей, odds, stats
    |       |
    |       +-- redis_hub.py  <- Транспорт (urllib → Upstash REST API)
    |               +-- Circuit Breaker (10 ошибок → 60с → авто-восстановление)
    |
    +-- search_module.py     <- Нормализация команд
    +-- team_registry.py     <- Алиасы команд (257+ записей)
```

## Структура репозитория (плоская — правило 1.1)

| Файл | Назначение |
|------|-----------|
| `gatekeeper_hub.py` | Центральный хаб (единственный шлюз для match:*) |
| `redis_hub.py` | Транспортный слой (urllib, circuit breaker, конверт v700-prod) |
| `gatekeeper_config.py` | Загрузка конфигурации (YAML + env) |
| `gatekeeper_config.yaml` | Единая конфигурация экосистемы |
| `search_module.py` | Нормализация команд, поиск |
| `team_registry.py` | Алиасы команд (TEAM_ALIASES, clean_team_name) |
| `base_collector.py` | Базовый класс коллекторов (5-шаговый алгоритм) |
| `collector_sharpapi.py` | Коллектор SharpAPI (коэффициенты) |
| `collector_odds_api.py` | Коллектор The Odds API |
| `collector_bzzoiro.py` | Коллектор Bzzoiro (predictions, stats, h2h) |
| `collector_propline.py` | Коллектор Propline (Pinnacle) |
| `football_data_to_redis.py` | Загрузчик истории (CSV → Redis) |
| `main.py` | Аналитический пайплайн |
| `metrics.py` | Сбор и отправка метрик |
| `value_engine.py` | Движок оценки ценности (value bets) |
| `odds_priority.yaml` | Приоритеты букмекеров |
| `schema_v710.json` | JSON Schema v710 |
| `country_code_map.py` | Маппинг кодов стран и лиг |
| `redis_config.py` | Конфигурация Redis |
| `redis_diagnostics.py` | Диагностика Redis |
| `source_diagnostics.py` | Диагностика источников |
| `telegram_transport.py` | Отправка дашборда в Telegram |
| `test_contracts.py` | Тесты контрактов API |
| `test_fixtures.py` | Эталонные объекты для тестов |
| `requirements.txt` | Зависимости |

## Запуск

### Коллектор (ручной запуск через GitHub Actions)

```bash
python collector_sharpapi.py
```

### Тесты

```bash
python test_contracts.py
```

### Диагностика

```bash
python source_diagnostics.py
python redis_diagnostics.py --json
```

## Ключевые правила

1. **Плоская структура** — все .py файлы в корне (правило 1.1)
2. **Единый хаб** — запись в Redis только через gatekeeper_hub.py (правило 1.4)
3. **Native transport** — redis_hub.py использует только urllib (правило 1.3)
4. **Единый формат odds** — формат 1x2 (правило 1.21)
5. **Очистка** — завершённые матчи удаляются через 2 часа (правило 1.7)
6. **Circuit breaker** — 10 ошибок → 60с блокировка (правило 1.9)
