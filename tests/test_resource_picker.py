from pathlib import Path
from types import SimpleNamespace
import shutil

import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QDialog

from componentpress.editor import dialogs
from componentpress.editor import main_window
from componentpress.editor.main_window import MainWindow
from componentpress.i18n import set_language
from componentpress.project_io.repository import FileProjectRepository


@pytest.mark.parametrize("language", ("ru", "en"))
def test_picker_selects_existing_image_and_double_click_matches_ok(tmp_path: Path, qtbot, language: str) -> None:
    set_language(language)
    root = tmp_path / "project"
    image_path = root / "assets" / "images" / "nested" / "card.png"
    image_path.parent.mkdir(parents=True)
    assert QImage(2, 2, QImage.Format.Format_RGB32).save(str(image_path))
    for double_click in (False, True):
        picker = dialogs.ResourcePickerDialog(root, lambda *_: "")
        qtbot.addWidget(picker)
        index = picker.model.index(str(image_path))
        qtbot.waitUntil(index.isValid, timeout=3000)
        picker.tree.setCurrentIndex(index)
        if double_click:
            picker.tree.doubleClicked.emit(index)
        else:
            picker._accept_selection()
        assert picker.result() == QDialog.DialogCode.Accepted
        assert picker.selected_path == "assets/images/nested/card.png"
        picker.close()


def test_picker_import_cancel_success_and_error_do_not_leak_partial_selection(tmp_path: Path, qtbot, monkeypatch) -> None:
    root = tmp_path / "project"
    source = tmp_path / "source.png"
    calls = []
    picker = dialogs.ResourcePickerDialog(root, lambda path, folder: calls.append((path, folder)) or "assets/images/new.png")
    qtbot.addWidget(picker)
    monkeypatch.setattr(dialogs.QFileDialog, "getOpenFileName", lambda *args: ("", ""))
    picker._import()
    assert picker.result() == 0
    assert picker.selected_path == ""
    assert calls == []

    monkeypatch.setattr(dialogs.QFileDialog, "getOpenFileName", lambda *args: (str(source), ""))
    monkeypatch.setattr(dialogs.QInputDialog, "getText", lambda *args: ("", False))
    picker._import()
    assert picker.result() == 0
    assert calls == []

    monkeypatch.setattr(dialogs.QInputDialog, "getText", lambda *args: ("//nested\\sub/", True))
    picker._import()
    assert calls == [(source, "nested/sub")]
    assert picker.result() == QDialog.DialogCode.Accepted
    assert picker.selected_path == "assets/images/new.png"


def test_picker_rejects_a_directory_without_accepting(tmp_path: Path, qtbot, monkeypatch) -> None:
    root = tmp_path / "project"
    directory = root / "assets" / "images" / "nested"
    directory.mkdir(parents=True)
    picker = dialogs.ResourcePickerDialog(root, lambda *_: "")
    qtbot.addWidget(picker)
    index = picker.model.index(str(directory))
    qtbot.waitUntil(index.isValid, timeout=3000)
    picker.tree.setCurrentIndex(index)
    notices = []
    monkeypatch.setattr(dialogs.QMessageBox, "information", lambda *args: notices.append(args[-1]))
    picker._accept_selection()
    assert picker.result() == 0
    assert picker.selected_path == ""
    assert notices


def test_picker_import_error_keeps_dialog_open_and_manager_still_constructs(tmp_path: Path, qtbot, monkeypatch) -> None:
    picker = dialogs.ResourcePickerDialog(tmp_path / "project", lambda *_: (_ for _ in ()).throw(ValueError("import failed")))
    manager = dialogs.ImageManagerDialog(tmp_path / "project", lambda *_: "", lambda *_: None)
    qtbot.addWidget(picker)
    qtbot.addWidget(manager)
    monkeypatch.setattr(dialogs.QFileDialog, "getOpenFileName", lambda *args: ("source.png", ""))
    monkeypatch.setattr(dialogs.QInputDialog, "getText", lambda *args: ("", True))
    shown = []
    monkeypatch.setattr(dialogs.QMessageBox, "critical", lambda *args: shown.append(args[-1]))
    picker._import()
    assert picker.result() == 0
    assert picker.selected_path == ""
    assert shown == ["import failed"]
    assert manager.folderImportRequested is not None
