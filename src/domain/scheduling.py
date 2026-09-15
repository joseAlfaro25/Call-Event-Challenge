"""Deterministic calendar arithmetic for outbound calls and tasks."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
import re
import unicodedata
from zoneinfo import ZoneInfo


DAY_NAMES = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")
NUMBER_WORDS = {
    "una": 1,
    "uno": 1,
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
    "diez": 10,
    "once": 11,
    "doce": 12,
}


def get_timezone(config) -> ZoneInfo:
    """Return the campaign timezone instead of depending on host local time."""
    return ZoneInfo(config.timezone)


def to_local(value: datetime, config) -> datetime:
    """Convert an aware datetime to the campaign timezone."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include an explicit UTC offset")
    return value.astimezone(get_timezone(config))


def is_in_call_window(value: datetime, config) -> bool:
    """Check inclusive opening and closing bounds for outbound calls."""
    local_value = to_local(value, config)
    span = config.raw["ventana_llamadas"][DAY_NAMES[local_value.weekday()]]
    if not span:
        return False
    return (
        time.fromisoformat(span[0]) <= local_value.time() <= time.fromisoformat(span[1])
    )


def first_valid_slot(value: datetime, config) -> datetime:
    """Move a requested instant to the first valid configured call slot."""
    candidate = to_local(value, config)
    timezone = get_timezone(config)
    for _ in range(14):
        span = config.raw["ventana_llamadas"][DAY_NAMES[candidate.weekday()]]
        if span:
            opening = datetime.combine(
                candidate.date(), time.fromisoformat(span[0]), timezone
            )
            closing = datetime.combine(
                candidate.date(), time.fromisoformat(span[1]), timezone
            )
            if candidate <= opening:
                return opening
            if candidate <= closing:
                return candidate
        candidate = datetime.combine(
            candidate.date() + timedelta(days=1), time.min, timezone
        )
    raise ValueError("no valid call window found within two weeks")


def next_attempt(
    start: datetime,
    config,
    minimum_minutes: int = 120,
    maximum_minutes: int | None = None,
) -> datetime:
    """Calculate a regular or busy retry and then apply the call window."""
    if minimum_minutes < 0 or (
        maximum_minutes is not None and maximum_minutes < minimum_minutes
    ):
        raise ValueError("invalid retry interval")
    delay = minimum_minutes
    if maximum_minutes is not None:
        delay = (minimum_minutes + maximum_minutes) // 2
    return first_valid_slot(to_local(start, config) + timedelta(minutes=delay), config)


def next_cut_off_attempt(
    start: datetime,
    config,
    minimum_minutes: int = 30,
    maximum_minutes: int = 240,
) -> tuple[datetime, bool]:
    """Find the earliest cut-off retry in its bounded recovery interval."""
    local_start = to_local(start, config)
    earliest = local_start + timedelta(minutes=minimum_minutes)
    latest = local_start + timedelta(minutes=maximum_minutes)
    candidate = first_valid_slot(earliest, config)
    if candidate <= latest:
        return candidate, False
    return first_valid_slot(latest, config), True


def add_business_days(value: datetime, days: int, config) -> datetime:
    """Add weekdays from campaign configuration; Saturday is not a business day."""
    if days < 0:
        raise ValueError("business-day increment cannot be negative")
    result = to_local(value, config)
    while days:
        result += timedelta(days=1)
        if DAY_NAMES[result.weekday()] in config.raw["dias_habiles"]:
            days -= 1
    return result


@dataclass(frozen=True)
class CallbackResolution:
    requested: datetime
    scheduled: datetime
    adjusted: bool


def _parse_callback_day(value: str, anchor: datetime, config) -> datetime:
    """Resolve a structured relative day against the campaign-local anchor."""
    normalized = _without_accents(value)
    base = to_local(anchor, config)
    if normalized in {"hoy", "today"}:
        return base
    if normalized in {"manana", "tomorrow"}:
        return base + timedelta(days=1)
    if normalized == "pasado manana":
        return base + timedelta(days=2)
    for index, day_name in enumerate(DAY_NAMES):
        if normalized == day_name:
            return base + timedelta(days=(index - base.weekday()) % 7)
    raise ValueError(f"callback day is not understood: {value!r}")


def _without_accents(value: str) -> str:
    """Normalize Spanish accents while preserving the original text for errors."""
    text = " ".join((value or "").lower().split())
    return "".join(
        character
        for character in unicodedata.normalize("NFD", text)
        if unicodedata.category(character) != "Mn"
    )


def _parse_callback_time(value: str) -> tuple[int, int]:
    """Parse a structured local 24-hour callback time."""
    if not re.fullmatch(r"\d{2}:\d{2}", value or ""):
        raise ValueError(f"callback time must use HH:MM: {value!r}")
    hour, minute = (int(part) for part in value.split(":"))
    if hour > 23 or minute > 59:
        raise ValueError(f"callback time is invalid: {value!r}")
    return hour, minute


def _parse_requested_datetime(
    raw: str,
    anchor: datetime,
    config,
    callback_day: str | None = None,
    callback_time: str | None = None,
) -> datetime:
    text = " ".join((raw or "").lower().split())
    normalized_text = _without_accents(text)
    base = to_local(anchor, config)
    if callback_day:
        base = _parse_callback_day(callback_day, anchor, config)
    elif "pasado manana" in normalized_text:
        base += timedelta(days=2)
    elif "manana" in normalized_text or "tomorrow" in normalized_text:
        base += timedelta(days=1)
    elif "hoy" not in normalized_text and "today" not in normalized_text:
        for index, day_name in enumerate(DAY_NAMES):
            if day_name in normalized_text:
                delta = (index - base.weekday()) % 7
                base += timedelta(days=delta)
                break

    if callback_time:
        hour, minute = _parse_callback_time(callback_time)
    else:
        hour_pattern = r"(?:a\s+las|a\s+la|las|at)\s+(\d{1,2}|[a-záéíóú]+)(?::|\.)?(\d{2})?"
        match = re.search(hour_pattern, text)
        if not match:
            match = re.search(r"\b(\d{1,2})(?::|\.)(\d{2})\b", text)
        if not match:
            raise ValueError(f"callback time is not understood: {raw!r}")
        hour_token = match.group(1)
        word_hour = NUMBER_WORDS.get(hour_token)
        hour = (
            word_hour
            if word_hour is not None
            else int(hour_token)
            if hour_token.isdigit()
            else None
        )
        if hour is None or not 0 <= hour <= 23:
            raise ValueError(f"callback hour is invalid: {raw!r}")
        minute = int(match.group(2) or 0)
        if minute > 59:
            raise ValueError(f"callback minute is invalid: {raw!r}")
        if (
            word_hour is not None
            and hour <= 7
            and not any(
                phrase in text
                for phrase in (
                    "de la mañana",
                    "de la manana",
                    "por la mañana",
                    "por la manana",
                    "am",
                    "madrugada",
                )
            )
        ):
            hour += 12
        if hour <= 12 and any(word in text for word in ("tarde", "noche", "pm")):
            if hour != 12:
                hour += 12
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def resolve_callback(
    raw: str | None,
    anchor: datetime,
    config,
    callback_day: str | None = None,
    callback_time: str | None = None,
) -> CallbackResolution:
    """Parse a relative callback request and report whether its time was adjusted."""
    requested = _parse_requested_datetime(
        raw or "", anchor, config, callback_day, callback_time
    )
    scheduled = first_valid_slot(requested, config)
    return CallbackResolution(requested, scheduled, scheduled != requested)
