"""Small resource-backed localization layer shared by the GUI and diagnostics."""

from __future__ import annotations

import json
from importlib.resources import files
from string import Formatter
from typing import Any

_LANGUAGE = "ru"
_CATALOGS: dict[str, dict[str, str]] = {}


def catalogs() -> dict[str, dict[str, str]]:
    global _CATALOGS
    if not _CATALOGS:
        root = files("componentpress.i18n")
        _CATALOGS = {language: json.loads(root.joinpath(f"{language}.json").read_text(encoding="utf-8")) for language in ("ru", "en")}
    return _CATALOGS


def set_language(language: str) -> None:
    global _LANGUAGE
    _LANGUAGE = language if language in catalogs() else "ru"


def language() -> str:
    return _LANGUAGE


def tr(key: str, *, locale: str | None = None, **values: Any) -> str:
    catalog = catalogs()
    selected = locale if locale in catalog else _LANGUAGE
    template = catalog.get(selected, {}).get(key, catalog["ru"].get(key, key))
    try:
        return template.format(**values)
    except (KeyError, ValueError):
        return template


def validate_catalogs() -> None:
    catalog = catalogs()
    if catalog["ru"].keys() != catalog["en"].keys():
        raise ValueError("localization catalogs have different keys")
    for key in catalog["ru"]:
        ru_fields = {field for _, field, _, _ in Formatter().parse(catalog["ru"][key]) if field}
        en_fields = {field for _, field, _, _ in Formatter().parse(catalog["en"][key]) if field}
        if ru_fields != en_fields:
            raise ValueError(f"placeholder mismatch for {key}")
