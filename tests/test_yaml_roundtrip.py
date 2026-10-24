from pathlib import Path

import pytest

from componentpress.domain.diagnostics import ProjectError
from componentpress.domain.component import ComponentDefinition, DataBinding, SizeMM
from componentpress.i18n.diagnostics import format_project_error
from componentpress.domain.nodes import ConditionalGroupNode, GroupNode, HtmlNode, ImageNode, LineNode, ShapeNode
from componentpress.domain.project import ProjectDefinition
from componentpress.project_io.yaml_codec import dump_yaml, parse_component, parse_project, update_tree
from ruamel.yaml.comments import CommentedMap


COMPONENT = """schema_version: 8
id: card
name: Card
size_mm: {width: 40, height: 30}
__DATA__elements:
__NODE__
"""
IMAGE = """  - id: image
    name: Image
    type: image
    x_mm: 0
    y_mm: 0
    width_mm: 10
    height_mm: 10
    source: {mode: manual, path: assets/images/a.png__EXTRA__}
"""
HTML = """  - id: html
    name: HTML
    type: html
    x_mm: 0
    y_mm: 0
    width_mm: 10
    height_mm: 10
    font_family: Arial
    font_size_pt: 10
    content: {mode: manual, html: Hi__EXTRA__}
"""


@pytest.mark.parametrize(
    ("node", "data", "field"),
    (
        (IMAGE.replace("__EXTRA__", ", typo: ignored"), "", "elements.0.source.typo"),
        (HTML.replace("__EXTRA__", ", typo: ignored"), "", "elements.0.content.typo"),
        (IMAGE.replace("__EXTRA__", "").replace("    source:", "    source_mode: ignored\n    source:"), "", "elements.0.source_mode"),
        (HTML.replace("__EXTRA__", "").replace("    content:", "    content_mode: ignored\n    content:"), "", "elements.0.content_mode"),
        (IMAGE.replace("__EXTRA__", ""), "data: {sheet: Cards, source: hidden}\n", "data.source"),
    ),
    ids=("image-source-extra", "html-content-extra", "internal-source-mode", "internal-content-mode", "internal-data-source"),
)
def test_public_yaml_rejects_unknown_and_internal_fields(node: str, data: str, field: str) -> None:
    with pytest.raises(ProjectError) as caught:
        parse_component(COMPONENT.replace("__DATA__", data).replace("__NODE__", node), Path("card.yaml"))

    assert caught.value.diagnostic.code == "DOCUMENT_INVALID"
    assert caught.value.diagnostic.field == field
    field_name = field.rsplit(".", 1)[-1]
    assert format_project_error(caught.value, locale="ru").endswith(f"\u041d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u043e\u0435 \u043f\u043e\u043b\u0435 YAML: {field_name}.")
    assert format_project_error(caught.value, locale="en").endswith(f"Unknown YAML field: {field_name}.")


def test_component_round_trip_preserves_public_node_variants() -> None:
    component = ComponentDefinition(
        schema_version=8,
        id="card",
        name="Card",
        size_mm=SizeMM(width=40, height=30),
        data=DataBinding(sheet="Cards"),
        elements=(
            ImageNode(id="manual-image", name="Manual", type="image", x_mm=0, y_mm=0, width_mm=10, height_mm=10, source="assets/images/a.png"),
            ImageNode(id="column-image", name="Column", type="image", x_mm=0, y_mm=0, width_mm=10, height_mm=10, source="", source_mode="column", source_column="Image"),
            ImageNode(id="optional-image", name="Optional", type="image", x_mm=0, y_mm=0, width_mm=10, height_mm=10, source="", optional=True),
            HtmlNode(id="manual-html", name="Manual HTML", type="html", x_mm=0, y_mm=0, width_mm=10, height_mm=10, font_family="Arial", font_size_pt=10, html="<p>Card</p>"),
            HtmlNode(id="column-html", name="Column HTML", type="html", x_mm=0, y_mm=0, width_mm=10, height_mm=10, font_family="Arial", font_size_pt=10, html="", content_mode="column", content_column="Title"),
            GroupNode(
                id="group", name="Group", type="group", x_mm=0, y_mm=0,
                children=(
                    ShapeNode(id="shape", name="Shape", type="rectangle", x_mm=0, y_mm=0, width_mm=5, height_mm=5),
                    LineNode(id="line", name="Line", type="line", x_mm=0, y_mm=0, dx_mm=-2, dy_mm=3),
                ),
            ),
            *_conditions(),
        ),
    )
    tree = CommentedMap()

    update_tree(tree, component)
    text = dump_yaml(tree)
    parsed, _ = parse_component(text, Path("card.yaml"))

    assert parsed == component


def _conditions() -> tuple[ConditionalGroupNode, ...]:
    return tuple(
        ConditionalGroupNode(
            id=operator, name=operator, type="conditional_group", x_mm=0, y_mm=0,
            condition_column="Kind", condition_operator=operator,
            condition_value="yes" if operator in {"equals", "not_equals"} else None,
        )
        for operator in ("equals", "not_equals", "is_empty", "is_not_empty")
    )


def test_project_round_trip_preserves_comment_and_applies_model_edit() -> None:
    original = """schema_version: 8
id: game
name: Game # project note
version: "1.0"
components: []
"""
    model, tree = parse_project(original, Path("project.cpress"))
    update_tree(tree, model.model_copy(update={"name": "Updated"}))
    text = dump_yaml(tree)
    parsed, _ = parse_project(text, Path("project.cpress"))

    assert parsed.name == "Updated"
    assert "# project note" in text


def test_comments_follow_node_ids_and_round_trip_stabilizes() -> None:
    original = """schema_version: 8
id: card
name: Card
size_mm: {width: 40, height: 30}
elements:
  # front note
  - id: front
    name: Front
    type: rectangle
    x_mm: 0
    y_mm: 0
    width_mm: 5
    height_mm: 5
  # back note
  - id: back
    name: Back
    type: rectangle
    x_mm: 5
    y_mm: 0
    width_mm: 5
    height_mm: 5
"""
    model, tree = parse_component(original, Path("card.yaml"))
    update_tree(tree, model.model_copy(update={"elements": tuple(reversed(model.elements))}))
    text = dump_yaml(tree)

    assert text.index("# back note") < text.index("id: back") < text.index("# front note") < text.index("id: front")
    previous = text
    for _ in range(3):
        model, tree = parse_component(text, Path("card.yaml"))
        update_tree(tree, model)
        text = dump_yaml(tree)
        assert text == previous
        previous = text
    assert text.count("\n\n") == 1