@pytest.mark.parametrize("language", ("ru", "en"))
def test_image_and_html_add_dialogs_only_apply_accepted_picker_results(tmp_path: Path, qtbot, monkeypatch, language: str) -> None:
    from componentpress.i18n import set_language

    set_language(language)
    image = dialogs.ImageAddDialog(tmp_path, lambda *_: "")
    html = dialogs.HtmlAddDialog(tmp_path, lambda *_: "")
    qtbot.addWidget(image)
    qtbot.addWidget(html)
    image.source.setText("existing.png")
    original_html = html.html.toPlainText()
    monkeypatch.setattr(dialogs, "ResourcePickerDialog", lambda *_: SimpleNamespace(
        selected_path="picked.png", exec=lambda: QDialog.DialogCode.Rejected,
    ))
    image._choose()
    html._insert_image()
    assert image.source.text() == "existing.png"
    assert html.html.toPlainText() == original_html

    monkeypatch.setattr(dialogs, "ResourcePickerDialog", lambda *_: SimpleNamespace(
        selected_path="picked.png", exec=lambda: QDialog.DialogCode.Accepted,
    ))
    image._choose()
    assert image.source.text() == "picked.png"
    responses = iter(((24, True), (18, True), ("middle", True)))
    monkeypatch.setattr(dialogs.QInputDialog, "getInt", lambda *_args: next(responses))
    monkeypatch.setattr(dialogs.QInputDialog, "getItem", lambda *_args: next(responses))
    html._insert_image()
    assert 'src="picked.png" width="24" height="18" align="middle"' in html.html.toPlainText()


@pytest.mark.parametrize("language", ("ru", "en"))
def test_main_window_html_insert_uses_picker_path_and_updates_only_after_acceptance(
    tmp_path: Path, monkeypatch, language: str
) -> None:
    set_language(language)
    root = tmp_path / "project"
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "examples" / "demo-game",
        root,
        ignore=shutil.ignore_patterns("output", "archive", ".componentpress"),
    )
    document = FileProjectRepository().open(root).documents["forest-card"]
    calls = []
    fake_window = SimpleNamespace(
        _selected_ids=lambda: ("heading",),
        _user_editing_allowed=lambda: True,
        session=SimpleNamespace(snapshot=SimpleNamespace(root=root), documents={"forest-card": document}),
        active_tab=SimpleNamespace(component_id="forest-card"),
        element_properties=SimpleNamespace(
            insert_html_image=lambda *args: calls.append(args),
            html_edit=SimpleNamespace(toPlainText=lambda: "<img>"),
        ),
        _import_image=lambda *_args: "",
        _edit_selected_node=lambda *_args: None,
    )
    monkeypatch.setattr(main_window, "ResourcePickerDialog", lambda *_: SimpleNamespace(
        selected_path="assets/images/picked.png", DialogCode=QDialog.DialogCode,
        exec=lambda: QDialog.DialogCode.Accepted,
    ))
    responses = iter(((32, True), (16, True), ("", True)))
    monkeypatch.setattr(main_window.QInputDialog, "getInt", lambda *_args: next(responses))
    monkeypatch.setattr(main_window.QInputDialog, "getItem", lambda *_args: next(responses))
    MainWindow.insert_html_image(fake_window)
    assert calls == [("assets/images/picked.png", 32, 16, "")]


def test_main_window_html_insert_cancel_preserves_html_and_history(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "examples" / "demo-game",
        root,
        ignore=shutil.ignore_patterns("output", "archive", ".componentpress"),
    )
    document = FileProjectRepository().open(root).documents["forest-card"]
    model = document.model
    original = (root / "components" / "forest-card.yaml").read_bytes()
    original_html = "original html"
    commits = []
    edits = []
    fake_window = SimpleNamespace(
        _selected_ids=lambda: ("heading",),
        _user_editing_allowed=lambda: True,
        session=SimpleNamespace(snapshot=SimpleNamespace(root=root), documents={"forest-card": document}),
        active_tab=SimpleNamespace(component_id="forest-card"),
        element_properties=SimpleNamespace(
            insert_html_image=lambda *args: commits.append(args),
            html_edit=SimpleNamespace(toPlainText=lambda: original_html),
        ),
        _import_image=lambda *_args: "",
        _edit_selected_node=lambda *args: edits.append(args),
    )
    monkeypatch.setattr(main_window, "ResourcePickerDialog", lambda *_: SimpleNamespace(
        selected_path="assets/images/unused.png", DialogCode=QDialog.DialogCode,
        exec=lambda: QDialog.DialogCode.Rejected,
    ))
    MainWindow.insert_html_image(fake_window)
    assert commits == []
    assert edits == []
    assert fake_window.element_properties.html_edit.toPlainText() == original_html
    assert document.model is model
    assert (root / "components" / "forest-card.yaml").read_bytes() == original
