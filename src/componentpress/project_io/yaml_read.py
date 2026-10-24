"""Load round-trip YAML trees and their source locations."""

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.tokens import AliasToken, AnchorToken, TagToken

from componentpress.domain.diagnostics import Diagnostic, ProjectError


def _yaml() -> YAML:
    parser = YAML(typ="rt")
    parser.version = (1, 2)
    parser.preserve_quotes = True
    parser.allow_duplicate_keys = False
    parser.width = 4096
    return parser


def _field_line(tree: Any, location: tuple[Any, ...]) -> int | None:
    node = tree
    line: int | None = None
    for segment in location:
        try:
            if isinstance(node, CommentedMap):
                line = node.lc.key(segment)[0] + 1
            elif isinstance(node, CommentedSeq):
                line = node.lc.item(segment)[0] + 1
            node = node[segment]
        except (KeyError, IndexError, TypeError):
            break
    return line


def _reject_unsupported_tokens(yaml: YAML, text: str, path: Path) -> None:
    for token in yaml.scan(text):
        if isinstance(token, (AliasToken, AnchorToken, TagToken)):
            line = token.start_mark.line + 1
            raise ProjectError(Diagnostic("YAML_UNSUPPORTED", "теги и якоря YAML не поддерживаются", path, line=line))


def _yaml_error(exc: YAMLError, path: Path) -> ProjectError:
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    line = mark.line + 1 if mark else None
    return ProjectError(Diagnostic("YAML_SYNTAX", str(exc).splitlines()[0], path, line=line))


def _reject_merge(node: Any, path: Path) -> None:
    if isinstance(node, CommentedMap):
        _reject_mapping_merge(node, path)
    elif isinstance(node, CommentedSeq):
        for child in node:
            _reject_merge(child, path)


def _reject_mapping_merge(node: CommentedMap, path: Path) -> None:
    if getattr(node, "merge", None) or "<<" in node:
        raise ProjectError(Diagnostic("YAML_UNSUPPORTED", "слияние YAML не поддерживается", path))
    for child in node.values():
        _reject_merge(child, path)


def _root_mapping(tree: Any, path: Path) -> CommentedMap:
    if not isinstance(tree, CommentedMap):
        raise ProjectError(Diagnostic("YAML_STRUCTURE", "корневое значение должно быть словарём", path))
    _reject_merge(tree, path)
    return tree


def _load(text: str, path: Path) -> CommentedMap:
    yaml = _yaml()
    try:
        _reject_unsupported_tokens(yaml, text, path)
        return _root_mapping(yaml.load(text), path)
    except YAMLError as exc:
        raise _yaml_error(exc, path) from exc
