import argparse
from pathlib import Path
import sys
import atexit
from PySide6.QtCore import QSettings

from componentpress.bootstrap import project_service
from componentpress.application.build_service import BuildService
from componentpress.application.contracts import BuildRequest
from componentpress.domain.diagnostics import ProjectError
from componentpress.diagnostics import logging as technical_log
from componentpress.i18n import set_language, validate_catalogs, tr


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="componentpress", description=tr("cli.description"))
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new", help=tr("cli.create"))
    new.add_argument("folder", type=Path)
    new.add_argument("--name", default="Новая игра")
    validate = commands.add_parser("validate", help=tr("cli.validate"))
    validate.add_argument("folder", type=Path)
    migrate = commands.add_parser("migrate", help=tr("cli.migrate"))
    migrate.add_argument("folder", type=Path)
    rename = commands.add_parser("set-name", help=tr("cli.rename"))
    rename.add_argument("folder", type=Path)
    rename.add_argument("name")
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
    image.add_argument("--into", default="", help="вложенная папка внутри assets/images")
    render = commands.add_parser("render", help="отрисовать компонент или выбранный экземпляр в PNG")
    render.add_argument("folder", type=Path)
    render.add_argument("--component", required=True)
    render.add_argument("--output", required=True, type=Path)
    render.add_argument("--dpi", type=int)
    render.add_argument("--mode", choices=("prod", "test"), default="prod")
    render.add_argument("--pdf", type=Path, help="создать пробный PDF с компонентом на отдельной странице")
    selection = render.add_mutually_exclusive_group()
    selection.add_argument("--row", type=int, help="номер строки Excel")
    selection.add_argument("--instance-id", help="ID экземпляра из настроенного столбца")
    build = commands.add_parser("build", help="собрать PNG тиража и печатный PDF")
    build.add_argument("folder", type=Path)
    build.add_argument("--component", help="собрать только один компонент")
    build.add_argument("--workers", type=int, default=2, choices=range(1, 65), metavar="N")
    build.add_argument("--mode", choices=("prod", "test"), default="prod")
    return parser


def main(argv: list[str] | None = None) -> int:
    technical_log.initialize()
    technical_log.install_exception_hooks()
    atexit.register(technical_log.shutdown)
    validate_catalogs()
    set_language(QSettings("ComponentPress", "ComponentPress").value("language", "ru", type=str))
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
    if not sys.stderr.isatty():
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    technical_log.log_event("cli.started", operation=args.command)
    service = project_service()
    try:
        if args.command == "new":
            snapshot = service.create(args.folder, args.name)
            print(tr("cli.create_ok", path=snapshot.root))
        elif args.command == "validate":
            snapshot = service.open(args.folder)
            print(tr("cli.validate_ok", name=snapshot.model.name, count=len(snapshot.documents)))
        elif args.command == "migrate":
            snapshot = service.migrate(args.folder)
            print(tr("cli.migrate_ok", version=snapshot.model.schema_version, path=snapshot.root))
        elif args.command == "set-name":
            service.rename(args.folder, args.name)
            print(tr("cli.rename_ok"))
        elif args.command == "set-size":
            service.resize(args.folder, args.component, args.width_mm, args.height_mm)
            print("Размер сохранён")
        elif args.command == "add-component":
            service.register(args.folder, args.id, args.name)
            print(tr("cli.add_ok"))
        elif args.command == "import-image":
            relative = service.import_image(args.folder, args.source, args.into)
            print(relative)
        elif args.command == "render":
            from componentpress.rendering import render_component
            from componentpress.application.preview_service import PreviewService
            from componentpress.data_sources import XlsxReader
            snapshot = service.open(args.folder)
            document = snapshot.documents.get(args.component)
            component = None
            if document is not None and document.model.data is not None:
                resolved = PreviewService(XlsxReader()).select_row(
                    snapshot, args.component, row_number=args.row, instance_id=args.instance_id, mode=args.mode
                )
                component = resolved.component
            result = render_component(snapshot, args.component, args.output, component=component, dpi=args.dpi, pdf=args.pdf)
            suffix = f"; PDF: {result.pdf}" if result.pdf else ""
            print(f"PNG: {result.png}; {result.width_px}x{result.height_px}; {result.dpi} DPI{suffix}")
        elif args.command == "build":
            from componentpress.rendering.exporter import ensure_gui_application

            ensure_gui_application()
            session = service.open_session(args.folder)
            builder = BuildService(service)
            request = BuildRequest(
                component_ids=(args.component,) if args.component else None,
                mode=args.mode,
                max_render_workers=args.workers,
            )
            result = builder.run(
                session,
                request,
                on_progress=lambda item: print(
                    f"[{item.phase}] {item.completed}/{item.total} {item.message}",
                    file=sys.stderr,
                ),
            )
            if result.status != "succeeded":
                for diagnostic in result.diagnostics:
                    print(diagnostic, file=sys.stderr)
                return 2
            for diagnostic in result.diagnostics:
                print(f"Предупреждение: {diagnostic}", file=sys.stderr)
            print(f"Результат: {result.output_directory}")
            print(f"PDF: {result.pdf_path}")
        return 0
    except ProjectError as exc:
        technical_log.log_event("cli.failed", operation=args.command, status="failed", code=exc.diagnostic.code)
        print(exc.diagnostic, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
