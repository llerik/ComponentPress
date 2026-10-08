"""Observable stage-one project behavior and failure cases."""

from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys

import pytest

from componentpress.bindings.grammar import GrammarError, parse_references
from componentpress.domain.diagnostics import ProjectError
from componentpress.project_io.repository import FileProjectRepository
from componentpress.project_io.files import file_hash
from componentpress.project_io.yaml_codec import parse_component, parse_project
from componentpress.resources.repository import ResourceRepository


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "demo-game"


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "componentpress", *args], text=True, capture_output=True, encoding="utf-8")


def demo(tmp_path: Path) -> Path:
    root = tmp_path / "Игра с пробелом"
    shutil.copytree(EXAMPLE, root)
    return root


def test_cli_new_validate_register_and_nested_import_after_move(tmp_path: Path) -> None:
    root = tmp_path / "Новая игра"
    assert cli("new", str(root), "--name", "Лесная игра").returncode == 0
    assert cli("add-component", str(root), "card", "Карта").returncode == 0
    picture = EXAMPLE / "assets/images/animals/enemy/leaf.png"
    imported = cli("import-image", str(root), str(picture), "--into", "animals/enemy")
    assert imported.returncode == 0, imported.stderr
    assert "assets/images/animals/enemy/leaf.png" in imported.stdout
    destination = tmp_path / "Переезд" / "Игра"
    destination.parent.mkdir()
    shutil.move(root, destination)
    validated = cli("validate", str(destination))
    assert validated.returncode == 0, validated.stderr
    assert "компонентов: 1" in validated.stdout


def test_comments_group_ids_and_values_survive_save_cycle(tmp_path: Path) -> None:
    root = demo(tmp_path)
    repo = FileProjectRepository()
    snapshot = repo.open(root)
    assert snapshot.model.icons["ic_leaf_6"].width_mm == 6
    assert snapshot.documents["forest-card"].model.elements[1].children[1].id == "caption"
    snapshot = repo.save_project(snapshot, name="Новое название")
    size = snapshot.documents["forest-card"].model.size_mm.model_copy(update={"width": 70.5})
    repo.save_component(snapshot, "forest-card", size_mm=size)
    project_text = (root / "project.yaml").read_text(encoding="utf-8")
    component_text = (root / "components/forest-card.yaml").read_text(encoding="utf-8")
    assert "# Пример проекта второго этапа" in project_text
    assert "# Статический макет с вложенной группой" in component_text
    assert "ic_leaf_6" in project_text
    assert "{{ vars[\"edition\"] }}" in component_text
    reopened = repo.open(root)
    assert reopened.model.name == "Новое название"
    assert reopened.documents["forest-card"].model.size_mm.width == 70.5
    assert reopened.documents["forest-card"].model.elements[1].children[1].id == "caption"


@pytest.mark.parametrize(
    ("replacement", "code"),
    [
        ("schema_version: 2", "SCHEMA_UNSUPPORTED"),
        ("name: [", "YAML_SYNTAX"),
        ("width: -1", "DOCUMENT_INVALID"),
        ("id: heading", "NODE_ID_DUPLICATE"),
        ('{{ row["Название"] }}', "ROW_SYNTAX_UNSUPPORTED"),
        ("&anchor Лесная карта", "YAML_UNSUPPORTED"),
    ],
)
def test_invalid_component_has_diagnostic_and_is_not_rewritten(tmp_path: Path, replacement: str, code: str) -> None:
    root = demo(tmp_path)
    path = root / "components/forest-card.yaml"
    original = path.read_text(encoding="utf-8")
    if code == "SCHEMA_UNSUPPORTED":
            broken = original.replace("schema_version: 4", replacement)
    elif code == "YAML_SYNTAX":
        broken = original.replace('name: "Лесная карта"', replacement)
    elif code == "DOCUMENT_INVALID":
        broken = original.replace("width: 63", replacement)
    elif code == "NODE_ID_DUPLICATE":
        broken = original.replace("id: caption", replacement)
    elif code == "YAML_UNSUPPORTED":
        broken = original.replace('name: "Лесная карта"', f'name: {replacement}')
    else:
        broken = original.replace('vars["edition"]', 'row["Название"]')
    path.write_text(broken, encoding="utf-8")
    result = cli("validate", str(root))
    assert result.returncode == 2
    assert code in result.stderr
    assert "forest-card.yaml" in result.stderr
    assert path.read_text(encoding="utf-8") == broken


