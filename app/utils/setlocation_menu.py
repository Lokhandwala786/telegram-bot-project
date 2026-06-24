from __future__ import annotations

from collections.abc import Sequence

# Always hidden from /setlocation (merged with config setlocation.exclude_from_menu).
DEFAULT_SETLOCATION_EXCLUDES: tuple[str, ...] = (
    "Croydon",
    "Birmingham",
    "Manchester",
    "Leeds",
    "Sheffield",
    "Dartford",
)


def merge_setlocation_excludes(extra: Sequence[str] | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for name in (*DEFAULT_SETLOCATION_EXCLUDES, *(extra or ())):
        s = str(name).strip()
        if not s:
            continue
        key = s.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def location_excluded(loc: str, exclude_substrings: Sequence[str]) -> bool:
    lc = loc.casefold()
    for ex in exclude_substrings:
        e = str(ex).strip().casefold()
        if e and e in lc:
            return True
    return False


def filter_location_strings(
    locations: Sequence[str],
    exclude_substrings: Sequence[str] | None,
) -> list[str]:
    excludes = merge_setlocation_excludes(exclude_substrings)
    if not excludes:
        return list(locations)
    return [loc for loc in locations if not location_excluded(loc, excludes)]
