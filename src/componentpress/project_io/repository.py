"""Project creation, strict opening and comment-preserving saves."""

from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
from types import MappingProxyType
import re
import unicodedata

from componentpress.application.contracts import DocumentSnapshot, ProjectSnapshot, RemovedComponent
from componentpress.domain.component import ComponentDefinition
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.html_resources import html_image_sources
from componentpress.domain.nodes import GroupNode, HtmlNode, ImageNode, Node
from componentpress.domain.project import ProjectDefinition
from componentpress.domain.schema import SCHEMA_VERSION
from componentpress.bindings.grammar import parse_references
from componentpress.platforms.services import ProjectWriteLock
from .files import atomic_create, atomic_write, file_hash
from .paths import PROJECT_MANIFEST, check_case_collisions, check_exact_case, project_manifest_path, resolve_project_path
from .transactions import change_component_membership, recover, recover_add_component, register_component
from .migrations import migrate_v1_to_v2, migration_required, recover_migration, recovery_required
from .schema_checks import preflight_project_schemas
from .yaml_codec import _load, dump_yaml, parse_component, parse_project, update_tree


def _read_text(path: Path) -> tuple[str, str]:
    try:
        data = path.read_bytes()
        return data.decode("utf-8"), hashlib.sha256(data).hexdigest()
    except UnicodeDecodeError as exc:
        raise ProjectError(Diagnostic("TEXT_ENCODING", "файл должен быть UTF-8", path)) from exc
    except OSError as exc:
        raise ProjectError(Diagnostic("FILE_READ", str(exc), path)) from exc


def _journal_uses_backup(journal: Path, backup_path: str) -> bool:
    try:
        return json.loads(journal.read_text(encoding="utf-8")).get("backup") == backup_path
    except (OSError, ValueError, AttributeError):
        return True


def _registration_journal(root: Path) -> Path:
    return root / ".componentpress" / "transactions" / "add-component.json"


def _pending_recovery(root: Path) -> bool:
    transactions = root / ".componentpress" / "transactions"
    return any((
        _registration_journal(root).exists(),
        (transactions / "component-membership.json").exists(),
        any((transactions / "resource-imports").glob("*.json")),
        (root / ".componentpress" / "migrations" / "v1-to-v2-active.json").exists(),
        any((root / ".componentpress" / "versioning").glob("*/publish.json")),
    ))


def _other_recovery_pending(root: Path) -> bool:
    transactions = root / ".componentpress" / "transactions"
    return ((transactions / "component-membership.json").exists()
            or (root / ".componentpress" / "migrations" / "v1-to-v2-active.json").exists()
            or any((transactions / "resource-imports").glob("*.json"))
            or any((root / ".componentpress" / "versioning").glob("*/publish.json")))


def _assert_snapshot_current(snapshot: ProjectSnapshot) -> None:
    project = snapshot.root / PROJECT_MANIFEST
    _assert_file_hash(project, snapshot.disk_hash, "описание проекта изменено после чтения")
    for document in snapshot.documents.values():
        _assert_file_hash(document.path, document.disk_hash, "компонент изменён после чтения")


def _verified_open_snapshot(snapshot: ProjectSnapshot) -> ProjectSnapshot:
    _assert_snapshot_current(snapshot)
    return snapshot


def _assert_file_hash(path: Path, expected: str, message: str) -> None:
    try:
        unchanged = file_hash(path) == expected
    except OSError:
        unchanged = False
    if not unchanged:
        raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", message, path))


def _editor_project(root: Path):
    check_case_collisions(root)
    path = project_manifest_path(root)
    text, digest = _read_text(path)
    model, _ = parse_project(text, path)
    if migration_required(root):
        raise ProjectError(Diagnostic("MIGRATION_REQUIRED", "проект формата 1; подтвердите миграцию в формат 2", path, "schema_version"))
    paths = (model.data_source.path,) if model.data_source else ()
    paths += (*model.images, *(icon.path for icon in model.icons.values()))
    for relative in paths:
        check_exact_case(root, relative)
        resolve_project_path(root, relative)
    return path, text, digest, model


