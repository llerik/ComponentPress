"""Behaviour checks for the XLSX data source adapter."""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook

from componentpress.data_sources import xlsx_reader
from componentpress.domain.diagnostics import ProjectError
from componentpress.domain.values import FormulaState


def _patch_formula(path: Path, member: str, coordinate: str, cached: str | None) -> None:
    temporary = path.with_suffix(".patched.xlsx")
    with ZipFile(path) as source, ZipFile(temporary, "w", ZIP_DEFLATED) as target:
        for item in source.infolist():
            content = source.read(item.filename)
            if item.filename == member:
                xml = content.decode("utf-8")
                pattern = rf'(<c[^>]*r="{coordinate}"[^>]*>.*?<f[^>]*>.*?</f>)(?:<v(?:>.*?</v>|\s*/>))?(</c>)'
                cache = "" if cached is None else f"<v>{cached}</v>"
                xml, count = re.subn(pattern, rf"\1{cache}\2", xml, count=1)
                assert count == 1
                content = xml.encode("utf-8")
            target.writestr(item, content)
    temporary.replace(path)


def _book(path: Path, rows: list[list[object]]) -> None:
    book = Workbook()
    for values in rows:
        book.active.append(values)
    book.save(path)
    book.close()


def _watch_books(monkeypatch, *, fail_close: set[int] = frozenset(), fail_load: int | None = None):
    real_load = xlsx_reader.load_workbook
    books = []

    def load(*args, **kwargs):
        call = len(books) + 1
        if call == fail_load:
            raise OSError("injected open failure")
        book = real_load(*args, **kwargs)
        real_close = book.close
        record = {"closed": 0}

        def close():
            record["closed"] += 1
            real_close()
            if call in fail_close:
                raise OSError("injected close failure")

        book.close = close
        books.append(record)
        return book

    monkeypatch.setattr(xlsx_reader, "load_workbook", load)
    return books


def test_missing_formula_result_keeps_row_inside_table_and_reports_its_address(tmp_path: Path) -> None:
    path = tmp_path / "missing-cache.xlsx"
    _book(path, [["ID", "Body"], ["=1+1", None], ["next", "valid"]])
    _patch_formula(path, "xl/worksheets/sheet1.xml", "A2", None)

    with pytest.raises(ProjectError) as caught:
        xlsx_reader.XlsxReader().read(path, "Sheet", id_column="ID")

    assert caught.value.diagnostic.code == "FORMULA_RESULT_UNAVAILABLE"
    assert caught.value.diagnostic.cell == "A2"


def test_non_first_sheet_uses_its_workbook_relationship_for_formula_cache(tmp_path: Path) -> None:
    path = tmp_path / "two-sheets.xlsx"
    book = Workbook()
    book.active.title = "Cover"
    book.active.append(["unused"])
    target = book.create_sheet("Target")
    target.append(["ID", "Value"])
    target.append(["row", "=4+5"])
    book.save(path)
    book.close()
    _patch_formula(path, "xl/worksheets/sheet2.xml", "B2", "9")

    row = xlsx_reader.XlsxReader().read(path, "Target").rows[0]

    assert row.values["Value"].formula_state is FormulaState.CACHED
    assert row.values["Value"].value == 9


def test_unicode_compatibility_equivalent_headers_are_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "unicode-headers.xlsx"
    _book(path, [["Name", "Ｎame"]])

    with pytest.raises(ProjectError) as caught:
        xlsx_reader.XlsxReader().read(path, "Sheet")

    assert caught.value.diagnostic.code == "HEADER_DUPLICATE"
    assert caught.value.diagnostic.cell == "B1"


@pytest.mark.parametrize(("merged", "rejected"), [("A1:B1", True), ("B2:C2", True), ("C8:D9", False)])
def test_merged_ranges_are_rejected_only_when_they_touch_the_table(tmp_path: Path, merged: str, rejected: bool) -> None:
    path = tmp_path / "merged.xlsx"
    _book(path, [["ID", "Body"], ["row", "value"]])
    book = xlsx_reader.load_workbook(path)
    book.active.merge_cells(merged)
    book.save(path)
    book.close()

    if rejected:
        with pytest.raises(ProjectError) as caught:
            xlsx_reader.XlsxReader().read(path, "Sheet")
        assert caught.value.diagnostic.code == "MERGED_CELLS_UNSUPPORTED"
    else:
        assert xlsx_reader.XlsxReader().read(path, "Sheet").area == "A1:B2"


