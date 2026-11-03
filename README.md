# GatekeeperAI Ecosystem (v9.6.8-patched)

Распределённая экосистема для сбора, нормализации, склейки, анализа и стресс-тестирования футбольных данных в режиме реального времени.

### 📊 CI/CD Status: Patch Cycle 9.6.8 — AUDIT COMPLETE (2026-11-03)
![CI/CD](https://img.shields.io/badge/Workflows-18%2F18%20Dispatch--Only-blue?style=flat-square)
![Audit](https://img.shields.io/badge/Patch%20Cycle%209.6.8-6%2F6%20Workflows%20Audited-orange?style=flat-square)
![Architecture](https://img.shields.io/badge/Pattern-Zero--Folder%20Architecture-blue?style=flat-square)
![Security](https://img.shields.io/badge/Security-%C2%A71.3%20Compliant-red?style=flat-square)

> ⚠️ **Примечание к бейджу.** Прежний статус «28/28 PASSED / Absolute GREEN» признан
> недостоверным: аудит цикла 9.6.8 выявил системный паттерн «fake green» — шаги CI
> завершались с кодом 0 при фактически не выполненных или проваленных проверках
> (CI-обход в `stress_test_runtime.py`, отсутствие `exit 1` в `test_gluing.py` и
> `match_linker.py`, глушение алертов `|| true` и битый Telegram-URL в `backtest.yml`).
> Текущий бейдж отражает **состояние исходников**, а не результат прогона. Реальный
> статус — зелёный только после первого честного прогона всей партии.

---

## 🩹 Патч-цикл 9.6.8 (аудит 2026-11-03): закрыто 6/6 воркфлоу + 3 скрипта

| # | Файл | Ключевые исправления |
|---|---|---|
| 1 | `football_data_loader.yml` | Снят `|| true` в алерте; таймаут 180→60; concurrency → `gatekeeper-pipeline`; fallback `|| ''` для всех секретов |
| 2 | `backtest.yml` | Исправлен битый Telegram-URL (`api.telegram.org/bot`); сняты `|| true` и тихий `except: pass`; concurrency → pipeline; дубль `GATEKEEPER_CONFIG_FILE` удалён |
| 3 | `stress_test.yml` + `stress_test_runtime.py` | Снят CI-обход в `__main__` — стресс-конвейер реально запускается; фикстуры приведены к контракту v710 (`odds_1x2` с ключами "1"/"X"/"2", строковые `*_clean`, без `predictions` по §1.29); concurrency → `gatekeeper-diagnostics` |
| 4 | `test_gluing_ci.yml` + `test_gluing.py` | Триггеры push/PR → `workflow_dispatch`; матрица 3.10/3.12 удалена (§1.10 — единый Python 3.11); добавлен честный `exit 1` при провале |
| 5 | `match_dump.yml` | Добавлена concurrency-группа; удалён дубль env-передачи конфига |
| 6 | `match_linker.yml` + `match_linker.py` | **Удалён cron `0 */6 * * *`** (§1.2); исправлен NameError в обработчике исключений цикла; введены честные exit-коды; снят легаси-фолбэк прямого чтения Redis (§1.4) |

**Системный паттерн «fake green» — 4 подтверждения:** CI-заглушка стресс-теста,
отсутствие `exit 1` в `test_gluing.py` и `match_linker.py`, битый Telegram-URL в
`backtest.yml`. Дополнительно зарегистрирован CI-guard в `__main__` `value_engine.py`
(безвреден при импорте, но даёт ложный зелёный при прямом запуске в CI — к правке
в следующем цикле).

**Конвенции, приведённые в цикле:**
* Все воркфлоу — строго `workflow_dispatch`, без `schedule`/`push`/`pull_request` (§1.2).
* Канонические concurrency-группы (§16.4): `collector-{name}`, `gatekeeper-pipeline`,
  `gatekeeper-diagnostics`.
* Таймауты (§1.10): утилиты и диагностика — 5 минут; конвейеры (loader, backtest,
  linker) — 30–60 минут по нагрузке.
* Секреты: имя секрета GitHub = имя env-переменной, 1:1, обязательный fallback `|| ''`.
* Telegram-алерты: без `|| true`, канонический URL `api.telegram.org/bot`, plain-текст
  с `%0A`.

**Открытые пункты следующего цикла:**
1. Сверить `gatekeeper_hub._adapt_to_core_engine_contract` с фикстурами стресс-теста
   (сохранение поля `home_team`, построение плоского паспорта).
2. Унифицировать имя конфиг-переменной по репозиторию: `GK_CONFIG_FILE` vs
   `GATEKEEPER_CONFIG_FILE` — отдельным коммитом.
3. Обновить реестр README (выполнено данным коммитом: 12 → 18 воркфлоу) и получить
   реальный зелёный прогон после патчей.

---

## 🧭 Архитектура и ИБ-контур