def _editor_component_path(root: Path, ref, project_path: Path, seen: set[str]) -> Path:
    if not ref.path.startswith("components/") or not ref.path.endswith(".yaml"):
        raise ProjectError(Diagnostic("COMPONENT_PATH", "документ должен находиться в components/ и иметь .yaml", project_path, f"components.{ref.id}.path"))
    if ref.path.casefold() in seen:
        raise ProjectError(Diagnostic("COMPONENT_PATH_DUPLICATE", "повторный путь компонента", project_path, f"components.{ref.id}.path"))
    seen.add(ref.path.casefold())
    check_exact_case(root, ref.path)
    return resolve_project_path(root, ref.path)


def _editor_node_diagnostics(root: Path, path: Path, nodes: tuple[Node, ...]) -> list[Diagnostic]:
    diagnostics = []
    for node in nodes:
        candidates = (node.source,) if isinstance(node, ImageNode) and node.source_mode == "manual" else ()
        if isinstance(node, HtmlNode) and node.content_mode == "manual":
            candidates = html_image_sources(node.html)
        if isinstance(node, GroupNode):
            diagnostics.extend(_editor_node_diagnostics(root, path, node.children))
        for candidate in candidates:
            if parse_references(candidate):
                continue
            relative = candidate.replace(r"\{", "{").replace(r"\}", "}")
            try:
                check_exact_case(root, relative)
                resolve_project_path(root, relative)
            except ProjectError as exc:
                diagnostics.append(exc.diagnostic)
    return diagnostics


def _editor_component(root: Path, project_path: Path, ref, project_model, seen: set[str]) -> DocumentSnapshot:
    path = _editor_component_path(root, ref, project_path, seen)
    text, digest = _read_text(path)
    try:
        model, _ = parse_component(text, path, project_model.variables)
    except ProjectError as exc:
        return DocumentSnapshot(path, text, digest, None, (exc.diagnostic,))
    diagnostics = []
    if model.id != ref.id:
        diagnostics.append(Diagnostic("COMPONENT_ID_MISMATCH", "ID в реестре не совпадает с документом", path, "id"))
    if model.data and project_model.data_source is None:
        diagnostics.append(Diagnostic("DATA_SOURCE_UNKNOWN", "задайте XLSX в настройках проекта", path, "data.sheet"))
    diagnostics.extend(_editor_node_diagnostics(root, path, model.elements))
    return DocumentSnapshot(path, text, digest, model, tuple(diagnostics))


def _recover_registration_before_open(root: Path) -> None:
    if _registration_journal(root).exists():
        with ProjectWriteLock(root):
            preflight_project_schemas(root)
            recover_add_component(root)


@contextmanager
def _registration_write_lock(root: Path):
    with ProjectWriteLock(root):
        if _pending_recovery(root):
            if _other_recovery_pending(root):
                raise ProjectError(Diagnostic("RECOVERY_PENDING", "завершите восстановление проекта перед записью", root))
            preflight_project_schemas(root)
        recover_add_component(root)
        yield


def _strict_project_model(root: Path, project_path: Path):
    check_case_collisions(root)
    text, digest = _read_text(project_path)
    model, _ = parse_project(text, project_path)
    paths = (model.data_source.path,) if model.data_source else ()
    paths += (*model.images, *(icon.path for icon in model.icons.values()))
    for relative in paths:
        check_exact_case(root, relative)
        resolve_project_path(root, relative)
    return text, digest, model


