"""File operations used by application preview validation."""

import json
from pathlib import Path

from componentpress.application.contracts import FileFingerprint
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.project_io.files import atomic_write
from componentpress.project_io.paths import check_exact_case, resolve_project_path


class PreviewValidationStore:
    def canonical_root(self, root: Path) -> Path:
        return root.resolve()

    def source_path(self, root: Path, relative_path: str) -> Path:
        path = resolve_project_path(root, relative_path)
        check_exact_case(root, relative_path)
        return path

    @staticmethod
    def fingerprint(path: Path) -> FileFingerprint:
        try:
            metadata = path.stat()
        except FileNotFoundError as exc:
            raise ProjectError(Diagnostic("DATA_SOURCE_MISSING", "книга XLSX не найдена", path)) from exc
        except OSError as exc:
            raise ProjectError(Diagnostic("FILE_READ", str(exc), path)) from exc
        return FileFingerprint(metadata.st_size, metadata.st_mtime_ns)

    @staticmethod
    def store_validation(
        root: Path, source_path: str, fingerprint: FileFingerprint,
        component_ids: list[str],
    ) -> Diagnostic | None:
        path = root / ".componentpress" / "validation.json"
        folder = path.parent
        payload = {
            "format": 1,
            "source": {"path": source_path, "size": fingerprint.size, "mtime_ns": fingerprint.mtime_ns},
            "validated_components": sorted(component_ids),
        }
        try:
            if folder.is_symlink() or path.is_symlink():
                raise ProjectError(Diagnostic("PATH_SYMLINK", "символические ссылки для состояния проверки не поддерживаются", folder))
            atomic_write(path, json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        except ProjectError as exc:
            return exc.diagnostic
        return None
