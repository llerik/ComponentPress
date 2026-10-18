from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from componentpress.domain.diagnostics import ProjectError
from componentpress.i18n import set_language
from componentpress.i18n.diagnostics import format_project_error
from componentpress.project_io.yaml_codec import parse_component


def _conditional(operator: str, value: str | None) -> str:
    node = f"""      - id: condition
        name: Condition
        type: conditional_group
        x_mm: 0
        y_mm: 0
        condition_column: Kind
        condition_operator: {operator}
"""
    return node + (f"        condition_value: {value}\n" if value is not None else "        children: []\n")


def _component(operator: str, value: str | None, placement: str, *, bound: bool = False) -> str:
    image = """  - id: previous-image
    name: Previous image
    type: image
    x_mm: 0
    y_mm: 0
    width_mm: 2
    height_mm: 2
    source: {mode: manual, path: assets/images/leaf.png}
"""
    html = """  - id: previous-html
    name: Previous HTML
    type: html
    x_mm: 0
    y_mm: 0
    width_mm: 2
    height_mm: 2
    font_family: Arial
    font_size_pt: 10
    content: {mode: manual, html: '<p>Previous</p>'}
"""
    condition = _conditional(operator, value)
    if placement != "nested":
        condition = textwrap.indent(textwrap.dedent(condition), "  ")
    sibling = image if placement == "after_image" else html if placement == "after_html" else ""
    nested = """  - id: parent
    name: Parent
    type: group
    x_mm: 0
    y_mm: 0
    children:
""" + condition
    elements = nested if placement == "nested" else sibling + condition
    data = "data: {sheet: Cards}\n" if bound else ""
    return (
        "schema_version: 7\nid: card\nname: Card\n"
        "size_mm: {width: 63, height: 88}\n"
        + data
        + "elements:\n"
        + elements
    )


@pytest.mark.parametrize("placement", ("first", "after_image", "after_html", "nested"), ids=("first", "after_image", "after_html", "nested"))
@pytest.mark.parametrize(
    ("operator", "value"),
    (("equals", "yes"), ("not_equals", "yes"), ("is_empty", None), ("is_not_empty", None)),
    ids=("equals", "not_equals", "is_empty", "is_not_empty"),
)
@pytest.mark.parametrize(
    ("locale", "message"),
    (
        ("ru", "Для условной группы нужен источник данных Excel для столбца Kind."),
        ("en", "Conditional group requires an Excel data source for column Kind."),
    ),
    ids=("ru", "en"),
)
def test_unbound_condition_reports_its_exact_yaml_address(
    tmp_path: Path, placement: str, operator: str, value: str | None,
    locale: str, message: str,
) -> None:
    set_language(locale)
    text = _component(operator, value, placement)
    path = tmp_path / "components" / "card.yaml"
    expected_field = {
        "nested": "elements.0.children.0.condition_column",
        "first": "elements.0.condition_column",
        "after_image": "elements.1.condition_column",
        "after_html": "elements.1.condition_column",
    }[placement]
    with pytest.raises(ProjectError) as error:
        parse_component(text, path)
    assert error.value.diagnostic.code == "COLUMN_WITHOUT_DATA"
    assert error.value.diagnostic.path == path
    assert error.value.diagnostic.field == expected_field
    assert format_project_error(error.value).endswith(message)
    assert not path.exists()


@pytest.mark.parametrize(
    ("operator", "value"),
    (("equals", "yes"), ("not_equals", "yes"), ("is_empty", None), ("is_not_empty", None)),
    ids=("equals", "not_equals", "is_empty", "is_not_empty"),
)
def test_bound_condition_with_each_operator_parses(operator: str, value: str | None) -> None:
    component, _tree = parse_component(
        _component(operator, value, "first", bound=True), Path("components/card.yaml")
    )
    condition = component.elements[0]
    assert condition.condition_operator == operator
    assert condition.condition_value == value
