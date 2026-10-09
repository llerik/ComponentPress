import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .nodes import GroupNode, HtmlNode, ImageNode, Node
from .project import PositiveMM, StrictModel, _finite
from .schema import SCHEMA_VERSION, require_current_schema


class SizeMM(StrictModel):
    width: PositiveMM
    height: PositiveMM

    @field_validator("width", "height", mode="before")
    @classmethod
    def finite_size(cls, value: object) -> float:
        return _finite(value)


class DataBinding(StrictModel):
    sheet: str = Field(min_length=1)
    # Internal compatibility key: schema 3 has one project-wide source.
    source: str = "main"
    id_column: str | None = Field(default=None, exclude=True)
    copies_column: str | None = Field(default=None, exclude=True)


class ComponentDefinition(StrictModel):
    schema_version: Literal[SCHEMA_VERSION]
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    size_mm: SizeMM
    background: str = "#FFFFFF"
    data: DataBinding | None = None
    elements: tuple[Node, ...] = ()

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value: object) -> int:
        return require_current_schema(value)

    @field_validator("elements", mode="before")
    @classmethod
    def element_list(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("имя не может быть пустым")
        return value

    @field_validator("background")
    @classmethod
    def valid_background(cls, value: str) -> str:
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
            raise ValueError("ожидается цвет #RRGGBB")
        return value

    @model_validator(mode="after")
    def unique_node_ids(self) -> "ComponentDefinition":
        seen: set[str] = set()

        def visit(nodes: tuple[Node, ...]) -> None:
            for node in nodes:
                if node.id in seen:
                    raise ValueError(f"повторный ID узла {node.id!r}")
                seen.add(node.id)
                if isinstance(node, GroupNode):
                    visit(node.children)

        visit(self.elements)
        return self
