import functools
import inspect
import math
import re
from typing import Any

_DURATION_TOKEN_RE = re.compile(
    r"(?P<val>\d+(?:\.\d+)?)(?P<unit>ms|[smhdw])?",
    flags=re.IGNORECASE,
)
_DURATION_FULL_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)?(?:ms|[smhdw])?\s*)+$",
    flags=re.IGNORECASE,
)
_UNIT_SECONDS: dict[str, float] = {
    "": 1.0,
    "ms": 0.001,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
    "w": 604800.0,
}


def parse_duration(
    value: str | int | float | None,
    *,
    name: str = "timeout",
) -> float | None:
    """Parse a non-negative duration in seconds from a number or duration string.

    Supported string units: ``ms``, ``s``, ``m``, ``h``, ``d``, ``w`` (or bare
    seconds), e.g. ``"500ms"``, ``"1.5s"``, ``"2m30s"``, ``"1h 15m"``.
    """
    if value is None:
        return None

    if isinstance(value, bool):
        raise TypeError(f"{name} must be a str, int, float, or None, not bool")

    if isinstance(value, (int, float)):
        seconds = float(value)
        if math.isnan(seconds) or math.isinf(seconds) or seconds < 0:
            raise ValueError(f"{name} must be a non-negative finite number, got {value!r}")
        return seconds

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or not _DURATION_FULL_RE.match(stripped):
            raise ValueError(f"Invalid {name} duration string: {value!r}")

        total = 0.0
        for match in _DURATION_TOKEN_RE.finditer(stripped):
            amount = float(match.group("val"))
            unit = (match.group("unit") or "").lower()
            total += amount * _UNIT_SECONDS[unit]

        if math.isnan(total) or math.isinf(total) or total < 0:
            raise ValueError(f"Invalid {name} duration value: {value!r}")
        return total

    raise TypeError(f"{name} must be a str, int, float, or None, got {type(value).__name__}")


def resolve_callable_name(target: Any) -> str:
    """Resolve a human-readable task name for callables, partials, or strings."""
    if isinstance(target, str):
        return target

    current = target
    while isinstance(current, functools.partial):
        current = current.func

    name = getattr(current, "__name__", None)
    if isinstance(name, str) and name:
        return name

    return type(current).__name__


def is_async_callable(target: Any) -> bool:
    """Return True if ``target`` is an async function, partial of one, or async callable object."""
    current = target
    while isinstance(current, functools.partial):
        current = current.func

    if inspect.iscoroutinefunction(current):
        return True

    if callable(current) and not inspect.isclass(current):
        call_attr = getattr(current, "__call__", None)
        if call_attr is not None and inspect.iscoroutinefunction(call_attr):
            return True

    return False
