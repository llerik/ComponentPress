from pathlib import Path

import pytest
from pydantic import ValidationError
from PySide6.QtGui import QColor, QImage

from componentpress.bindings.icons import expand_html_icons
from componentpress.bindings.resolver import BindingResolver
from componentpress.domain.component import ComponentDefinition, SizeMM
from componentpress.domain.diagnostics import ProjectError
from componentpress.domain.nodes import HtmlNode
from componentpress.domain.project import IconDefinition, ProjectDefinition
from componentpress.domain.values import CellValue, DataRow, DataSheetSnapshot


def _project(keys: dict[str, IconDefinition]) -> ProjectDefinition:
    return ProjectDefinition(
        schema_version=9,
        id="game",
        name="Game",
        version="1.0",
        images=("assets/images/magic.png",),
        icons=keys,
    )


def _html_component(html: str, *, column: str | None = None, bound: bool = False) -> ComponentDefinition:
    return ComponentDefinition(
        schema_version=9, id="card", name="Card", size_mm=SizeMM(width=40, height=30),
        data={"sheet": "Cards"} if column or bound else None,
        elements=(HtmlNode(
            id="text", name="Text", type="html", x_mm=0, y_mm=0,
            width_mm=30, height_mm=20, font_family="Arial", font_size_pt=8,
            html=html, content_mode="column" if column else "manual", content_column=column,
        ),),
    )


def test_image_keys_are_lowercase_and_case_insensitively_unique() -> None:
    project = _project({
        "IMAGE_2": IconDefinition(path="assets/images/magic.png"),
    })

    assert tuple(project.icons) == ("image_2",)
    with pytest.raises(ValidationError):
        _project({
            "IMAGE_2": IconDefinition(path="assets/images/magic.png"),
            "image_2": IconDefinition(path="assets/images/magic.png"),
        })


@pytest.mark.parametrize("invalid", ("a b", "имя", "a.b", "a/b", "a\\b", "a:b"))
def test_image_key_accepts_only_ascii_letters_digits_underscore_and_hyphen(invalid: str) -> None:
    with pytest.raises(ValidationError):
        _project({invalid: IconDefinition(path="assets/images/magic.png")})


@pytest.mark.parametrize("path", (
    "assets/images/icons/magic.png",
    "assets/images/..\\magic.png",
    "assets/images/magic.gif",
))
def test_registered_images_must_be_flat_png_or_jpeg_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        ProjectDefinition(schema_version=9, id="game", name="Game", version="1.0", images=(path,))


def test_key_must_reference_a_registered_image() -> None:
    with pytest.raises(ValidationError):
        ProjectDefinition(
            schema_version=9, id="game", name="Game", version="1.0",
            icons={"magic": IconDefinition(path="assets/images/magic.png")},
        )


def test_html_key_expands_using_text_size_and_preserves_other_html() -> None:
    project = _project({
        "image_2": IconDefinition(path="assets/images/magic.png"),
    })

    result = expand_html_icons(
        '<p>{IMAGE_2}</p><img alt="{image_2}" />', project.icons,
        loader=None, owner=Path("card.yaml"), field="elements.text.html",
        source="", sheet="", cell="", node_id="text",
    )

    assert '<p><img src="assets/images/magic.png" height="10"' in result
    assert 'alt="{image_2}"' in result


def test_resolver_expands_only_original_html_keys_and_keeps_opaque_values() -> None:
    project = _project({"magic": IconDefinition(path="assets/images/magic.png")})
    project = project.model_copy(update={"variables": {"label": "{magic}"}})
    component = _html_component(
        '<p>{magic} {{ vars["label"] }} \\{magic\\}</p><img alt="{magic}" src="assets/images/leaf.png" />&lt;{magic}',
    )

    result = BindingResolver(Path("."), project, validate_resources=False).resolve_component(
        component, None, None,
    )

    assert result.component.elements[0].html == (
        '<p><img src="assets/images/magic.png" height="8" alt="magic" /> '
        '{magic} {magic}</p><img alt="{magic}" src="assets/images/leaf.png" />&lt;'
        '<img src="assets/images/magic.png" height="8" alt="magic" />'
    )


def test_column_html_expands_keys_from_the_original_cell(tmp_path: Path) -> None:
    project = _project({"magic": IconDefinition(path="assets/images/magic.png")})
    component = _html_component("", column="Markup", bound=True)
    row = DataRow(2, "instance", 1, {"Markup": CellValue("<p>{magic}</p>", "C2")})
    sheet = DataSheetSnapshot("main", tmp_path / "book.xlsx", "Cards", "hash", 1, ("Markup",), (row,))

    resolved = BindingResolver(tmp_path, project, validate_resources=False).resolve_component(component, sheet, row)

    assert resolved.component.elements[0].html == (
        '<p><img src="assets/images/magic.png" height="8" alt="magic" /></p>'
    )


def test_unknown_excel_html_key_is_preserved_and_reports_address() -> None:
    project = _project({})
    component = _html_component("", column="Markup", bound=True)
    row = DataRow(4, "instance", 1, {"Markup": CellValue("<p>{missing}</p>", "C4")})
    sheet = DataSheetSnapshot("main", Path("book.xlsx"), "Cards", "hash", 1, ("Markup",), (row,))
    resolver = BindingResolver(Path("."), project, validate_resources=False)

    resolved = resolver.resolve_component(component, sheet, row)
    assert "{missing}" in resolved.component.elements[0].html
    diagnostic = resolved.diagnostics[0]
    assert diagnostic.code == "IMAGE_KEY_UNKNOWN"
    assert diagnostic.message_key == "error.image_key_unknown"
    assert diagnostic.params == {"name": "missing"}
    assert (diagnostic.source, diagnostic.sheet, diagnostic.cell, diagnostic.node_id) == (
        "main", "Cards", "C4", "text",
    )


