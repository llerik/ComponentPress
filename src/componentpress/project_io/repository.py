"""Project creation, strict opening and comment-preserving saves."""

from pathlib import Path
import hashlib
import json
from types import MappingProxyType
import re
import unicodedata

from componentpress.application.contracts import DocumentSnapshot, ProjectSnapshot
from componentpress.domain.component import ComponentDefinition
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.nodes import GroupNode, HtmlNode, ImageNode, Node
from componentpress.domain.project import ProjectDefinition
from componentpress.domain.schema import SCHEMA_VERSION
from componentpress.bindings.grammar import parse_references
from componentpress.platforms.services import ProjectWriteLock
from .files import atomic_create, atomic_write, file_hash
from .paths import check_case_collisions, check_exact_case, resolve_project_path
from .transactions import change_component_membership, recover, recover_component_membership, register_component
from .migrations import migrate_v1_to_v2, migration_required, recover_migration, recovery_required
from .schema_checks import preflight_project_schemas
from .yaml_codec import _load, dump_yaml, html_image_sources, parse_component, parse_project, update_tree


def _read_text(path: Path) -> tuple[str, str]:
    try:
        data = path.read_bytes()
        return data.decode("utf-8"), hashlib.sha256(data).hexdigest()
    except UnicodeDecodeError as exc:
        raise ProjectError(Diagnostic("TEXT_ENCODING", "файл должен быть UTF-8", path)) from exc
    except OSError as exc:
        raise ProjectError(Diagnostic("FILE_READ", str(exc), path)) from exc


