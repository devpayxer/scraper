"""Configuration loading."""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass(frozen=True)
class Seller:
    user: str
    store: str | None = None
    label: str | None = None
    category: int | None = None
    enabled: bool = True

    @property
    def name(self) -> str:
        return self.label or self.user


@dataclass
class Pacing:
    """How hard the collector is allowed to push. See settings.yml."""
    daily_page_budget: int = 80
    sellers_per_run: int = 6
    pages_per_seller: int = 3
    pages_without_new: int = 2
    delay_seconds: float = 25.0
    delay_jitter: float = 35.0
    long_pause_every: int = 12
    long_pause_seconds: float = 240.0
    active_hours: tuple[int, int] = (8, 23)
    stop_on_first_block: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "Pacing":
        raw = dict(raw or {})
        hours = raw.pop("active_hours", None)
        pacing = cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
        if hours and len(hours) == 2:
            pacing.active_hours = (int(hours[0]), int(hours[1]))
        return pacing

    def within_active_hours(self, now: "dt.datetime | None" = None) -> bool:
        hour = (now or dt.datetime.now()).hour
        start, end = self.active_hours
        return start <= hour < end if start <= end else (hour >= start or hour < end)


@dataclass
class Settings:
    default_category: int | None = 6028
    marketplace: str = "www.ebay.com"
    lookback_days: int = 90
    items_per_page: int = 240
    max_pages_per_seller: int = 40
    stop_on_empty_page: bool = True
    delay_seconds: float = 6.0
    delay_jitter: float = 4.0
    timeout_seconds: int = 45
    max_retries: int = 3
    retry_backoff: float = 5.0
    pause_on_block_seconds: int = 900
    mode: str = "passive"
    engine: str = "curl_cffi"
    impersonate: str = "chrome146"
    warm_up: bool = True
    cache_dir: str = "data/cache"
    cache_ttl_hours: int = 20
    database: str = "data/parts.db"

    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or CONFIG_DIR / "settings.yml"
        raw = _load(path) if path.exists() else {}
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        kwargs = {k: v for k, v in raw.items() if k in known}
        extra = {k: v for k, v in raw.items() if k not in known}
        return cls(extra=extra, **kwargs)

    def pacing(self, mode: str | None = None) -> Pacing:
        """The pacing profile for `mode` (defaults to the configured mode)."""
        mode = mode or self.mode
        return Pacing.from_dict(self.extra.get(mode))

    def resolve(self, name: str) -> Path:
        """Resolve a configured relative path against the project root."""
        value = getattr(self, name)
        p = Path(value)
        return p if p.is_absolute() else ROOT / p


def load_sellers(path: Path | None = None, only: list[str] | None = None) -> list[Seller]:
    path = path or CONFIG_DIR / "sellers.yml"
    raw = _load(path)
    sellers: list[Seller] = []
    for entry in raw.get("sellers") or []:
        if isinstance(entry, str):
            entry = {"user": entry}
        seller = Seller(
            user=str(entry["user"]).strip(),
            store=entry.get("store"),
            label=entry.get("label"),
            category=entry.get("category"),
            enabled=bool(entry.get("enabled", True)),
        )
        sellers.append(seller)
    if only:
        wanted = {s.lower() for s in only}
        sellers = [s for s in sellers if s.user.lower() in wanted]
    return sellers


def load_vehicles(path: Path | None = None) -> dict[str, Any]:
    return _load(path or CONFIG_DIR / "vehicles.yml").get("makes", {})


def load_categories(path: Path | None = None) -> list[dict[str, Any]]:
    return _load(path or CONFIG_DIR / "categories.yml").get("categories", [])


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}