def _check_strict_node_paths(root: Path, path: Path, nodes: tuple[Node, ...]) -> None:
    for node in nodes:
        if isinstance(node, ImageNode) and node.source_mode == "manual":
            candidates = (node.source,)
        elif isinstance(node, HtmlNode) and node.content_mode == "manual":
            candidates = html_image_sources(node.html)
        elif isinstance(node, GroupNode):
            _check_strict_node_paths(root, path, node.children)
            continue
        else:
            continue
        for candidate in candidates:
            if parse_references(candidate):
                continue
            relative = candidate.replace(r"\{", "{").replace(r"\}", "}")
            check_exact_case(root, relative)
            resolve_project_path(root, relative)


def _strict_component(root: Path, project_path: Path, project_model, ref, seen: set[str]):
    if not ref.path.startswith("components/") or not ref.path.endswith(".yaml"):
        raise ProjectError(Diagnostic("COMPONENT_PATH", "документ должен находиться в components/ и иметь .yaml", project_path, f"components.{ref.id}.path"))
    key = ref.path.casefold()
    if key in seen:
        raise ProjectError(Diagnostic("COMPONENT_PATH_DUPLICATE", "повторный путь компонента", project_path, f"components.{ref.id}.path"))
    seen.add(key)
    check_exact_case(root, ref.path)
    path = resolve_project_path(root, ref.path)
    text, digest = _read_text(path)
    model, _ = parse_component(text, path, project_model.variables)
    _check_strict_node_paths(root, path, model.elements)
    if model.id != ref.id:
        raise ProjectError(Diagnostic("COMPONENT_ID_MISMATCH", "ID в реестре не совпадает с документом", path, "id"))
    if model.data and project_model.data_source is None:
        raise ProjectError(Diagnostic("DATA_SOURCE_UNKNOWN", "задайте XLSX в настройках проекта", path, "data.sheet"))
    return ref.id, DocumentSnapshot(path, text, digest, model)


def _strict_open_snapshot(root: Path, project_path: Path) -> ProjectSnapshot:
    text, digest, model = _strict_project_model(root, project_path)
    seen: set[str] = set()
    documents = dict(_strict_component(root, project_path, model, ref, seen) for ref in model.components)
    snapshot = ProjectSnapshot(root, text, digest, model, MappingProxyType(documents))
    return _verified_open_snapshot(snapshot)


def _recover_project_transactions(root: Path) -> None:
    transactions = root / ".componentpress" / "transactions"
    resource_journals = transactions / "resource-imports"
    if ((transactions / "component-membership.json").exists()
            or (resource_journals.exists() and any(resource_journals.glob("*.json")))):
        with ProjectWriteLock(root):
            preflight_project_schemas(root)
            recover(root)


def _recover_migration_before_open(root: Path, project_path: Path) -> None:
    if recovery_required(root):
        with ProjectWriteLock(root):
            preflight_project_schemas(root)
            recover_migration(root)
    if migration_required(root):
        raise ProjectError(Diagnostic(
            "MIGRATION_REQUIRED", "проект формата 1; подтвердите миграцию в формат 2",
            project_path, "schema_version",
        ))


def _new_project_template(project_id: str, name: str) -> str:
    return (
        f"schema_version: {SCHEMA_VERSION}\n"
        f"id: {project_id}\n"
        f"name: {json.dumps(name, ensure_ascii=False)}\n"
        'version: "1.0"\n'
        "variables: {}\nimages: []\nicons: {}\ndata_source: null\n"
        "copies_columns:\n  prod: Prod\n  test: Debug\ncomponents: []\n"
        "print:\n  paper: A4\n  orientation: portrait\n  margin_mm: 5\n"
        "  gap_mm: 3\n  dpi: 300\n  cut_lines: true\n"
        "  cut_line_width_mm: 0.2\n"
    )


def _write_project_manifest(root: Path, name: str, project_id: str) -> None:
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString

    path = root / PROJECT_MANIFEST
    _model, tree = parse_project(_new_project_template(project_id, name), path)
    tree["name"] = DoubleQuotedScalarString(name)
    atomic_create(path, dump_yaml(tree).encode("utf-8"))


