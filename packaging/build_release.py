"""Build and publish the immutable one-file release for the current stage."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile


PACKAGING_DIRECTORY = Path(__file__).resolve().parent
SETTINGS = runpy.run_path(str(PACKAGING_DIRECTORY / "common.py"))
ROOT: Path = SETTINGS["ROOT"]
EXECUTABLE_NAME: str = SETTINGS["EXECUTABLE_NAME"]
RELEASE_EXECUTABLE: Path = SETTINGS["RELEASE_EXECUTABLE"]
RELEASE_VERSION: str = SETTINGS["RELEASE_VERSION"]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    if RELEASE_EXECUTABLE.exists():
        print(f"Выпуск уже существует и не будет перезаписан: {RELEASE_EXECUTABLE}", file=sys.stderr)
        return 2
    staging_directory = ROOT / "dist" / f"release-{RELEASE_VERSION}"
    work_directory = ROOT / "build" / f"pyinstaller-{RELEASE_VERSION}"
    build_environment = os.environ.copy()
    windows_root = Path(build_environment.get("SystemRoot", r"C:\Windows"))
    build_environment["PATH"] = os.pathsep.join(
        str(path)
        for path in (
            Path(sys.executable).parent,
            Path(sys.base_prefix),
            Path(sys.base_prefix) / "DLLs",
            windows_root / "System32",
            windows_root,
        )
    )
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(staging_directory),
        "--workpath",
        str(work_directory),
        str(PACKAGING_DIRECTORY / "windows.spec"),
    ]
    completed = subprocess.run(command, cwd=ROOT, env=build_environment, check=False)
    if completed.returncode != 0:
        return completed.returncode
    staged = staging_directory / f"{EXECUTABLE_NAME}.exe"
    if not staged.is_file():
        print(f"PyInstaller не создал ожидаемый файл: {staged}", file=sys.stderr)
        return 3
    with tempfile.TemporaryDirectory(prefix="componentpress-release-smoke-") as temporary:
        temporary_root = Path(temporary)
        smoke_project = temporary_root / "project"
        shutil.copytree(
            ROOT / "examples" / "demo-excel-game",
            smoke_project,
            ignore=shutil.ignore_patterns("output", "archive", ".componentpress"),
        )
        environment = os.environ.copy()
        environment.pop("PYTHONHOME", None)
        environment.pop("PYTHONPATH", None)
        try:
            smoke = subprocess.run(
                [str(staged), "--release-smoke", str(smoke_project)],
                cwd=temporary_root,
                env=environment,
                check=False,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            print("Smoke-проверка EXE превысила 120 секунд", file=sys.stderr)
            return 4
        if smoke.returncode != 0:
            diagnostics = smoke_project / ".componentpress" / "release-smoke-error.txt"
            if diagnostics.exists():
                print(diagnostics.read_text(encoding="utf-8"), file=sys.stderr)
            print(f"Smoke-проверка EXE завершилась с кодом {smoke.returncode}", file=sys.stderr)
            return 5
        saved_component = smoke_project / "components" / "release-smoke.yaml"
        if not saved_component.is_file():
            print("Smoke-проверка не создала второй компонент", file=sys.stderr)
            return 6
        saved_text = saved_component.read_text(encoding="utf-8")
        if (
            "width: 70" not in saved_text
            or "height: 90" not in saved_text
            or "id: smoke-group" not in saved_text
            or "id: smoke-image" not in saved_text
            or "id: smoke-title" not in saved_text
            or "# Проверка текстового режима" not in saved_text
        ):
            print("Smoke-проверка не сохранила синхронизированный макет", file=sys.stderr)
            return 7
        row1 = smoke_project / ".componentpress" / "release-smoke-row1.png"
        row2 = smoke_project / ".componentpress" / "release-smoke-row2.png"
        if not row1.is_file() or not row2.is_file() or row1.read_bytes() == row2.read_bytes():
            print("Smoke-проверка не экспортировала два разных Excel-экземпляра", file=sys.stderr)
            return 8
        outputs = sorted((smoke_project / "output").glob("build-*/build-report.json"))
        if len(outputs) != 2 or not all((item.parent / "print.pdf").is_file() for item in outputs):
            print("Smoke-проверка не опубликовала Prod- и Test-сборки", file=sys.stderr)
            return 9
        reports = [json.loads(item.read_text(encoding="utf-8")) for item in outputs]
        if {item.get("mode") for item in reports} != {"prod", "test"}:
            print("Smoke-проверка не зафиксировала оба режима сборки", file=sys.stderr)
            return 11
        test_zip = smoke_project / ".componentpress" / "release-smoke-test.zip"
        if not test_zip.is_file():
            print("Smoke-проверка не создала Test PNG ZIP", file=sys.stderr)
            return 12
        jobs = smoke_project / ".componentpress" / "jobs"
        if jobs.exists() and any(jobs.iterdir()):
            print("Smoke-проверка оставила временный каталог задания", file=sys.stderr)
            return 10
        print("Smoke: GUI, Excel, редактор, потоковая сборка PDF, отмена и очистка — успешно")
    RELEASE_EXECUTABLE.parent.mkdir(parents=True, exist_ok=True)
    staged.replace(RELEASE_EXECUTABLE)
    print(RELEASE_EXECUTABLE)
    print(f"SHA-256: {sha256(RELEASE_EXECUTABLE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