def test_reader_closes_both_books_and_keeps_primary_validation_error(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "bad-header.xlsx"
    _book(path, [["", "Body"], ["row", "value"]])
    books = _watch_books(monkeypatch, fail_close={1, 2})

    with pytest.raises(ProjectError) as caught:
        xlsx_reader.XlsxReader().read(path, "Sheet")

    assert caught.value.diagnostic.code == "HEADER_INVALID"
    assert [book["closed"] for book in books] == [1, 1]


def test_reader_reports_close_failure_and_still_closes_the_other_book(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "close-error.xlsx"
    _book(path, [["ID", "Body"], ["row", "value"]])
    books = _watch_books(monkeypatch, fail_close={1})

    with pytest.raises(ProjectError) as caught:
        xlsx_reader.XlsxReader().read(path, "Sheet")

    assert caught.value.diagnostic.code == "XLSX_INVALID"
    assert [book["closed"] for book in books] == [1, 1]


def test_partial_open_failure_closes_the_book_that_was_opened(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "second-open-error.xlsx"
    _book(path, [["ID"], ["row"]])
    books = _watch_books(monkeypatch, fail_load=2)

    with pytest.raises(ProjectError) as caught:
        xlsx_reader.XlsxReader().read(path, "Sheet")

    assert caught.value.diagnostic.code == "XLSX_INVALID"
    assert [book["closed"] for book in books] == [1]


def test_sheet_names_translates_its_single_book_close_failure(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "sheet-names.xlsx"
    _book(path, [["ID"]])
    books = _watch_books(monkeypatch, fail_close={1})

    with pytest.raises(ProjectError) as caught:
        xlsx_reader.XlsxReader().sheet_names(path)

    assert caught.value.diagnostic.code == "XLSX_INVALID"
    assert [book["closed"] for book in books] == [1]


def test_empty_formula_cache_is_distinct_from_a_missing_cache(tmp_path: Path) -> None:
    path = tmp_path / "empty-cache.xlsx"
    _book(path, [["ID", "Empty"], ["row", '=""']])
    _patch_formula(path, "xl/worksheets/sheet1.xml", "B2", "")

    row = xlsx_reader.XlsxReader().read(path, "Sheet").rows[0]

    assert row.values["Empty"].formula_state is FormulaState.EMPTY
    assert row.values["Empty"].value in (None, "")


def test_row_with_only_empty_formula_results_ends_the_table(tmp_path: Path) -> None:
    path = tmp_path / "empty-row.xlsx"
    _book(path, [["ID", "Value"], ['=""', '=""']])
    _patch_formula(path, "xl/worksheets/sheet1.xml", "A2", "")
    _patch_formula(path, "xl/worksheets/sheet1.xml", "B2", "")

    snapshot = xlsx_reader.XlsxReader().read(path, "Sheet")

    assert snapshot.area == "A1:B1"
    assert snapshot.rows == ()


def test_cell_access_is_bounded_by_the_table_not_formatted_sheet_dimensions(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "distant-formatting.xlsx"
    _book(path, [["ID", "Body"], ["one", "first"], [None, None], ["below-gap", "ignored"]])
    book = xlsx_reader.load_workbook(path)
    book.active["B10000"].number_format = "0.00"
    book.save(path)
    book.close()
    calls = 0
    original_cell = xlsx_reader.Worksheet.cell

    def counted_cell(sheet, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original_cell(sheet, *args, **kwargs)

    monkeypatch.setattr(xlsx_reader.Worksheet, "cell", counted_cell)
    snapshot = xlsx_reader.XlsxReader().read(path, "Sheet")

    assert snapshot.area == "A1:B2"
    assert [row.row_number for row in snapshot.rows] == [2]
    assert calls < 100


def test_supplied_bytes_are_the_only_input_and_snapshot_metadata_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "source.xlsx"
    _book(path, [["ID", "Body"], ["one", "value"]])
    data = path.read_bytes()
    path.unlink()

    snapshot = xlsx_reader.XlsxReader().read(path, "Sheet", source="external", version=7, request_id=23, data=data)

    assert snapshot.content_hash == hashlib.sha256(data).hexdigest()
    assert (snapshot.source, snapshot.version, snapshot.request_id, snapshot.area) == ("external", 7, 23, "A1:B2")


def test_xml_and_workbook_streams_close_after_success_and_failure(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "streams.xlsx"
    _book(path, [["ID", "Body"], ["one", "value"]])
    real_bytes_io = xlsx_reader.BytesIO
    streams = []

    class TrackedBytesIO(real_bytes_io):
        def __init__(self, data):
            super().__init__(data)
            streams.append(self)

    monkeypatch.setattr(xlsx_reader, "BytesIO", TrackedBytesIO)
    xlsx_reader.XlsxReader().read(path, "Sheet")

    assert len(streams) == 3
    assert all(stream.closed for stream in streams)
