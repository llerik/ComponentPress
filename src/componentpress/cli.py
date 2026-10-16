import argparse
from pathlib import Path
import re
import sys
import atexit
from PySide6.QtCore import QSettings

from componentpress.bootstrap import project_service
from componentpress.application.build_service import BuildService
from componentpress.application.contracts import BuildRequest
from componentpress.domain.diagnostics import ProjectError
from componentpress.diagnostics import logging as technical_log
from componentpress.i18n import set_language, validate_catalogs, tr


_ARGPARSE_PATTERNS = (
    ("cli.argparse_required", re.compile(r"the following arguments are required: (?P<arguments>.*)")),
    ("cli.argparse_choice", re.compile(r"(?P<argument>argument [^:]+): invalid choice: (?P<value>.+) \(choose from (?P<choices>.*)\)")),
    ("cli.argparse_unrecognized", re.compile(r"unrecognized arguments: (?P<arguments>.*)")),
    ("cli.argparse_expected", re.compile(r"(?P<argument>argument [^:]+): expected one argument")),
    ("cli.argparse_conflict", re.compile(r"(?P<argument>argument [^:]+): not allowed with (?P<other>argument .+)")),
)


class LocalizedHelpFormatter(argparse.HelpFormatter):
    def _format_usage(self, usage, actions, groups, prefix):
        if prefix is None or prefix.startswith("usage: "):
            prefix = tr("cli.argparse_usage") + prefix[len("usage: "):] if prefix else tr("cli.argparse_usage")
        return super()._format_usage(usage, actions, groups, prefix)

    def start_section(self, heading):
        labels = {
            "positional arguments": "cli.argparse_positionals",
            "options": "cli.argparse_options",
            "optional arguments": "cli.argparse_options",
        }
        super().start_section(tr(labels[heading]) if heading in labels else heading)

    def _get_help_string(self, action):
        return tr("cli.argparse_help") if action.dest == "help" else super()._get_help_string(action)


def _localize_argparse_error(message: str) -> str:
    for key, pattern in _ARGPARSE_PATTERNS:
        match = pattern.fullmatch(message)
        if match:
            return tr(key, **match.groupdict())
    number = re.fullmatch(r"(?P<argument>argument [^:]+): invalid (?P<kind>float|int) value: (?P<value>.*)", message)
    if number:
        values = number.groupdict()
        values["kind"] = tr(f"cli.argparse_type_{values['kind']}")
        return tr("cli.argparse_number", **values)
    return tr("cli.argparse_invalid_arguments")


class LocalizedArgumentParser(argparse.ArgumentParser):
    def _get_formatter(self):
        return LocalizedHelpFormatter(prog=self.prog)

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, tr("cli.argparse_parser_error", program=self.prog, message=_localize_argparse_error(message)))


def _add_project_commands(commands) -> None:
    new = commands.add_parser("new", help=tr("cli.create"))
    new.add_argument("folder", type=Path)
    new.add_argument("--name", default=tr("cli.new_default_name"))
    validate = commands.add_parser("validate", help=tr("cli.validate"))
    validate.add_argument("folder", type=Path)
    commands.add_parser("migrate", help=tr("cli.migrate")).add_argument("folder", type=Path)
    rename = commands.add_parser("set-name", help=tr("cli.rename"))
    rename.add_argument("folder", type=Path)
    rename.add_argument("name")


def _add_component_commands(commands) -> None:
    size = commands.add_parser("set-size", help=tr("cli.resize"))
    size.add_argument("folder", type=Path)
    size.add_argument("component")
    size.add_argument("width_mm", type=float)
    size.add_argument("height_mm", type=float)
    add = commands.add_parser("add-component", help=tr("cli.add_component"))
    add.add_argument("folder", type=Path)
    add.add_argument("id")
    add.add_argument("name")
    image = commands.add_parser("import-image", help=tr("cli.import_image"))
    image.add_argument("folder", type=Path)
    image.add_argument("source", type=Path)
    image.add_argument("--into", default="", help=tr("cli.import_image_into"))


def _add_render_command(commands) -> None:
    render = commands.add_parser("render", help=tr("cli.render"))
    render.add_argument("folder", type=Path)
    render.add_argument("--component", required=True, help=tr("cli.component"))
    render.add_argument("--output", required=True, type=Path, help=tr("cli.output_png"))
    render.add_argument("--dpi", type=int, help=tr("cli.dpi"))
    render.add_argument("--mode", choices=("prod", "test"), default="prod", help=tr("cli.mode"))
    render.add_argument("--pdf", type=Path, help=tr("cli.preview_pdf"))
    selection = render.add_mutually_exclusive_group()
    selection.add_argument("--row", type=int, help=tr("cli.excel_row"))
    selection.add_argument("--instance-id", help=tr("cli.instance_id"))


def _add_build_command(commands) -> None:
    build = commands.add_parser("build", help=tr("cli.build"))
    build.add_argument("folder", type=Path)
    build.add_argument("--component", help=tr("cli.component"))
    build.add_argument("--workers", type=int, default=2, choices=range(1, 65), metavar="N", help=tr("cli.workers"))
    build.add_argument("--mode", choices=("prod", "test"), default="prod", help=tr("cli.mode"))


