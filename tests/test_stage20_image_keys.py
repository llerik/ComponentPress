from pathlib import Path

import pytest
from pydantic import ValidationError
from PySide6.QtGui import QColor, QImage

from componentpress.bindings.icons import expand_html_icons
from componentpress.domain.project import IconDefinition, ProjectDefinition


def _project(keys: dict[str, IconDefinition]) -> ProjectDefinition:
    return ProjectDefinition(
        schema_version=8,
        id="game",
        name="Game",
        version="1.0",
        images=("assets/images/magic.png",),
        icons=keys,
    )


def test_image_keys_are_lowercase_and_case_insensitively_unique() -> None:
    project = _project({
        "IMAGE_2": IconDefinition(path="assets/images/magic.png", width_px=8, height_px=8),
    })

    assert tuple(project.icons) == ("image_2",)
    with pytest.raises(ValidationError):
        _project({
            "IMAGE_2": IconDefinition(path="assets/images/magic.png", width_px=8, height_px=8),
            "image_2": IconDefinition(path="assets/images/magic.png", width_px=8, height_px=8),
        })


@pytest.mark.parametrize("invalid", ("a b", "имя", "a.b", "a/b", "a\\b", "a:b"))
def test_image_key_accepts_only_ascii_letters_digits_underscore_and_hyphen(invalid: str) -> None:
    with pytest.raises(ValidationError):
        _project({invalid: IconDefinition(path="assets/images/magic.png", width_px=8, height_px=8)})


@pytest.mark.parametrize("path", (
    "assets/images/icons/magic.png",
    "assets/images/..\\magic.png",
    "assets/images/magic.gif",
))
def test_registered_images_must_be_flat_png_or_jpeg_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        ProjectDefinition(schema_version=8, id="game", name="Game", version="1.0", images=(path,))


def test_key_must_reference_a_registered_image() -> None:
    with pytest.raises(ValidationError):
        ProjectDefinition(
            schema_version=8, id="game", name="Game", version="1.0",
            icons={"magic": IconDefinition(path="assets/images/magic.png", width_px=8, height_px=8)},
        )


def test_html_key_expands_from_text_nodes_using_configured_pixel_size() -> None:
    project = _project({
        "image_2": IconDefinition(path="assets/images/magic.png", width_px=8, height_px=8),
    })

    result = expand_html_icons(
        '<p>{IMAGE_2}</p><img alt="{image_2}" />', project.icons,
        loader=None, owner=Path("card.yaml"), field="elements.text.html",
        source="", sheet="", cell="", node_id="text",
    )

    assert '<p><img src="assets/images/magic.png" width="8" height="8"' in result
    assert 'alt="{image_2}"' in result


def test_variable_value_is_not_reparsed_as_an_image_key() -> None:
    from componentpress.bindings.resolver import BindingLocation, BindingResolver

    project = ProjectDefinition(
        schema_version=8, id="game", name="Game", version="1.0",
        variables={"label": "{image_2}"}, images=("assets/images/magic.png",),
        icons={"image_2": IconDefinition(path="assets/images/magic.png", width_px=8, height_px=8)},
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
    service.add_image_key(session, "IMAGE_2", relative, 8, 8)
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


def test_image_key_edit_renames_and_resizes_atomically(tmp_path: Path) -> None:
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
    service.add_image_key(session, "magic", relative, 100001, 8)
    service.add_image_key(session, "other", relative, 8, 8)

    service.update_image_key(session, "magic", "MAGIC", 120001, 60001)
    edited = session.snapshot.model.icons["magic"]
    assert (edited.path, edited.width_px, edited.height_px) == (relative, 120001, 60001)
    assert session.snapshot.model.icons["other"].width_px == 8

    original_snapshot = session.snapshot
    original_file = (root / "project.yaml").read_bytes()
    with pytest.raises(ProjectError):
        service.update_image_key(session, "magic", "other", 1, 1)
    assert session.snapshot is original_snapshot
    assert (root / "project.yaml").read_bytes() == original_file

    with pytest.raises(ProjectError):
        service.update_image_key(session, "magic", "bad name", 1, 1)
    assert session.snapshot is original_snapshot
    assert (root / "project.yaml").read_bytes() == original_file