def test_unknown_manual_html_key_remains_a_validation_error() -> None:
    component = _html_component("<p>{missing}</p>")
    with pytest.raises(ProjectError) as error:
        BindingResolver(Path("."), _project({}), validate_resources=False).resolve_component(
            component, None, None,
        )
    assert error.value.diagnostic.code == "IMAGE_KEY_UNKNOWN"


def test_variable_value_is_not_reparsed_as_an_image_key() -> None:
    from componentpress.bindings.resolver import BindingLocation, BindingResolver

    project = ProjectDefinition(
        schema_version=9, id="game", name="Game", version="1.0",
        variables={"label": "{image_2}"}, images=("assets/images/magic.png",),
        icons={"image_2": IconDefinition(path="assets/images/magic.png")},
    )
    html = BindingResolver(Path("."), project, validate_resources=False).resolve_html(
        '<p>{{ vars["label"] }}</p>', None,
        BindingLocation(Path("card.yaml"), "elements.text.html", "text", "", ""),
    )

    assert html == "<p>{image_2}</p>"


def test_html_unknown_image_key_fails_with_a_diagnostic() -> None:
    project = _project({})
    from componentpress.domain.diagnostics import ProjectError

    with pytest.raises(ProjectError) as caught:
        expand_html_icons(
            "<p>{missing}</p>", project.icons,
            loader=None, owner=Path("card.yaml"), field="html",
            source="", sheet="", cell="", node_id="text",
        )
    assert caught.value.diagnostic.code == "IMAGE_KEY_UNKNOWN"


def test_html_key_resource_error_keeps_diagnostic_metadata(tmp_path: Path) -> None:
    from componentpress.domain.diagnostics import Diagnostic

    class FailingLoader:
        def image(self, relative_path: str, *, owner: Path, field: str) -> None:
            raise ProjectError(Diagnostic(
                "RESOURCE_MISSING", relative_path, owner, field,
                message_key="error.resource_missing", params={"reason": "unavailable"},
                incident_id="incident-test",
            ))

    project = _project({"magic": IconDefinition(path="assets/images/magic.png")})
    with pytest.raises(ProjectError) as caught:
        expand_html_icons(
            "<p>{magic}</p>", project.icons, loader=FailingLoader(), owner=tmp_path / "card.yaml",
            field="elements.text.html", source="main", sheet="Cards", cell="C4", node_id="text",
        )

    diagnostic = caught.value.diagnostic
    assert (diagnostic.message_key, diagnostic.params, diagnostic.incident_id) == (
        "error.resource_missing", {"reason": "unavailable"}, "incident-test",
    )
    assert (diagnostic.source, diagnostic.sheet, diagnostic.cell, diagnostic.node_id) == (
        "main", "Cards", "C4", "text",
    )


def test_unregistered_file_can_be_registered_again_and_keys_follow_registration(tmp_path: Path) -> None:
    from componentpress.bootstrap import project_service

    service = project_service()
    root = tmp_path / "project"
    service.create(root, "Game")
    source = tmp_path / "magic.png"
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(QColor("#FF00FF"))
    assert image.save(str(source), "PNG")

    relative = service.import_image(root, source)
    session = service.open_session(root)
    service.remove_image(session, relative, delete_file=False)
    assert (root / relative).is_file()
    assert session.snapshot.model.images == ()

    assert service.import_image(root, source) == relative
    session = service.open_session(root)
    service.add_image_key(session, "IMAGE_2", relative)
    assert tuple(session.snapshot.model.icons) == ("image_2",)
    from componentpress.domain.nodes import HtmlNode

    service.add_to_session(session, "Card", "card")
    document = session.documents["card"]
    model = document.model.model_copy(update={
        "elements": (HtmlNode(
            id="text", name="Text", type="html", x_mm=0, y_mm=0,
            width_mm=40, height_mm=20, font_family="Arial", font_size_pt=8,
            html="<p>{image_2}</p>",
        ),),
    })
    document.replace_snapshot(service.prepare_model(session, "card", model))
    service.save_document(session, "card")
    service.remove_image_key(session, "IMAGE_2")
    assert service.open(root).model.icons == {}
    service.remove_image(session, relative, delete_file=True)
    assert not (root / relative).exists()
    assert session.snapshot.model.images == ()


def test_image_key_edit_renames_atomically_and_rejects_collisions(tmp_path: Path) -> None:
    from componentpress.bootstrap import project_service
    from componentpress.domain.diagnostics import ProjectError

    service = project_service()
    root = tmp_path / "project"
    service.create(root, "Game")
    source = tmp_path / "magic.png"
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(QColor("#FF00FF"))
    assert image.save(str(source), "PNG")
    relative = service.import_image(root, source)
    session = service.open_session(root)
    service.add_image_key(session, "magic", relative)

    service.update_image_key(session, "magic", "MAGIC")
    edited = session.snapshot.model.icons["magic"]
    assert edited.path == relative

    original_snapshot = session.snapshot
    original_file = (root / "project.cpress").read_bytes()
    with pytest.raises(ProjectError):
        service.update_image_key(session, "magic", "bad name")
    assert session.snapshot is original_snapshot
    assert (root / "project.cpress").read_bytes() == original_file

    with pytest.raises(ProjectError):
        service.add_image_key(session, "other", relative)
    assert session.snapshot is original_snapshot
    assert (root / "project.cpress").read_bytes() == original_file
