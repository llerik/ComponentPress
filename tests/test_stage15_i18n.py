"""Localization catalog and startup-language contracts for stage 15."""

from pathlib import Path
import pytest

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication, QComboBox, QDialogButtonBox, QLabel, QMessageBox

from componentpress.domain.diagnostics import Diagnostic
from componentpress.domain.schema import SCHEMA_VERSION
from componentpress.i18n import catalogs, language, set_language, tr, validate_catalogs
from componentpress.i18n.diagnostics import format_diagnostic, format_diagnostic_event
from componentpress.version import APPLICATION_VERSION
from componentpress.editor.component_dialog import ComponentDialog
from componentpress.editor.copy_mode_switch import CopyModeSwitch
from componentpress.editor.document_tab import DocumentTab
from componentpress.editor.properties import ElementProperties
from componentpress.domain.nodes import HtmlNode
from componentpress.rendering.image_quality import ImageQuality


def test_catalogs_have_matching_keys_and_placeholders() -> None:
    validate_catalogs()
    assert catalogs()["ru"].keys() == catalogs()["en"].keys()
    set_language("ru")


@pytest.mark.parametrize(("locale", "label"), (("ru", "Поменять ширину и высоту местами"), ("en", "Swap width and height")))
def test_component_dimension_swap_label_is_localized(qtbot, locale, label) -> None:
    previous = language()
    set_language(locale)
    try:
        dialog = ComponentDialog()
        qtbot.addWidget(dialog)
        assert dialog.swap_dimensions.toolTip() == label
        assert dialog.swap_dimensions.accessibleName() == label
    finally:
        set_language(previous)


def test_archive_task_messages_are_translated_in_both_catalogs() -> None:
    values = {key: tr(key, locale="ru", path="/game", version="0.1.1", detail="") for key in (
        "archive.export_title", "archive.import_title", "archive.exists_title",
        "archive.replace_confirm", "archive.export_succeeded",
    )}
    for key, russian in values.items():
        assert russian != tr(key, locale="en", path="/game", version="0.1.1", detail="")


def test_unsaved_alert_and_about_close_buttons_use_both_catalogs(tmp_path: Path, qtbot) -> None:
    from test_stage14_ui import make_window

    expected = {
        "ru": ("Сохранить", "Не сохранять", "Отмена", "Закрыть", "Несохранённые изменения"),
        "en": ("Save", "Discard", "Cancel", "Close", "Unsaved changes"),
    }
    previous = language()
    try:
        for locale, labels in expected.items():
            set_language(locale)
            (tmp_path / locale).mkdir()
            _root, window, _service = make_window(tmp_path / locale, qtbot)
            document = window.session.documents["forest-card"]
            document.set_draft(document.current_text + "\n# unsaved")

            def cancel_prompt() -> None:
                dialog = QApplication.activeModalWidget()
                assert dialog is not None
                assert dialog.windowTitle() == labels[4]
                assert [dialog.button(button).text() for button in (
                    QMessageBox.StandardButton.Save,
                    QMessageBox.StandardButton.Discard,
                    QMessageBox.StandardButton.Cancel,
                )] == list(labels[:3])
                assert all(button.width() >= button.sizeHint().width() for button in dialog.buttons())
                dialog.button(QMessageBox.StandardButton.Cancel).click()

            QTimer.singleShot(0, cancel_prompt)
            assert not window._confirm_all_dirty()
            assert document.draft_text is not None

            def close_about() -> None:
                dialog = QApplication.activeModalWidget()
                box = dialog.findChild(QDialogButtonBox)
                assert box is not None
                assert box.button(QDialogButtonBox.StandardButton.Close).text() == labels[3]
                box.button(QDialogButtonBox.StandardButton.Close).click()

            QTimer.singleShot(0, close_about)
            window._show_about()
            window.controller.revert_document("forest-card")
    finally:
        set_language(previous)
    assert tr("about.schema", version=SCHEMA_VERSION).endswith(str(SCHEMA_VERSION))
    set_language("en")
    assert tr("about.schema", version=SCHEMA_VERSION).startswith("Supported")
    assert format_diagnostic(Diagnostic("YAML_SYNTAX", "Синтаксис YAML нарушен")).endswith("The operation needs attention (YAML_SYNTAX).")
    set_language("ru")


def test_about_keeps_language_in_program_settings(monkeypatch, tmp_path: Path, qtbot) -> None:
    from test_stage14_ui import make_window

    _root, window, _service = make_window(tmp_path, qtbot)

    def inspect_about() -> None:
        dialog = QApplication.activeModalWidget()
        assert dialog.findChild(QComboBox, "languageSelector") is None
        labels = [label.text() for label in dialog.findChildren(QLabel)]
        assert any("GNU GPL" in label for label in labels)
        assert any(APPLICATION_VERSION in label for label in labels)
        assert any(str(SCHEMA_VERSION) in label for label in labels)
        dialog.reject()

    QTimer.singleShot(0, inspect_about)
    window._show_about()
    assert any(action is window.program_settings_action for action in window.settings_menu.actions())
    assert language() == "ru"
    set_language("en")
    english_tmp = tmp_path / "english"
    english_tmp.mkdir()
    _root, english_window, _service = make_window(english_tmp, qtbot)
    assert english_window.file_menu.title() == "File"
    assert english_window.new_action.text() == "New project…"
    assert english_window.right_tabs.tabText(0) == "Components"
    set_language("ru")


