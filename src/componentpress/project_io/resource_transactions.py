"""All-or-nothing publication of a prepared set of project images."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from componentpress.domain.diagnostics import Diagnostic, ProjectError
from .files import atomic_create, atomic_write, file_hash
from .paths import resolve_project_path


def _journal_directory(root: Path) -> Path:
    return root / ".componentpress" / "transactions" / "resource-imports"


def _rollback(root: Path, record: dict) -> None:
    staging = resolve_project_path(root, record["staging"])
    for index, item in enumerate(record["files"]):
        target = resolve_project_path(root, item["target"])
        staged = resolve_project_path(root, f'{record["staging"]}/{item["name"]}')
        if target.exists():
            owned = index in record.get("published", []) or record.get("moving") == index
            if staged.exists():
                try:
                    owned = target.samefile(staged)
                except OSError:
                    owned = False
            if owned:
                if file_hash(target) != item["sha256"]:
                    raise ProjectError(Diagnostic("RESOURCE_TRANSACTION_CONFLICT", "импортированный файл изменён; восстановление остановлено", target))
                target.unlink()
        staged.unlink(missing_ok=True)
    for directory in sorted(staging.parents, key=lambda item: len(item.parts), reverse=True):
        if directory == root / ".componentpress" / "resource-imports":
            break
        try:
            directory.rmdir()
        except OSError:
            pass


def recover_resource_imports(root: Path) -> None:
    directory = _journal_directory(root)
    if not directory.exists():
        return
    for journal in sorted(directory.glob("*.json")):
        try:
            record = json.loads(journal.read_text(encoding="utf-8"))
            if not isinstance(record.get("files"), list) or not isinstance(record.get("staging"), str):
                raise ValueError("invalid transaction record")
            _rollback(root, record)
            journal.unlink()
        except ProjectError:
            raise
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ProjectError(Diagnostic("RESOURCE_TRANSACTION_CONFLICT", f"не удалось восстановить импорт изображений: {exc}", journal)) from exc


def publish_resources(root: Path, entries, *, verify, cancellation=None, on_progress=None) -> None:
    root = root.resolve()
    transaction_id = uuid4().hex
    staging_relative = f".componentpress/resource-imports/{transaction_id}"
    staging = resolve_project_path(root, staging_relative)
    journal_directory = _journal_directory(root)
    journal = journal_directory / f"{transaction_id}.json"
    files = [
        {"target": target, "name": f"{index:08d}.image", "sha256": digest, "size": size}
        for index, (_source, target, digest, size) in enumerate(entries)
    ]
    record = {"staging": staging_relative, "files": files, "published": [], "moving": None}
    journal_directory.mkdir(parents=True, exist_ok=True)
    atomic_create(journal, (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    try:
        staging.mkdir(parents=True, exist_ok=False)
        for index, (source, _target, digest, size) in enumerate(entries):
            if cancellation is not None:
                cancellation.check()
            data = source.read_bytes()
            if len(data) != size or __import__("hashlib").sha256(data).hexdigest() != digest:
                raise ProjectError(Diagnostic("FILE_CHANGED_EXTERNALLY", "исходное изображение изменилось во время импорта", source))
            atomic_create(staging / files[index]["name"], data)
            if on_progress is not None:
                on_progress(index + 1, len(entries))
        verify()
        for index, (_source, target, _digest, _size) in enumerate(entries):
            if cancellation is not None:
                cancellation.check()
            target_path = resolve_project_path(root, target)
            staged_path = staging / files[index]["name"]
            target_path.parent.mkdir(parents=True, exist_ok=True)
            from .files import atomic_move

            record["moving"] = index
            atomic_write(journal, (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            atomic_move(staged_path, target_path)
            record["published"].append(index)
            record["moving"] = None
            atomic_write(journal, (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            if on_progress is not None:
                on_progress(index + 1, len(entries))
        journal.unlink()
    except Exception:
        _rollback(root, record)
        journal.unlink(missing_ok=True)
        raise
    finally:
        try:
            staging.rmdir()
        except OSError:
            pass
