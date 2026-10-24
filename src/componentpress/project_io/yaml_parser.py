"""Parse public project and component YAML into validated domain models."""

from copy import deepcopy
from pathlib import Path
from ruamel.yaml.comments import CommentedMap

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.diagnostics import ProjectError
from componentpress.domain.project import ProjectDefinition
from .yaml_read import _load
from .yaml_validation import (
    _check_component_schema, _reject_unknown_fields, _validate,
    _validate_project_paths, _normalize_component_nodes, _visit_component_nodes,
)


def parse_project(text: str, path: Path) -> tuple[ProjectDefinition, CommentedMap]:
    tree = _load(text, path)
    model = _validate(tree, ProjectDefinition, path)
    _validate_project_paths(model, path)
    return model, tree


def parse_component(text: str, path: Path, variables: dict[str, str] | None = None) -> tuple[ComponentDefinition, CommentedMap]:
    tree = _load(text, path)
    _check_component_schema(tree, path)
    model_tree = deepcopy(tree)
    _normalize_component_data(model_tree, path)
    _normalize_component_nodes(model_tree.get("elements"), path, model_tree)
    model = _validate(model_tree, ComponentDefinition, path)
    _visit_component_nodes(model.elements, ("elements",), model, path, tree, variables)
    return model, tree


def _normalize_component_data(tree: CommentedMap, path: Path) -> None:
    data = tree.get("data")
    if isinstance(data, dict):
        _reject_unknown_fields(data, {"sheet"}, path, tree, ("data",))
        data.setdefault("source", "main")