def test_editor_panels_and_dynamic_controls_use_english_catalog(tmp_path: Path, qtbot) -> None:
    from test_stage14_ui import make_window

    previous = language()
    set_language("en")
    try:
        root = tmp_path / "editor-en"
        root.mkdir()
        _root, window, _service = make_window(root, qtbot)
        assert window.image_manager_action.text() == "Project images…"
        tab = window.open_component("forest-card")
        assert tab is not None
        assert tab.preview_first.toolTip() == "First instance"
        assert tab.preview_first.accessibleName() == "First instance"
        assert window.data_insert.text() == "Insert Column"
        assert window.add_component_button.accessibleName() == "Add component"
        assert window.undo_action.text() == "Undo"
        assert window.redo_action.text() == "Redo"
        assert window.validate_all_action.text() == "Refresh data and validate project"
        assert window.validate_all_button.accessibleName() == "Refresh data and validate project"
        assert window.validate_all_button.toolTip() == "Refresh data and validate project"
        window._update_actions()
        assert window.undo_action.text() == "Undo"
        assert window.redo_action.text() == "Redo"

        switch = CopyModeSwitch()
        qtbot.addWidget(switch)
        assert switch.accessibleName() == "Copy mode"
        assert switch.toolTip() == "Copy mode: Full set (prod)"
        switch.setChecked(True)
        assert switch.accessibleDescription() == "Copy mode: Partial set (test)"

        tab = DocumentTab("card")
        qtbot.addWidget(tab)
        assert tab.layout_mode.toolTip() == "Drag and drop"
        assert tab.text_mode.accessibleName() == "Text"

        dialog = ComponentDialog()
        qtbot.addWidget(dialog)
        assert dialog.windowTitle() == "New component"
        assert dialog.size.itemText(0) == "Custom size…"
        assert dialog.size.itemData(1) == (63, 88)

        html = HtmlNode(
            id="node", name="User name", type="html", x_mm=0, y_mm=0,
            width_mm=20, height_mm=10, font_family="Unavailable Test Font",
            font_size_pt=10, html="<p>Keep this text</p>",
        )
        properties = ElementProperties()
        qtbot.addWidget(properties)
        changes = []
        properties.nodeChanged.connect(changes.append)
        properties.show_node(html, font_families=("Arial",))
        assert properties.color_swatch.accessibleName() == "Choose text color"
        assert properties.font_combo.currentText() == "Unavailable Test Font (unavailable)"
        assert properties.font_combo.currentData() == "Unavailable Test Font"
        assert not changes
        properties.font_combo.setCurrentIndex(properties.font_combo.findData("Arial"))
        assert changes[-1] == {"font_family": "Arial"}
        assert html.name == "User name"
        assert html.html == "<p>Keep this text</p>"

        diagnostic = ImageQuality("node", "assets/icon.png", 80, 60, 20, 15, 101.6, 101.6).diagnostic(
            tmp_path / "project.cpress", source="main", sheet="Cards",
        )
        assert diagnostic is not None
        rendered = format_diagnostic(diagnostic)
        assert "source main" in rendered and "sheet Cards" in rendered and "element node" in rendered
        assert "Image assets/icon.png: 80×60 px" in rendered
        assert "effective resolution 101.6×101.6 dpi" in rendered
    finally:
        set_language(previous)


def test_event_list_uses_the_selected_diagnostic_language(tmp_path: Path, qtbot) -> None:
    from test_stage14_ui import make_window

    previous = language()
    try:
        set_language("en")
        _root, window, _service = make_window(tmp_path, qtbot)
        diagnostic = Diagnostic("FILE_READ", "файл недоступен", tmp_path / "game.yaml")
        window._add_events((diagnostic,))
        assert window.event_list.item(0).text() == format_diagnostic_event(
            diagnostic, project_root=window.session.snapshot.root, hide_project_path=True,
        )
        assert "The operation needs attention (FILE_READ)." in window.event_list.item(0).text()
    finally:
        set_language(previous)


def test_new_default_element_names_use_english_catalog(tmp_path: Path, qtbot, monkeypatch) -> None:
    from test_stage14_ui import make_window

    previous = language()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Discard))
    set_language("en")
    try:
        _root, window, _service = make_window(tmp_path, qtbot)
        monkeypatch.setattr(window, "_ask_unsaved_choice", lambda *_args: QMessageBox.StandardButton.Discard)
        window.open_component("forest-card")
        window._draw_element("rectangle", 1, 1, 8, 6)
        assert window.add_html("<p>English</p>", 2, 3)
        assert window.add_image("assets/images/leaf.png", 8, 8)
        names = {node.type: node.name for node in window.session.documents["forest-card"].model.elements if node.type in {"rectangle", "html", "image"}}
        assert names == {"rectangle": "Rectangle 1", "html": "HTML text 1", "image": "Image 1"}
        window._set_selection((next(node.id for node in window.session.documents["forest-card"].model.elements if node.type == "rectangle"),), source="tree")
        assert window.duplicate_selected()
        duplicates = [node.name for node in window.session.documents["forest-card"].model.elements if node.type == "rectangle"]
        assert duplicates == ["Rectangle 1", "Rectangle 2"]
    finally:
        set_language(previous)
