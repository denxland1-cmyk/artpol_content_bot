"""Ежедневное напоминание бригадирам о недосланном контенте.

План работ берётся из Кроноса (записи услуги «Бригада», resourceTypeId 1818),
факт — из бакета artpol-content. Разница → личное сообщение бригадиру в этот бот.

Сверка идёт по паре (бригадир, дата): сколько заливок стояло в графике против
того, сколько объектов бригадир прислал в этот день. Адреса из Кроноса даются
в тексте, чтобы бригадир понял, о чём речь.
"""
import asyncio
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger("content-bot.reminders")
MSK = ZoneInfo("Europe/Moscow")

KRONOS_BASE = "https://genezis-platform-api.gnzs.ru/api/v1"
KRONOS_API_KEY = os.getenv("KRONOS_API_KEY", "")
KRONOS_FILIAL_ID = os.getenv("KRONOS_FILIAL_ID", "1654")
BRIGADE_RESOURCE_TYPE = 1818          # ресурсы-бригады (1817 — замерщики)
KRONOS_COMMENT_FIELD = "1379"

# Имя бригады в Кроносе → имя бригадира в этом боте (подтверждено Денисом 20.08.2026).
# Бригады вне карты (напр. «НЕ РАБОТАЕТ Паша») в напоминаниях не участвуют.
BRIGADE_TO_FOREMAN = {
    "Саня": "Саня",
    "Дмитрий": "Диман",
    "Гарик": "Женя",      # в переписке обращаемся «Женя», имя «Гарик» не показываем
}

REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "10"))     # 10:00 МСК
LOOKBACK_DAYS = int(os.getenv("REMINDER_LOOKBACK", "7"))  # окно догоняния
MAX_PER_OBJECT = 3                                        # сколько раз напоминать об объекте
STATE_KEY = "_state/reminders.json"


def _kronos_events(d_from: date, d_to: date) -> list:
    url = f"{KRONOS_BASE}/events?" + urllib.parse.urlencode(
        {"dateFrom": d_from.isoformat(), "dateTo": d_to.isoformat()})
    req = urllib.request.Request(url, headers={
        "X-API-KEY": KRONOS_API_KEY, "x-filial-id": KRONOS_FILIAL_ID})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode()).get("data", [])


def _plan(d_from: date, d_to: date) -> dict:
    """(бригадир, YYYY-MM-DD) → [описание объекта]."""
    plan = defaultdict(list)
    for ev in _kronos_events(d_from, d_to):
        brigades = [r.get("name") for r in (ev.get("_resources") or [])
                    if r.get("resourceTypeId") == BRIGADE_RESOURCE_TYPE]
        if not brigades:
            continue
        foreman = BRIGADE_TO_FOREMAN.get(brigades[0])
        if not foreman:
            continue
        values = ev.get("customFields", {}).get(KRONOS_COMMENT_FIELD, {}).get("values", [])
        title = (ev.get("name") or (values[0].get("value") if values else "") or "").strip()
        plan[(foreman, ev["dateFrom"])].append(title.replace("\n", " ")[:70] or "объект")
    return plan


def _submissions(s3, bucket: str, since: date) -> dict:
    """бригадир → [(дата папки, текст описания)] за период от `since`.

    Раньше сверка шла по паре (бригадир, дата папки). Но дата папки — это дата
    ОТПРАВКИ, а бригады выкладывают не с объекта: Саня 21.09 отработал два
    объекта и прислал их 22.09 в 05:59 и 06:01, перед выездом. Из-за этого
    приходили напоминания о том, что уже прислано. Поэтому собираем ещё и текст
    описания — по нему сверяем адрес, а не календарь.
    """
    subs = defaultdict(list)
    seen = set()
    desc_keys = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            parts = obj["Key"].split("/")
            if len(parts) < 3:
                continue
            folder = "/".join(parts[:3])
            m = re.match(r"(\d{2})\.(\d{2})_", parts[2])
            if not m:
                continue
            day = date.fromisoformat(f"{parts[1][:4]}-{m.group(2)}-{m.group(1)}")
            if day < since:
                continue
            if folder not in seen:
                seen.add(folder)
                subs[parts[0]].append([day, ""])
            if obj["Key"].endswith("писание.txt"):
                desc_keys[folder] = (parts[0], day, obj["Key"])
    # описания читаем только за нужный период — их немного
    for foreman, day, key in desc_keys.values():
        try:
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8", "replace")
        except Exception:
            continue
        for item in subs[foreman]:
            if item[0] == day and not item[1]:
                item[1] = body
                break
    return subs


_STOP = {"квартира", "коммерция", "объект", "первый", "второй", "третий",
         "улица", "город", "район", "участок", "основание", "этаж", "нижний"}


def _tokens(text: str) -> set:
    """Значимые слова адреса: длинные, без служебных. «Боталово», «Энергия»."""
    norm = re.sub(r"[^\w\s]+", " ", (text or "").lower().replace("ё", "е"))
    return {w for w in norm.split() if len(w) >= 5 and not w.isdigit() and w not in _STOP}


