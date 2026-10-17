"""Launch one crash probe under CDB; keep its dump, trace and copied fixture together."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import importlib.metadata
import hashlib
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / "build" / "crash-diagnostics" / f"{stamp}-{uuid4().hex[:8]}"
    folder.mkdir(parents=True)
    cdb = os.environ["COMPONENTPRESS_CDB"]
    dump = folder / "crash.dmp"
    commands = folder / "debugger.txt"
    commands.write_text(f'sxi ibp\nsxe -c ".dump /ma {dump.as_posix()}; .exr -1; r; ~* kb; q" av\ng\n', encoding="ascii")
    # Windows venv python.exe is a launcher; -o follows the actual interpreter child.
    arguments = [cdb, "-o", "-G", "-logo", str(folder / "debugger.log"), "-cf", str(commands),
                 sys.executable, "-B", "-X", "faulthandler", str(Path(__file__).with_name("render_crash_probe.py")), str(folder)]
    environment = dict(os.environ, QT_SCALE_FACTOR="0.8", PYTHONDONTWRITEBYTECODE="1",
                       COMPONENTPRESS_LOG_DIR=str(folder / "application-logs"),
                       COMPONENTPRESS_TEST_SETTINGS_ROOT=str(folder / "settings"))
    configuration = {key: value for key, value in environment.items() if key.startswith("COMPONENTPRESS_PROBE_")}
    versions = {name: importlib.metadata.version(name) for name in ("PySide6", "shiboken6", "pytest", "pytest-qt")}
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    sources = ("src/componentpress/rendering/painter.py", "tests/support/render_crash_probe.py",
               "tests/support/launch_render_probe.py", "tests/test_stage9_modes.py", "requirements/windows-py314.lock")
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources}
    diff = subprocess.run(["git", "diff", "--no-ext-diff", "--", *sources], cwd=ROOT, capture_output=True, check=True)
    (folder / "source.diff").write_bytes(diff.stdout)
    (folder / "run.json").write_text(json.dumps({"command": arguments, "probe": configuration, "versions": versions,
                                                  "source_hashes": hashes, "revision": revision, "python": sys.version}, indent=2), encoding="utf-8")
    print(folder, flush=True)
    started = time.monotonic()
    with (folder / "console.log").open("wb") as output:
        process = subprocess.Popen(arguments, cwd=ROOT, env=environment, stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            code = process.wait(timeout=int(os.environ.get("COMPONENTPRESS_PROBE_TIMEOUT", "90")))
            status = "native_crash" if dump.exists() else "incomplete"
            receipt = folder / "probe-result.json"
            if not dump.exists() and receipt.exists():
                status = "passed" if json.loads(receipt.read_text())["exit_code"] == 0 else "failed"
        except subprocess.TimeoutExpired:
            # Kill only this debugger and its own child tree, never an unrelated python.exe.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=10)
            process.wait(timeout=10)
            code, status = process.returncode, "timeout"
    paths_file = folder / "test-paths.json"
    if paths_file.exists() and not (folder / "test-settings").exists():
        actual = Path(json.loads(paths_file.read_text(encoding="utf-8"))["settings_root"])
        if actual.is_dir():
            shutil.copytree(actual, folder / "test-settings")
    result = {"status": status, "debugger_exit": code, "elapsed_seconds": round(time.monotonic() - started, 3),
              "dump_bytes": dump.stat().st_size if dump.exists() else 0}
    (folder / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)
    return int(status != "passed" or code != 0)


if __name__ == "__main__":
    raise SystemExit(main())
