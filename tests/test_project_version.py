"""Game-version text rules for project schema seven."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from componentpress.domain.project import ProjectDefinition
from componentpress.domain.schema import SCHEMA_VERSION
from componentpress.project_io.repository import FileProjectRepository
from componentpress.project_io.yaml_codec import dump_yaml, parse_project, update_tree
from ruamel.yaml.comments import CommentedMap


def _project(**values) -> ProjectDefinition:
    return ProjectDefinition(schema_version=SCHEMA_VERSION, id="game", name="Game", **values)


def test_new_project_defaults_to_one_point_zero(tmp_path: Path) -> None:
    snapshot = FileProjectRepository().create(tmp_path / "game", "Game")
    assert snapshot.model.version == "1.0"


@pytest.mark.parametrize("version", ["", "1.0", "34.0", "abc  ", "😀😀😀😀😀"])
def test_game_version_accepts_uninterpreted_portable_text(version: str) -> None:
    assert _project(version=version).version == version


@pytest.mark.parametrize("version", ["123456", "a/b", "a\\b", "a:b", "a?b", "a\x00b", "a\x1fb"])
def test_game_version_rejects_overlong_or_filename_forbidden_text(version: str) -> None:
    with pytest.raises(ValidationError):
        _project(version=version)


@pytest.mark.parametrize("version", ["", "A.1", "😀x"])
def test_game_version_text_round_trips_yaml_without_normalization(version: str, tmp_path: Path) -> None:
    model = _project(version=version)
    tree = CommentedMap()
    update_tree(tree, model)
    loaded, _ = parse_project(dump_yaml(tree), tmp_path / "project.cpress")
    assert loaded.version == version