def test_old_row_syntax_reports_field_and_line(tmp_path: Path) -> None:
    root = demo(tmp_path)
    path = root / "components/forest-card.yaml"
    text = path.read_text(encoding="utf-8").replace('vars["edition"]', 'row["Название"]')
    with pytest.raises(ProjectError) as caught:
        parse_component(text, path, {"edition": "x"})
    assert caught.value.diagnostic.code == "ROW_SYNTAX_UNSUPPORTED"
    assert caught.value.diagnostic.field == "elements.1.children.1.html"
    assert caught.value.diagnostic.line is not None


def test_icon_and_portable_path_errors(tmp_path: Path) -> None:
    root = demo(tmp_path)
    path = root / "project.yaml"
    original = path.read_text(encoding="utf-8")
    for broken, code in [
        (original.replace("ic_leaf_6", "leaf"), "DOCUMENT_INVALID"),
        (original.replace("width_mm: 6", "width_mm: 0"), "DOCUMENT_INVALID"),
        (original.replace("animals/enemy/leaf.png", "../leaf.png"), "PATH_INVALID"),
        (original.replace("animals/enemy/leaf.png", "animals/enemy/leaf.svg"), "ICON_PATH"),
    ]:
        with pytest.raises(ProjectError) as caught:
            parse_project(broken, path)
        assert caught.value.diagnostic.code == code


def test_external_edit_blocks_save_without_overwrite(tmp_path: Path) -> None:
    root = demo(tmp_path)
    repo = FileProjectRepository()
    snapshot = repo.open(root)
    path = root / "project.yaml"
    external = path.read_text(encoding="utf-8") + "# Изменено снаружи\n"
    path.write_text(external, encoding="utf-8")
    with pytest.raises(ProjectError) as caught:
        repo.save_project(snapshot, name="Не сохранится")
    assert caught.value.diagnostic.code == "FILE_CHANGED_EXTERNALLY"
    assert path.read_text(encoding="utf-8") == external


def test_import_never_overwrites_and_rejects_traversal(tmp_path: Path) -> None:
    root = demo(tmp_path)
    source = EXAMPLE / "assets/images/animals/enemy/leaf.png"
    resources = ResourceRepository()
    with pytest.raises(ProjectError) as caught:
        resources.import_image(root, source, "animals/enemy")
    assert caught.value.diagnostic.code == "FILE_EXISTS"
    with pytest.raises(ProjectError) as caught:
        resources.import_image(root, source, "../outside")
    assert caught.value.diagnostic.code == "PATH_INVALID"
    fake = tmp_path / "fake.png"
    fake.write_text("not an image", encoding="utf-8")
    with pytest.raises(ProjectError) as caught:
        resources.import_image(root, fake)
    assert caught.value.diagnostic.code == "RESOURCE_INVALID"


def test_grammar_accepts_only_public_forms() -> None:
    refs = parse_references(r'\{literal\} { Название } {{ vars["edition"] }}')
    assert [(ref.kind, ref.name) for ref in refs] == [("column", "Название"), ("variable", "edition")]
    for invalid in ('{{ row["Название"] }}', '{{ 1+2 }}', '{broken', 'unexpected }'):
        with pytest.raises(GrammarError):
            parse_references(invalid)


def test_column_content_requires_a_sheet_binding(tmp_path: Path) -> None:
    root = demo(tmp_path)
    project = root / "project.yaml"
    project.write_text(project.read_text(encoding="utf-8").replace("data_source: null", "data_source:\n  path: data/game.xlsx"), encoding="utf-8")
    (root / "data").mkdir(exist_ok=True)
    shutil.copy2(EXAMPLE.parent / "demo-excel-game" / "data" / "game.xlsx", root / "data" / "game.xlsx")
    component = root / "components/forest-card.yaml"
    text = component.read_text(encoding="utf-8").replace(
        'content:\n          mode: manual\n          html: |-\n            <p><b>Лесная карта</b></p>',
        'content:\n          mode: column\n          column: "Название"',
    )
    component.write_text(text, encoding="utf-8")
    with pytest.raises(ProjectError) as caught:
        FileProjectRepository().open(root)
    assert caught.value.diagnostic.code == "COLUMN_WITHOUT_DATA"
    component.write_text(text.replace("background: \"#FFFFFF\"", 'background: "#FFFFFF"\ndata:\n  sheet: "Карты"'), encoding="utf-8")
    assert cli("validate", str(root)).returncode == 0


