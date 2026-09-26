"""Photographs for the letters and the front-page features, embedded as data URIs.

Files live in assay/assets as {name}-640.jpg and {name}-240.jpg with credits.json beside them.
"""

from __future__ import annotations

import base64
import json
from importlib import resources
from typing import Any

LETTERS = {"A": ("bull", "Bull"), "B": ("elk", "Elk"), "C": ("tortoise", "Tortoise"), "D": ("sloth", "Sloth"), "E": ("bear", "Bear")}
ASSET_BASE: str | None = None


def configure(asset_base: str | None) -> None:
    """With a base such as img/, pages reference photographs as files instead of embedding them."""
    global ASSET_BASE
    ASSET_BASE = asset_base


def asset_files() -> list:
    folder = resources.files("assay").joinpath("assets")
    return [p for p in folder.iterdir() if p.name.endswith(".jpg")]


def photo(name: str, width: int = 240) -> str | None:
    path = resources.files("assay").joinpath("assets").joinpath(f"{name}-{width}.jpg")
    if not path.is_file():
        return None
    if ASSET_BASE is not None:
        return f"{ASSET_BASE}{name}-{width}.jpg"
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def for_letter(letter: str | None, width: int = 240) -> tuple[str, str | None]:
    """Animal name and data URI for a letter; a boundary letter takes its lower band."""
    if not letter:
        return "", None
    name, label = LETTERS.get(letter[0], ("", ""))
    return label, photo(name, width) if name else None


def credits() -> list[dict[str, Any]]:
    path = resources.files("assay").joinpath("assets").joinpath("credits.json")
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))