class FileProjectRepository:
    def __init__(self) -> None:
        self.version_recovery_diagnostics: tuple[Diagnostic, ...] = ()

    def create(self, root: Path, name: str) -> ProjectSnapshot:
        root = root.resolve()
        if root.exists() and any(root.iterdir()):
            raise ProjectError(Diagnostic("PROJECT_EXISTS", "папка не пуста", root))
        if not name.strip():
            raise ProjectError(Diagnostic("PROJECT_NAME", "название не может быть пустым", root))
        project_id = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", root.name).lower()).strip("-") or "game"
        root.mkdir(parents=True, exist_ok=True)
        for folder in ("components", "data", "assets/images", "assets/fonts"):
            (root / folder).mkdir(parents=True, exist_ok=True)
        template = (
            f"schema_version: {SCHEMA_VERSION}\n"
            f"id: {project_id}\n"
            f"name: {json.dumps(name, ensure_ascii=False)}\n"
            'version: "0.1.0"\n'
            "variables: {}\n"
            "icons: {}\n"
            "data_source: null\n"
            "copies_columns:\n  prod: Prod\n  test: Debug\n"
            "components: []\n"
            "print:\n"
            "  paper: A4\n  orientation: portrait\n  margin_mm: 5\n  gap_mm: 3\n"
            "  dpi: 300\n  cut_lines: true\n  cut_line_width_mm: 0.2\n"
        )
        # repr() quotes Unicode safely but may use YAML-incompatible escapes; use ruamel for the user string.
        from ruamel.yaml.scalarstring import DoubleQuotedScalarString
        model, tree = parse_project(template, root / "project.yaml")
        tree["name"] = DoubleQuotedScalarString(name)
        atomic_create(root / "project.yaml", dump_yaml(tree).encode("utf-8"))
        return self.open(root)

    def open(self, root: Path) -> ProjectSnapshot:
        root = root.resolve()
        project_path = root / "project.yaml"
        preflight_project_schemas(root)
        from componentpress.application.version_service import recover_project_versions

        self.version_recovery_diagnostics = recover_project_versions(root)
        if recovery_required(root):
            with ProjectWriteLock(root):
                recover_migration(root)
        if migration_required(root):
            raise ProjectError(Diagnostic(
                "MIGRATION_REQUIRED",
                "проект формата 1; подтвердите миграцию в формат 2",
                root / "project.yaml",
                "schema_version",
            ))
        if (root / ".componentpress" / "transactions" / "add-component.json").exists():
            with ProjectWriteLock(root):
                recover(root)
        if (root / ".componentpress" / "transactions" / "component-membership.json").exists():
            with ProjectWriteLock(root):
                recover_component_membership(root)
        check_case_collisions(root)
        text, disk_hash = _read_text(project_path)
        model, _ = parse_project(text, project_path)
        if model.data_source is not None:
            check_exact_case(root, model.data_source.path)
            resolve_project_path(root, model.data_source.path)
        for item in model.icons.values():
            check_exact_case(root, item.path)
            resolve_project_path(root, item.path)
        documents: dict[str, DocumentSnapshot] = {}
        seen_paths: set[str] = set()
        for ref in model.components:
            if not ref.path.startswith("components/") or not ref.path.endswith(".yaml"):
                raise ProjectError(Diagnostic("COMPONENT_PATH", "документ должен находиться в components/ и иметь .yaml", project_path, f"components.{ref.id}.path"))
            key = ref.path.casefold()
            if key in seen_paths:
                raise ProjectError(Diagnostic("COMPONENT_PATH_DUPLICATE", "повторный путь компонента", project_path, f"components.{ref.id}.path"))
            seen_paths.add(key)
            check_exact_case(root, ref.path)
            path = resolve_project_path(root, ref.path)
            doc_text, doc_hash = _read_text(path)
            doc_model, _ = parse_component(doc_text, path, model.variables)

            def check_node_paths(nodes: tuple[Node, ...]) -> None:
                for node in nodes:
                    if isinstance(node, ImageNode) and node.source_mode == "manual":
                        candidates = (node.source,)
                    elif isinstance(node, HtmlNode) and node.content_mode == "manual":
                        candidates = html_image_sources(node.html)
                    elif isinstance(node, GroupNode):
                        check_node_paths(node.children)
                        continue
                    else:
                        continue
                    for candidate in candidates:
                        if not parse_references(candidate):
                            static_path = candidate.replace(r"\{", "{").replace(r"\}", "}")
                            check_exact_case(root, static_path)
                            resolve_project_path(root, static_path)

            check_node_paths(doc_model.elements)
            if doc_model.id != ref.id:
                raise ProjectError(Diagnostic("COMPONENT_ID_MISMATCH", "ID в реестре не совпадает с документом", path, "id"))
            if doc_model.data and model.data_source is None:
                raise ProjectError(Diagnostic("DATA_SOURCE_UNKNOWN", "задайте XLSX в настройках проекта", path, "data.sheet"))
            documents[ref.id] = DocumentSnapshot(path, doc_text, doc_hash, doc_model)
        return ProjectSnapshot(root, text, disk_hash, model, MappingProxyType(documents))

    def migrate(self, root: Path) -> ProjectSnapshot:
        root = root.resolve()
        preflight_project_schemas(root)
        with ProjectWriteLock(root):
            recover_migration(root)
            migrate_v1_to_v2(root)
        return self.open(root)

    def save_project(self, snapshot: ProjectSnapshot, **changes: object) -> ProjectSnapshot:
        original, tree = parse_project(snapshot.text, snapshot.root / "project.yaml")
        updated = original.model_copy(update=changes)
        # model_copy does not validate updates; always re-parse before touching disk.
        update_tree(tree, updated)
        text = dump_yaml(tree)
        validated, _ = parse_project(text, snapshot.root / "project.yaml")
        if validated.components != snapshot.model.components:
            raise ProjectError(Diagnostic("COMPONENT_REGISTRY", "реестр компонентов изменяется отдельной операцией", snapshot.root / "project.yaml", "components"))
        for document in snapshot.documents.values():
            checked, _ = parse_component(document.text, document.path, validated.variables)
            if checked.data and validated.data_source is None:
                raise ProjectError(Diagnostic("DATA_SOURCE_UNKNOWN", "задайте XLSX в настройках проекта", document.path, "data.sheet"))
        with ProjectWriteLock(snapshot.root):
            atomic_write(snapshot.root / "project.yaml", text.encode("utf-8"), snapshot.disk_hash)
        return self.open(snapshot.root)

    def save_component(self, snapshot: ProjectSnapshot, component_id: str, **changes: object) -> ProjectSnapshot:
        document = snapshot.documents[component_id]
        project_model, _ = parse_project(snapshot.text, snapshot.root / "project.yaml")
        original, tree = parse_component(document.text, document.path, project_model.variables)
        updated = original.model_copy(update=changes)
        update_tree(tree, updated)
        text = dump_yaml(tree)
        checked, _ = parse_component(text, document.path, project_model.variables)
        if checked.data and project_model.data_source is None:
            raise ProjectError(Diagnostic("DATA_SOURCE_UNKNOWN", "задайте XLSX в настройках проекта", document.path, "data.sheet"))
        with ProjectWriteLock(snapshot.root):
            if file_hash(snapshot.root / "project.yaml") != snapshot.disk_hash:
                raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "описание проекта изменено после чтения", snapshot.root / "project.yaml"))
            atomic_write(document.path, text.encode("utf-8"), document.disk_hash)
        return self.open(snapshot.root)

    def prepare_component(
        self,
        snapshot: ProjectSnapshot,
        component_id: str,
        model: ComponentDefinition,
        base: DocumentSnapshot | None = None,
    ) -> DocumentSnapshot:
        document = base or snapshot.documents[component_id]
        project_model, _ = parse_project(snapshot.text, snapshot.root / "project.yaml")
        _original, tree = parse_component(document.text, document.path, project_model.variables)
        update_tree(tree, model)
        text = dump_yaml(tree)
        checked, _ = parse_component(text, document.path, project_model.variables)
        if checked.id != component_id:
            raise ProjectError(Diagnostic("COMPONENT_ID_MISMATCH", "ID компонента нельзя изменить", document.path, "id"))
        return DocumentSnapshot(document.path, text, document.disk_hash, checked)

    def prepare_text(self, snapshot: ProjectSnapshot, component_id: str, text: str) -> DocumentSnapshot:
        document = snapshot.documents[component_id]
        checked, _ = parse_component(text, document.path, snapshot.model.variables)
        if checked.id != component_id:
            raise ProjectError(Diagnostic("COMPONENT_ID_MISMATCH", "ID компонента нельзя изменить", document.path, "id"))
        if checked.data and snapshot.model.data_source is None:
            raise ProjectError(Diagnostic("DATA_SOURCE_UNKNOWN", "задайте XLSX в настройках проекта", document.path, "data.sheet"))
        return DocumentSnapshot(document.path, text, document.disk_hash, checked)

    def save_prepared(
        self,
        snapshot: ProjectSnapshot,
        component_id: str,
        document: DocumentSnapshot,
    ) -> ProjectSnapshot:
        checked = self.prepare_text(snapshot, component_id, document.text)
        with ProjectWriteLock(snapshot.root):
            if file_hash(snapshot.root / "project.yaml") != snapshot.disk_hash:
                raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "описание проекта изменено после чтения", snapshot.root / "project.yaml"))
            atomic_write(checked.path, checked.text.encode("utf-8"), snapshot.documents[component_id].disk_hash)
        return self.open(snapshot.root)

    def add_component(self, snapshot: ProjectSnapshot, component_id: str, name: str, **values: object) -> ProjectSnapshot:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", component_id):
            raise ProjectError(Diagnostic("COMPONENT_ID", "ID: строчные латинские буквы, цифры и дефис", snapshot.root))
        if component_id in snapshot.documents:
            raise ProjectError(Diagnostic("COMPONENT_ID_DUPLICATE", "компонент уже зарегистрирован", snapshot.root))
        path = f"components/{component_id}.yaml"
        from componentpress.domain.component import ComponentDefinition, DataBinding, SizeMM
        width_mm = float(values.get("width_mm", 63))
        height_mm = float(values.get("height_mm", 88))
        binding = values.get("data")
        component = ComponentDefinition(
            schema_version=SCHEMA_VERSION, id=component_id, name=name,
            size_mm=SizeMM(width=width_mm, height=height_mm),
            data=binding if isinstance(binding, DataBinding) else None,
        )
        from ruamel.yaml import YAML
        from io import StringIO
        stream = StringIO()
        YAML().dump(component.model_dump(mode="json", exclude_none=True), stream)
        component_text = stream.getvalue()
        project_model, tree = parse_project(snapshot.text, snapshot.root / "project.yaml")
        parse_component(component_text, snapshot.root / path, project_model.variables)
        tree["components"].append({"id": component_id, "path": path})
        project_text = dump_yaml(tree)
        parse_project(project_text, snapshot.root / "project.yaml")
        with ProjectWriteLock(snapshot.root):
            register_component(snapshot.root, path, component_text.encode("utf-8"), project_text.encode("utf-8"), snapshot.disk_hash)
        return self.open(snapshot.root)

    def remove_component(self, snapshot: ProjectSnapshot, component_id: str, *, backup_path: str) -> tuple[ProjectSnapshot, bytes, int, str]:
        reference = next((item for item in snapshot.model.components if item.id == component_id), None)
        document = snapshot.documents.get(component_id)
        if reference is None or document is None:
            raise ProjectError(Diagnostic("COMPONENT_UNKNOWN", "компонент не найден", snapshot.root))
        if len(snapshot.model.components) <= 1:
            raise ProjectError(Diagnostic("COMPONENT_LAST", "нельзя удалить последний компонент проекта", snapshot.root))
        raw = document.path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        project_model, tree = parse_project(snapshot.text, snapshot.root / "project.yaml")
        position = next(i for i, item in enumerate(project_model.components) if item.id == component_id)
        tree["components"].pop(position)
        new_text = dump_yaml(tree)
        parse_project(new_text, snapshot.root / "project.yaml")
        with ProjectWriteLock(snapshot.root):
            if file_hash(snapshot.root / "project.yaml") != snapshot.disk_hash:
                raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "описание проекта изменено после чтения", snapshot.root / "project.yaml"))
            if file_hash(document.path) != document.disk_hash:
                raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "компонент изменён после чтения", document.path))
            change_component_membership(
                snapshot.root, operation="remove", relative_path=reference.path, backup_path=backup_path,
                project_bytes=new_text.encode("utf-8"), old_project_hash=snapshot.disk_hash, component_hash=digest,
            )
        checked_project, _ = parse_project(new_text, snapshot.root / "project.yaml")
        documents = dict(snapshot.documents)
        documents.pop(component_id)
        committed = ProjectSnapshot(
            snapshot.root, new_text, hashlib.sha256(new_text.encode("utf-8")).hexdigest(),
            checked_project, MappingProxyType(documents),
        )
        return committed, raw, position, reference.path

    def restore_component(self, snapshot: ProjectSnapshot, component_id: str, *, reference_path: str,
                          backup_path: str, component_hash: str, position: int) -> ProjectSnapshot:
        from .paths import resolve_project_path
        backup = resolve_project_path(snapshot.root, backup_path)
        if not backup.is_file() or file_hash(backup) != component_hash:
            raise ProjectError(Diagnostic("COMPONENT_HISTORY", "снимок удалённого компонента отсутствует или изменён", backup))
        component_bytes = backup.read_bytes()
        component_text = component_bytes.decode("utf-8")
        component_model, _ = parse_component(component_text, snapshot.root / reference_path, snapshot.model.variables)
        if component_id in snapshot.documents or any(ref.path.casefold() == reference_path.casefold() for ref in snapshot.model.components):
            raise ProjectError(Diagnostic("COMPONENT_HISTORY_CONFLICT", "ID или путь компонента уже используется", snapshot.root / reference_path))
        from ruamel.yaml.comments import CommentedMap
        project_model, tree = parse_project(snapshot.text, snapshot.root / "project.yaml")
        refs = tree["components"]
        index = max(0, min(position, len(refs)))
        ref = CommentedMap(id=component_id, path=reference_path)
        refs.insert(index, ref)
        new_text = dump_yaml(tree)
        parse_project(new_text, snapshot.root / "project.yaml")
        with ProjectWriteLock(snapshot.root):
            if file_hash(snapshot.root / "project.yaml") != snapshot.disk_hash:
                raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "описание проекта изменено после чтения", snapshot.root / "project.yaml"))
            change_component_membership(
                snapshot.root, operation="restore", relative_path=reference_path, backup_path=backup_path,
                project_bytes=new_text.encode("utf-8"), old_project_hash=snapshot.disk_hash, component_hash=component_hash,
            )
        checked_project, _ = parse_project(new_text, snapshot.root / "project.yaml")
        documents = dict(snapshot.documents)
        component_snapshot = DocumentSnapshot(
            snapshot.root / reference_path, component_text, component_hash, component_model,
        )
        documents[component_id] = component_snapshot
        return ProjectSnapshot(
            snapshot.root, new_text, hashlib.sha256(new_text.encode("utf-8")).hexdigest(),
            checked_project, MappingProxyType(documents),
        )
