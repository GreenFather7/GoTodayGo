"""Измерение накопленных просмотров за первые сутки для публикаций @GoTodayGo."""

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from telegram_views import get_observation


ROOT = Path(__file__).resolve().parent
UTC = timezone.utc
MSK = ZoneInfo("Europe/Moscow")
CUTOFF = datetime(2026, 11, 19, 16, 18, tzinfo=UTC)
PERIOD = timedelta(days=14)
MIN_AGE = timedelta(hours=24)
MAX_AGE = timedelta(hours=25)
CADENCE = timedelta(hours=3)
OFFSET = timedelta(hours=1, minutes=18)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRIORITY = (16, 19, 22)


def nominal(slot):
    return EPOCH + OFFSET + slot * CADENCE


def timestamp(value):
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if when.tzinfo is None:
        raise ValueError("Для времени публикации нужен часовой пояс.")
    return when.astimezone(UTC)


def iso(when):
    return when.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def msk(when):
    return when.astimezone(MSK).strftime("%d.%m.%Y %H:%M")


def new_state():
    return {"version": 1, "completed": False, "start": None, "samples": {}}


def validate(state):
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("Неизвестная версия файла эксперимента.")
    if type(state.get("completed")) is not bool or not isinstance(state.get("samples"), dict):
        raise ValueError("Некорректный файл эксперимента.")
    if "start" not in state or (state["start"] is not None and timestamp(state["start"]) < CUTOFF):
        raise ValueError("Некорректное начало эксперимента.")
    for key, sample in state["samples"].items():
        if not (key.isdecimal() and int(key) > 3507 and isinstance(sample, dict)
                and type(sample.get("slot")) is int):
            raise ValueError("Некорректная запись о публикации.")
        if "published_at" in sample:
            timestamp(sample["published_at"])
        if "measurement" in sample:
            measured = sample["measurement"]
            age = timestamp(measured["observed_at"]) - timestamp(sample["published_at"])
            if not (MIN_AGE <= age < MAX_AGE and type(measured["views"]) is int
                    and measured["views"] >= 0 and measured["resolution"] >= 1 and measured["label"]):
                raise ValueError("Повреждён сохранённый замер.")
    return state


def scheduled_entries(ledger):
    if ledger.get("version") != 1 or not isinstance(ledger.get("entries"), list):
        raise ValueError("Неизвестный формат журнала публикаций.")
    return [e for e in ledger["entries"] if e.get("run_id") and type(e.get("slot")) is int
            and nominal(e["slot"]) >= CUTOFF]


def check_observation(sample, read, message_id):
    try:
        return read(message_id)
    except (ValueError, OSError) as error:
        # Store just an error class; signed URLs or raw HTTP content do not belong in the report.
        sample["read_error"] = type(error).__name__
        return None


def collect(state, ledger, now, read=get_observation):
    validate(state)
    if state["completed"] or now < CUTOFF:
        return
    entries = scheduled_entries(ledger)
    horizon = timestamp(state["start"]) + PERIOD if state["start"] else CUTOFF + PERIOD
    for entry in sorted(entries, key=lambda row: row["slot"]):
        msg = entry.get("message_id")
        if entry["status"] != "sent" or type(msg) is not int or msg <= 3507 or nominal(entry["slot"]) >= horizon:
            continue
        sample = state["samples"].setdefault(str(msg), {"slot": entry["slot"]})
        if sample["slot"] != entry["slot"]:
            raise ValueError("Записи журнала противоречат друг другу.")
        observation = None
        if "published_at" not in sample:
            if entry.get("published_at"):
                sample["published_at"] = iso(timestamp(entry["published_at"]))
            else:
                observation = check_observation(sample, read, msg)
                if observation is None:
                    continue
                sample["published_at"] = iso(observation.published_at)
        published = timestamp(sample["published_at"])
        if state["start"] is None and CUTOFF <= published < CUTOFF + PERIOD and published <= now:
            state["start"] = iso(published)
            horizon = published + PERIOD
        age = now - published
        if not (CUTOFF <= published < horizon and MIN_AGE <= age < MAX_AGE) or "measurement" in sample:
            continue
        observation = observation or check_observation(sample, read, msg)
        if observation is None:
            continue
        actual_age = observation.observed_at - observation.published_at
        if observation.published_at != published:
            sample["read_error"] = "PublicationTimestampMismatch"
        elif MIN_AGE <= actual_age < MAX_AGE:
            sample["measurement"] = {
                "observed_at": iso(observation.observed_at), "views": observation.views,
                "resolution": observation.resolution, "label": observation.label,
            }

    if state["start"] is None and now >= CUTOFF + PERIOD:
        state["completed"] = True
    if state["start"] and now >= timestamp(state["start"]) + PERIOD + MAX_AGE:
        state["completed"] = True


def median_label(values):
    if not values:
        return "—"
    return f"{median(values):,.1f}".rstrip("0").rstrip(".").replace(",", " ").replace(".", ",")


