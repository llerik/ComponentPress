"""Shared constants for staged Windows releases."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE_VERSION = "0.08"
EXECUTABLE_NAME = f"ComponentPress-{RELEASE_VERSION}-win-x64"
RELEASE_DIRECTORY = ROOT / "releases" / RELEASE_VERSION
RELEASE_EXECUTABLE = RELEASE_DIRECTORY / f"{EXECUTABLE_NAME}.exe"
