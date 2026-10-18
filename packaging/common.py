"""Shared constants for staged Windows releases."""

from pathlib import Path
import runpy


ROOT = Path(__file__).resolve().parents[1]
RELEASE_VERSION = runpy.run_path(str(ROOT / "src" / "componentpress" / "version.py"))["APPLICATION_VERSION"]
SCHEMA_VERSION = runpy.run_path(str(ROOT / "src" / "componentpress" / "domain" / "schema.py"))["SCHEMA_VERSION"]
EXECUTABLE_NAME = f"ComponentPress-{RELEASE_VERSION}-win-x64"
RELEASE_DIRECTORY = ROOT / "releases" / RELEASE_VERSION
RELEASE_EXECUTABLE = RELEASE_DIRECTORY / f"{EXECUTABLE_NAME}.exe"
