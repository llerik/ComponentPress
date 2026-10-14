"""Localization catalog and startup-language contracts for stage 15."""

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication, QComboBox, QLabel

from componentpress.domain.diagnostics import Diagnostic
from componentpress.domain.schema import SCHEMA_VERSION
from componentpress.i18n import catalogs, language, set_language, tr, validate_catalogs
from componentpress.version import APPLICATION_VERSION


def test_catalogs_have_matching_keys_and_placeholders() -> None:
    validate_catalogs()
    assert catalogs()["ru"].keys() == catalogs()["en"].keys()
    set_language("ru")
    assert tr("about.schema", version=SCHEMA_VERSION).endswith(str(SCHEMA_VERSION))
    set_language("en")
    assert tr("about.schema", version=SCHEMA_VERSION).startswith("Supported")
    assert str(Diagnostic("YAML_SYNTAX", "Синтаксис YAML нарушен")).endswith("The operation needs attention (YAML_SYNTAX).")
    assert str(APPLICATION_VERSION) == "0.16"
    set_language("ru")


def test_about_saves_language_for_restart(monkeypatch, tmp_path: Path, qtbot) -> None:
    from test_stage14_ui import make_window

    _root, window, _service = make_window(tmp_path, qtbot)

    def choose_english() -> None:
        dialog = QApplication.activeModalWidget()
        selector = dialog.findChild(QComboBox, "languageSelector")
        assert selector is not None
        labels = [label.text() for label in dialog.findChildren(QLabel)]
        assert any("GNU GPL" in label for label in labels)
        assert any("0.16" in label for label in labels)
        assert any(str(SCHEMA_VERSION) in label for label in labels)
        selector.setCurrentIndex(1)
        dialog.accept()

    QTimer.singleShot(0, choose_english)
    window._show_about()
    settings = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "ComponentPress", "ComponentPress")
    settings.sync()
    assert settings.value("language") == "en"
    assert language() == "ru"
    set_language("en")
    english_tmp = tmp_path / "english"
    english_tmp.mkdir()
    _root, english_window, _service = make_window(english_tmp, qtbot)
    assert english_window.file_menu.title() == "File"
    assert english_window.new_action.text() == "New project…"
    assert english_window.right_tabs.tabText(0) == "Components"
    set_language("ru")
