# collector_schema.py
"""
Единая схема данных для всех коллекторов Gatekeeper-AI v600-prod.

Все коллекторы создают и обновляют матчи через этот модуль,
чтобы гарантировать единый формат в Redis.

Единая структура матча:
  home_team, away_team, date_utc, competition, country, status
  odds:        {home, draw, away, source, updated_at}
  predictions: {home_win, draw, away_win, ..., source, updated_at}
  stats:       {possession_home, ..., source, updated_at}
  h2h:         {total_meetings, home_wins, ..., source, updated_at}
  source_ids:  {sharpapi: "evt_123", odds_api: "abc", bzzoiro: 98765}

Внимание: normalize_date, is_future_match, now_msk, save_meta
импортируются из gatekeeper_hub.py (правило 1.11). Функции-дубликаты
в этом модуле оставлены для обратной совместимости — коллекторы
должны импортировать хелперы из хаба, а не отсюда.
"""
from datetime import datetime, timezone, timedelta

MSK_TIMEZONE = timezone(timedelta(hours=3))


def normalize_date(raw_date: str) -> tuple:
    """
    Нормализует дату в (date_str YYYY-MM-DD, date_utc ISO UTC).
    Принимает любые форматы: ISO с Z, с +offset, без offset, YYYY-MM-DD.

    Внимание: gatekeeper_hub.normalize_date() возвращает строку (ISO UTC),
    а эта функция — кортеж (date_str, date_utc). Коллекторы должны
    использовать gatekeeper_hub.normalize_date() (правило 1.11).
    """
    if not raw_date:
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%dT%H:%M:%SZ")

    date_str = raw_date[:10]

    try:
        if raw_date.endswith("Z"):
            dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        elif "+" in raw_date[10:] or raw_date[10:11] == "-":
            dt = datetime.fromisoformat(raw_date)
        else:
            dt = datetime.fromisoformat(raw_date).replace(tzinfo=timezone.utc)
        date_utc = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        date_utc = f"{date_str}T00:00:00Z"

    return date_str, date_utc


def is_future_match(date_utc: str, now: datetime = None) -> bool:
    """
    Проверяет, что матч в будущем (строго позже now).
    Без даты (date_utc="") — считаем будущим (True), как в гайде (правило 1.5).
    """
    if not date_utc:
        return True
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
        return dt > now
    except (ValueError, TypeError):
        return True


def make_odds(home="-", draw="-", away="-", source="unknown") -> dict:
    """Создаёт секцию odds в едином формате."""
    now = datetime.now(MSK_TIMEZONE).isoformat()
    odds = {}
    if home is not None and home != "":
        odds["home"] = str(home) if home != "-" else "-"
    else:
        odds["home"] = "-"
    if draw is not None and draw != "":
        odds["draw"] = str(draw) if draw != "-" else "-"
    else:
        odds["draw"] = "-"
    if away is not None and away != "":
        odds["away"] = str(away) if away != "-" else "-"
    else:
        odds["away"] = "-"
    odds["source"] = source
    odds["updated_at"] = now
    return odds


def make_predictions(
    prob_home=None, prob_draw=None, prob_away=None,
    home_win=None, draw=None, away_win=None,
    predicted=None, expected_goals_home=None, expected_goals_away=None,
    prob_over_25=None, prob_btts_yes=None, most_likely_score=None,
    favorite=None, favorite_prob=None, model_confidence=None, model_version=None,
    source="unknown"
) -> dict:
    """
    Создаёт секцию predictions в едином формате.
    Канонические ключи (гайд раздел 3): home_win, draw, away_win.
    Legacy-ключи (prob_home, prob_draw, prob_away) сохранены как алиасы.
    """
    now = datetime.now(MSK_TIMEZONE).isoformat()
    preds = {}

    # Канонические ключи (приоритет) + legacy-алиасы
    ph = home_win if home_win is not None else prob_home
    pd = draw if draw is not None else prob_draw
    pa = away_win if away_win is not None else prob_away

    if ph is not None:
        preds["home_win"] = ph
    if pd is not None:
        preds["draw"] = pd
    if pa is not None:
        preds["away_win"] = pa

    if predicted is not None:
        preds["predicted"] = predicted
    if expected_goals_home is not None:
        preds["expected_goals_home"] = expected_goals_home
    if expected_goals_away is not None:
        preds["expected_goals_away"] = expected_goals_away
    if prob_over_25 is not None:
        preds["prob_over_25"] = prob_over_25
    if prob_btts_yes is not None:
        preds["prob_btts_yes"] = prob_btts_yes
    if most_likely_score is not None:
        preds["most_likely_score"] = most_likely_score
    if favorite is not None:
        preds["favorite"] = favorite
    if favorite_prob is not None:
        preds["favorite_prob"] = favorite_prob
    if model_confidence is not None:
        preds["model_confidence"] = model_confidence
    if model_version is not None:
        preds["model_version"] = model_version
    preds["source"] = source
    preds["updated_at"] = now
    return preds