def build_parser() -> argparse.ArgumentParser:
    parser = LocalizedArgumentParser(
        prog="componentpress", description=tr("cli.description"),
        formatter_class=LocalizedHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    _add_project_commands(commands)
    _add_component_commands(commands)
    _add_render_command(commands)
    _add_build_command(commands)
    return parser


def _initialize_cli() -> None:
    technical_log.initialize()
    technical_log.install_exception_hooks()
    atexit.register(technical_log.shutdown)
    validate_catalogs()
    set_language(QSettings("ComponentPress", "ComponentPress").value("language", "ru", type=str))
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
    if not sys.stderr.isatty():
        sys.stderr.reconfigure(encoding="utf-8")
def _create_or_validate(args, service) -> int | None:
    if args.command == "new":
        snapshot = service.create(args.folder, args.name)
        print(tr("cli.create_ok", path=snapshot.root))
    elif args.command == "validate":
        _validate_project(args, service)
    else:
        return None
    return 0


def _validate_project(args, service) -> None:
    snapshot = service.open(args.folder)
    print(tr("cli.validate_ok", name=snapshot.model.name, count=len(snapshot.documents)))
    if snapshot.model.data_source is None:
        return
    from componentpress.data_sources import XlsxReader
    from componentpress.project_io.paths import resolve_project_path

    source = resolve_project_path(snapshot.root, snapshot.model.data_source.path)
    reader = XlsxReader()
    for document in snapshot.documents.values():
        if document.model.data is None:
            continue
        binding = document.model.data
        data = reader.read(source, binding.sheet, copies_columns=(
            snapshot.model.copies_columns.prod, snapshot.model.copies_columns.test,
        ))
        print(f"Excel {data.sheet}: {data.area}")


def _update_project(args, service) -> int | None:
    if args.command == "migrate":
        snapshot = service.migrate(args.folder)
        print(tr("cli.migrate_ok", version=snapshot.model.schema_version, path=snapshot.root))
    elif args.command == "set-name":
        service.rename(args.folder, args.name)
        print(tr("cli.rename_ok"))
    elif args.command == "set-size":
        service.resize(args.folder, args.component, args.width_mm, args.height_mm)
        print(tr("cli.set_size_ok"))
    elif args.command == "add-component":
        service.register(args.folder, args.id, args.name)
        print(tr("cli.add_ok"))
    elif args.command == "import-image":
        print(service.import_image(args.folder, args.source, args.into))
    else:
        return None
    return 0


def _run_project_command(args, service) -> int | None:
    result = _create_or_validate(args, service)
    return result if result is not None else _update_project(args, service)


def _select_render_component(args, snapshot):
    document = snapshot.documents.get(args.component)
    if document is None or document.model.data is None:
        return None
    from componentpress.application.preview_service import PreviewService
    from componentpress.data_sources import XlsxReader

    return PreviewService(XlsxReader()).select_row(
        snapshot, args.component, row_number=args.row,
        instance_id=args.instance_id, mode=args.mode,
    ).component


def _run_render(args, service) -> int:
    from componentpress.rendering import render_component

    snapshot = service.open(args.folder)
    component = _select_render_component(args, snapshot)
    result = render_component(
        snapshot, args.component, args.output, component=component,
        dpi=args.dpi, pdf=args.pdf,
    )
    suffix = tr("cli.pdf_suffix", path=result.pdf) if result.pdf else ""
    print(tr("cli.png_result", png=result.png, width=result.width_px,
             height=result.height_px, dpi=result.dpi, pdf_suffix=suffix))
    return 0


def _run_build(args, service) -> int:
    from componentpress.rendering.exporter import ensure_gui_application

    ensure_gui_application()
    session = service.open_session(args.folder)
    request = BuildRequest(
        component_ids=(args.component,) if args.component else None,
        mode=args.mode,
        max_render_workers=args.workers,
    )
    result = BuildService(service).run(session, request, on_progress=_print_build_progress)
    if result.status != "succeeded":
        for diagnostic in result.diagnostics:
            print(diagnostic, file=sys.stderr)
        return 2
    for diagnostic in result.diagnostics:
        print(tr("cli.warning", message=diagnostic), file=sys.stderr)
    print(tr("cli.result", path=result.output_directory))
    print(tr("cli.pdf", path=result.pdf_path))
    return 0


def _print_build_progress(item) -> None:
    print(f"[{item.phase}] {item.completed}/{item.total} {item.message}", file=sys.stderr)


def _dispatch(args) -> int:
    technical_log.log_event("cli.started", operation=args.command)
    service = project_service()
    try:
        result = _run_project_command(args, service)
        if result is not None:
            return result
        return _run_render(args, service) if args.command == "render" else _run_build(args, service)
    except ProjectError as exc:
        technical_log.log_event("cli.failed", operation=args.command, status="failed", code=exc.diagnostic.code)
        print(exc.diagnostic, file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    _initialize_cli()
    args = build_parser().parse_args(argv)
    return _dispatch(args)


if __name__ == "__main__":
    raise SystemExit(main())