def test_interrupted_registration_removes_only_its_unchanged_file(tmp_path: Path) -> None:
    root = demo(tmp_path)
    orphan = root / "components/orphan.yaml"
    content = b"orphan\n"
    orphan.write_bytes(content)
    journal = root / ".componentpress/transactions/add-component.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({
        "component": "components/orphan.yaml",
        "component_hash": hashlib.sha256(content).hexdigest(),
        "old_project_hash": file_hash(root / "project.yaml"),
        "new_project_hash": "not-committed",
    }), encoding="utf-8")
    FileProjectRepository().open(root)
    assert not orphan.exists()
    assert not journal.exists()
    assert (root / "components/forest-card.yaml").exists()


def test_conflicting_interrupted_registration_preserves_files(tmp_path: Path) -> None:
    root = demo(tmp_path)
    orphan = root / "components/orphan.yaml"
    orphan.write_text("user changed this file", encoding="utf-8")
    journal = root / ".componentpress/transactions/add-component.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({
        "component": "components/orphan.yaml",
        "component_hash": "different-hash",
        "old_project_hash": file_hash(root / "project.yaml"),
        "new_project_hash": "not-committed",
    }), encoding="utf-8")
    with pytest.raises(ProjectError) as caught:
        FileProjectRepository().open(root)
    assert caught.value.diagnostic.code == "TRANSACTION_CONFLICT"
    assert orphan.read_text(encoding="utf-8") == "user changed this file"
    assert journal.exists()


def test_invalid_size_is_not_saved_and_case_mismatch_is_rejected(tmp_path: Path) -> None:
    root = demo(tmp_path)
    repo = FileProjectRepository()
    snapshot = repo.open(root)
    document = root / "components/forest-card.yaml"
    original = document.read_bytes()
    bad_size = snapshot.documents["forest-card"].model.size_mm.model_copy(update={"width": -5})
    with pytest.raises(ProjectError):
        repo.save_component(snapshot, "forest-card", size_mm=bad_size)
    assert document.read_bytes() == original
    with pytest.raises(ProjectError) as caught:
        ResourceRepository().import_image(root, EXAMPLE / "assets/images/animals/enemy/leaf.png", "animals/Enemy")
    assert caught.value.diagnostic.code == "PATH_CASE_MISMATCH"


def test_project_edits_preserve_document_validity_and_remove_deleted_icon(tmp_path: Path) -> None:
    root = demo(tmp_path)
    repo = FileProjectRepository()
    snapshot = repo.open(root)
    original = (root / "project.yaml").read_bytes()
    with pytest.raises(ProjectError) as caught:
        repo.save_project(snapshot, variables={})
    assert caught.value.diagnostic.code == "VARIABLE_UNKNOWN"
    assert (root / "project.yaml").read_bytes() == original
    saved = repo.save_project(snapshot, icons={})
    assert saved.model.icons == {}
    assert "ic_leaf_6" not in (root / "project.yaml").read_text(encoding="utf-8")


def test_component_save_checks_project_revision(tmp_path: Path) -> None:
    root = demo(tmp_path)
    repo = FileProjectRepository()
    snapshot = repo.open(root)
    project = root / "project.yaml"
    project.write_text(project.read_text(encoding="utf-8") + "# external\n", encoding="utf-8")
    document = root / "components/forest-card.yaml"
    original = document.read_bytes()
    with pytest.raises(ProjectError) as caught:
        repo.save_component(snapshot, "forest-card", name="Изменено")
    assert caught.value.diagnostic.code == "FILE_CHANGED_EXTERNALLY"
    assert document.read_bytes() == original


def test_project_reference_does_not_follow_symlink(tmp_path: Path) -> None:
    root = demo(tmp_path)
    picture = root / "assets/images/animals/enemy/leaf.png"
    link = root / "assets/images/animals/enemy/linked.png"
    try:
        link.symlink_to(picture)
    except OSError:
        pytest.skip("создание symlink недоступно в этой среде")
    project = root / "project.yaml"
    project.write_text(project.read_text(encoding="utf-8").replace("animals/enemy/leaf.png", "animals/enemy/linked.png"), encoding="utf-8")
    with pytest.raises(ProjectError) as caught:
        FileProjectRepository().open(root)
    assert caught.value.diagnostic.code == "PATH_SYMLINK"