def make_stats(
    possession_home=None, possession_away=None,
    total_shots_home=None, total_shots_away=None,
    shots_on_target_home=None, shots_on_target_away=None,
    xg_home=None, xg_away=None,
    source="unknown"
) -> dict:
    """Создаёт секцию stats в едином формате."""
    now = datetime.now(MSK_TIMEZONE).isoformat()
    stats = {}
    if possession_home is not None:
        stats["possession_home"] = possession_home
    if possession_away is not None:
        stats["possession_away"] = possession_away
    if total_shots_home is not None:
        stats["total_shots_home"] = total_shots_home
    if total_shots_away is not None:
        stats["total_shots_away"] = total_shots_away
    if shots_on_target_home is not None:
        stats["shots_on_target_home"] = shots_on_target_home
    if shots_on_target_away is not None:
        stats["shots_on_target_away"] = shots_on_target_away
    if xg_home is not None:
        stats["xg_home"] = xg_home
    if xg_away is not None:
        stats["xg_away"] = xg_away
    stats["source"] = source
    stats["updated_at"] = now
    return stats


def make_h2h(
    total_meetings=None, home_wins=None, draws=None, away_wins=None,
    total_matches=None, avg_total_goals=None,
    source="unknown"
) -> dict:
    """
    Создаёт секцию h2h в едином формате.
    Канонический ключ (гайд раздел 3): total_meetings.
    Legacy-ключ total_matches сохранён как алиас.
    """
    now = datetime.now(MSK_TIMEZONE).isoformat()
    h2h = {}
    # Канонический ключ (приоритет) + legacy-алиас
    tm = total_meetings if total_meetings is not None else total_matches
    if tm is not None:
        h2h["total_meetings"] = tm
    if home_wins is not None:
        h2h["home_wins"] = home_wins
    if draws is not None:
        h2h["draws"] = draws
    if away_wins is not None:
        h2h["away_wins"] = away_wins
    if avg_total_goals is not None:
        h2h["avg_total_goals"] = avg_total_goals
    h2h["source"] = source
    h2h["updated_at"] = now
    return h2h


def make_source_ids(**kwargs) -> dict:
    """
    Создаёт словарь source_ids.
    Пример: make_source_ids(sharpapi="evt_123", odds_api="abc", bzzoiro=98765)
    """
    result = {}
    for k, v in kwargs.items():
        if v is not None and v != "":
            result[k] = v
    return result


def make_match(
    home_team="", away_team="", date_utc="",
    competition="", country="", status="scheduled",
    source="unknown", source_ids=None, odds=None,
    predictions=None, stats=None, h2h=None,
) -> dict:
    """
    Создаёт матч в едином формате для batch_upsert_matches.
    Все коллекторы используют эту функцию.
    """
    match = {
        "home_team": home_team,
        "away_team": away_team,
        "date_utc": date_utc,
        "competition": competition,
        "country": country,
        "status": status,
    }
    if source_ids:
        match["source_ids"] = source_ids
    if odds:
        match["odds"] = odds
    if predictions:
        match["predictions"] = predictions
    if stats:
        match["stats"] = stats
    if h2h:
        match["h2h"] = h2h
    return match


def validate_match(data: dict) -> tuple:
    """
    Проверяет, что матч соответствует единой схеме.
    Возвращает (is_valid, errors_list).

    Проверяет:
      - Обязательные поля: home_team, away_team, date_utc
      - Формат date_utc: ISO UTC
      - Типы секций: odds/predictions/stats/h2h/source_ids — dict или отсутствуют
      - source_ids — dict с непустыми значениями
      - odds — home/draw/away должны быть числами или "-"
    """
    errors = []

    if not isinstance(data, dict):
        return False, ["Match data is not a dict"]

    # Обязательные поля
    required = ["home_team", "away_team", "date_utc"]
    for field in required:
        val = data.get(field, "")
        if not val or (isinstance(val, str) and not val.strip()):
            errors.append(f"Missing or empty required field: {field}")

    # Проверка date_utc
    date_utc = data.get("date_utc", "")
    if date_utc:
        try:
            datetime.fromisoformat(str(date_utc).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            errors.append(f"Invalid date_utc format: {date_utc}")

    # Проверка секций
    dict_sections = ["odds", "predictions", "stats", "h2h", "source_ids"]
    for section in dict_sections:
        val = data.get(section)
        if val is None:
            continue
        if not isinstance(val, dict):
            errors.append(f"Section '{section}' must be dict, got {type(val).__name__}")

    # Проверка source_ids — значения должны быть непустыми
    source_ids = data.get("source_ids", {})
    if isinstance(source_ids, dict):
        for k, v in source_ids.items():
            if v is None or v == "":
                errors.append(f"source_ids['{k}'] is empty")

    # Проверка odds — home/draw/away должны быть числами или "-"
    odds = data.get("odds", {})
    if isinstance(odds, dict):
        for key in ("home", "draw", "away"):
            val = odds.get(key, "-")
            if val is None or val == "-":
                continue
            try:
                float(val)
            except (ValueError, TypeError):
                errors.append(f"odds['{key}']='{val}' is not numeric")

    is_valid = len(errors) == 0
    return is_valid, errors