def _create_project_directories(root: Path, name: str) -> Path:
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        raise ProjectError(Diagnostic("PROJECT_EXISTS", "папка не пуста", root))
    if not name.strip():
        raise ProjectError(Diagnostic("PROJECT_NAME", "название не может быть пустым", root))
    root.mkdir(parents=True, exist_ok=True)
    for folder in ("components", "data", "assets/images", "assets/fonts"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    return root


def _new_component_text(component_id: str, name: str, values: dict) -> str:
    from componentpress.domain.component import ComponentDefinition, DataBinding, SizeMM
    from io import StringIO
    from ruamel.yaml import YAML

    component = ComponentDefinition(
        schema_version=SCHEMA_VERSION, id=component_id, name=name,
        size_mm=SizeMM(width=float(values.get("width_mm", 63)), height=float(values.get("height_mm", 88))),
        data=values["data"] if isinstance(values.get("data"), DataBinding) else None,
    )
    stream = StringIO()
    YAML().dump(component.model_dump(mode="json", exclude_none=True, exclude={"data": {"source"}}), stream)
    return stream.getvalue()


def _prepare_removal(snapshot: ProjectSnapshot, component_id: str):
    reference = next((item for item in snapshot.model.components if item.id == component_id), None)
    document = snapshot.documents.get(component_id)
    if reference is None or document is None:
        raise ProjectError(Diagnostic("COMPONENT_UNKNOWN", "компонент не найден", snapshot.root))
    if len(snapshot.model.components) <= 1:
        raise ProjectError(Diagnostic("COMPONENT_LAST", "нельзя удалить последний компонент проекта", snapshot.root))
    digest = hashlib.sha256(document.path.read_bytes()).hexdigest()
    project_model, tree = parse_project(snapshot.text, snapshot.root / PROJECT_MANIFEST)
    position = next(i for i, item in enumerate(project_model.components) if item.id == component_id)
    tree["components"].pop(position)
    new_text = dump_yaml(tree)
    checked_project, _ = parse_project(new_text, snapshot.root / PROJECT_MANIFEST)
    return reference, document, digest, new_text, checked_project, position


def _prepare_restoration(snapshot: ProjectSnapshot, component_id: str, reference_path: str,
                         backup_path: str, component_hash: str, position: int):
    from ruamel.yaml.comments import CommentedMap

    backup = resolve_project_path(snapshot.root, backup_path)
    if not backup.is_file() or file_hash(backup) != component_hash:
        raise ProjectError(Diagnostic("COMPONENT_HISTORY", "снимок удалённого компонента отсутствует или изменён", backup))
    component_text = backup.read_text(encoding="utf-8")
    component_model, _ = parse_component(component_text, snapshot.root / reference_path, snapshot.model.variables)
    if component_id in snapshot.documents or any(ref.path.casefold() == reference_path.casefold() for ref in snapshot.model.components):
        raise ProjectError(Diagnostic("COMPONENT_HISTORY_CONFLICT", "ID или путь компонента уже используется", snapshot.root / reference_path))
    project_model, tree = parse_project(snapshot.text, snapshot.root / PROJECT_MANIFEST)
    tree["components"].insert(max(0, min(position, len(tree["components"]))), CommentedMap(id=component_id, path=reference_path))
    project_text = dump_yaml(tree)
    checked_project, _ = parse_project(project_text, snapshot.root / PROJECT_MANIFEST)
    return project_text, checked_project, component_text, component_model


def _publish_membership_change(snapshot: ProjectSnapshot, operation: str, reference_path: str,
                               backup_path: str, component_hash: str, project_text: str,
                               document: DocumentSnapshot | None = None) -> None:
    with _registration_write_lock(snapshot.root):
        if file_hash(snapshot.root / PROJECT_MANIFEST) != snapshot.disk_hash:
            raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "описание проекта изменено после чтения", snapshot.root / PROJECT_MANIFEST))
        if document and file_hash(document.path) != document.disk_hash:
            raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "компонент изменён после чтения", document.path))
        change_component_membership(
            snapshot.root, operation=operation, relative_path=reference_path,
            backup_path=backup_path, project_bytes=project_text.encode("utf-8"),
            old_project_hash=snapshot.disk_hash, component_hash=component_hash,
        )


