"""Acceptance checks for the unified project Excel source and full-sheet validation."""

from __future__ import annotations

from pathlib import Path
import shutil

from openpyxl import load_workbook
import pytest

from componentpress.application.identifiers import instance_id
from componentpress.application.preview_service import PreviewService, ValidationIssues
from componentpress.bootstrap import project_repository
from componentpress.data_sources import XlsxReader
from componentpress.domain.diagnostics import ProjectError


DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo-excel-game"


def copy_demo(tmp_path: Path) -> Path:
    root = tmp_path / "game"
    shutil.copytree(DEMO, root)
    return root


def test_schema_three_rejects_old_project_without_rewriting_files(tmp_path: Path) -> None:
    root = copy_demo(tmp_path)
    project = root / "project.yaml"
    original = project.read_bytes()
    project.write_text(project.read_text(encoding="utf-8").replace("schema_version: 5", "schema_version: 2", 1), encoding="utf-8")
    old = project.read_bytes()
    with pytest.raises(ProjectError) as caught:
        project_repository().open(root)
    assert caught.value.diagnostic.code == "SCHEMA_UNSUPPORTED"
    assert project.read_bytes() == old and original != old


def test_validation_checks_zero_copy_rows_and_keeps_last_successful_snapshot(tmp_path: Path) -> None:
    root = copy_demo(tmp_path)
    snapshot = project_repository().open(root)
    preview = PreviewService(XlsxReader())
    component_id = "forest-card"
    book_path = root / "data/game.xlsx"
    book = load_workbook(book_path)
    sheet = book["Карты"]
    assert sheet["I3"].value == 0  # Prod excludes this row.
    sheet["D3"] = "assets/images/not-found.png"
    book.save(book_path)
    book.close()

    with pytest.raises(ValidationIssues) as caught:
        preview.refresh(snapshot, component_id)
    issue = caught.value.diagnostics[0]
    assert (issue.code, issue.sheet, issue.cell, issue.node_id) == ("RESOURCE_MISSING", "Карты", "D3", "leaf-art")
    assert preview._data == {}

    book = load_workbook(book_path)
    book["Карты"]["D3"] = "assets/images/animals/enemy/leaf.png"
    book.save(book_path)
    book.close()
    checked = preview.refresh(snapshot, component_id)
    assert checked is not None and len(checked.rows) == 2
    first_id = checked.rows[0].instance_id

    book = load_workbook(book_path)
    book["Карты"]["D3"] = "assets/images/still-missing.png"
    book.save(book_path)
    book.close()
    with pytest.raises(ValidationIssues):
        preview.refresh(snapshot, component_id)
    assert preview.data(snapshot, component_id) is checked
    assert first_id == instance_id(snapshot.model.id, component_id, "Карты", 2)


def test_uuidv5_tracks_project_component_sheet_and_excel_row() -> None:
    expected = instance_id("game", "card", "Карты", 2)
    assert instance_id("game", "card", "Карты", 2) == expected
    assert len(expected) == 36
    assert instance_id("game", "card", "Карты", 3) != expected
    assert instance_id("game", "card", "События", 2) != expected
    assert instance_id("game", "other-card", "Карты", 2) != expected
    assert instance_id("other-game", "card", "Карты", 2) != expected


def test_column_html_rejects_user_supplied_image_tags(tmp_path: Path) -> None:
    root = copy_demo(tmp_path)
    book_path = root / "data/game.xlsx"
    book = load_workbook(book_path)
    book["Карты"]["K2"] = '<p><img src="assets/images/animals/enemy/leaf.png" /></p>'
    book.save(book_path)
    book.close()
    snapshot = project_repository().open(root)
    with pytest.raises(ValidationIssues) as caught:
        PreviewService(XlsxReader()).refresh(snapshot, "forest-card")
    issue = caught.value.diagnostics[0]
    assert (issue.code, issue.cell, issue.node_id) == ("HTML_UNSUPPORTED", "K2", "heading")
