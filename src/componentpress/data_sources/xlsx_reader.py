"""Read one immutable XLSX byte sequence with formulas and cached values."""

from __future__ import annotations

from io import BytesIO
import hashlib
from pathlib import Path
import posixpath
from typing import Iterable
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.values import CellValue, DataRow, DataSheetSnapshot, FormulaState, value_to_text


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_REL_NS = {"p": "http://schemas.openxmlformats.org/package/2006/relationships"}


def _formula_xml_states(data: bytes, sheet: str) -> dict[str, tuple[FormulaState, str | None]]:
    try:
        with ZipFile(BytesIO(data)) as archive:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            rel_targets = {
                item.attrib["Id"]: item.attrib["Target"]
                for item in relationships.findall("p:Relationship", _REL_NS)
            }
            target = None
            for item in workbook.findall("m:sheets/m:sheet", _NS):
                if item.attrib.get("name") == sheet:
                    target = rel_targets.get(item.attrib.get(f"{{{_NS['r']}}}id", ""))
                    break
            if target is None:
                return {}
            normalized = target.lstrip("/")
            if not normalized.startswith("xl/"):
                normalized = posixpath.normpath(posixpath.join("xl", normalized))
            root = ET.fromstring(archive.read(normalized))
    except (BadZipFile, KeyError, ET.ParseError) as exc:
        raise ProjectError(Diagnostic("XLSX_INVALID", "не удалось прочитать структуру XLSX", Path("."), sheet=sheet)) from exc
    states: dict[str, tuple[FormulaState, str | None]] = {}
    for cell in root.findall(".//m:c", _NS):
        formula = cell.find("m:f", _NS)
        if formula is None:
            continue
        cached = cell.find("m:v", _NS)
        if cell.attrib.get("t") == "e":
            state = FormulaState.ERROR
        elif cached is None:
            state = FormulaState.MISSING
        elif cached.text is None:
            state = FormulaState.EMPTY
        else:
            state = FormulaState.CACHED
        states[cell.attrib["r"]] = (state, formula.text)
    return states


