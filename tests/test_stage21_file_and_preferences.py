"""Acceptance checks for the project manifest and program preferences."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import zipfile

import pytest
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication

from componentpress.domain.diagnostics import ProjectError
from componentpress.domain.project import PrintSettings
from componentpress.editor.main_window import MainWindow
from componentpress.i18n import language, set_language
from componentpress.platforms.windows_association import register_executable
from componentpress.project_io.files import file_hash
from componentpress.project_io.repository import FileProjectRepository
from componentpress.user_preferences import ProgramPreferences, load_preferences, save_preferences
from componentpress.versioning.repository import _manifest_for


DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo-game"


def test_schema8_project_is_upgraded_to_9_on_open_without_retaining_old_files(tmp_path):
    root = tmp_path / "legacy-schema"
    shutil.copytree(DEMO, root)
    for path in (root / "project.cpress", *sorted((root / "components").glob("*.yaml"))):
        text = path.read_text(encoding="utf-8").replace("schema_version: 9", "schema_version: 8", 1)
        if path.name == "project.cpress":
            text = text.replace("    path: assets/images/leaf.png", "    path: assets/images/leaf.png\n    width_px: 24\n    height_px: 24", 1)
        path.write_text(text, encoding="utf-8")

    snapshot = FileProjectRepository().open(root)

    assert snapshot.model.schema_version == 9
    manifest = (root / "project.cpress").read_text(encoding="utf-8")
    component = (root / "components/forest-card.yaml").read_text(encoding="utf-8")
    assert "width_px" not in manifest and "height_px" not in manifest
    assert "schema_version: 9" in manifest and "schema_version: 9" in component
    assert not list((root / ".componentpress/migrations").glob("*/"))


def test_registered_schema_migration_resumes_after_partial_file_replacement(tmp_path, monkeypatch):
    root = tmp_path / "interrupted-schema"
    shutil.copytree(DEMO, root)
    for path in (root / "project.cpress", *sorted((root / "components").glob("*.yaml"))):
        path.write_text(path.read_text(encoding="utf-8").replace("schema_version: 9", "schema_version: 8", 1), encoding="utf-8")
    import componentpress.project_io.schema_migration_engine as engine
    write = engine.atomic_write
    calls = 0

    def fail_manifest(path, data, expected_hash=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("interrupted")
        write(path, data, expected_hash)

    monkeypatch.setattr(engine, "atomic_write", fail_manifest)
    with pytest.raises(OSError):
        FileProjectRepository().open(root)
    monkeypatch.setattr(engine, "atomic_write", write)
    snapshot = FileProjectRepository().open(root)
    assert snapshot.model.schema_version == 9
    assert not (root / ".componentpress/migrations/active.json").exists()


def test_incomplete_registered_migration_path_is_rejected_before_any_write(tmp_path, monkeypatch):
    root = tmp_path / "unreachable-schema"
    shutil.copytree(DEMO, root)
    for path in (root / "project.cpress", *sorted((root / "components").glob("*.yaml"))):
        path.write_text(path.read_text(encoding="utf-8").replace("schema_version: 9", "schema_version: 8", 1), encoding="utf-8")
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    import componentpress.project_io.schema_migration_engine as engine
    monkeypatch.setattr(engine, "SCHEMA_VERSION", 10)
    with pytest.raises(ProjectError) as error:
        FileProjectRepository().open(root)
    assert error.value.diagnostic.code == "SCHEMA_UNSUPPORTED"
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    assert not (root / ".componentpress").exists()


def test_legacy_and_ambiguous_manifest_are_read_only(tmp_path):
    root = tmp_path / "legacy"
    shutil.copytree(DEMO, root)
    current = root / "project.cpress"
    legacy = root / "project.yaml"
    current.replace(legacy)
    original = legacy.read_bytes()
    with pytest.raises(ProjectError) as error:
        FileProjectRepository().open(root)
    assert error.value.diagnostic.code == "PROJECT_MANIFEST_LEGACY"
    assert legacy.read_bytes() == original

    current.write_bytes(original)
    with pytest.raises(ProjectError) as error:
        FileProjectRepository().open(root)
    assert error.value.diagnostic.code == "PROJECT_MANIFEST_AMBIGUOUS"
    assert current.read_bytes() == legacy.read_bytes() == original
    assert not (root / ".componentpress").exists()


def test_manifest_symbolic_link_is_rejected_without_following_it(tmp_path):
    root = tmp_path / "linked"
    root.mkdir()
    target = tmp_path / "outside.cpress"
    target.write_text("schema_version: 9\n", encoding="utf-8")
    try:
        (root / "project.cpress").symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable in this Windows environment")
    with pytest.raises(ProjectError) as error:
        FileProjectRepository().open(root)
    assert error.value.diagnostic.code == "PROJECT_MANIFEST_LINK"
    assert target.read_text(encoding="utf-8") == "schema_version: 9\n"


def test_invalid_manifest_is_rejected_before_transaction_recovery(tmp_path):
    root = tmp_path / "damaged"
    shutil.copytree(DEMO, root)
    manifest = root / "project.cpress"
    manifest.write_text("schema_version: 9\nid: [\n", encoding="utf-8")
    manifest_bytes = manifest.read_bytes()
    orphan = root / "components/orphan.yaml"
    orphan_bytes = b"orphan\n"
    orphan.write_bytes(orphan_bytes)
    journal = root / ".componentpress/transactions/add-component.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({
        "component": "components/orphan.yaml",
        "component_hash": hashlib.sha256(orphan_bytes).hexdigest(),
        "old_project_hash": file_hash(manifest),
        "new_project_hash": "not-committed",
    }), encoding="utf-8")
    journal_bytes = journal.read_bytes()

    with pytest.raises(ProjectError) as error:
        FileProjectRepository().open(root)
    assert error.value.diagnostic.code == "YAML_SYNTAX"
    assert manifest.read_bytes() == manifest_bytes
    assert orphan.read_bytes() == orphan_bytes
    assert journal.read_bytes() == journal_bytes


def test_old_project_archive_is_rejected_without_rewriting_it(tmp_path):
    path = tmp_path / "legacy.zip"
    payload = b"old project"
    metadata = {
        "format_version": 1,
        "project_id": "game",
        "project_version": "1.0",
        "schema_version": 8,
        "files": {"project.yaml": {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}},
        "directories": [],
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(metadata))
        archive.writestr("project/project.yaml", payload)
    before = path.read_bytes()
    with zipfile.ZipFile(path) as archive, pytest.raises(ProjectError) as error:
        _manifest_for(archive)
    assert error.value.diagnostic.code == "ARCHIVE_MANIFEST_LEGACY"
    assert path.read_bytes() == before


def test_program_preferences_round_trip_and_recover_corrupt_values(tmp_path, monkeypatch):
    path = tmp_path / "preferences.ini"
    settings = QSettings(str(path), QSettings.Format.IniFormat)
    defaults = load_preferences(settings)
    assert defaults == ProgramPreferences()
    saved = ProgramPreferences(
        language="en", theme="dark",
        print_settings=PrintSettings(cut_lines=False, dpi=450, margin_mm=7),
        png_workers=5,
    )
    assert save_preferences(saved, settings)
    settings.sync()
    assert load_preferences(settings) == saved

    events = []
    monkeypatch.setattr("componentpress.user_preferences.technical_log.log_event", lambda *args, **kwargs: events.append(kwargs))
    settings.setValue("program/preferences", "{broken")
    settings.sync()
    assert load_preferences(settings) == ProgramPreferences()
    assert events and events[-1]["code"] == "PROGRAM_SETTINGS_INVALID"


def test_settings_dialog_applies_theme_on_ok_and_leaves_cancel_untouched(tmp_path, qtbot):
    from test_stage14_ui import make_window

    previous_language = language()
    set_language("ru")
    _root, window, _service = make_window(tmp_path, qtbot)
    settings = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "ComponentPress", "ComponentPress")
    original = load_preferences(settings)

    def cancel_dialog():
        dialog = QApplication.activeModalWidget()
        assert dialog is not None
        assert dialog.findChild(type(dialog.language), "programLanguage") is not None
        dialog.theme.setCurrentIndex(dialog.theme.findData("light"))
        dialog.reject()

    QTimer.singleShot(0, cancel_dialog)
    window._show_program_settings()
    assert load_preferences(settings) == original
    assert window._theme == original.theme

    def accept_dialog():
        dialog = QApplication.activeModalWidget()
        dialog.language.setCurrentIndex(dialog.language.findData("en"))
        dialog.theme.setCurrentIndex(dialog.theme.findData("dark"))
        dialog.cut_lines.setCurrentIndex(dialog.cut_lines.findData(False))
        dialog.workers.setValue(4)
        dialog.accept()

    QTimer.singleShot(0, accept_dialog)
    window._show_program_settings()
    saved = load_preferences(settings)
    assert saved.language == "en" and saved.theme == "dark"
    assert saved.print_settings.cut_lines is False and saved.png_workers == 4
    assert window._theme == "dark"
    assert language() == "ru"
    set_language(previous_language)


def test_registry_adapter_quotes_executable_and_reports_partial_failure(tmp_path):
    class Registry:
        HKEY_CURRENT_USER = object()
        REG_SZ = 1

        def __init__(self, fail_on=None):
            self.writes = []
            self.fail_on = fail_on

        class Key:
            def __init__(self, name):
                self.name = name

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        def CreateKey(self, root, subkey):
            return self.Key(subkey)

        def SetValueEx(self, key, _name, _reserved, _kind, value):
            if key.name == self.fail_on:
                raise OSError("denied")
            self.writes.append((key.name, value))

    executable = Path("C:/Program Files/ComponentPress/ComponentPress.exe")
    registry = Registry()
    assert register_executable(executable, registry) == (True, "REGISTRATION_SUCCEEDED")
    assert registry.writes[-1][1] == f'"{executable}" "%1"'
    assert registry.writes[2][1] == f'"{executable}",0'
    failed = Registry(fail_on=r"Software\Classes\ComponentPress.Project\DefaultIcon")
    assert register_executable(executable, failed) == (False, "REGISTRATION_FAILED")
    assert len(failed.writes) == 2
