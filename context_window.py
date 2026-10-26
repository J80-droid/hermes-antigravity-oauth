"""Per-model context window from the models.dev catalog Hermes already keeps.

agy's own `models` list is only an id and a label. The window lives in
models_dev_cache.json, under the vendor that publishes the model. A reseller
entry for the same id is ignored when that vendor is present.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

_EFFORT_SUFFIXES = ("-high", "-medium", "-low", "-thinking")
# Bare id prefix -> models.dev provider whose limit.context is the model spec.
_VENDOR = (
    ("gemini", "google"),
    ("claude", "anthropic"),
    ("gpt-oss", "groq"),
)

_cached: tuple[str, float, dict] | None = None


def base_model_id(model: str) -> str:
    text = (model or "").strip()
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    text = text.split("@", 1)[0]
    for suffix in _EFFORT_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    return text


def registry_path() -> Path | None:
    candidates: list[Path] = []
    try:
        from hermes_constants import get_hermes_home

        candidates.append(Path(get_hermes_home()) / "models_dev_cache.json")
    except Exception:
        pass
    home = os.environ.get("HERMES_HOME")
    if home:
        candidates.append(Path(home) / "models_dev_cache.json")
    for path in candidates:
        if path.is_file():
            return path
    return None


def load_registry() -> dict | None:
    """The on-disk models.dev catalog, or None when it is missing or unreadable."""
    global _cached
    path = registry_path()
    if path is None:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = str(path)
    if _cached and _cached[0] == key and _cached[1] == mtime:
        return _cached[2]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    _cached = (key, mtime, data)
    return data


def _index(registry: dict) -> dict[str, dict[str, int]]:
    found: dict[str, dict[str, int]] = {}
    for provider, info in registry.items():
        models = info.get("models") if isinstance(info, dict) else None
        if not isinstance(models, dict):
            continue
        for model_id, meta in models.items():
            bare = str(model_id).rsplit("/", 1)[-1].split("@", 1)[0]
            limit = meta.get("limit") if isinstance(meta, dict) else None
            context = limit.get("context") if isinstance(limit, dict) else None
            if isinstance(context, int) and context > 0 and bare:
                found.setdefault(bare, {})[str(provider)] = context
    return found


def _agreed(values: list[int]) -> int | None:
    """The single most common window, or None when two windows tie."""
    if not values:
        return None
    ranked = Counter(values).most_common(2)
    best, count = ranked[0]
    if len(ranked) > 1 and ranked[1][1] == count:
        return None
    return best


def context_length_for(model: str, registry: dict | None = None) -> int | None:
    """Tokens of context for this model id, or None when the catalog has no spec."""
    data = registry if registry is not None else load_registry()
    if not isinstance(data, dict):
        return None
    base = base_model_id(model)
    if not base:
        return None
    by_vendor = _index(data).get(base) or {}
    if not by_vendor:
        return None
    for prefix, vendor in _VENDOR:
        if base.startswith(prefix) and vendor in by_vendor:
            return by_vendor[vendor]
    return _agreed(list(by_vendor.values()))
