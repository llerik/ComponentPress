"""Validated local preferences shared by the desktop UI and CLI."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, ValidationError
from PySide6.QtCore import QSettings

from componentpress.diagnostics import logging as technical_log
from componentpress.domain.project import PrintSettings, StrictModel


class ProgramPreferences(StrictModel):
    language: Literal["ru", "en"] = "ru"
    theme: Literal["system", "light", "dark"] = "system"
    print_settings: PrintSettings = Field(default_factory=PrintSettings)
    png_workers: int = Field(default=2, ge=1, le=64)
    recent_projects: list[str] = Field(default_factory=list, max_length=10)


def user_settings() -> QSettings:
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "ComponentPress", "ComponentPress")


def load_preferences(settings: QSettings | None = None) -> ProgramPreferences:
    settings = settings or user_settings()
    raw = settings.value("program/preferences")
    if raw is None:
        try:
            return ProgramPreferences(
                language=settings.value("language", "ru", type=str),
                theme=settings.value("theme", "system", type=str),
            )
        except ValidationError:
            technical_log.log_event(
                "application.settings_load", operation="program_settings", phase="preparation",
                status="failed", code="PROGRAM_SETTINGS_INVALID",
            )
            return ProgramPreferences()
    try:
        return ProgramPreferences.model_validate_json(str(raw))
    except (ValueError, TypeError, ValidationError):
        technical_log.log_event(
            "application.settings_load", operation="program_settings", phase="preparation",
            status="failed", code="PROGRAM_SETTINGS_INVALID",
        )
        return ProgramPreferences()


def save_preferences(preferences: ProgramPreferences, settings: QSettings | None = None) -> bool:
    settings = settings or user_settings()
    key = "program/preferences"
    previous = settings.value(key)
    old_language = settings.value("language")
    old_theme = settings.value("theme")
    settings.setValue(key, preferences.model_dump_json())
    settings.sync()
    if settings.status() == QSettings.Status.NoError:
        settings.setValue("language", preferences.language)
        settings.setValue("theme", preferences.theme)
        settings.sync()
    if settings.status() == QSettings.Status.NoError:
        return True
    if previous is None:
        settings.remove(key)
    else:
        settings.setValue(key, previous)
    if old_language is None:
        settings.remove("language")
    else:
        settings.setValue("language", old_language)
    if old_theme is None:
        settings.remove("theme")
    else:
        settings.setValue("theme", old_theme)
    settings.sync()
    technical_log.log_event(
        "application.settings_save", operation="program_settings", phase="publishing",
        status="failed", code="PROGRAM_SETTINGS_WRITE",
    )
    return False
