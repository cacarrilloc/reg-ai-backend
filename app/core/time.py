from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return an offset-aware UTC timestamp."""
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    """Normalize aware or legacy naive datetimes to aware UTC."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def days_since(value: datetime, *, now: datetime | None = None) -> int:
    elapsed = to_utc(now or utc_now()) - to_utc(value)
    return max(elapsed.days, 0)