def report(state, ledger, now):
    start = timestamp(state["start"]) if state["start"] else None
    lines = ["# Просмотры @GoTodayGo за первые сутки", ""]
    if not start:
        lines.append(f"Ожидание первой плановой публикации с {msk(CUTOFF)} по {msk(CUTOFF + PERIOD)} МСК.")
        if state["completed"]:
            lines += ["", "Эксперимент завершён. В этом интервале журнал не подтвердил ни одной плановой публикации с известным временем выхода."]
        unknown = len([s for s in state["samples"].values() if "published_at" not in s])
        if unknown:
            lines += ["", f"Публикаций без подтверждённого времени выхода: {unknown}."]
        return "\n".join(lines) + "\n"

    end = start + PERIOD
    lines += [("Итоговый отчёт." if state["completed"] else "Сбор продолжается."),
              f"Период выхода карточек: [{msk(start)}, {msk(end)}) МСК. Окончание наблюдений: {msk(end + MAX_AGE)} МСК.",
              "", "В выборку входят подтверждённые плановые отправки. Час группы определяется по фактическому времени выхода в Москве. "
              "Для каждого сообщения сохраняется первый публичный счётчик, прочитанный в возрасте от 24 часов включительно до 25 часов исключительно. "
              "Telegram может округлять значения. Медианы рассчитаны по успешным замерам.", ""]
    groups = defaultdict(list)
    details = []
    seen_ids = set()
    for key, s in sorted(state["samples"].items(), key=lambda item: item[1]["slot"]):
        if "published_at" not in s:
            details.append((key, s, None, "Время выхода неизвестно"))
            continue
        published = timestamp(s["published_at"])
        if not start <= published < end:
            continue
        measured = s.get("measurement")
        if measured:
            result = f"{measured['label']} · {msk(timestamp(measured['observed_at']))}"
        elif now >= published + MAX_AGE:
            result = "Окно замера пропущено"
        else:
            result = "Ожидает замера"
        if s.get("read_error") and not measured:
            result += f" ({s['read_error']})"
        groups[published.astimezone(MSK).hour].append(s)
        seen_ids.add(key)
        details.append((key, s, published, result))
    for heading, hours in [("Основные часы", PRIORITY),
                           ("Прочие фактические часы", sorted(set(groups) - set(PRIORITY)))]:
        if not hours:
            continue
        lines += [f"## {heading}", "", "| Час, МСК | Вышло | Замерено | Медиана просмотров |",
                  "|---|---:|---:|---:|"]
        for h in hours:
            samples = groups[h]
            views = [s["measurement"]["views"] for s in samples if "measurement" in s]
            lines.append(f"| {h:02d}:00 | {len(samples)} | {len(views)} | {median_label(views)} |")
        lines.append("")

    if state["completed"]:
        medians = {}
        for hour in PRIORITY:
            views = [s["measurement"]["views"] for s in groups[hour] if "measurement" in s]
            if views:
                medians[hour] = median(views)
        if len(medians) == len(PRIORITY):
            leaders = [f"{h:02d}:00" for h in PRIORITY if medians[h] == max(medians.values())]
            if len(leaders) > 1:
                lines += ["Наибольшая наблюдаемая медиана одинакова у часов " + ", ".join(leaders) + " МСК."]
            else:
                lines += [f"Среди трёх основных часов наибольшая наблюдаемая медиана у {leaders[0]} МСК."]
            lines += ["Небольшие группы и округление счётчиков ограничивают точность сравнения.", ""]
        else:
            lines += ["Сравнение трёх основных часов невозможно: хотя бы в одной группе нет замеров.", ""]

    # The first nominal slot may precede the actual start by a few minutes.
    first_slot = min(s["slot"] for s in state["samples"].values() if s.get("published_at") == state["start"])
    entries = {e["slot"]:e for e in scheduled_entries(ledger)}
    last_due = min(now, end)
    slot = first_slot
    missing = Counter()
    while nominal(slot) < last_due:
        entry = entries.get(slot, {})
        if str(entry.get("message_id")) not in seen_ids and nominal(slot) + CADENCE <= now:
            missing[entry.get("status", "нет записи")] += 1
        slot += 1
    lines += ["## Полнота данных", "", f"Плановые слоты без подтверждённой публикации внутри периода: {sum(missing.values())}."]
    if missing:
        lines.append(", ".join(f"{k} — {v}" for k,v in sorted(missing.items())) + ".")
    lines += ["", "Задержка показана от планового слота до фактической отправки. По этим наблюдениям нельзя отделить влияние часа от темы карточки и состава аудитории.",
              "", "| Пост | Слот, МСК | Выход, МСК | Задержка, мин | Просмотры и время замера, МСК |",
              "|---|---|---|---:|---|"]
    for key, sample, published, result in details:
        due = nominal(sample["slot"])
        delay = f"{(published-due).total_seconds()/60:.1f}" if published else "—"
        lines.append(f"| [№{key}](https://t.me/GoTodayGo/{key}) | {msk(due)} | {msk(published) if published else '—'} | {delay} | {result} |")
    return "\n".join(lines) + "\n"


def write_changed(path, text):
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        pending = path.with_suffix(path.suffix + ".tmp")
        pending.write_text(text, encoding="utf-8")
        pending.replace(path)


def main():
    path = ROOT / "experiment-state.json"
    state = validate(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else new_state()
    ledger = json.loads((ROOT / "publication-state.json").read_text(encoding="utf-8"))
    now = datetime.now(UTC)
    collect(state, ledger, now)
    validate(state)
    report_text = report(state, ledger, now)
    write_changed(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    write_changed(ROOT / "experiment-report.md", report_text)


if __name__ == "__main__":
    main()
