from typing import Annotated, Literal, Union

from pydantic import Field, field_validator

from .project import PositiveMM, StrictModel, _finite


class PositionedNode(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    x_mm: float
    y_mm: float

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
    fit: Literal["contain", "cover", "stretch"] = "contain"

    @field_validator("width_mm", "height_mm", mode="before")
    @classmethod
    def finite_size(cls, value: object) -> float:
        return _finite(value)


class HtmlNode(PositionedNode):
    type: Literal["html"]
    width_mm: PositiveMM
    height_mm: PositiveMM
    font_family: str = Field(min_length=1)
    font_size_pt: PositiveMM
    color: str = "#111111"
    html: str

    @field_validator("width_mm", "height_mm", "font_size_pt", mode="before")
    @classmethod
    def finite_size(cls, value: object) -> float:
        return _finite(value)


class GroupNode(PositionedNode):
    type: Literal["group"]
    children: tuple["Node", ...] = ()

    @field_validator("children", mode="before")
    @classmethod
    def child_list(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


Node = Annotated[Union[ImageNode, HtmlNode, GroupNode], Field(discriminator="type")]
GroupNode.model_rebuild()
