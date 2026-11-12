"""
my_tasks.py — единый лист «Мои задачи — ФИО» из трёх источников.

Источники строк:
  • protocol — задачи из протоколов встреч (листы MYmeet), где пользователь
    распознан как ответственный (ФИО, сокращения, инициалы, падежи, «Speaker A»).
    Новые такие задачи получают статус «Требует верификации»; верификация —
    ручное изменение статуса (в «Мои задачи» или в листе протокола).
  • gtasks   — все задачи Google Tasks пользователя (двусторонняя синхронизация
    статусов, ежедневно в 19:00 по часовому поясу пользователя).
  • telegram — пересланные боту сообщения (без подтверждения).

Хранилище: таблица SQLite `my_tasks` (db.py) — источник истины для строк
gtasks/telegram и учёт статусов для всех строк.  Лист — редактируемое
представление: при каждой записи бот сначала читает колонку «Статус»
и принимает ручные изменения.

Сверка статуса (3-сторонняя) по каждой строке:
  sheet_status       — что бот записал в лист в прошлый раз;
  ext_status         — что бот видел во внешнем источнике в прошлый раз
                       (сырой статус Google Tasks или статус листа протокола);
  ext_synced_status  — канонический статус на момент последней сверки
                       с Google Tasks (чтобы понять, менялся ли он локально).
Ручное изменение в листе «Мои задачи» имеет приоритет над изменением во
внешнем источнике, если они случились между двумя синхронизациями.

Защиты:
  • дубли — UNIQUE(telegram_id, source_key) в БД + уникальный ключ строки листа;
  • массовые удаления — бот никогда не удаляет задачи в Google Tasks и строки
    в БД; пропавшие из Google Tasks задачи помечаются «Отменено» только если
    их немного (см. MASS_MISSING_*), иначе изменения не применяются;
    лист не перезаписывается, если строк стало меньше половины (mymeet_sync).

Модуль не импортирует telegram/pytz/googleapiclient на уровне модуля —
чистые функции тестируются в CI без этих пакетов.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ── Статусы (совпадают с mymeet_sync.STATUS_VALUES) ──────────────────────────
ST_UNVERIFIED = "Требует верификации"
ST_IN_WORK = "В работе"
ST_DONE = "Выполнено"
ST_OVERDUE = "Просрочено"
ST_CANCELLED = "Отменено"
STATUS_VALUES = [ST_UNVERIFIED, ST_IN_WORK, ST_DONE, ST_OVERDUE, ST_CANCELLED]
_CLOSED = (ST_DONE, ST_CANCELLED)

SRC_PROTOCOL = "protocol"
SRC_GTASKS = "gtasks"
SRC_TELEGRAM = "telegram"
KEY_GTASKS = "gtasks:"
KEY_TELEGRAM = "tg:"
KEY_PROTOCOL = "p:"

# Пропавшие из Google Tasks задачи: если их больше порога — считаем это сбоем
# (частичный ответ API, смена аккаунта) и ничего не помечаем.
MASS_MISSING_ABS = 3        # до 3 задач — всегда допустимо
MASS_MISSING_SHARE = 0.2    # больше 3 — только если ≤ 20 % известных задач

TITLE_MAX_LEN = 1000


# ══════════════════════════════════════════════════════════════════════════
# 1. Сопоставление ответственного с ФИО пользователя
# ══════════════════════════════════════════════════════════════════════════

# Окончания падежей для имён/фамилий (длинные — первыми).
_CASE_ENDINGS = (
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими",
    "ой", "ей", "ою", "ею", "ую", "юю", "ая", "яя", "ом", "ем", "ым", "им",
    "ах", "ях",
    "а", "я", "у", "ю", "е", "ы", "и", "о", "й", "ь",
)
_MIN_STEM = 3
# «Speaker A», «Speaker 1», «Спикер 2», «Участник B» — нераспознанный спикер MYmeet
_SPEAKER_RE = re.compile(
    r"^(?:speaker|спикер|участник|говорящий)\s*[-_#№]?\s*[a-zа-я0-9]{1,3}$",
    re.IGNORECASE,
)
_PARTS_SPLIT_RE = re.compile(r"\s*[,;/]\s*|\s+и\s+", re.IGNORECASE)


def _norm_person(s) -> str:
    """Как mymeet_sync._norm_person: без регистра и пробелов."""
    return "".join(str(s or "").casefold().split())


def _norm_word(w: str) -> str:
    return w.casefold().replace("ё", "е")


def _word_forms(w: str) -> set:
    """Слово + основы без падежного окончания (основа ≥ 3 букв)."""
    forms = {w}
    for e in _CASE_ENDINGS:
        if w.endswith(e) and len(w) - len(e) >= _MIN_STEM:
            forms.add(w[: -len(e)])
    return forms


def _tokens(s: str) -> list:
    """Разбивает строку на токены [('word'|'init', value)].
    «Петров И.С.» → word петров, init и, init с.
    «ИС» (2–3 заглавные без точек) → инициалы."""
    out = []
    for raw in re.split(r"[\s.]+", str(s or "")):
        raw = raw.strip("()[]\"'«»")
        if not raw:
            continue
        if len(raw) == 1 and raw.isalpha():
            out.append(("init", _norm_word(raw)))
        elif 2 <= len(raw) <= 3 and raw.isalpha() and raw.isupper():
            out.extend(("init", _norm_word(ch)) for ch in raw)
        else:
            out.append(("word", _norm_word(raw)))
    return out


def _person_matches(part: str, full_name: str) -> bool:
    """Нечёткое совпадение одной персоны с ФИО пользователя.

    Каждый токен ответственного должен соответствовать своему (разному) слову
    ФИО: слово — по совпадению основ (падежи), инициал — по первой букве.
    Нужен хотя бы один полный (не инициал) токен длиной ≥ 3.
    Порядок слов не важен («Петров Иван» == «Иван Петров»).
    """
    user = [v for k, v in _tokens(full_name) if k == "word"]
    toks = _tokens(part)
    if not user or not toks or len(toks) > len(user):
        return False
    words = [v for k, v in toks if k == "word"]
    if not words or all(len(w) < _MIN_STEM for w in words):
        return False
    user_forms = [_word_forms(u) for u in user]
    for perm in itertools.permutations(range(len(user)), len(toks)):
        ok = True
        for (kind, val), ui in zip(toks, perm):
            if kind == "init":
                if not user[ui].startswith(val):
                    ok = False
                    break
            elif not (_word_forms(val) & user_forms[ui]):
                ok = False
                break
        if ok:
            return True
    return False


def is_speaker_label(s: str) -> bool:
    return bool(_SPEAKER_RE.match(str(s or "").strip()))


def match_assignee(assignee: str, full_name: str, allow_speaker: bool = False):
    """Распознаёт, назначена ли задача пользователю.

    Возвращает 'exact' | 'fuzzy' | 'speaker' | None.
      exact   — ФИО совпадает (без регистра/пробелов);
      fuzzy   — сокращение, инициалы, падеж, другой порядок слов, одна фамилия;
      speaker — нераспознанный спикер («Speaker A»); только при allow_speaker
                (встреча пользователя), т.к. это лишь возможное совпадение.
    Несколько ответственных через «,», «;», «/», «и» — проверяется каждый.
    """
    a = str(assignee or "").strip()
    if not a or not _norm_person(full_name):
        return None
    if _norm_person(a) == _norm_person(full_name):
        return "exact"
    parts = [p for p in _PARTS_SPLIT_RE.split(a) if p.strip()]
    if any(_person_matches(p, full_name) for p in parts):
        return "fuzzy"
    if allow_speaker and any(is_speaker_label(p) for p in parts):
        return "speaker"
    return None


# ══════════════════════════════════════════════════════════════════════════
# 2. Срок из текста
# ══════════════════════════════════════════════════════════════════════════

_NUM_WORDS = {
    "один": 1, "одну": 1, "одна": 1, "два": 2, "две": 2, "пару": 2, "три": 3,
    "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
    "десять": 10,
}
_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_MONTH_RE = (r"(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|"
             r"октября|ноября|декабря)")
_WEEKDAY_STEMS = (("понедельник", 0), ("вторник", 1), ("сред", 2), ("четверг", 3),
                  ("пятниц", 4), ("суббот", 5), ("воскресен", 6))
_PREP = r"(?:к|до|в|во|на|по|не\s+позднее|не\s+позже|срок(?:ом)?:?|дедлайн:?)"

_RE_REL_DAY = re.compile(r"(?<![\w-])(послезавтра|завтра|сегодня)(?![\w-])", re.I)
_RE_IN_N = re.compile(
    r"(?<![\w-])через\s+(\d{1,3}|" + "|".join(_NUM_WORDS) + r")?\s*"
    r"(дн(?:я|ей)|день|недел[юиь]|недел[ья]|месяц(?:а|ев)?)(?![\w-])", re.I)
_RE_WEEKDAY = re.compile(
    r"(?<![\w-])" + _PREP + r"\s+(понедельник\w*|вторник\w*|сред[аеуы]|четверг\w*|"
    r"пятниц[аеуы]|суббот[аеуы]|воскресень[еяю])(?![\w-])", re.I)
_RE_NUMERIC = re.compile(
    r"(?<![\w-])" + _PREP + r"\s*(\d{1,2})[./](\d{1,2})(?:[./](\d{2}|\d{4}))?(?!\d)", re.I)
_RE_TEXT_DATE = re.compile(
    r"(?<![\w-])" + _PREP + r"\s+(\d{1,2})\s+" + _MONTH_RE + r"(?:\s+(\d{4}))?", re.I)
_RE_END_OF = re.compile(
    r"(?<![\w-])(?:к|до)\s+конц[уа]\s+(дня|недели|месяца)(?![\w-])", re.I)


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    return d


def _future_ymd(base: date, day: int, month: int, year: int | None) -> date | None:
    try:
        if year is not None:
            if year < 100:
                year += 2000
            return date(year, month, day)
        cand = date(base.year, month, day)
        if cand < base:
            cand = date(base.year + 1, month, day)
        return cand
    except ValueError:
        return None


def parse_deadline(text: str, base: date) -> date | None:
    """Ищет срок в тексте относительно даты base (дата сообщения).

    Поддерживает: сегодня / завтра / послезавтра; через N дней / неделю /
    N недель / месяц (числом или словом); к/до/в пятницу (ближайшая будущая,
    тот же день недели → через неделю); до 15.11[.2026]; к 15 ноября [2026];
    до конца дня / недели / месяца.  При нескольких — первое по тексту.
    """
    if not text:
        return None
    found: list = []   # (position, date)

    for m in _RE_REL_DAY.finditer(text):
        w = m.group(1).lower()
        found.append((m.start(), base + timedelta(days={"сегодня": 0, "завтра": 1,
                                                        "послезавтра": 2}[w])))
    for m in _RE_IN_N.finditer(text):
        raw_n, unit = (m.group(1) or "").lower(), m.group(2).lower()
        n = int(raw_n) if raw_n.isdigit() else _NUM_WORDS.get(raw_n, 1)
        if unit.startswith("недел"):
            d = base + timedelta(weeks=n)
        elif unit.startswith("месяц"):
            d = _add_months(base, n)
        else:
            d = base + timedelta(days=n)
        found.append((m.start(), d))
    for m in _RE_WEEKDAY.finditer(text):
        w = m.group(1).lower()
        wd = next(i for stem, i in _WEEKDAY_STEMS if w.startswith(stem))
        delta = (wd - base.weekday()) % 7 or 7
        found.append((m.start(), base + timedelta(days=delta)))
    for m in _RE_NUMERIC.finditer(text):
        d = _future_ymd(base, int(m.group(1)), int(m.group(2)),
                        int(m.group(3)) if m.group(3) else None)
        if d:
            found.append((m.start(), d))
    for m in _RE_TEXT_DATE.finditer(text):
        mon_word = m.group(2).lower()
        month = next(v for k, v in _MONTHS.items() if mon_word.startswith(k))
        d = _future_ymd(base, int(m.group(1)), month,
                        int(m.group(3)) if m.group(3) else None)
        if d:
            found.append((m.start(), d))
    for m in _RE_END_OF.finditer(text):
        unit = m.group(1).lower()
        if unit == "дня":
            d = base
        elif unit == "недели":
            d = base + timedelta(days=max(0, 4 - base.weekday()))   # пятница
        else:
            d = _add_months(base.replace(day=1), 1) - timedelta(days=1)
        found.append((m.start(), d))

    if not found:
        return None
    return min(found, key=lambda x: x[0])[1]


def fmt_date(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else ""


# ══════════════════════════════════════════════════════════════════════════
# 3. Пересланные сообщения Telegram
# ══════════════════════════════════════════════════════════════════════════

def _tz(tz_name: str | None):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name or "Europe/Moscow")
    except Exception:
        return timezone(timedelta(hours=3))


def _person_name(user) -> str:
    if user is None:
        return ""
    name = (getattr(user, "full_name", None)
            or " ".join(x for x in (getattr(user, "first_name", "") or "",
                                    getattr(user, "last_name", "") or "") if x)).strip()
    uname = getattr(user, "username", None)
    if uname:
        return f"{name} (@{uname})" if name else f"@{uname}"
    return name


def _with_signature(title: str, sig) -> str:
    title = (title or "").strip()
    return f"{title} ({sig})" if sig and title else (title or (sig or ""))


def forward_info(message, tz_name: str | None = None) -> dict | None:
    """Данные задачи из пересланного сообщения (python-telegram-bot Message).

    Возвращает dict(title, assigner, task_date, deadline, source_key) или
    None, если сообщение не переслано или это часть альбома без подписи.
    Постановщик и дата — из forward_origin (если Telegram их отдал),
    иначе — дата самого сообщения.  Срок ищется относительно даты оригинала.
    """
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None
    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    if not text and getattr(message, "media_group_id", None):
        return None   # вторая и далее картинки альбома — без отдельной задачи

    otype = str(getattr(origin, "type", "") or "").lower()
    assigner, ident = "", ""
    if otype.endswith("hidden_user"):
        assigner = getattr(origin, "sender_user_name", "") or ""
        ident = f"h:{assigner}"
    elif otype.endswith("user"):
        u = getattr(origin, "sender_user", None)
        assigner = _person_name(u)
        ident = f"u:{getattr(u, 'id', '')}"
    elif otype.endswith("channel"):
        chat = getattr(origin, "chat", None)
        assigner = _with_signature(getattr(chat, "title", "") or "",
                                   getattr(origin, "author_signature", None))
        ident = f"c:{getattr(chat, 'id', '')}:{getattr(origin, 'message_id', '')}"
    elif otype.endswith("chat"):
        chat = getattr(origin, "sender_chat", None)
        assigner = _with_signature(getattr(chat, "title", "") or "",
                                   getattr(origin, "author_signature", None))
        ident = f"g:{getattr(chat, 'id', '')}"

    tz = _tz(tz_name)
    odate = getattr(origin, "date", None) or getattr(message, "date", None)
    if isinstance(odate, datetime):
        if odate.tzinfo is None:
            odate = odate.replace(tzinfo=timezone.utc)
        local = odate.astimezone(tz)
        ts = int(odate.timestamp())
    else:
        local = datetime.now(tz)
        ts = 0
    base = local.date()

    if not text:
        kind = next((k for k in ("photo", "video", "voice", "audio", "document",
                                 "sticker", "video_note", "animation", "location",
                                 "contact", "poll") if getattr(message, k, None)), "")
        text = f"Пересланное сообщение{f' ({kind})' if kind else ''}"
        if assigner:
            text += f" от {assigner}"

    deadline = parse_deadline(text, base)
    if otype.endswith("channel"):
        key_src = ident    # канал: id чата + id сообщения однозначны
    else:
        key_src = f"{ident}|{ts}|{text}"
    digest = hashlib.sha256(key_src.encode("utf-8")).hexdigest()[:24]
    return {
        "title": text[:TITLE_MAX_LEN],
        "assigner": assigner,
        "task_date": fmt_date(base),
        "deadline": fmt_date(deadline),
        "source_key": KEY_TELEGRAM + digest,
    }


def new_telegram_record(info: dict, full_name: str) -> dict:
    return {
        "source": SRC_TELEGRAM,
        "source_key": info["source_key"],
        "title": info["title"],
        "origin": "Telegram" + (f" — {info['assigner']}" if info.get("assigner") else ""),
        "assignee": full_name or "",
        "task_date": info.get("task_date", ""),
        "deadline": info.get("deadline", ""),
        "status": ST_IN_WORK,
        "verified": 1,
    }


# ══════════════════════════════════════════════════════════════════════════
# 4. Сверка статусов
# ══════════════════════════════════════════════════════════════════════════

def gt_raw_status(item: dict) -> str:
    return "completed" if item.get("status") == "completed" else "needsAction"


def status_to_gt(status: str) -> str:
    """«Выполнено»/«Отменено» → completed, остальные → needsAction."""
    return "completed" if status in _CLOSED else "needsAction"


def _gt_due(item: dict) -> str:
    due = str(item.get("due") or "")[:10]
    try:
        return datetime.strptime(due, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return ""


def row_key(row) -> str | None:
    """Ключ строки листа «Мои задачи» (A–H) для чтения статусов.
    H содержит ключ gtasks:/tg:, иначе ключ протокола из ячеек."""
    cells = [str(c).strip() if c is not None else "" for c in (row or [])]
    cells += [""] * (8 - len(cells))
    h = cells[7]
    if h.startswith((KEY_GTASKS, KEY_TELEGRAM)):
        return h
    date_, meeting, task, assignee = cells[1], cells[2], cells[3], cells[4]
    if not date_ or not task:
        return None
    base = h or f"~{meeting.lower()}"
    return f"{KEY_PROTOCOL}{base}|{date_}|{task.lower()}|{assignee.lower()}"


def sheet_statuses(my_rows) -> dict:
    """{key: status} из текущего листа «Мои задачи» (без заголовка)."""
    out: dict = {}
    for row in (my_rows or [])[1:]:
        k = row_key(row)
        if not k:
            continue
        st = str(row[6]).strip() if len(row) > 6 and row[6] is not None else ""
        if k not in out:
            out[k] = st
    return out


def apply_sheet_edit(rec: dict, sheet_val: str | None) -> bool:
    """Ручное изменение статуса в листе «Мои задачи» → в запись.
    Изменением считается валидный статус, отличный от записанного ботом."""
    if not sheet_val or sheet_val not in STATUS_VALUES:
        return False
    if rec.get("sheet_status") is None or sheet_val == rec.get("sheet_status"):
        return False
    if sheet_val != rec.get("status"):
        rec["status"] = sheet_val
    rec["verified"] = 1
    rec["_dirty"] = True
    return True


def reconcile_protocol(records: dict, candidates: list, sheet_vals: dict,
                       stats: dict) -> list:
    """Строки протоколов (формат mymeet_sync._build_my_tasks_rows) → записи.

    Новая задача: «Требует верификации», если в протоколе статус не задан
    вручную; иначе статус протокола (уже проверена там).  Существующая:
    ручная правка в «Мои задачи» важнее, затем изменение статуса в протоколе.
    Возвращает ключи активных (видимых в этом прогоне) протокольных задач.
    """
    active: list = []
    seen: set = set()
    for row in candidates:
        key = row_key(row)
        if not key or key in seen:
            continue
        seen.add(key)
        active.append(key)
        _n, date_, meeting, task, assignee, deadline, pstatus, _mid = (list(row) + [""] * 8)[:8]
        pstatus = str(pstatus or "").strip()
        rec = records.get(key)
        if rec is None:
            manual = pstatus not in ("", ST_UNVERIFIED) and pstatus in STATUS_VALUES
            rec = {
                "source": SRC_PROTOCOL, "source_key": key,
                "status": pstatus if manual else ST_UNVERIFIED,
                "verified": 1 if manual else 0,
                "ext_status": pstatus, "sheet_status": None,
                "_dirty": True,
            }
            records[key] = rec
            stats["protocol_new"] = stats.get("protocol_new", 0) + 1
        else:
            if not apply_sheet_edit(rec, sheet_vals.get(key)):
                if (pstatus in STATUS_VALUES and pstatus != (rec.get("ext_status") or "")
                        and pstatus != rec.get("status")):
                    rec["status"] = pstatus
                    if pstatus != ST_UNVERIFIED:
                        rec["verified"] = 1
                    rec["_dirty"] = True
            if (rec.get("ext_status") or "") != pstatus:
                rec["ext_status"] = pstatus
                rec["_dirty"] = True
        for field, val in (("title", task), ("origin", meeting), ("assignee", assignee),
                           ("task_date", date_), ("deadline", deadline)):
            val = str(val or "")
            if rec.get(field) != val:
                rec[field] = val
                rec["_dirty"] = True
    return active


def reconcile_gtasks(records: dict, items: list, complete: bool, full_name: str,
                     today: str, stats: dict, warnings: list) -> list:
    """Google Tasks → записи; возвращает список push-операций
    [(key, list_id, task_id, gt_status)] для отправки в Google Tasks.

    • новые задачи импортируются (completed → «Выполнено», иначе «В работе»);
    • изменение в Google Tasks при неизменном локальном статусе — принимается;
    • локальное изменение (открыта ↔ закрыта) — отправляется в GT;
      если GT тоже изменился и не совпадает — побеждает «Мои задачи»;
    • смена статуса внутри класса («В работе» → «Просрочено») в GT не уходит;
    • задача пропала из GT — «Отменено», но только если пропавших немного и
      ответ API полный; иначе — ничего не меняем (защита от массовых удалений).
    """
    pushes: list = []
    seen: set = set()
    for it in items:
        tid = it.get("id")
        title = str(it.get("title") or "").strip()
        if not tid or not title:
            continue
        key = KEY_GTASKS + tid
        if key in seen:
            continue        # дубль в ответе API (задача в двух списках/страницах)
        seen.add(key)
        raw = gt_raw_status(it)
        origin = "Google Tasks" + (f" — {it['_listTitle']}" if it.get("_listTitle") else "")
        rec = records.get(key)
        if rec is None:
            st = ST_DONE if raw == "completed" else ST_IN_WORK
            rec = {
                "source": SRC_GTASKS, "source_key": key, "status": st, "verified": 1,
                "ext_status": raw, "ext_synced_status": st, "sheet_status": None,
                "task_date": today, "_dirty": True,
            }
            records[key] = rec
            stats["gtasks_new"] = stats.get("gtasks_new", 0) + 1
        else:
            if rec.get("missing_since"):
                rec["missing_since"] = None
                rec["_dirty"] = True
            # Локальное изменение значимо для GT, только если сменился «класс»
            # (открыта/закрыта): «В работе» → «Просрочено» в GT не отражается.
            synced = rec.get("ext_synced_status")
            local_changed = (synced is None
                             or status_to_gt(rec.get("status")) != status_to_gt(synced))
            remote_changed = raw != rec.get("ext_status")
            if remote_changed and not local_changed:
                if raw == "completed" and rec["status"] != ST_DONE:
                    rec["status"] = ST_DONE
                    stats["gtasks_status_in"] = stats.get("gtasks_status_in", 0) + 1
                elif raw == "needsAction" and rec["status"] in _CLOSED:
                    rec["status"] = ST_IN_WORK
                    stats["gtasks_status_in"] = stats.get("gtasks_status_in", 0) + 1
            elif local_changed and status_to_gt(rec["status"]) != raw:
                pushes.append((key, it.get("_listId"), tid, status_to_gt(rec["status"])))
            rec["ext_status"] = raw
            rec["ext_synced_status"] = rec["status"]
            rec["_dirty"] = True
        for field, val in (("title", title[:TITLE_MAX_LEN]), ("origin", origin),
                           ("assignee", full_name or ""), ("deadline", _gt_due(it)),
                           ("gtasks_list_id", it.get("_listId")),
                           ("gtasks_task_id", tid)):
            if rec.get(field) != val:
                rec[field] = val
                rec["_dirty"] = True

    known = [k for k, r in records.items()
             if r.get("source") == SRC_GTASKS and not r.get("missing_since")]
    missing = [k for k in known if k not in seen]
    if missing:
        if not complete:
            warnings.append("Google Tasks: ответ неполный — удалённые задачи не обработаны")
        elif (len(missing) > MASS_MISSING_ABS
              and len(missing) > MASS_MISSING_SHARE * len(known)):
            warnings.append(
                f"Google Tasks: пропали {len(missing)} из {len(known)} задач — похоже "
                f"на сбой, изменения не применены (защита от массового удаления)")
            stats["gtasks_mass_missing"] = len(missing)
        else:
            now = time.time()
            for k in missing:
                r = records[k]
                r["missing_since"] = now
                if r.get("status") not in _CLOSED:
                    r["status"] = ST_CANCELLED
                r["ext_synced_status"] = r["status"]
                r["_dirty"] = True
            stats["gtasks_missing"] = len(missing)
    return pushes


def _deadline_key(d: str):
    try:
        return (0, datetime.strptime((d or "").strip(), "%d.%m.%Y"))
    except ValueError:
        return (1, datetime.max)


def build_sheet_rows(records: dict, active_protocol: list) -> list:
    """Строки листа: все gtasks/telegram + активные протокольные.
    [№, Дата, Источник, Задача, Ответственный, Срок, Статус, ключ]
    Для протокольных H = mtg_id (как раньше), иначе H = ключ."""
    rows = []
    active = set(active_protocol)
    for key, r in records.items():
        src = r.get("source")
        if src == SRC_PROTOCOL:
            if key not in active:
                continue
            base = key[len(KEY_PROTOCOL):].split("|", 1)[0]
            h = "" if base.startswith("~") else base
        else:
            h = key
        rows.append([None, r.get("task_date") or "", r.get("origin") or "",
                     r.get("title") or "", r.get("assignee") or "",
                     r.get("deadline") or "", r.get("status") or ST_IN_WORK, h, key])
    rows.sort(key=lambda x: (_deadline_key(x[5]), _deadline_key(x[1]), x[3]))
    for i, x in enumerate(rows, 1):
        x[0] = i
    return rows


# ══════════════════════════════════════════════════════════════════════════
# 5. Построитель листа (вызывается из mymeet_sync._sync_my_tasks_and_colors)
# ══════════════════════════════════════════════════════════════════════════

def _db():
    import db
    return db


class MyTasksBuilder:
    """Формирует строки «Мои задачи» и сохраняет сверку в БД.

    mymeet_sync вызывает build(protocol_sheets, my_rows) → rows, затем
    commit() только после успешной записи листа.
    gtasks: (items, complete) — если задан, выполняется сверка с Google Tasks;
    pusher(telegram_id, list_id, task_id, gt_status) -> bool — запись статуса в GT.
    """

    def __init__(self, telegram_id: int, full_name: str, *, gtasks=None,
                 pusher=None, dbm=None, tz_name: str | None = None):
        self.telegram_id = telegram_id
        self.full_name = (full_name or "").strip()
        self.gtasks = gtasks
        self.pusher = pusher
        self.dbm = dbm
        self.tz_name = tz_name
        self.built = False
        self.stats: dict = {}
        self.warnings: list = []
        self._own_meetings: set = set()
        self._written: dict = {}

    def _store(self):
        return self.dbm or _db()

    def add_own_meetings(self, ids) -> None:
        ids = {str(i) for i in (ids or []) if i}
        if not ids:
            return
        self._own_meetings |= ids
        try:
            self._store().my_meetings_add(self.telegram_id, ids)
        except Exception as e:
            logger.warning("my_meetings_add failed for %s: %s", self.telegram_id, e)

    def matcher(self):
        """matcher(assignee, mtg_id) → bool для mymeet_sync._build_my_tasks_rows."""
        own = set(self._own_meetings)
        try:
            own |= set(self._store().my_meetings_get(self.telegram_id))
        except Exception as e:
            logger.warning("my_meetings_get failed for %s: %s", self.telegram_id, e)
        name = self.full_name

        def _m(assignee, mid) -> bool:
            return match_assignee(assignee, name, allow_speaker=bool(mid and mid in own)) \
                is not None
        return _m

    def build(self, protocol_candidates: list | None, my_rows: list | None,
              sheet_available: bool = True) -> list:
        """protocol_candidates — строки из _build_my_tasks_rows (уже отфильтрованы
        matcher-ом); my_rows — текущий лист «Мои задачи» (A:H) или None."""
        store = self._store()
        records = {r["source_key"]: r for r in store.my_tasks_load(self.telegram_id)}
        sheet_vals = sheet_statuses(my_rows) if my_rows else {}

        # Ручные правки статуса для gtasks/telegram (протокольные — внутри reconcile)
        for key, rec in records.items():
            if rec.get("source") != SRC_PROTOCOL:
                apply_sheet_edit(rec, sheet_vals.get(key))

        active = (reconcile_protocol(records, protocol_candidates or [], sheet_vals, self.stats)
                  if sheet_available else [])

        if self.gtasks is not None:
            items, complete = self.gtasks
            today = fmt_date(datetime.now(_tz(self.tz_name)).date())
            pushes = reconcile_gtasks(records, items, complete, self.full_name, today,
                                      self.stats, self.warnings)
            for key, list_id, task_id, gt_status in pushes:
                ok = False
                try:
                    ok = bool(self.pusher and self.pusher(self.telegram_id, list_id,
                                                          task_id, gt_status))
                except Exception as e:
                    logger.warning("gtasks push failed %s: %s", task_id, e)
                rec = records[key]
                if ok:
                    rec["ext_status"] = gt_status
                    self.stats["gtasks_status_out"] = self.stats.get("gtasks_status_out", 0) + 1
                else:
                    # Повторим в следующий раз: локальное изменение остаётся «несверенным»
                    rec["ext_synced_status"] = None
                    self.stats["gtasks_push_failed"] = self.stats.get("gtasks_push_failed", 0) + 1
                rec["_dirty"] = True
            if self.stats.get("gtasks_push_failed"):
                self.warnings.append(
                    f"Google Tasks: не удалось обновить {self.stats['gtasks_push_failed']} "
                    f"задач — повторю при следующей синхронизации")

        dirty = [r for r in records.values() if r.pop("_dirty", False)]
        if dirty:
            store.my_tasks_upsert(self.telegram_id, dirty)
        rows = build_sheet_rows(records, active)
        self._written = {r[8]: r[6] for r in rows}
        self.built = True
        return [r[:8] for r in rows]

    def commit(self) -> None:
        """Лист записан — запоминаем записанные статусы."""
        if self._written:
            self._store().my_tasks_mark_written(self.telegram_id, self._written)


# ══════════════════════════════════════════════════════════════════════════
# 6. Оркестрация (синхронно; main.py вызывает в executor)
# ══════════════════════════════════════════════════════════════════════════

def _row_get(row, key, default=None):
    try:
        v = row[key]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


def run_sync(telegram_id: int, with_gtasks: bool = True, *, dbm=None, gt=None,
             ms=None) -> dict:
    """Сверка «Мои задачи»: Google Tasks (если with_gtasks) + лист.

    Возвращает {"text": отчёт, "warnings": [...], "stats": {...},
                "sheet_count": int|None, "anomaly": bool}.
    """
    store = dbm or _db()
    user = store.get_user(telegram_id)
    full_name = str(_row_get(user, "mymeet_full_name", "") or "").strip()
    sheet_id = str(_row_get(user, "mymeet_spreadsheet_id", "") or "").strip()
    tz_name = _row_get(user, "timezone", None)
    warnings: list = []

    gt_data = None
    if with_gtasks:
        if gt is None:
            import gtasks as gt   # noqa: F811 — ленивый импорт (googleapiclient)
        items, err, complete = gt.list_all_tasks_full(telegram_id)
        if err == "no_scope":
            warnings.append("Google Tasks: нет доступа — переподключите Google: /connect")
        elif err == "no_auth":
            warnings.append("Google Tasks: Google не подключён — /connect")
        elif err:
            warnings.append("Google Tasks: ошибка API — задачи не синхронизированы")
        else:
            gt_data = (items, complete)

    builder = MyTasksBuilder(telegram_id, full_name, gtasks=gt_data,
                             pusher=getattr(gt, "set_task_status", None) if gt else None,
                             dbm=store, tz_name=tz_name)
    sheet_count, sheet_title = None, None
    if sheet_id and full_name:
        if ms is None:
            import mymeet_sync as ms   # noqa: F811
        try:
            w, sheet_count, sheet_title = ms.refresh_my_tasks_sheet(sheet_id, full_name, builder)
            warnings.extend(w)
        except Exception as e:
            logger.warning("my_tasks sheet refresh failed for %s: %s", telegram_id, e)
            warnings.append(f"Лист «Мои задачи»: {e}")
    if not builder.built:
        # Листа нет/ошибка — сверяем только БД (Google Tasks), лист не трогаем
        builder.build(None, None, sheet_available=False)
    warnings = builder.warnings + warnings

    s = builder.stats
    lines = []
    if with_gtasks and gt_data is not None:
        lines.append(
            f"✅ Google Tasks: получено {len(gt_data[0])}, новых {s.get('gtasks_new', 0)}, "
            f"статусов из GT {s.get('gtasks_status_in', 0)}, "
            f"в GT {s.get('gtasks_status_out', 0)}"
            + (f", помечено удалёнными {s['gtasks_missing']}" if s.get("gtasks_missing") else ""))
    if sheet_count is not None:
        lines.append(f"👤 Лист «{sheet_title}»: {sheet_count} задач")
    elif not sheet_id:
        lines.append("ℹ️ Таблица не настроена — задачи сохранены, лист появится после /setup_mymeet")
    elif not full_name:
        lines.append("ℹ️ Укажите ФИО: /setname Имя Фамилия — тогда появится лист «Мои задачи»")
    if warnings:
        lines.append("⚠️ " + "\n⚠️ ".join(warnings))
    anomaly = bool(s.get("gtasks_mass_missing")) or any("массов" in w for w in warnings)
    return {"text": "\n".join(lines) or "✅ Готово", "warnings": warnings, "stats": s,
            "sheet_count": sheet_count, "anomaly": anomaly}


def add_forwarded(telegram_id: int, info: dict, *, dbm=None) -> bool:
    """Сохраняет задачу из пересланного сообщения. True — создана, False — дубль."""
    store = dbm or _db()
    user = store.get_user(telegram_id)
    full_name = str(_row_get(user, "mymeet_full_name", "") or "").strip()
    return store.my_tasks_insert_new(telegram_id, new_telegram_record(info, full_name))
