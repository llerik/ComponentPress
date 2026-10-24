"""About and error-report dialogs for the main window."""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError, metadata, version
import platform
from pathlib import Path
import zipfile

from PySide6 import __version__ as qt_version
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QLabel, QMessageBox, QVBoxLayout,
)

from componentpress.diagnostics import logging as technical_log
from componentpress.domain.schema import SCHEMA_VERSION
from componentpress.i18n import tr
from componentpress.platforms.services import PlatformServices
from componentpress.version import APPLICATION_VERSION


class MainWindowDialogs:
    """Present application information and technical error reports."""

    def _show_about(self) -> None:
        dialog = QDialog(self)
        dialog.setObjectName("aboutDialog")
        dialog.setWindowTitle(tr("about.title"))
        layout = QVBoxLayout(dialog)
        self._add_about_identity(layout)
        self._add_about_dependencies(layout)
        self._add_about_buttons(dialog, layout)
        dialog.resize(720, 340)
        dialog.exec()

    @staticmethod
    def _add_about_identity(layout: QVBoxLayout) -> None:
        layout.addWidget(QLabel(f"ComponentPress {APPLICATION_VERSION}\n{tr('about.description')}"))
        details = QFormLayout()
        details.addRow(QLabel(tr("about.schema", version=SCHEMA_VERSION)))
        details.addRow(QLabel(tr("about.runtime", python=platform.python_version(), qt=qt_version)))
        layout.addLayout(details)

    def _add_about_dependencies(self, layout: QVBoxLayout) -> None:
        details = layout.itemAt(layout.count() - 1).layout()
        details.addRow(QLabel(tr("about.dependencies", dependencies="; ".join(self._dependency_rows()))))
        details.addRow(QLabel(tr("about.license")))
        details.addRow(QLabel(tr("about.theme_licenses")))

    def _dependency_rows(self) -> list[str]:
        packages = {
            "openpyxl": ("openpyxl", "MIT"), "ruamel.yaml": ("ruamel.yaml", "MIT"),
            "pydantic": ("pydantic", "MIT"), "Jinja2": ("jinja2", "BSD-3-Clause"),
            "PySide6": ("PySide6", "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only"),
            "PyQtDarkTheme-fork": ("qdarktheme", "MIT"),
        }
        return [row for package, (module, license_name) in packages.items()
                if (row := self._dependency_row(package, module, license_name)) is not None]

    @staticmethod
    def _dependency_row(package: str, module_name: str, fallback: str) -> str | None:
        try:
            try:
                package_version = version(package)
                package_metadata = metadata(package)
                license_name = (package_metadata.get("License-Expression") or package_metadata.get("License") or fallback).splitlines()[0][:100]
            except PackageNotFoundError:
                package_module = import_module(module_name)
                package_version = getattr(package_module, "__version__", getattr(package_module, "VERSION", "included"))
                license_name = fallback
            return f"{package} {package_version} ({license_name})"
        except (PackageNotFoundError, ImportError):
            return None

    def _add_about_buttons(self, dialog: QDialog, layout: QVBoxLayout) -> None:
        actions = QDialogButtonBox()
        open_logs = actions.addButton(tr("about.open_logs"), QDialogButtonBox.ButtonRole.ActionRole)
        report = actions.addButton(tr("about.create_report"), QDialogButtonBox.ButtonRole.ActionRole)
        close_button = actions.addButton(QDialogButtonBox.StandardButton.Close)
        close_button.setText(tr("ui.close"))
        open_logs.clicked.connect(lambda: self._open_logs(dialog))
        report.clicked.connect(lambda: self._create_error_report(dialog))
        actions.rejected.connect(dialog.reject)
        layout.addWidget(actions)

    @staticmethod
    def _open_logs(dialog: QDialog) -> None:
        folder = technical_log.log_folder()
        if folder is None or not folder.exists() or not PlatformServices.open_path(folder):
            QMessageBox.information(dialog, tr("about.title"), tr("about.open_logs_failed"))

    def _create_error_report(self, parent=None) -> None:
        parent = parent or self
        filename, _ = QFileDialog.getSaveFileName(
            parent, tr("about.report_title"), "componentpress-error.zip", tr("about.report_filter")
        )
        if not filename:
            return
        target = Path(filename)
        try:
            summary = self._generate_error_report(target)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            QMessageBox.warning(parent, tr("about.report_title"), tr("about.report_failed", error=type(exc).__name__))
            return
        self._show_report_saved(parent, target, summary)

    def _generate_error_report(self, target: Path):
        result = self._last_build_result
        diagnostic = next(iter(result.diagnostics), None) if result is not None else None
        technical_log.create_report(
            target,
            incident_id=getattr(diagnostic, "incident_id", None),
            code=getattr(diagnostic, "code", None),
            job_id=result.job_id if result is not None else None,
            phase=result.status if result is not None else None,
            status=result.status if result is not None else None,
            mode=result.mode if result is not None else None,
        )
        return technical_log.report_summary(target)

    def _show_report_saved(self, parent, target: Path, summary) -> None:
        current = self._session_report_status(summary)
        crash = self._crash_report_status(summary)
        message = tr("about.report_saved", path=target.name) + "\n" + current + "\n" + crash
        box = QMessageBox(QMessageBox.Icon.Information, tr("about.report_title"), message,
                          QMessageBox.StandardButton.Close, parent)
        button = box.addButton(tr("about.report_open_folder"), QMessageBox.ButtonRole.ActionRole)
        box.exec()
        if box.clickedButton() is button:
            PlatformServices.open_path(target.parent)

    @staticmethod
    def _session_report_status(summary) -> str:
        return tr("about.report_current_status", status=tr(f"about.report.status.{summary.current_status}"),
                  session_id=summary.current_session_id,
                  started_at=summary.current_started_at or tr("about.report.time_unavailable"))

    @staticmethod
    def _crash_report_status(summary) -> str:
        return tr("about.report_crash_status", status=tr(f"about.report.status.{summary.crash_status}"),
                  session_id=summary.crash_session_id or tr("about.report.id_unavailable"),
                  started_at=summary.crash_started_at or tr("about.report.time_unavailable"))
