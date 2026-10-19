from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from componentpress.i18n import catalogs, set_language, validate_catalogs
from componentpress.domain.diagnostics import Diagnostic
from componentpress.project_io.yaml_codec import parse_project


ROOT = Path(__file__).resolve().parents[1]


def _cli(language: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["COMPONENTPRESS_TEST_LANGUAGE"] = language
    return subprocess.run(
        [sys.executable, "-B", "-m", "componentpress", *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
    )


@pytest.mark.parametrize(
    ("language", "labels"),
    (
        ("ru", ("использование:", "позиционные аргументы", "импортировать PNG/JPEG", "отрисовать", "собрать тираж", "ошибка:")),
        ("en", ("usage:", "positional arguments", "import PNG/JPEG", "render a component", "build the print run", "error:")),
    ),
    ids=("ru", "en"),
)
def test_cli_help_and_argparse_failures_use_the_selected_language(
    language: str, labels: tuple[str, ...], tmp_path: Path
) -> None:
    help_result = _cli(language, "--help")
    assert help_result.returncode == 0
    assert all(label in help_result.stdout for label in labels[:-1])
    command_help = {
        "render": "номер строки Excel" if language == "ru" else "Excel row number",
        "build": "рабочих потоков" if language == "ru" else "rendering workers",
    }
    for command, expected in command_help.items():
        result = _cli(language, command, "--help")
        assert result.returncode == 0
        assert expected in result.stdout
    errors = (
        ("new",),
        ("set-size", str(tmp_path), "card", "wide", "88"),
        ("import-image", str(tmp_path)),
        ("render", str(tmp_path), "--component", "card"),
        ("build", str(tmp_path), "--workers", "0"),
        ("render", str(tmp_path), "--component", "card", "--output", "out.png", "--row", "1", "--instance-id", "id"),
    )
    for arguments in errors:
        result = _cli(language, *arguments)
        assert result.returncode == 2
        assert labels[-1] in result.stderr
        assert ("использование:" in result.stderr if language == "ru" else "usage:" in result.stderr), (arguments, result.stderr)
    assert "обязательные аргументы" in _cli(language, "new").stderr if language == "ru" else "required" in _cli(language, "new").stderr


@pytest.mark.parametrize("language", ("ru", "en"))
def test_cli_commands_preserve_user_names_and_localize_results(
    language: str, tmp_path: Path
) -> None:
    set_language(language)
    validate_catalogs()
    assert catalogs()["ru"].keys() == catalogs()["en"].keys()
    custom = tmp_path / "custom"
    created = _cli(language, "new", str(custom), "--name", "Forest card 🦊")
    assert created.returncode == 0, created.stderr
    project_file = custom / "project.yaml"
    assert parse_project(project_file.read_text(encoding="utf-8"), project_file)[0].name == "Forest card 🦊"
    default = tmp_path / "default"
    assert _cli(language, "new", str(default)).returncode == 0
    expected_default = "Новая игра" if language == "ru" else "New game"
    default_file = default / "project.yaml"
    assert parse_project(default_file.read_text(encoding="utf-8"), default_file)[0].name == expected_default

    assert _cli(language, "add-component", str(custom), "card", "Card").returncode == 0
    resized = _cli(language, "set-size", str(custom), "card", "70", "90")
    assert resized.returncode == 0, resized.stderr
    assert resized.stdout.strip() == ("Размер сохранён" if language == "ru" else "Size saved")

    image = ROOT / "examples" / "demo-game" / "assets" / "images" / "leaf.png"
    imported = _cli(language, "import-image", str(custom), str(image))
    assert imported.returncode == 0, imported.stderr
    assert imported.stdout.strip() == "assets/images/leaf.png"
    copied = custom / "assets" / "images" / "leaf.png"
    assert copied.read_bytes() == image.read_bytes()


@pytest.mark.parametrize("language", ("ru", "en"))
def test_render_and_build_report_success_and_project_diagnostics(
    language: str, tmp_path: Path
) -> None:
    project = tmp_path / "game"
    shutil.copytree(
        ROOT / "examples" / "demo-game",
        project,
        ignore=shutil.ignore_patterns("output", "archive", ".componentpress"),
    )
    project_file = project / "project.yaml"
    project_file.write_text(project_file.read_text(encoding="utf-8").replace("dpi: 300", "dpi: 10"), encoding="utf-8")
    rendered = _cli(language, "render", str(project), "--component", "forest-card", "--output", str(tmp_path / "preview.png"))
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout.startswith("PNG:")
    assert (tmp_path / "preview.png").is_file()
    built = _cli(language, "build", str(project), "--workers", "1")
    assert built.returncode == 0, built.stderr
    assert "Result:" in built.stdout if language == "en" else "Результат:" in built.stdout
    assert "PDF:" in built.stdout
    invalid = _cli(language, "set-size", str(project), "missing-card", "70", "90")
    assert invalid.returncode == 2
    assert "COMPONENT_UNKNOWN" in invalid.stderr


@pytest.mark.parametrize("language", ("ru", "en"))
def test_cli_subject_failures_preserve_diagnostic_code_and_address(language: str, tmp_path: Path) -> None:
    project = tmp_path / "project"
    created = _cli(language, "new", str(project))
    assert created.returncode == 0, created.stderr
    empty_name = _cli(language, "new", str(tmp_path / "empty-name"), "--name", "")
    assert empty_name.returncode == 2
    assert "PROJECT_NAME" in empty_name.stderr and str(tmp_path / "empty-name") in empty_name.stderr
    bad_size = _cli(language, "set-size", str(project), "unknown", "70", "90")
    assert bad_size.returncode == 2
    assert "COMPONENT_UNKNOWN" in bad_size.stderr and str(project) in bad_size.stderr
    absent = tmp_path / "missing-project"
    absent.mkdir()
    (absent / "project.yaml").write_text("invalid: [", encoding="utf-8")
    commands = (
        ("import-image", str(absent), "missing.png"),
        ("render", str(absent), "--component", "card", "--output", "preview.png"),
        ("build", str(absent),),
    )
    for arguments in commands:
        result = _cli(language, *arguments)
        assert result.returncode == 2
        assert "YAML_SYNTAX" in result.stderr
        assert str(absent / "project.yaml") in result.stderr


@pytest.mark.parametrize(
    ("language", "warning"),
    (("ru", "Предупреждение:"), ("en", "Warning:")),
    ids=("ru", "en"),
)
def test_build_warning_uses_the_selected_language(language: str, warning: str, tmp_path: Path, monkeypatch, capsys) -> None:
    from componentpress import cli

    set_language(language)
    diagnostic = Diagnostic("CLEANUP_PENDING", "cleanup pending", tmp_path, severity="warning")
    result = SimpleNamespace(
        status="succeeded", diagnostics=(diagnostic,), output_directory=tmp_path / "output",
        pdf_path=tmp_path / "output" / "print.pdf",
    )
    monkeypatch.setattr(cli, "build_service", lambda _service: SimpleNamespace(
        start=lambda _session, _request, *, on_progress, on_finished: (on_finished(result), "job-1")[1],
    ))
    monkeypatch.setattr("componentpress.rendering.exporter.ensure_gui_application", lambda: None)
    args = SimpleNamespace(folder=tmp_path, component=None, mode="prod", workers=1)
    assert cli._run_build(args, SimpleNamespace(open_session=lambda _folder: object())) == 0
    assert warning in capsys.readouterr().err
