import math
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


PositiveMM = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonnegativeMM = Annotated[float, Field(ge=0, allow_inf_nan=False)]


def _finite(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("ожидается число")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("число должно быть конечным")
    return result


class ComponentRef(StrictModel):
    id: str = Field(min_length=1)
    path: str


class IconDefinition(StrictModel):
    path: str
    width_mm: PositiveMM
    height_mm: PositiveMM

    @field_validator("width_mm", "height_mm", mode="before")
    @classmethod
    def finite_size(cls, value: object) -> float:
        return _finite(value)


class DataSource(StrictModel):
    path: str
    formula_mode: Literal["cached"] = "cached"


class CopiesColumns(StrictModel):
    prod: str = "Prod"
    test: str = "Debug"


class PrintSettings(StrictModel):
    paper: Literal["A4", "Letter"] = "A4"
    orientation: Literal["portrait", "landscape"] = "portrait"
    margin_mm: NonnegativeMM = 5
    gap_mm: NonnegativeMM = 3
    dpi: int = Field(default=300, gt=0)
    cut_lines: bool = True
    cut_line_width_mm: PositiveMM = 0.2

    @field_validator("margin_mm", "gap_mm", "cut_line_width_mm", mode="before")
    @classmethod
    def finite_size(cls, value: object) -> float:
        return _finite(value)


class ProjectDefinition(StrictModel):
    schema_version: Literal[3]
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str
    variables: dict[str, str] = Field(default_factory=dict)
    icons: dict[str, IconDefinition] = Field(default_factory=dict)
    data_source: DataSource | None = None
    copies_columns: CopiesColumns = Field(default_factory=CopiesColumns)
    components: tuple[ComponentRef, ...] = ()
    print: PrintSettings = Field(default_factory=PrintSettings)

    @field_validator("components", mode="before")
    @classmethod
    def component_list(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("имя не может быть пустым")
        return value

    @field_validator("version")
    @classmethod
    def semantic_version(cls, value: str) -> str:
        if not re.fullmatch(r"\d+\.\d+\.\d+", value):
            raise ValueError("версия должна иметь вид major.minor.patch")
        return value

    @field_validator("icons")
    @classmethod
    def icon_names(cls, value: dict[str, IconDefinition]) -> dict[str, IconDefinition]:
        for key in value:
            if not re.fullmatch(r"ic_[A-Za-z0-9_]+", key):
                raise ValueError(f"неверное имя иконки {key!r}")
        return value

    @model_validator(mode="after")
    def unique_components(self) -> "ProjectDefinition":
        ids = [entry.id for entry in self.components]
        if len(ids) != len(set(ids)):
            raise ValueError("повторный ID компонента")
        return self