class XlsxReader:
    """No openpyxl objects escape this adapter and both workbooks are closed."""

    def sheet_names(self, path: Path) -> tuple[str, ...]:
        try:
            data = path.read_bytes()
            book = load_workbook(BytesIO(data), read_only=True, data_only=True)
            try:
                return tuple(book.sheetnames)
            finally:
                book.close()
        except (OSError, BadZipFile, ValueError) as exc:
            raise ProjectError(Diagnostic("XLSX_INVALID", str(exc), path)) from exc

    def read(
        self,
        path: Path,
        sheet: str,
        *,
        source: str = "main",
        id_column: str | None = None,
        copies_column: str | None = None,
        version: int = 1,
        request_id: int = 0,
        data: bytes | None = None,
    ) -> DataSheetSnapshot:
        if data is None:
            try:
                data = path.read_bytes()
            except FileNotFoundError as exc:
                raise ProjectError(Diagnostic("DATA_SOURCE_MISSING", "книга XLSX не найдена", path, source=source)) from exc
            except OSError as exc:
                raise ProjectError(Diagnostic("FILE_READ", str(exc), path, source=source)) from exc
        digest = hashlib.sha256(data).hexdigest()
        formulas = _formula_xml_states(data, sheet)
        formula_book = value_book = None
        try:
            formula_book = load_workbook(BytesIO(data), read_only=False, data_only=False)
            value_book = load_workbook(BytesIO(data), read_only=False, data_only=True)
            if sheet not in formula_book.sheetnames:
                raise ProjectError(Diagnostic("SHEET_UNKNOWN", f"лист {sheet!r} не найден", path, source=source, sheet=sheet))
            formula_sheet = formula_book[sheet]
            value_sheet = value_book[sheet]
            if any(item.max_row >= 2 for item in formula_sheet.merged_cells.ranges):
                merged = next(item for item in formula_sheet.merged_cells.ranges if item.max_row >= 2)
                raise ProjectError(Diagnostic("MERGED_CELLS_UNSUPPORTED", f"объединённые ячейки данных не поддерживаются: {merged}", path, source=source, sheet=sheet))
            max_column = formula_sheet.max_column
            headers: list[str] = []
            seen: set[str] = set()
            for column in range(1, max_column + 1):
                coordinate = f"{get_column_letter(column)}1"
                raw = formula_sheet.cell(1, column).value
                if not isinstance(raw, str) or not raw.strip():
                    raise ProjectError(Diagnostic("HEADER_INVALID", "заголовок должен быть непустым текстом", path, source=source, sheet=sheet, cell=coordinate))
                header = raw.strip()
                if "{" in header or "}" in header:
                    raise ProjectError(Diagnostic("HEADER_BRACES", "фигурные скобки в заголовке запрещены", path, source=source, sheet=sheet, cell=coordinate))
                if header in seen:
                    raise ProjectError(Diagnostic("HEADER_DUPLICATE", f"повторный заголовок {header!r}", path, source=source, sheet=sheet, cell=coordinate))
                seen.add(header)
                headers.append(header)
            for configured, field in ((id_column, "data.id_column"), (copies_column, "data.copies_column")):
                if configured is not None and configured not in seen:
                    raise ProjectError(Diagnostic("COLUMN_UNKNOWN", f"нет столбца {configured!r}", path, field, source=source, sheet=sheet))
            rows: list[DataRow] = []
            ids: set[str] = set()
            for row_number in range(2, formula_sheet.max_row + 1):
                cells: dict[str, CellValue] = {}
                occupied = False
                for column, header in enumerate(headers, 1):
                    coordinate = f"{get_column_letter(column)}{row_number}"
                    formula_raw = formula_sheet.cell(row_number, column).value
                    cached_value = value_sheet.cell(row_number, column).value
                    state, formula = formulas.get(coordinate, (FormulaState.NONE, None))
                    if state is FormulaState.NONE and formula_sheet.cell(row_number, column).data_type == "e":
                        state = FormulaState.ERROR
                    value = cached_value if state is not FormulaState.NONE else formula_raw
                    if formula_raw is not None or state is not FormulaState.NONE:
                        occupied = True
                    cells[header] = CellValue(value, coordinate, state, formula)
                if not occupied:
                    continue
                if id_column is None:
                    instance_id = str(row_number)
                else:
                    id_cell = cells[id_column]
                    self._require_usable(id_cell, path, source, sheet, id_column)
                    instance_id = value_to_text(id_cell.value)
                    if not instance_id:
                        raise ProjectError(Diagnostic("INSTANCE_ID_EMPTY", "ID экземпляра не может быть пустым", path, source=source, sheet=sheet, cell=id_cell.coordinate))
                    if instance_id in ids:
                        raise ProjectError(Diagnostic("INSTANCE_ID_DUPLICATE", f"повторный ID экземпляра {instance_id!r}", path, source=source, sheet=sheet, cell=id_cell.coordinate))
                ids.add(instance_id)
                copies = 1
                if copies_column is not None:
                    copy_cell = cells[copies_column]
                    self._require_usable(copy_cell, path, source, sheet, copies_column)
                    raw = copy_cell.value
                    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or int(raw) != raw or raw < 0:
                        raise ProjectError(Diagnostic("COPIES_INVALID", "тираж должен быть целым числом от 0", path, source=source, sheet=sheet, cell=copy_cell.coordinate))
                    copies = int(raw)
                rows.append(DataRow(row_number, instance_id, copies, cells))
            return DataSheetSnapshot(source, path, sheet, digest, version, tuple(headers), tuple(rows), request_id)
        except ProjectError:
            raise
        except (BadZipFile, OSError, ValueError, KeyError) as exc:
            raise ProjectError(Diagnostic("XLSX_INVALID", str(exc), path, source=source, sheet=sheet)) from exc
        finally:
            if formula_book is not None:
                formula_book.close()
            if value_book is not None:
                value_book.close()

    @staticmethod
    def _require_usable(cell: CellValue, path: Path, source: str, sheet: str, column: str) -> None:
        if cell.formula_state is FormulaState.MISSING:
            raise ProjectError(Diagnostic("FORMULA_RESULT_UNAVAILABLE", "нет сохранённого результата формулы", path, column, source=source, sheet=sheet, cell=cell.coordinate))
        if cell.formula_state is FormulaState.ERROR:
            raise ProjectError(Diagnostic("FORMULA_ERROR", f"ошибка Excel: {cell.value}", path, column, source=source, sheet=sheet, cell=cell.coordinate))
