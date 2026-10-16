from pathlib import Path

import pytest

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.i18n import language, set_language
from componentpress.i18n.diagnostics import format_diagnostic, format_project_error


@pytest.mark.parametrize(
    ("locale", "details"),
    (
        ("ru", "источник main, лист Cards, ячейка B4, элемент node-1"),
        ("en", "source main, sheet Cards, cell B4, element node-1"),
    ),
)
def test_format_diagnostic_keeps_context_order_and_incident(locale: str, details: str, tmp_path: Path) -> None:
    diagnostic = Diagnostic(
        "RESOURCE_MISSING", "Ресурс отсутствует", tmp_path / "game.yaml",
        "elements.0.source", 3, source="main", sheet="Cards", cell="B4",
        node_id="node-1", incident_id="a" * 32,
    )
    rendered = format_diagnostic(diagnostic, locale=locale)
    assert f"{diagnostic.path}:3 (elements.0.source) [{details}]" in rendered
    assert "RESOURCE_MISSING" in rendered and "a" * 32 in rendered
    assert str(diagnostic) == "RESOURCE_MISSING"


@pytest.mark.parametrize("locale", ("ru", "en"))
def test_format_diagnostic_uses_translated_key_parameters(locale: str) -> None:
    diagnostic = Diagnostic(
        "VALIDATION_OK", "", message_key="ui.validation_component_ok",
        params={"name": "Лесная карта"},
    )
    rendered = format_diagnostic(diagnostic, locale=locale)
    expected = "Компонент Лесная карта" if locale == "ru" else "Component Лесная карта"
    assert expected in rendered


def test_formatter_preserves_no_key_fallback_and_prepared_error_language() -> None:
    diagnostic = Diagnostic("YAML_SYNTAX", "Синтаксис YAML нарушен")
    error = ProjectError(diagnostic)
    original = language()
    try:
        set_language("ru")
        prepared = format_project_error(error, locale=language())
        assert "Синтаксис YAML нарушен" in prepared
        set_language("en")
        assert format_project_error(error, locale="ru") == prepared
        assert format_diagnostic(diagnostic).endswith("The operation needs attention (YAML_SYNTAX).")
        assert str(error) == "YAML_SYNTAX"
    finally:
        set_language(original)


@pytest.mark.parametrize("locale", ("ru", "en"))
def test_dialog_error_adapter_formats_project_error(locale: str, tmp_path: Path) -> None:
    from componentpress.editor.dialogs import format_error

    original = language()
    try:
        set_language(locale)
        error = ProjectError(Diagnostic("FILE_READ", "файл не читается", tmp_path / "game.yaml"))
        assert format_error(error) == format_diagnostic(error.diagnostic)
        assert str(error) == "FILE_READ"
    finally:
        set_language(original)


def test_version_worker_freezes_project_error_before_queued_delivery() -> None:
    from componentpress.editor.version_worker import VersionTask

    original = language()
    received = []

    def fail(_cancellation, _progress):
        raise ProjectError(Diagnostic("ARCHIVE_READ", "Архив недоступен"))

    try:
        set_language("ru")
        task = VersionTask(fail, object(), locale=language())
        task.signals.finished.connect(lambda *result: received.append(result))
        set_language("en")
        task.run()
        assert received[0][2].endswith("Архив недоступен")
        assert format_diagnostic(received[0][1].diagnostic).endswith("The operation needs attention (ARCHIVE_READ).")
    finally:
        set_language(original)
