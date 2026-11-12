"""
my_tasks.py — единый лист «Мои задачи — ФИО» из трёх источников.

Источники строк:
  • protocol — задачи из протоколов встреч (листы MYmeet), где ответственный
    ДОСТОВЕРНО распознан как пользователь: полное ФИО (любой порядок, падежи),
    фамилия + инициалы.  Одна фамилия, одно имя, «Speaker A» и иные метки
    спикеров — НЕ распознаются (нет достоверной привязки к участнику).
    Новые задачи получают «Требует верификации»; бот этот статус сам не снимает,
    верификация — ручное изменение статуса в Sheets.
  • gtasks   — все задачи Google Tasks (все списки, выполненные, скрытые);
    двусторонняя синхронизация статусов ежедневно в 19:00 по поясу пользователя.
  • telegram — пересланные боту в личном чате сообщения (без подтверждения).

Хранилище: таблица SQLite `my_tasks` (db.py; текстовые поля зашифрованы
Fernet).  Лист — редактируемое представление: перед каждой записью бот читает
колонку «Статус» и принимает ручные изменения.

Google Tasks (ключ строки — gtasks:{tasklist_id}:{task_id}):
  • «Выполнено» ↔ completed, «В работе»/«Просрочено»/«Требует верификации» ↔
    needsAction (смена внутри класса в GT не отражается);
  • НОВОЕ ручное изменение на «Отменено» в листе → адресное удаление задачи
    в GT (только при подтверждённой привязке, только в сеансе сверки с GT,
    после повторного чтения листа; не при импорте/миграции/повторе);
  • задача удалена в GT → «Отменено» в листе (строка сохраняется), только после
    подтверждения адресным GET (404/deleted) и при полном ответе API;
  • «Отменено» никогда не превращается в «Выполнено».

Модуль не импортирует telegram/pytz/googleapiclient на уровне модуля.
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

SRC_PROTOCOL = "protocol"
SRC_GTASKS = "gtasks"
SRC_TELEGRAM = "telegram"
KEY_GTASKS = "gtasks:"
KEY_TELEGRAM = "tg:"
KEY_PROTOCOL = "p:"

GT_OPEN = "needsAction"
GT_DONE = "completed"
GT_DELETED = "deleted"          # внутренний ext_status: задачи в GT больше нет

# Пропавшие из GT / массовые отмены: больше порога — аномалия, ничего не делаем.
MASS_ABS = 3            # до 3 — допустимо всегда
MASS_SHARE = 0.2        # больше 3 — только если ≤ 20 % задач GT пользователя
MAX_DELETES_PER_RUN = 10

TITLE_MAX_LEN = 1000
ALBUM_PLACEHOLDER = "Пересланный альбом"

# Итог сверки
OUT_OK = "ok"                 # всё выполнено
OUT_PARTIAL = "partial"       # часть шагов не выполнена (повторим)
OUT_TRANSIENT = "transient"   # временная ошибка (повторим)
OUT_ANOMALY = "anomaly"       # опасная аномалия — изменения приостановлены
OUT_CONFIG = "config"         # нужно действие пользователя (/connect)


def _is_mass(n: int, total: int) -> bool:
    return n > MASS_ABS and n > MASS_SHARE * max(total, 1)


# ══════════════════════════════════════════════════════════════════════════
# 1. Сопоставление ответственного с ФИО пользователя
# ══════════════════════════════════════════════════════════════════════════

_CASE_ENDINGS = (
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими",
    "ой", "ей", "ою", "ею", "ую", "юю", "ая", "яя", "ом", "ем", "ым", "им",
    "ах", "ях",
    "а", "я", "у", "ю", "е", "ы", "и", "о", "й", "ь",
)
_MIN_STEM = 3
_PATRONYMIC_RE = re.compile(r"(ович|евич|ьич|ич|овна|евна|ична|инична)$")
_SURNAME_RE = re.compile(
    r"(ов|ев|ин|ын|ский|цкий|ская|цкая|ова|ева|ина|ына|ко|ук|юк|ян|янц|дзе|швили|ых|их)$")
_SPEAKER_RE = re.compile(
    r"^(?:speaker|спикер|участник|говорящий)\s*[-_#№]?\s*[a-zа-я0-9]{1,3}$", re.IGNORECASE)
_PARTS_SPLIT_RE = re.compile(r"\s*[,;/]\s*|\s+и\s+", re.IGNORECASE)


def _norm_person(s) -> str:
    """Как mymeet_sync._norm_person: без регистра и пробелов."""
    return "".join(str(s or "").casefold().split())


def _norm_word(w: str) -> str:
    return w.casefold().replace("ё", "е")


def _word_forms(w: str) -> set:
    forms = {w}
    for e in _CASE_ENDINGS:
        if w.endswith(e) and len(w) - len(e) >= _MIN_STEM:
            forms.add(w[: -len(e)])
    return forms


def _tokens(s: str) -> list:
    """[('word'|'init', value)]: «Петров И.С.» → петров, и, с; «ИС» → и, с."""
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


def user_name_parts(full_name: str) -> dict | None:
    """Разбор ФИО пользователя на фамилию / имя / отчество.

    2 слова: фамилия — слово с типичным окончанием фамилии, если такое одно,
    иначе второе (формат /setname «Имя Фамилия»).  3 слова: отчество по
    окончанию, имя — слово перед ним, фамилия — оставшееся.  Иначе None
    (допускается только точное совпадение ФИО).
    """
    words = [v for k, v in _tokens(full_name) if k == "word"]
    if len(words) == 2:
        a, b = words
        if any(_PATRONYMIC_RE.search(w) and len(w) > 4 for w in words):
            return None                      # «Иван Сергеевич» — нет фамилии
        sa, sb = bool(_SURNAME_RE.search(a)), bool(_SURNAME_RE.search(b))
        surname, first = (a, b) if (sa and not sb) else (b, a)
        return {"surname": surname, "first": first, "patronymic": None}
    if len(words) == 3:
        pi = [i for i, w in enumerate(words) if _PATRONYMIC_RE.search(w) and len(w) > 4]
        if len(pi) == 1 and pi[0] >= 1:
            p = pi[0]
            rest = [w for i, w in enumerate(words) if i not in (p, p - 1)]
            return {"surname": rest[0], "first": words[p - 1], "patronymic": words[p]}
    return None


def _word_match(a: str, u: str) -> str | None:
    if a == u:
        return "same"
    if _word_forms(a) & _word_forms(u):
        return "inflected"
    return None


def _person_matches(part: str, parts: dict) -> bool:
    """Достоверное совпадение одной персоны с ФИО пользователя.

    Обязательно: фамилия полным словом + имя (полностью или инициалом);
    отчество — необязательно (полностью или инициалом).  Лишних токенов нет.
    Падежная форма (не дословное совпадение) допустима только без инициалов:
    «Петрова Ивана» — да, «Петрова И.» — нет (может быть Ирина Петрова).
    """
    slots = [("surname", parts["surname"]), ("first", parts["first"])]
    if parts.get("patronymic"):
        slots.append(("patronymic", parts["patronymic"]))
    toks = _tokens(part)
    if not toks or len(toks) > len(slots):
        return False
    for perm in itertools.permutations(range(len(slots)), len(toks)):
        assigned, inflected, has_init, ok = {}, False, False, True
        for (kind, val), si in zip(toks, perm):
            name, uval = slots[si]
            if kind == "init":
                if not uval.startswith(val):
                    ok = False
                    break
                has_init = True
                assigned[name] = "init"
            else:
                m = _word_match(val, uval)
                if not m:
                    ok = False
                    break
                inflected |= m == "inflected"
                assigned[name] = "word"
        if (ok and assigned.get("surname") == "word" and "first" in assigned
                and not (inflected and has_init)):
            return True
    return False


def is_speaker_label(s: str) -> bool:
    return bool(_SPEAKER_RE.match(str(s or "").strip()))


def match_assignee(assignee: str, full_name: str):
    """'exact' | 'fuzzy' | None — назначена ли задача пользователю достоверно.

    exact — ФИО совпадает (без регистра/пробелов); fuzzy — другой порядок слов,
    падежи, фамилия + инициалы.  Метки спикеров («Speaker A») не совпадают
    никогда: MYmeet не даёт достоверной привязки метки к участнику.
    Несколько ответственных через «,», «;», «/», «и» — проверяется каждый.
    """
    a = str(assignee or "").strip()
    if not a or not _norm_person(full_name):
        return None
    if _norm_person(a) == _norm_person(full_name):
        return "exact"
    parts = user_name_parts(full_name)
    if not parts:
        return None
    for p in _PARTS_SPLIT_RE.split(a):
        p = p.strip()
        if not p or is_speaker_label(p):
            continue
        if _norm_person(p) == _norm_person(full_name) or _person_matches(p, parts):
            return "fuzzy"
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


def find_deadlines(text: str, base: date) -> list:
    """Все распознанные сроки [(позиция, дата)] относительно даты base."""
    if not text:
        return []
    found: list = []
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
    return found


def parse_deadline(text: str, base: date) -> date | None:
    """Однозначный срок или None.

    Поддерживает: сегодня / завтра / послезавтра; через N дней / неделю /
    N недель / месяц; к/до/в пятницу (ближайшая будущая; тот же день недели →
    через неделю); до 15.11[.2026]; к 15 ноября [2026]; до конца дня / недели /
    месяца.  Если найдено несколько РАЗНЫХ дат — срок неоднозначен → None
    (не угадываем).  Совпадающие упоминания одной даты допустимы.
    """
    dates = {d for _pos, d in find_deadlines(text, base)}
    return dates.pop() if len(dates) == 1 else None


def fmt_date(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else ""


# ══════════════════════════════════════════════════════════════════════════
# 3. Пересланные сообщения Telegram
# ══════════════════════════════════════════════════════════════════════════

_MEDIA_KINDS = ("photo", "video", "voice", "audio", "document", "sticker",
                "video_note", "animation", "location", "contact", "poll")


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


def _media(message) -> tuple:
    """(kind, file_unique_id) первого вложения сообщения."""
    for k in _MEDIA_KINDS:
        v = getattr(message, k, None)
        if not v:
            continue
        obj = v[-1] if isinstance(v, (list, tuple)) else v
        return k, str(getattr(obj, "file_unique_id", "") or "")
    return "", ""


def forward_info(message, tz_name: str | None = None) -> dict | None:
    """Данные задачи из пересланного сообщения (python-telegram-bot Message).

    None — сообщение не переслано.  Постановщик и дата — из forward_origin;
    если даты оригинала нет — дата пересылки.  Срок — относительно этой даты
    в часовом поясе пользователя.

    Ключ дедупликации (повторная пересылка того же сообщения → тот же ключ):
      канал — chat_id + message_id; иначе — отправитель + время оригинала +
      текст + file_unique_id вложения.  Если даты оригинала нет, ключ
      уникален для каждой пересылки (chat_id + message_id пересылки), чтобы
      не склеить разные сообщения.  Части одного альбома → одна задача.
    """
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None
    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    album = bool(getattr(message, "media_group_id", None))

    otype = str(getattr(origin, "type", "") or "").lower()
    assigner, ident = "", f"x:{otype}"
    if otype.endswith("hidden_user"):
        assigner = getattr(origin, "sender_user_name", "") or ""
        ident = f"h:{assigner}"
    elif otype.endswith("user"):
        u = getattr(origin, "sender_user", None)
        assigner = _person_name(u)
        ident = f"u:{getattr(u, 'id', '')}"
    elif otype.endswith("channel"):
        chat = getattr(origin, "chat", None)
        sig = getattr(origin, "author_signature", None)
        assigner = _with_signature(getattr(chat, "title", "") or "", sig)
        ident = f"c:{getattr(chat, 'id', '')}"
    elif otype.endswith("chat"):
        chat = getattr(origin, "sender_chat", None)
        sig = getattr(origin, "author_signature", None)
        assigner = _with_signature(getattr(chat, "title", "") or "", sig)
        ident = f"g:{getattr(chat, 'id', '')}:{sig or ''}"

    tz = _tz(tz_name)
    odate = getattr(origin, "date", None)
    fwd_date = getattr(message, "date", None)
    use = odate if isinstance(odate, datetime) else fwd_date
    if isinstance(use, datetime):
        if use.tzinfo is None:
            use = use.replace(tzinfo=timezone.utc)
        base = use.astimezone(tz).date()
    else:
        base = datetime.now(tz).date()

    kind, media_uid = _media(message)
    placeholder = not text
    if not text:
        noun = ALBUM_PLACEHOLDER if album else "Пересланное сообщение"
        text = f"{noun}{f' ({kind})' if kind else ''}"
        if assigner:
            text += f" от {assigner}"

    chat_id = getattr(getattr(message, "chat", None), "id", "") or getattr(message, "chat_id", "")
    fwd_id = getattr(message, "message_id", "")
    omid = getattr(origin, "message_id", None)
    if isinstance(odate, datetime) and odate.tzinfo is None:
        odate = odate.replace(tzinfo=timezone.utc)
    if not isinstance(odate, datetime):
        key_src = f"nodate|{chat_id}|{fwd_id}"           # не склеиваем разные
    elif album:
        key_src = f"album|{ident}|{int(odate.timestamp())}"
    elif otype.endswith("channel") and omid:
        key_src = f"{ident}:{omid}"
    else:
        key_src = f"{ident}|{int(odate.timestamp())}|{text}|{media_uid}"
    digest = hashlib.sha256(key_src.encode("utf-8")).hexdigest()[:32]
    deadline = parse_deadline(text, base)
    return {
        "title": text[:TITLE_MAX_LEN],
        "assigner": assigner,
        "task_date": fmt_date(base),
        "deadline": fmt_date(deadline),
        "source_key": KEY_TELEGRAM + digest,
        "album": album,
        "placeholder": placeholder,
        "media": kind,
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

def gt_key(list_id: str, task_id: str) -> str:
    return f"{KEY_GTASKS}{list_id}:{task_id}"


def gt_class(status: str | None) -> str | None:
    """Класс статуса для Google Tasks: completed / needsAction / None (отмена)."""
    if status == ST_CANCELLED:
        return None
    return GT_DONE if status == ST_DONE else GT_OPEN


def gt_raw_status(item: dict) -> str:
    return GT_DONE if item.get("status") == GT_DONE else GT_OPEN


def _gt_due(item: dict) -> str:
    due = str(item.get("due") or "")[:10]
    try:
        return datetime.strptime(due, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return ""


def _gt_email_link(item: dict) -> str:
    """Ссылка на письмо — только из read-only поля Tasks API links[type=email]."""
    for ln in item.get("links") or []:
        if isinstance(ln, dict) and ln.get("type") == "email" and ln.get("link"):
            desc = str(ln.get("description") or "").strip()
            return f"✉️ {desc + ': ' if desc else ''}{ln['link']}"
    return ""


def row_key(row) -> str | None:
    """Ключ строки листа «Мои задачи» (A–H): H = gtasks:/tg:-ключ, иначе
    ключ протокола из ячеек."""
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
        out.setdefault(k, st)
    return out


def apply_sheet_edit(rec: dict, sheet_val: str | None) -> bool:
    """Ручное изменение статуса в листе → запись.  Изменение — валидный статус,
    отличный от последнего записанного ботом (sheet_status).  Записи, которых
    бот в лист ещё не писал (sheet_status None), не меняются: старое
    «Отменено» при импорте/миграции не считается ручной отменой."""
    if not sheet_val or sheet_val not in STATUS_VALUES:
        return False
    if rec.get("sheet_status") is None or sheet_val == rec.get("sheet_status"):
        return False
    rec["status"] = sheet_val
    rec["verified"] = 1
    rec["_dirty"] = True
    if sheet_val != ST_CANCELLED and rec.get("gt_delete_pending"):
        rec["gt_delete_pending"] = 0          # передумал — удаление отменяется
    return True


def reconcile_protocol(records: dict, candidates: list, sheet_vals: dict,
                       stats: dict) -> list:
    """Строки протоколов (формат mymeet_sync._build_my_tasks_rows) → записи.

    Новая задача: «Требует верификации»; если в листе протокола у неё уже
    стоит другой статус (выставлен там ранее) — он сохраняется.
    Существующая: ручная правка в «Мои задачи» важнее; изменение статуса
    в протоколе принимается, только если это НЕ возврат к «Требует
    верификации» (бот сам верификацию не снимает и не возвращает).
    Возвращает ключи активных протокольных задач.
    """
    active, seen = [], set()
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
            manual = pstatus in STATUS_VALUES and pstatus != ST_UNVERIFIED
            rec = {
                "source": SRC_PROTOCOL, "source_key": key,
                "status": pstatus if manual else ST_UNVERIFIED,
                "verified": 1 if manual else 0,
                "ext_status": pstatus, "sheet_status": None, "_dirty": True,
            }
            records[key] = rec
            stats["protocol_new"] = stats.get("protocol_new", 0) + 1
        else:
            if (not apply_sheet_edit(rec, sheet_vals.get(key))
                    and pstatus in STATUS_VALUES and pstatus != ST_UNVERIFIED
                    and pstatus != (rec.get("ext_status") or "")
                    and pstatus != rec.get("status")):
                rec["status"] = pstatus
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


def _rekey(records: dict, old: str, new: str) -> dict:
    rec = records.pop(old)
    rec["source_key"] = new
    rec.setdefault("_old_keys", []).append(old)
    rec["_dirty"] = True
    records[new] = rec
    return rec


def migrate_legacy_gt_keys(records: dict) -> int:
    """gtasks:{task_id} (первая версия PR) → gtasks:{list_id}:{task_id}."""
    n = 0
    for key in [k for k, r in records.items() if r.get("source") == SRC_GTASKS]:
        r = records[key]
        lid, tid = r.get("gtasks_list_id"), r.get("gtasks_task_id")
        if lid and tid and key != gt_key(lid, tid) and gt_key(lid, tid) not in records:
            _rekey(records, key, gt_key(lid, tid))
            n += 1
    return n


class GTPlan:
    """План внешних операций сверки с Google Tasks (выполняется отдельно)."""

    def __init__(self):
        self.pushes: list = []            # (key, list_id, task_id, gt_status)
        self.deletes: list = []           # (key, list_id, task_id)
        self.confirm_missing: list = []   # (key, list_id, task_id) — адресный GET


def reconcile_gtasks(records: dict, items: list, complete: bool, list_ids,
                     full_name: str, today: str, stats: dict, warnings: list,
                     sheet_vals: dict | None = None) -> GTPlan:
    """Google Tasks → записи; возвращает план внешних операций.

    sheet_vals — статусы листа, прочитанные в ЭТОМ прогоне (None — лист не
    прочитан → удаления не планируются).
    """
    plan = GTPlan()
    list_ids = set(list_ids or [])
    gt_total = sum(1 for r in records.values() if r.get("source") == SRC_GTASKS)
    seen: set = set()
    by_task_id: dict = {}
    clean_items = []
    for it in items:
        tid, lid = it.get("id"), it.get("_listId")
        title = str(it.get("title") or "").strip()
        if not tid or not lid or not title:
            continue
        key = gt_key(lid, tid)
        if key in seen:
            continue                      # дубль в ответе API
        seen.add(key)
        by_task_id.setdefault(tid, []).append(key)
        clean_items.append((key, it, title))

    # Перемещение задачи между списками: тот же task_id в другом списке
    for key, rec in list(records.items()):
        if rec.get("source") != SRC_GTASKS or key in seen:
            continue
        cands = [k for k in by_task_id.get(rec.get("gtasks_task_id"), []) if k not in records]
        if len(cands) == 1:
            _rekey(records, key, cands[0])
            stats["gtasks_moved"] = stats.get("gtasks_moved", 0) + 1

    for key, it, title in clean_items:
        raw = gt_raw_status(it)
        origin = "Google Tasks" + (f" — {it['_listTitle']}" if it.get("_listTitle") else "")
        rec = records.get(key)
        if rec is None:
            st = ST_DONE if raw == GT_DONE else ST_IN_WORK
            rec = {
                "source": SRC_GTASKS, "source_key": key, "status": st, "verified": 1,
                "ext_status": raw, "ext_synced_status": st, "sheet_status": None,
                "task_date": today, "gt_delete_pending": 0, "_dirty": True,
            }
            records[key] = rec
            stats["gtasks_new"] = stats.get("gtasks_new", 0) + 1
        else:
            if rec.get("missing_since"):
                rec["missing_since"] = None
                rec["_dirty"] = True
            status = rec.get("status")
            if not (status == ST_CANCELLED or rec.get("gt_delete_pending")):
                # «Отменено» липкое: изменения из GT его не перетирают
                synced = rec.get("ext_synced_status")
                local_changed = synced is None or gt_class(status) != gt_class(synced)
                remote_changed = raw != rec.get("ext_status")
                if remote_changed and not local_changed:
                    if raw == GT_DONE and status != ST_DONE:
                        rec["status"] = ST_DONE
                        stats["gtasks_status_in"] = stats.get("gtasks_status_in", 0) + 1
                    elif raw == GT_OPEN and status == ST_DONE:
                        rec["status"] = ST_IN_WORK
                        stats["gtasks_status_in"] = stats.get("gtasks_status_in", 0) + 1
                elif local_changed and gt_class(status) != raw:
                    plan.pushes.append((key, it["_listId"], it["id"], gt_class(status)))
                rec["ext_synced_status"] = rec["status"]
            rec["ext_status"] = raw
            rec["_dirty"] = True
        for field, val in (("title", title[:TITLE_MAX_LEN]), ("origin", origin),
                           ("assignee", full_name or ""), ("deadline", _gt_due(it)),
                           ("link", _gt_email_link(it)),
                           ("gtasks_list_id", it["_listId"]), ("gtasks_task_id", it["id"])):
            if rec.get(field) != val:
                rec[field] = val
                rec["_dirty"] = True

    # ── Отмены в «Мои задачи» → адресное удаление в GT
    pending = [k for k, r in records.items()
               if r.get("source") == SRC_GTASKS and r.get("gt_delete_pending")]
    if pending:
        if sheet_vals is None or not complete:
            stats["gtasks_delete_deferred"] = len(pending)
            if not complete:
                warnings.append("Google Tasks: ответ неполный — удаление отложено")
        elif len(pending) > MAX_DELETES_PER_RUN:
            stats["gtasks_mass_delete"] = len(pending)
            warnings.append(f"Google Tasks: к удалению {len(pending)} задач — больше "
                            f"{MAX_DELETES_PER_RUN}, удаление приостановлено")
        else:
            for k in pending:
                r = records[k]
                if sheet_vals.get(k) != ST_CANCELLED:
                    continue               # в листе уже не «Отменено» — не удаляем
                if k in seen:
                    plan.deletes.append((k, r.get("gtasks_list_id"), r.get("gtasks_task_id")))
                else:
                    # Задачи уже нет в полном ответе API — удалять нечего
                    r["gt_delete_pending"] = 0
                    r["ext_status"] = GT_DELETED
                    r["missing_since"] = r.get("missing_since") or time.time()
                    r["_dirty"] = True

    # ── Пропавшие из GT → адресная проверка (одного отсутствия недостаточно)
    known = [k for k, r in records.items()
             if r.get("source") == SRC_GTASKS and r.get("ext_status") != GT_DELETED
             and not r.get("gt_delete_pending")]
    missing = [k for k in known if k not in seen]
    if missing:
        orphan = [k for k in missing if records[k].get("gtasks_list_id") not in list_ids]
        if not complete:
            warnings.append("Google Tasks: ответ неполный — пропавшие задачи не обработаны")
        elif orphan:
            stats["gtasks_list_missing"] = len(orphan)
            warnings.append(f"Google Tasks: недоступен список у {len(orphan)} задач — "
                            f"возможно, сменился аккаунт или список удалён; "
                            f"изменения не применены")
        elif _is_mass(len(missing), gt_total):
            stats["gtasks_mass_missing"] = len(missing)
            warnings.append(
                f"Google Tasks: пропали {len(missing)} из {gt_total} задач — похоже "
                f"на сбой, изменения не применены (защита от массового удаления)")
        else:
            for k in missing:
                r = records[k]
                plan.confirm_missing.append((k, r.get("gtasks_list_id"), r.get("gtasks_task_id")))
    return plan


def mark_deleted_in_gt(rec: dict) -> None:
    """Задача удалена в GT (подтверждено) → «Отменено», строка сохраняется,
    прежний статус — в prev_status."""
    if rec.get("status") != ST_CANCELLED:
        rec["prev_status"] = rec.get("status")
        rec["status"] = ST_CANCELLED
    rec["ext_status"] = GT_DELETED
    rec["ext_synced_status"] = ST_CANCELLED
    rec["missing_since"] = rec.get("missing_since") or time.time()
    rec["gt_delete_pending"] = 0
    rec["_dirty"] = True


def _deadline_key(d: str):
    try:
        return (0, datetime.strptime((d or "").strip(), "%d.%m.%Y"))
    except ValueError:
        return (1, datetime.max)


def build_sheet_rows(records: dict, active_protocol: list) -> list:
    """Строки листа: все gtasks/telegram + активные протокольные.
    [№, Дата, Источник, Задача, Ответственный, Срок, Статус, H, ключ]
    Для протокольных H = mtg_id (как раньше), иначе H = ключ."""
    rows = []
    active = set(active_protocol)
    for key, r in records.items():
        if r.get("source") == SRC_PROTOCOL:
            if key not in active:
                continue
            base = key[len(KEY_PROTOCOL):].split("|", 1)[0]
            h = "" if base.startswith("~") else base
        else:
            h = key
        title = r.get("title") or ""
        if r.get("link"):
            title = f"{title}\n{r['link']}"
        rows.append([None, r.get("task_date") or "", r.get("origin") or "", title,
                     r.get("assignee") or "", r.get("deadline") or "",
                     r.get("status") or ST_IN_WORK, h, key])
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


def _inc(stats: dict, key: str, n: int = 1) -> None:
    stats[key] = stats.get(key, 0) + n


class MyTasksBuilder:
    """Строки «Мои задачи» + сверка в БД + внешние операции с Google Tasks.

    Порядок вызовов из mymeet_sync:
      matcher() → build(candidates, my_rows) → [защита от сжатия]
      → sheet_unchanged(my_rows, fresh_rows) [повторное чтение листа]
      → execute_external() → запись листа → commit().
    blocked(reason) — запись листа не выполнена (сжатие/конфликт).
    gtasks: (items, complete, list_ids) — сверка с GT в этом прогоне;
    gt_ops: объект с set_task_status/delete_task/get_task_state (gtasks.py).
    """

    def __init__(self, telegram_id: int, full_name: str, *, gtasks=None,
                 gt_ops=None, dbm=None, tz_name: str | None = None):
        self.telegram_id = telegram_id
        self.full_name = (full_name or "").strip()
        self.gtasks = gtasks
        self.gt_ops = gt_ops
        self.dbm = dbm
        self.tz_name = tz_name
        self.built = False
        self.committed = False
        self.block_reason = None
        self.anomaly = None
        self.stats: dict = {}
        self.warnings: list = []
        self._records: dict = {}
        self._plan: GTPlan | None = None
        self._written: dict = {}

    def _store(self):
        return self.dbm or _db()

    def _save(self, records) -> None:
        dirty = [r for r in records if r.pop("_dirty", False)]
        for r in dirty:
            for old in r.pop("_old_keys", []):
                self._store().my_tasks_rekey(self.telegram_id, old, r["source_key"])
        if dirty:
            self._store().my_tasks_upsert(self.telegram_id, dirty)

    def matcher(self):
        name = self.full_name

        def _m(assignee, _mid) -> bool:
            return match_assignee(assignee, name) is not None
        return _m

    def build(self, protocol_candidates: list | None, my_rows: list | None,
              sheet_available: bool = True) -> list | None:
        """Возвращает строки листа или None (аномалия — лист не писать)."""
        self.built = True
        store = self._store()
        loaded = store.my_tasks_load(self.telegram_id)
        bad = [r for r in loaded if r.get("_decrypt_error")]
        if bad:
            self.anomaly = "decrypt"
            self.warnings.append(
                f"Не удалось расшифровать {len(bad)} задач (ENCRYPTION_KEY?) — "
                f"лист и Google Tasks не изменены, данные в БД сохранены")
            return None
        records = {r["source_key"]: r for r in loaded}
        for r in records.values():
            if r.pop("_plaintext", False):
                r["_dirty"] = True              # старая запись → будет зашифрована
        sheet_read = bool(sheet_available and my_rows)
        sheet_vals = sheet_statuses(my_rows) if sheet_read else {}

        # 1. Ручные правки gtasks/telegram (по ключам, как они сейчас в листе)
        gt_total = sum(1 for r in records.values() if r.get("source") == SRC_GTASKS)
        new_cancels = []
        for key, rec in records.items():
            if rec.get("source") == SRC_PROTOCOL:
                continue
            was = rec.get("status")
            if apply_sheet_edit(rec, sheet_vals.get(key)):
                if (rec.get("source") == SRC_GTASKS and rec["status"] == ST_CANCELLED
                        and was != ST_CANCELLED and rec.get("ext_status") != GT_DELETED):
                    new_cancels.append(rec)
        if new_cancels:
            if _is_mass(len(new_cancels), gt_total):
                self.anomaly = "mass_cancel"
                self.warnings.append(
                    f"В листе отменено сразу {len(new_cancels)} задач Google Tasks — "
                    f"удаление в Google Tasks НЕ выполняется (защита от массового "
                    f"удаления); при необходимости удалите их в Google Tasks вручную")
            else:
                for rec in new_cancels:
                    rec["gt_delete_pending"] = 1
        migrate_legacy_gt_keys(records)

        # 2. Протоколы
        active = (reconcile_protocol(records, protocol_candidates or [], sheet_vals, self.stats)
                  if sheet_available else [])

        # 3. Google Tasks
        if self.gtasks is not None:
            items, complete, list_ids = self.gtasks
            today = fmt_date(datetime.now(_tz(self.tz_name)).date())
            plan = reconcile_gtasks(records, items, complete, list_ids, self.full_name,
                                    today, self.stats, self.warnings,
                                    sheet_vals if sheet_read else None)
            if (self.stats.get("gtasks_mass_missing") or self.stats.get("gtasks_list_missing")
                    or self.stats.get("gtasks_mass_delete")):
                self.anomaly = self.anomaly or "gtasks"
            for key, lid, tid in plan.confirm_missing:      # только чтение
                state = "error"
                try:
                    state = self.gt_ops.get_task_state(self.telegram_id, lid, tid)
                except Exception as e:
                    logger.warning("gtasks get failed: %s", type(e).__name__)
                if state == "deleted":
                    mark_deleted_in_gt(records[key])
                    _inc(self.stats, "gtasks_deleted_remote")
                elif state == "error":
                    _inc(self.stats, "gtasks_confirm_failed")
                else:
                    _inc(self.stats, "gtasks_missing_but_exists")
            self._plan = plan

        # 4. Восстановление состояния: запись, которую бот в лист ещё не писал,
        #    а в листе по её ключу уже есть статус (восстановление БД, повторный
        #    импорт) — принимаем статус листа как есть, БЕЗ внешних действий
        #    (не удаляем и не отправляем в GT): не затираем пометку пользователя.
        for key, rec in records.items():
            v = sheet_vals.get(key)
            if (rec.get("sheet_status") is None and v in STATUS_VALUES
                    and v != rec.get("status") and rec.get("ext_status") != GT_DELETED):
                rec["status"] = v
                if rec.get("source") == SRC_GTASKS:
                    rec["ext_synced_status"] = v
                    rec["gt_delete_pending"] = 0
                if v != ST_UNVERIFIED:
                    rec["verified"] = 1
                rec["_dirty"] = True
                _inc(self.stats, "adopted_from_sheet")

        self._records = records
        self._save(list(records.values()))
        rows = build_sheet_rows(records, active)
        self._written = {r[8]: r[6] for r in rows}
        return [r[:8] for r in rows]

    def sheet_unchanged(self, before_rows, fresh_rows) -> bool:
        """Статусы листа не изменились между чтением и записью."""
        return sheet_statuses(before_rows) == sheet_statuses(fresh_rows)

    def blocked(self, reason: str) -> None:
        self.block_reason = reason
        if reason == "shrink":
            self.anomaly = self.anomaly or "shrink"

    def execute_external(self) -> None:
        """Внешние операции GT по плану: статусы и адресные удаления.
        Вызывается только после повторной проверки листа."""
        plan, self._plan = self._plan, None
        if not plan or not self.gt_ops:
            return
        recs = self._records
        changed = []
        for key, list_id, task_id, gt_status in plan.pushes:
            ok = False
            try:
                ok = bool(self.gt_ops.set_task_status(self.telegram_id, list_id, task_id,
                                                      gt_status))
            except Exception as e:
                logger.warning("gtasks push failed: %s", type(e).__name__)
            rec = recs[key]
            if ok:
                rec["ext_status"] = gt_status
                _inc(self.stats, "gtasks_status_out")
            else:
                rec["ext_synced_status"] = None    # повторим при следующей сверке
                _inc(self.stats, "gtasks_push_failed")
            rec["_dirty"] = True
            changed.append(rec)
        for key, list_id, task_id in plan.deletes:
            rec = recs[key]
            res = "error"
            # Привязка: ключ записи обязан совпадать с (list_id, task_id)
            if list_id and task_id and key == gt_key(list_id, task_id):
                try:
                    res = self.gt_ops.delete_task(self.telegram_id, list_id, task_id)
                except Exception as e:
                    logger.warning("gtasks delete failed: %s", type(e).__name__)
            if res in ("deleted", "absent"):
                rec["gt_delete_pending"] = 0
                rec["ext_status"] = GT_DELETED
                rec["ext_synced_status"] = ST_CANCELLED
                rec["missing_since"] = time.time()
                _inc(self.stats, "gtasks_deleted_out")
            else:
                _inc(self.stats, "gtasks_delete_failed")
            rec["_dirty"] = True
            changed.append(rec)
        if self.stats.get("gtasks_push_failed") or self.stats.get("gtasks_delete_failed"):
            self.warnings.append(
                "Google Tasks: часть изменений не применена — повторю при следующей сверке")
        self._save(changed)

    def commit(self) -> None:
        """Лист записан — запоминаем записанные статусы."""
        if self._written:
            self._store().my_tasks_mark_written(self.telegram_id, self._written)
        self.committed = True


# ══════════════════════════════════════════════════════════════════════════
# 6. Оркестрация (синхронно; main.py вызывает в executor)
# ══════════════════════════════════════════════════════════════════════════

def _row_get(row, key, default=None):
    try:
        v = row[key]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


_HEADINGS = {
    OUT_OK: "✅ «Мои задачи» синхронизированы",
    OUT_PARTIAL: "⚠️ Синхронизация выполнена частично — повторю автоматически",
    OUT_TRANSIENT: "⏳ Временная ошибка — синхронизация не выполнена, повторю автоматически",
    OUT_ANOMALY: "🛑 Обнаружена аномалия — опасные изменения приостановлены",
    OUT_CONFIG: "🔑 Синхронизация не выполнена — нужно ваше действие",
}


def run_sync(telegram_id: int, with_gtasks: bool = True, *, dbm=None, gt=None,
             ms=None) -> dict:
    """Сверка «Мои задачи»: Google Tasks (если with_gtasks) + лист.

    Возвращает {"outcome", "text", "warnings", "stats", "sheet_count"}.
    outcome == "ok" только если все обязательные шаги выполнены: импорт GT
    полный и (если таблица настроена) лист записан.
    """
    store = dbm or _db()
    user = store.get_user(telegram_id)
    full_name = str(_row_get(user, "mymeet_full_name", "") or "").strip()
    sheet_id = str(_row_get(user, "mymeet_spreadsheet_id", "") or "").strip()
    tz_name = _row_get(user, "timezone", None)
    warnings: list = []
    outcome = OUT_OK

    gt_data, gt_complete = None, True
    if with_gtasks:
        if gt is None:
            import gtasks as gt   # noqa: F811 — ленивый импорт (googleapiclient)
        try:
            items, err, complete, list_ids = gt.list_all_tasks_full(telegram_id)
        except Exception as e:
            logger.warning("gtasks list failed for %s: %s", telegram_id, type(e).__name__)
            items, err, complete, list_ids = [], "api_error", False, []
        if err in ("no_scope", "no_auth"):
            outcome = OUT_CONFIG
            warnings.append("Google Tasks: нет доступа — переподключите Google: /connect")
        elif err:
            outcome = OUT_TRANSIENT
            warnings.append("Google Tasks: ошибка API — задачи не синхронизированы")
        else:
            gt_data, gt_complete = (items, complete, list_ids), complete

    builder = MyTasksBuilder(telegram_id, full_name, gtasks=gt_data, gt_ops=gt,
                             dbm=store, tz_name=tz_name)
    sheet_count, sheet_title, sheet_error = None, None, False
    sheet_needed = bool(sheet_id and full_name)
    if sheet_needed:
        if ms is None:
            import mymeet_sync as ms   # noqa: F811
        try:
            w, sheet_count, sheet_title = ms.refresh_my_tasks_sheet(sheet_id, full_name, builder)
            warnings.extend(w)
        except Exception as e:
            logger.warning("my_tasks sheet refresh failed for %s: %s", telegram_id,
                           type(e).__name__)
            warnings.append(f"Лист «Мои задачи»: ошибка ({type(e).__name__})")
            sheet_error = True
    if not builder.built:
        # Лист не прочитан — сверяем только БД; удаления в GT не планируются
        builder.build(None, None, sheet_available=False)
        builder.execute_external()
    warnings = builder.warnings + warnings

    s = builder.stats
    if outcome == OUT_OK:
        if builder.anomaly:
            outcome = OUT_ANOMALY
        elif sheet_needed and (sheet_error or not builder.committed):
            outcome = OUT_TRANSIENT if builder.block_reason == "conflict" else OUT_PARTIAL
        elif (not gt_complete or s.get("gtasks_push_failed") or s.get("gtasks_delete_failed")
              or s.get("gtasks_confirm_failed")):
            outcome = OUT_PARTIAL

    lines = [_HEADINGS[outcome]]
    if with_gtasks and gt_data is not None:
        lines.append(
            f"Google Tasks: получено {len(gt_data[0])}, новых {s.get('gtasks_new', 0)}, "
            f"статусов из GT {s.get('gtasks_status_in', 0)}, в GT {s.get('gtasks_status_out', 0)}"
            + (f", удалено в GT {s['gtasks_deleted_out']}" if s.get("gtasks_deleted_out") else "")
            + (f", удалены в GT → «Отменено» {s['gtasks_deleted_remote']}"
               if s.get("gtasks_deleted_remote") else ""))
    if sheet_count is not None and builder.committed:
        lines.append(f"👤 Лист «{sheet_title}»: {sheet_count} задач")
    elif not sheet_id:
        lines.append("ℹ️ Таблица не настроена — задачи сохранены, лист появится после /setup_mymeet")
    elif not full_name:
        lines.append("ℹ️ Укажите ФИО: /setname Имя Фамилия — тогда появится лист «Мои задачи»")
    else:
        lines.append("Лист «Мои задачи» не обновлён")
    lines.extend(f"• {w}" for w in warnings)
    return {"outcome": outcome, "text": "\n".join(lines), "warnings": warnings,
            "stats": s, "sheet_count": sheet_count if builder.committed else None}


def add_forwarded(telegram_id: int, info: dict, *, dbm=None) -> bool:
    """Сохраняет задачу из пересланного сообщения. True — создана, False — дубль.
    Альбом: если задачу создала часть без подписи, подпись другой части
    заменяет заглушку (одна задача на альбом)."""
    store = dbm or _db()
    user = store.get_user(telegram_id)
    full_name = str(_row_get(user, "mymeet_full_name", "") or "").strip()
    created = store.my_tasks_insert_new(telegram_id, new_telegram_record(info, full_name))
    if not created and info.get("album") and not info.get("placeholder"):
        rec = store.my_tasks_get(telegram_id, info["source_key"])
        if (rec and not rec.get("_decrypt_error")
                and str(rec.get("title") or "").startswith(ALBUM_PLACEHOLDER)):
            rec["title"] = info["title"]
            rec["deadline"] = info.get("deadline") or rec.get("deadline") or ""
            rec.pop("_plaintext", None)
            store.my_tasks_upsert(telegram_id, [rec])
    return created


# ══════════════════════════════════════════════════════════════════════════
# 7. Расписание ежедневной сверки (19:00 по поясу пользователя)
# ══════════════════════════════════════════════════════════════════════════

DAILY_HOUR = 19
BACKOFF_MIN = (15, 30, 60, 120)        # минуты между повторами после сбоя
NOTIFY_AFTER_FAILS = 3


def daily_slot(local_dt: datetime) -> str:
    """«Слот» ежедневной сверки: дата, к 19:00 которой относится момент.
    До 19:00 — ещё вчерашний слот (сбой вчера повторяется до успеха)."""
    d = local_dt.date() if local_dt.hour >= DAILY_HOUR else local_dt.date() - timedelta(days=1)
    return d.isoformat()


def daily_due(state: dict | None, local_dt: datetime, now_ts: float) -> str | None:
    """Слот, который пора выполнить, или None.

    Без истории — только после 19:00 местного времени (не запускаемся сразу
    после деплоя утром).  Иначе — пока последний успешный слот < текущего,
    с паузой между попытками (next_attempt_ts)."""
    slot = daily_slot(local_dt)
    state = state or {}
    if now_ts < float(state.get("next_attempt_ts") or 0):
        return None
    last = state.get("last_success_slot")
    if not last:
        if local_dt.hour < DAILY_HOUR and not state.get("fail_count"):
            return None
        return slot
    return slot if last < slot else None


def next_attempt_delay(fail_count: int) -> int:
    """Секунды до следующей попытки после fail_count подряд неудачных."""
    i = max(0, min(fail_count, len(BACKOFF_MIN)) - 1)
    return BACKOFF_MIN[i] * 60


def should_notify(outcome: str, fail_count: int, notified_slot: str | None,
                  slot: str) -> bool:
    """Сообщать о проблеме не чаще раза за слот: аномалия и нужное действие —
    сразу, временные/частичные — после 3 сбоев подряд.  Об успехе отдельно
    не сообщаем."""
    if outcome == OUT_OK or notified_slot == slot:
        return False
    if outcome in (OUT_ANOMALY, OUT_CONFIG):
        return True
    return fail_count >= NOTIFY_AFTER_FAILS