def _load_state(s3, bucket: str) -> dict:
    try:
        body = s3.get_object(Bucket=bucket, Key=STATE_KEY)["Body"].read()
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}


def _save_state(s3, bucket: str, state: dict) -> None:
    try:
        s3.put_object(Bucket=bucket, Key=STATE_KEY,
                      Body=json.dumps(state, ensure_ascii=False).encode("utf-8"),
                      ContentType="application/json")
    except Exception as e:
        logger.error("не удалось сохранить состояние напоминаний: %s", e)


MATCH_WINDOW_DAYS = 7          # столько дней после работы ещё ждём выкладку


def build_reminders(plan: dict, subs: dict, state: dict, today: date, start_from: date) -> dict:
    """Бригадир → список строк «дата — объект», о которых стоит напомнить.

    Объект считается закрытым, если у этого бригадира есть заливка, в описании
    которой встречается адрес из плана Кроноса, и сделана она в окне от дня
    работы до +7 дней. По календарю сверять нельзя: выкладывают на следующий
    день и позже, из-за чего бот просил прислать то, что уже прислано.
    """
    counts = state.get("counts", {})
    out = defaultdict(list)
    used = set()                 # одна заливка не должна закрывать два объекта
    for (foreman, day), objects in sorted(plan.items()):
        d = date.fromisoformat(day)
        if d >= today or d < start_from:
            continue             # сегодняшние ещё рано, дораннее — «чистый лист»
        items = subs.get(foreman, [])
        for title in objects:
            want = _tokens(title)
            matched = False
            for i, (sub_day, desc) in enumerate(items):
                if (foreman, i) in used:
                    continue
                if not (d - timedelta(days=1) <= sub_day <= d + timedelta(days=MATCH_WINDOW_DAYS)):
                    continue
                # адрес распознан — сверяем по нему; если нет, засчитываем любую
                # заливку в окне, чтобы не напоминать вслепую
                if (want and want & _tokens(desc)) or not want:
                    used.add((foreman, i))
                    matched = True
                    break
            if matched:
                continue
            key = f"{foreman}|{day}|{title[:30]}"
            if counts.get(key, 0) >= MAX_PER_OBJECT:
                continue
            counts[key] = counts.get(key, 0) + 1
            out[foreman].append(f"• {d:%d.%m} — {title}")
    state["counts"] = counts
    return out


def _text(lines: list) -> str:
    body = "\n".join(lines[:10])
    tail = f"\n…и ещё {len(lines) - 10}" if len(lines) > 10 else ""
    return ("📸 <b>Напоминание: не хватает контента</b>\n\n"
            f"{body}{tail}\n\n"
            "Пришлите, пожалуйста, фото по этим объектам: "
            "текст объекта → тип → фото → «Отправить».")


async def run_once(bot, s3_read, s3_write, bucket: str, foreman_ids: dict, start_from: date) -> int:
    """Одна проверка. Возвращает число отправленных напоминаний."""
    today = datetime.now(MSK).date()
    d_from = max(today - timedelta(days=LOOKBACK_DAYS), start_from)
    plan = await asyncio.to_thread(_plan, d_from, today)
    # заливки берём с запасом: объект могли выложить сильно позже дня работы
    since = d_from - timedelta(days=1)
    subs = await asyncio.to_thread(_submissions, s3_read, bucket, since)
    state = await asyncio.to_thread(_load_state, s3_read, bucket)

    reminders = build_reminders(plan, subs, state, today, start_from)
    delivered = 0
    for foreman, lines in reminders.items():
        chat_id = foreman_ids.get(foreman)
        if not chat_id:
            continue
        try:
            await bot.send_message(chat_id=chat_id, text=_text(lines), parse_mode="HTML")
            delivered += 1
            logger.info("напоминание отправлено: %s (%d объектов)", foreman, len(lines))
        except Exception as e:
            # чаще всего 403: бригадир ни разу не писал боту
            logger.error("не доставлено %s: %s", foreman, e)

    state["last_run"] = today.isoformat()
    await asyncio.to_thread(_save_state, s3_write, bucket, state)
    return delivered


async def reminder_loop(bot, s3_read, s3_write, bucket: str, foreman_ids: dict, start_from: date):
    """Раз в час просыпается; работает один раз в сутки в REMINDER_HOUR по МСК."""
    logger.info("напоминания включены: %02d:00 МСК, старт с %s", REMINDER_HOUR, start_from)
    while True:
        try:
            now = datetime.now(MSK)
            state = await asyncio.to_thread(_load_state, s3_read, bucket)
            if now.hour == REMINDER_HOUR and state.get("last_run") != now.date().isoformat():
                n = await run_once(bot, s3_read, s3_write, bucket, foreman_ids, start_from)
                logger.info("проверка контента выполнена, напоминаний: %d", n)
        except Exception as e:
            logger.error("цикл напоминаний: %s", e)
        await asyncio.sleep(1800)
