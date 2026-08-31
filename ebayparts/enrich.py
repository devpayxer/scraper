"""Derive structured facts from a listing title.

This is where the analysis value lives: a title like

    2000-2003 CHEVY TAHOE WHEEL RIM TIRE PATHFINDER HT 245/75 R16 16X7 5 SPOKE OEM

becomes years 2000-2003, make Chevrolet, model Tahoe, part "Wheel / Rim".

Note the trap in that real example: "PATHFINDER" is the *tire* model, not a
Nissan. Models are therefore only ever matched inside the winning make's own
model list, so it can never be mislabelled as a Nissan Pathfinder.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from .config import load_categories, load_vehicles
from .normalize import clean_text


def _boundary(alias: str) -> re.Pattern[str]:
    """Word-boundary matcher that survives aliases like 'cr-v', 'f-150', '3 series'."""
    escaped = re.escape(alias.strip()).replace(r"\ ", r"[\s\-/]+")
    return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", re.IGNORECASE)


@dataclass
class MakeIndex:
    make: str
    patterns: list[re.Pattern[str]]
    models: list[tuple[str, re.Pattern[str], int]]  # (model, pattern, alias length)


@lru_cache(maxsize=1)
def _vehicle_index() -> list[MakeIndex]:
    index: list[MakeIndex] = []
    for make, spec in (load_vehicles() or {}).items():
        aliases = spec.get("aliases") or [make]
        models: list[tuple[str, re.Pattern[str], int]] = []
        for model, model_aliases in (spec.get("models") or {}).items():
            # YAML reads keys like `86:` and `1500:` as ints -- keep them strings
            model = str(model)
            for alias in (model_aliases or [model]):
                alias = str(alias)
                models.append((model, _boundary(alias), len(alias)))
        # longest alias first so "3 series" beats a bare "3"
        models.sort(key=lambda item: -item[2])
        index.append(MakeIndex(str(make), [_boundary(str(a)) for a in aliases], models))
    return index


@lru_cache(maxsize=1)
def _category_index() -> list[dict[str, Any]]:
    rules = []
    for rule in load_categories():
        rules.append(
            {
                "name": rule["name"],
                "group": rule.get("group", "Other"),
                "match": [_boundary(m) for m in rule.get("match", [])],
                "exclude": [_boundary(m) for m in rule.get("exclude", [])],
            }
        )
    return rules


# ------------------------------------------------------------------- years

# "2000-2003", "2013 - 2019", "2007-13", "2015"
_YEAR_RANGE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*[-–—/]\s*((?:19|20)?\d{2})(?!\d)")
_YEAR_SINGLE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_SHORT_RANGE_RE = re.compile(r"(?<![\d/])(\d{2})\s*[-–—]\s*(\d{2})(?![\d/])")


def parse_years(title: str) -> tuple[int | None, int | None]:
    """Return the (first, last) model year the part fits, if the title says."""
    title = clean_text(title)
    if not title:
        return None, None

    match = _YEAR_RANGE_RE.search(title)
    if match:
        start = int(match.group(1))
        end_raw = match.group(2)
        end = int(end_raw) if len(end_raw) == 4 else int(str(start)[:2] + end_raw)
        if end < start:  # e.g. "2007-03" is a typo or not a year range
            end = start
        if end - start <= 30:
            return start, end

    match = _YEAR_SINGLE_RE.search(title)
    if match:
        year = int(match.group(1))
        if 1950 <= year <= 2035:
            return year, year

    match = _SHORT_RANGE_RE.search(title)
    if match:
        start, end = (int(match.group(1)), int(match.group(2)))
        if start > end:
            return None, None
        to_full = lambda y: 1900 + y if y > 40 else 2000 + y  # noqa: E731
        start_f, end_f = to_full(start), to_full(end)
        if 0 <= end_f - start_f <= 30:
            return start_f, end_f

    return None, None


# ---------------------------------------------------------- make and model


def parse_vehicle(title: str) -> tuple[str | None, str | None, list[str]]:
    """Return (make, model, all_makes_mentioned).

    The leftmost make in the title wins, because eBay part titles lead with
    fitment: "<years> <make> <model> <part>".
    """
    title = clean_text(title)
    if not title:
        return None, None, []

    hits: list[tuple[int, MakeIndex]] = []
    for entry in _vehicle_index():
        best = None
        for pattern in entry.patterns:
            match = pattern.search(title)
            if match and (best is None or match.start() < best):
                best = match.start()
        if best is not None:
            hits.append((best, entry))

    if not hits:
        return None, None, []

    hits.sort(key=lambda item: item[0])
    all_makes = sorted({entry.make for _, entry in hits})
    _, winner = hits[0]

    model = None
    best_pos: int | None = None
    best_len = -1
    for name, pattern, alias_len in winner.models:
        match = pattern.search(title)
        if not match:
            continue
        # prefer the longest alias; break ties by position in the title
        if alias_len > best_len or (alias_len == best_len and match.start() < (best_pos or 10**9)):
            model, best_pos, best_len = name, match.start(), alias_len

    return winner.make, model, all_makes


# ------------------------------------------------------------- part category


def parse_part_category(title: str) -> tuple[str | None, str | None]:
    """Return (category, group). First matching rule wins; see categories.yml."""
    title = clean_text(title)
    if not title:
        return None, None
    for rule in _category_index():
        if any(pattern.search(title) for pattern in rule["exclude"]):
            continue
        if any(pattern.search(title) for pattern in rule["match"]):
            return rule["name"], rule["group"]
    return None, None


# ------------------------------------------------------------ small extras

_OEM_RE = re.compile(r"\b(oem|genuine|factory|mopar|acdelco|motorcraft)\b", re.IGNORECASE)
_AFTERMARKET_RE = re.compile(r"\b(aftermarket|replacement|non-oem)\b", re.IGNORECASE)
_SIDE_PATTERNS = [
    ("Left", r"\b(left|driver(?:'?s)?(?:\s+side)?|lh|l/h)\b"),
    ("Right", r"\b(right|passenger(?:'?s)?(?:\s+side)?|rh|r/h)\b"),
]
_POSITION_PATTERNS = [
    ("Front", r"\bfront\b"),
    ("Rear", r"\b(rear|back)\b"),
]


def parse_flags(title: str) -> dict[str, Any]:
    title = clean_text(title)
    side = [name for name, pattern in _SIDE_PATTERNS if re.search(pattern, title, re.IGNORECASE)]
    position = [n for n, p in _POSITION_PATTERNS if re.search(p, title, re.IGNORECASE)]
    return {
        "is_oem": bool(_OEM_RE.search(title)) and not _AFTERMARKET_RE.search(title),
        "side": side[0] if len(side) == 1 else None,
        "position": position[0] if len(position) == 1 else None,
    }


@dataclass
class Enrichment:
    year_from: int | None = None
    year_to: int | None = None
    make: str | None = None
    model: str | None = None
    part_category: str | None = None
    part_group: str | None = None
    is_oem: bool = False
    side: str | None = None
    position: str | None = None
    all_makes: list[str] = field(default_factory=list)

    @property
    def year_range(self) -> str | None:
        if self.year_from is None:
            return None
        if self.year_to and self.year_to != self.year_from:
            return f"{self.year_from}-{self.year_to}"
        return str(self.year_from)

    @property
    def vehicle(self) -> str | None:
        if not self.make:
            return None
        return f"{self.make} {self.model}" if self.model else self.make


def enrich_title(title: str) -> Enrichment:
    """Run every extractor over one title."""
    year_from, year_to = parse_years(title)
    make, model, all_makes = parse_vehicle(title)
    category, group = parse_part_category(title)
    flags = parse_flags(title)
    return Enrichment(
        year_from=year_from,
        year_to=year_to,
        make=make,
        model=model,
        part_category=category,
        part_group=group,
        all_makes=all_makes,
        **flags,
    )