def _membership_snapshot(root: Path, text: str, model, documents) -> ProjectSnapshot:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return ProjectSnapshot(root, text, digest, model, MappingProxyType(documents))


class FileProjectRepository:
    def __init__(self) -> None:
        self.version_recovery_diagnostics: tuple[Diagnostic, ...] = ()

    def create(self, root: Path, name: str) -> ProjectSnapshot:
        root = _create_project_directories(root, name)
        project_id = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", root.name).lower()).strip("-") or "game"
        _write_project_manifest(root, name, project_id)
        return self.open(root)

    def _recover_before_open(self, root: Path, project_path: Path) -> None:
        preflight_project_schemas(root)
        _recover_registration_before_open(root)
        from componentpress.versioning.recovery import recover_project_versions
        self.version_recovery_diagnostics = recover_project_versions(root)
        _recover_migration_before_open(root, project_path)
        _recover_project_transactions(root)

    def open(self, root: Path) -> ProjectSnapshot:
        root = root.resolve()
        project_path = project_manifest_path(root)
        self._recover_before_open(root, project_path)
        return _strict_open_snapshot(root, project_path)

    def open_for_editor(self, root: Path) -> ProjectSnapshot:
        """Open readable component YAML for editing, retaining validation failures."""
        root = root.resolve()
        if _pending_recovery(root):
            # The strict path proves every affected schema before it can recover files.
            return self.open(root)
        project_path, text, digest, model = _editor_project(root)
        seen: set[str] = set()
        documents = {ref.id: _editor_component(root, project_path, ref, model, seen)
                     for ref in model.components}
        return _verified_open_snapshot(
            ProjectSnapshot(root, text, digest, model, MappingProxyType(documents)),
        )

    def migrate(self, root: Path) -> ProjectSnapshot:
        root = root.resolve()
        preflight_project_schemas(root)
        with ProjectWriteLock(root):
            recover_migration(root)
            migrate_v1_to_v2(root)
        return self.open(root)

    def save_project(self, snapshot: ProjectSnapshot, **changes: object) -> ProjectSnapshot:
        original, tree = parse_project(snapshot.text, snapshot.root / PROJECT_MANIFEST)
        updated = original.model_copy(update=changes)
        # model_copy does not validate updates; always re-parse before touching disk.
        update_tree(tree, updated)
        text = dump_yaml(tree)
        validated, _ = parse_project(text, snapshot.root / PROJECT_MANIFEST)
        if validated.components != snapshot.model.components:
            raise ProjectError(Diagnostic("COMPONENT_REGISTRY", "реестр компонентов изменяется отдельной операцией", snapshot.root / PROJECT_MANIFEST, "components"))
        checked_snapshot = snapshot.__class__(snapshot.root, text, snapshot.disk_hash, validated, snapshot.documents)
        for component_id, document in snapshot.documents.items():
            if document.model is None:
                continue
            checked = self.prepare_text(checked_snapshot, component_id, document.text)
            old = {(item.code, item.field, item.line) for item in document.diagnostics}
            if checked.model is None or any((item.code, item.field, item.line) not in old for item in checked.diagnostics):
                raise ProjectError(next(iter(checked.diagnostics)))
        with _registration_write_lock(snapshot.root):
            atomic_write(snapshot.root / PROJECT_MANIFEST, text.encode("utf-8"), snapshot.disk_hash)
        return self.open_for_editor(snapshot.root)

    def save_component(self, snapshot: ProjectSnapshot, component_id: str, **changes: object) -> ProjectSnapshot:
        document = snapshot.documents[component_id]
        project_model, _ = parse_project(snapshot.text, snapshot.root / PROJECT_MANIFEST)
        original, tree = parse_component(document.text, document.path, project_model.variables)
        updated = original.model_copy(update=changes)
        update_tree(tree, updated)
        text = dump_yaml(tree)
        checked, _ = parse_component(text, document.path, project_model.variables)
        if checked.data and project_model.data_source is None:
            raise ProjectError(Diagnostic("DATA_SOURCE_UNKNOWN", "задайте XLSX в настройках проекта", document.path, "data.sheet"))
        with _registration_write_lock(snapshot.root):
            if file_hash(snapshot.root / PROJECT_MANIFEST) != snapshot.disk_hash:
                raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "описание проекта изменено после чтения", snapshot.root / PROJECT_MANIFEST))
            atomic_write(document.path, text.encode("utf-8"), document.disk_hash)
        return self.open_for_editor(snapshot.root)

    def prepare_component(
        self,
        snapshot: ProjectSnapshot,
        component_id: str,
        model: ComponentDefinition,
        base: DocumentSnapshot | None = None,
    ) -> DocumentSnapshot:
        document = base or snapshot.documents[component_id]
        project_model, _ = parse_project(snapshot.text, snapshot.root / PROJECT_MANIFEST)
        _original, tree = parse_component(document.text, document.path, project_model.variables)
        update_tree(tree, model)
        text = dump_yaml(tree)
        checked, _ = parse_component(text, document.path, project_model.variables)
        if checked.id != component_id:
            raise ProjectError(Diagnostic("COMPONENT_ID_MISMATCH", "ID компонента нельзя изменить", document.path, "id"))
        return DocumentSnapshot(document.path, text, document.disk_hash, checked)

    def prepare_text(self, snapshot: ProjectSnapshot, component_id: str, text: str) -> DocumentSnapshot:
        document = snapshot.documents[component_id]
        try:
            checked, _ = parse_component(text, document.path, snapshot.model.variables)
        except ProjectError as exc:
            return DocumentSnapshot(document.path, text, document.disk_hash, None, (exc.diagnostic,))
        diagnostics = list(document.diagnostics if text == document.text else ())
        if checked.id != component_id:
            diagnostics.append(Diagnostic("COMPONENT_ID_MISMATCH", "ID в реестре не совпадает с документом", document.path, "id"))
        if checked.data and snapshot.model.data_source is None:
            diagnostics.append(Diagnostic("DATA_SOURCE_UNKNOWN", "задайте XLSX в настройках проекта", document.path, "data.sheet"))
        return DocumentSnapshot(document.path, text, document.disk_hash, checked, tuple(diagnostics))

    def save_raw_component(self, snapshot: ProjectSnapshot, component_id: str, text: str) -> ProjectSnapshot:
        document = snapshot.documents[component_id]
        encoded = text.encode("utf-8")
        with ProjectWriteLock(snapshot.root):
            if _pending_recovery(snapshot.root):
                raise ProjectError(Diagnostic("RECOVERY_PENDING", "завершите восстановление проекта перед записью", snapshot.root))
            if file_hash(snapshot.root / PROJECT_MANIFEST) != snapshot.disk_hash:
                raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "описание проекта изменено после чтения", snapshot.root / PROJECT_MANIFEST))
            atomic_write(document.path, encoded, document.disk_hash)
        return self.open_for_editor(snapshot.root)

    def save_prepared(
        self,
        snapshot: ProjectSnapshot,
        component_id: str,
        document: DocumentSnapshot,
    ) -> ProjectSnapshot:
        checked = self.prepare_text(snapshot, component_id, document.text)
        with _registration_write_lock(snapshot.root):
            if file_hash(snapshot.root / PROJECT_MANIFEST) != snapshot.disk_hash:
                raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "описание проекта изменено после чтения", snapshot.root / PROJECT_MANIFEST))
            atomic_write(checked.path, checked.text.encode("utf-8"), snapshot.documents[component_id].disk_hash)
        return self.open_for_editor(snapshot.root)

    def add_component(self, snapshot: ProjectSnapshot, component_id: str, name: str, **values: object) -> ProjectSnapshot:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", component_id):
            raise ProjectError(Diagnostic("COMPONENT_ID", "ID: строчные латинские буквы, цифры и дефис", snapshot.root))
        if component_id in snapshot.documents:
            raise ProjectError(Diagnostic("COMPONENT_ID_DUPLICATE", "компонент уже зарегистрирован", snapshot.root))
        path = f"components/{component_id}.yaml"
        component_text = _new_component_text(component_id, name, values)
        candidate = self._prepare_added_component(snapshot, component_id, path, component_text)
        with _registration_write_lock(snapshot.root):
            _assert_snapshot_current(snapshot)
            register_component(snapshot.root, path, candidate.documents[component_id].text.encode("utf-8"), candidate.text.encode("utf-8"), snapshot.disk_hash)
        return candidate

    @staticmethod
    def _prepare_added_component(snapshot: ProjectSnapshot, component_id: str, path: str, component_text: str) -> ProjectSnapshot:
        component_path = snapshot.root / path
        project_model, tree = parse_project(snapshot.text, snapshot.root / PROJECT_MANIFEST)
        component_model, _ = parse_component(component_text, component_path, project_model.variables)
        if component_model.data and project_model.data_source is None:
            raise ProjectError(Diagnostic("DATA_SOURCE_UNKNOWN", "задайте XLSX в настройках проекта", component_path, "data.sheet"))
        tree["components"].append({"id": component_id, "path": path})
        project_text = dump_yaml(tree)
        validated_project, _ = parse_project(project_text, snapshot.root / PROJECT_MANIFEST)
        document = DocumentSnapshot(component_path, component_text, hashlib.sha256(component_text.encode("utf-8")).hexdigest(), component_model)
        documents = dict(snapshot.documents)
        documents[component_id] = document
        return ProjectSnapshot(snapshot.root, project_text, hashlib.sha256(project_text.encode("utf-8")).hexdigest(), validated_project, MappingProxyType(documents))

    def remove_component(self, snapshot: ProjectSnapshot, component_id: str, *, backup_path: str) -> RemovedComponent:
        reference, document, digest, text, model, position = _prepare_removal(snapshot, component_id)
        _publish_membership_change(snapshot, "remove", reference.path, backup_path, digest, text, document)
        documents = dict(snapshot.documents)
        documents.pop(component_id)
        return RemovedComponent(_membership_snapshot(snapshot.root, text, model, documents), reference.path, backup_path, digest, position)

    def discard_component_history_backup(self, root: Path, backup_path: str) -> bool:
        backup = resolve_project_path(root, backup_path)
        history = resolve_project_path(root, ".componentpress/transactions/component-history")
        if not backup.is_relative_to(history) or backup == history:
            raise ProjectError(Diagnostic("COMPONENT_HISTORY", "недопустимый путь резервной копии", backup))
        journal = root / ".componentpress" / "transactions" / "component-membership.json"
        if journal.exists() and _journal_uses_backup(journal, backup_path):
            return False
        if backup.is_file():
            backup.unlink()
        return True

    def restore_component(self, snapshot: ProjectSnapshot, component_id: str, *, reference_path: str,
                          backup_path: str, component_hash: str, position: int) -> ProjectSnapshot:
        text, model, component_text, component_model = _prepare_restoration(
            snapshot, component_id, reference_path, backup_path, component_hash, position,
        )
        _publish_membership_change(snapshot, "restore", reference_path, backup_path, component_hash, text)
        documents = dict(snapshot.documents)
        documents[component_id] = DocumentSnapshot(snapshot.root / reference_path, component_text, component_hash, component_model)
        return _membership_snapshot(snapshot.root, text, model, documents)
