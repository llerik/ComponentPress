from typing import Annotated, Literal, Union
import re

from pydantic import Field, field_validator, model_validator

from .project import PositiveMM, StrictModel, _finite


class PositionedNode(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    x_mm: float
    y_mm: float
    locked: bool = False

    @field_validator("x_mm", "y_mm", mode="before")
    @classmethod
    def finite_coordinate(cls, value: object) -> float:
        return _finite(value)

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("имя не может быть пустым")
        return value


class ImageNode(PositionedNode):
    type: Literal["image"]
    width_mm: PositiveMM
    height_mm: PositiveMM
    source: str
    source_mode: Literal["manual", "column"] = "manual"
    source_column: str | None = None
    fit: Literal["contain", "cover", "stretch"] = "contain"

    @field_validator("width_mm", "height_mm", mode="before")
    @classmethod
    def finite_size(cls, value: object) -> float:
        return _finite(value)

    @model_validator(mode="after")
    def valid_content_source(self):
        if self.source_mode == "column" and not self.source_column:
            raise ValueError("для режима column требуется source_column")
        if self.source_mode == "manual" and not self.source:
            raise ValueError("для ручного режима требуется путь изображения")
        return self


class HtmlNode(PositionedNode):
    type: Literal["html"]
    width_mm: PositiveMM
    height_mm: PositiveMM
    font_family: str = Field(min_length=1)
    font_size_pt: PositiveMM
    color: str = "#111111"
    text_align: Literal["left", "center", "right"] = "left"
    html: str
    content_mode: Literal["manual", "column"] = "manual"
    content_column: str | None = None

    @field_validator("color")
    @classmethod
    def valid_color(cls, value: str) -> str:
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?", value):
            raise ValueError("ожидается цвет #RRGGBB или #AARRGGBB")
        return value

    @field_validator("width_mm", "height_mm", "font_size_pt", mode="before")
    @classmethod
    def finite_size(cls, value: object) -> float:
        return _finite(value)

    @model_validator(mode="after")
    def valid_content_source(self):
        if self.content_mode == "column" and not self.content_column:
            raise ValueError("для режима column требуется content_column")
        return self


class GroupNode(PositionedNode):
    type: Literal["group"]
    children: tuple["Node", ...] = ()

    @field_validator("children", mode="before")
    @classmethod
    def child_list(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


Node = Annotated[Union[ImageNode, HtmlNode, GroupNode], Field(discriminator="type")]
GroupNode.model_rebuild()
