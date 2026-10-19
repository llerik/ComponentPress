import math
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .schema import SCHEMA_VERSION, require_current_schema


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
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)


def _validate_image_filename(value: str) -> str:
    if (not value.startswith("assets/images/") or "/" in value[len("assets/images/"):]
            or "\\" in value[len("assets/images/"):]
            or not value.lower().endswith((".png", ".jpg", ".jpeg"))):
        raise ValueError("изображение должно быть файлом PNG/JPEG в assets/images")
    return value


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
    schema_version: Literal[SCHEMA_VERSION]
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str
    variables: dict[str, str] = Field(default_factory=dict)
    images: tuple[str, ...] = ()
    icons: dict[str, IconDefinition] = Field(default_factory=dict)
    data_source: DataSource | None = None
    copies_columns: CopiesColumns = Field(default_factory=CopiesColumns)
    components: tuple[ComponentRef, ...] = ()
    print: PrintSettings = Field(default_factory=PrintSettings)

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value: object) -> int:
        return require_current_schema(value)

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
    def portable_version(cls, value: str) -> str:
        if len(value) > 5 or any(character in '<>:"/\\|?*' or ord(character) < 32 for character in value):
            raise ValueError("версия должна содержать не более 5 символов и не содержать символы имени файла")
        return value

    @field_validator("icons", mode="before")
    @classmethod
    def icon_names(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized: dict[str, object] = {}
        for key, definition in value.items():
            if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", key)):
                raise ValueError(f"неверное имя ключа изображения {key!r}")
            canonical = key.lower()
            if canonical in normalized:
                raise ValueError(f"повторное имя ключа изображения {key!r}")
            normalized[canonical] = definition
        return normalized

    @field_validator("images", mode="before")
    @classmethod
    def image_list(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("images")
    @classmethod
    def unique_flat_images(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for path in value:
            _validate_image_filename(path)
        folded = [path.casefold() for path in value]
        if len(folded) != len(set(folded)):
            raise ValueError("повторное имя изображения")
        return value

    @model_validator(mode="after")
    def icon_paths_are_registered(self) -> "ProjectDefinition":
        registered = set(self.images)
        for name, icon in self.icons.items():
            _validate_image_filename(icon.path)
            if icon.path not in registered:
                raise ValueError(f"ключ {name!r} ссылается на незарегистрированное изображение")
        return self

    @model_validator(mode="after")
    def unique_components(self) -> "ProjectDefinition":
        ids = [entry.id for entry in self.components]
        if len(ids) != len(set(ids)):
            raise ValueError("повторный ID компонента")
        return self
