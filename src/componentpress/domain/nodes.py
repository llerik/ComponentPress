from typing import Annotated, Literal, Union
import re

from pydantic import Field, field_validator, model_validator

from .project import PositiveMM, StrictModel, _finite


class Transform(StrictModel):
    m11: float = 1.0
    m12: float = 0.0
    m21: float = 0.0
    m22: float = 1.0

    @field_validator("m11", "m12", "m21", "m22", mode="before")
    @classmethod
    def finite_coefficient(cls, value: object) -> float:
        return _finite(value)

    @model_validator(mode="after")
    def positive_determinant(self):
        if self.m11 * self.m22 - self.m12 * self.m21 <= 0:
            raise ValueError("преобразование должно иметь положительный ненулевой определитель")
        return self


class GroupFrame(StrictModel):
    x_mm: float = 0.0
    y_mm: float = 0.0
    width_mm: PositiveMM = 10.0
    height_mm: PositiveMM = 10.0

    @field_validator("x_mm", "y_mm", "width_mm", "height_mm", mode="before")
    @classmethod
    def finite_dimension(cls, value: object) -> float:
        return _finite(value)


class PositionedNode(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    x_mm: float
    y_mm: float
    locked: bool = False
    transform: Transform = Field(default_factory=Transform)

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
    optional: bool = False
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
        if self.source_mode == "manual" and not self.source and not self.optional:
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
    frame: GroupFrame = Field(default_factory=GroupFrame)

    @field_validator("children", mode="before")
    @classmethod
    def child_list(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def initialize_frame(self):
        if "frame" not in self.model_fields_set:
            from .geometry import group_frame

            object.__setattr__(self, "frame", group_frame(self.children))
        return self


class ConditionalGroupNode(GroupNode):
    type: Literal["conditional_group"]
    condition_column: str = Field(min_length=1)
    condition_operator: Literal["equals", "not_equals", "is_empty", "is_not_empty"] = "equals"
    condition_value: str | None = None
    condition_matches: bool | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def valid_condition(self):
        if self.condition_operator in {"equals", "not_equals"} and self.condition_value is None:
            raise ValueError("для equals/not_equals требуется condition_value")
        if self.condition_operator in {"is_empty", "is_not_empty"} and self.condition_value is not None:
            raise ValueError("condition_value запрещён для операторов пустоты")
        return self



class ShapeNode(PositionedNode):
    type: Literal["rectangle", "ellipse"]
    width_mm: PositiveMM
    height_mm: PositiveMM
    fill: str | None = "#FFFFFF"
    stroke: str = "#000000"
    stroke_width_mm: PositiveMM = 0.2
    stroke_style: Literal["solid", "dash", "dot"] = "solid"

    @field_validator("width_mm", "height_mm", "stroke_width_mm", mode="before")
    @classmethod
    def finite_size(cls, value: object) -> float:
        return _finite(value)

    @field_validator("fill", "stroke")
    @classmethod
    def valid_color(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?", value):
            raise ValueError("ожидается цвет #RRGGBB, #AARRGGBB или пустая заливка")
        return value


class LineNode(PositionedNode):
    type: Literal["line"]
    dx_mm: float
    dy_mm: float
    stroke: str = "#000000"
    stroke_width_mm: PositiveMM = 0.2
    stroke_style: Literal["solid", "dash", "dot"] = "solid"

    @field_validator("dx_mm", "dy_mm", "stroke_width_mm", mode="before")
    @classmethod
    def finite_size(cls, value: object) -> float:
        return _finite(value)

    @field_validator("stroke")
    @classmethod
    def valid_color(cls, value: str) -> str:
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?", value):
            raise ValueError("ожидается цвет #RRGGBB или #AARRGGBB")
        return value

    @model_validator(mode="after")
    def nonzero_geometry(self):
        if self.dx_mm == 0 and self.dy_mm == 0:
            raise ValueError("у линии должен быть ненулевой размер")
        return self


Node = Annotated[
    Union[ImageNode, HtmlNode, ConditionalGroupNode, GroupNode, ShapeNode, LineNode],
    Field(discriminator="type"),
]
GroupNode.model_rebuild()
ConditionalGroupNode.model_rebuild()
