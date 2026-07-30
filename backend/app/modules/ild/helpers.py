"""
Helpers shared across ILD modules.

Scope filtering (single source of truth — do NOT reimplement elsewhere):
* PRR  : rule NAME must START WITH any of PRR_SCOPE_PREFIXES AND END WITH
         any of PRR_SCOPE_SUFFIXES (case-insensitive; empty prefix list
         disables the prefix check).
* RBAR : DESTINATION must match the prefix+suffix rule for the DRA
         instance CATEGORY, from RBAR_SCOPE_RULES (JSON). Categories
         without an explicit entry use the "default" rule.
         Defaults: Core/IoT/others → orcl…vdea; Policy → jio…pcrf.
Out-of-scope rows are passed through / skipped silently (no log, no DB record).
"""
import json
import logging
from decimal import Decimal, InvalidOperation

from app.core.config import settings

logger = logging.getLogger(__name__)

_FALLBACK_RBAR_RULES = {
    "default": {"prefixes": ["orcl"], "suffixes": ["vdea"]},
    "policy": {"prefixes": ["jio"], "suffixes": ["pcrf"]},
}


def _split(raw: str) -> list[str]:
    return [s.strip().lower() for s in raw.split(",") if s.strip()]


def _match(value: str, prefixes: list[str], suffixes: list[str]) -> bool:
    """prefix AND suffix match; an empty list disables that side of the check."""
    if prefixes and not any(value.startswith(p) for p in prefixes):
        return False
    if suffixes and not any(value.endswith(s) for s in suffixes):
        return False
    return bool(prefixes or suffixes)


def rbar_scope_rules() -> dict:
    """Parsed RBAR_SCOPE_RULES with lower-cased keys/values (env-driven)."""
    try:
        raw = json.loads(settings.RBAR_SCOPE_RULES)
        return {
            str(cat).lower(): {
                "prefixes": [str(p).lower() for p in rule.get("prefixes", [])],
                "suffixes": [str(s).lower() for s in rule.get("suffixes", [])],
            }
            for cat, rule in raw.items()
        }
    except (ValueError, AttributeError, TypeError) as ex:
        logger.warning("Invalid RBAR_SCOPE_RULES JSON (%s) — using built-in defaults", ex)
        return _FALLBACK_RBAR_RULES


def in_prr_scope(name: str | None) -> bool:
    """True when the PRR rule name starts with a configured prefix AND ends
    with a configured suffix."""
    nl = (name or "").strip().lower()
    if not nl:
        return False
    return _match(nl, _split(settings.PRR_SCOPE_PREFIXES), _split(settings.PRR_SCOPE_SUFFIXES))


def in_rbar_scope(destination: str | None, category: str | None = None) -> bool:
    """True when the RBAR destination matches the prefix+suffix rule for the
    given DRA category (falls back to the 'default' rule when the category is
    unknown or has no explicit entry)."""
    dl = (destination or "").strip().lower()
    if not dl:
        return False
    rules = rbar_scope_rules()
    rule = rules.get((category or "").strip().lower()) or rules.get("default") \
        or _FALLBACK_RBAR_RULES["default"]
    return _match(dl, rule.get("prefixes", []), rule.get("suffixes", []))


def _parse_number(value: str) -> int:
    """
    Safely parse integer or scientific-notation values without
    floating-point precision loss.

    Examples:
        "1234"          -> 1234
        "4.0584E+14"    -> 405840000000000
    """
    return int(Decimal(value.strip()))


def parse_range(range_str: str) -> tuple[int, int]:
    """
    Parse a range string into (start_addr, end_addr).

    Supported formats:
        1234-5678
        1234–5678      (en dash)
        1234—5678      (em dash)
        1234 5678      (single or multiple spaces)
        4.0584E+14-4.1E+14
        1234           (single value)

    Returns:
        (start_addr, end_addr)

    Raises:
        ValueError
            If the range cannot be parsed or start > end.
    """

    if range_str is None:
        raise ValueError("Range cannot be None")

    # Normalize whitespace
    s = " ".join(range_str.strip().split())

    # ------------------------------------------------------------
    # Space separated values
    # ------------------------------------------------------------
    parts = s.split()
    if len(parts) == 2:
        try:
            start = _parse_number(parts[0])
            end = _parse_number(parts[1])

            if start > end:
                raise ValueError(
                    f"Invalid range '{range_str}': start ({start}) > end ({end})"
                )

            return start, end

        except (InvalidOperation, ValueError):
            pass

    # ------------------------------------------------------------
    # Dash separated values
    # ------------------------------------------------------------
    for sep in ("-", "\u2013", "\u2014"):  # -, en dash, em dash
        if sep in s:
            left, _, right = s.partition(sep)

            left = left.strip()
            right = right.strip()

            if left and right:
                try:
                    start = _parse_number(left)
                    end = _parse_number(right)

                    if start > end:
                        raise ValueError(
                            f"Invalid range '{range_str}': start ({start}) > end ({end})"
                        )

                    return start, end

                except (InvalidOperation, ValueError):
                    continue

    # ------------------------------------------------------------
    # Single value
    # ------------------------------------------------------------
    try:
        value = _parse_number(s)
        return value, value

    except (InvalidOperation, ValueError):
        raise ValueError(f"Cannot parse range string: {range_str!r}")