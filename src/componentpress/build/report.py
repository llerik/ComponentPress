from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Mapping

from componentpress.project_io.files import atomic_write
from .imposition import ImpositionPlan
from .input_snapshot import BuildInputSnapshot


APPLICATION_VERSION = "0.14"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_report(
    snapshot: BuildInputSnapshot,
    plan: ImpositionPlan,
    output_hashes: Mapping[str, str],
    *,
    started_at: str,
) -> Path:
    target = snapshot.staging_directory / "build-report.json"
    payload = {
        "report_version": 2,
        "job_id": snapshot.job_id,
        "project_id": snapshot.project.id,
        "game_version": snapshot.project.version,
        "application_version": APPLICATION_VERSION,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": "succeeded",
        "complete": True,
        "print": snapshot.print_settings.model_dump(mode="json"),
        "mode": snapshot.mode,
        "input_hashes": dict(sorted(snapshot.input_hashes.items())),
        "output_hashes": dict(sorted(output_hashes.items())),
        "complete_files": [*sorted(name for name in output_hashes if name.startswith("images/")), "print.pdf", "build-report.json"],
        "fonts": list(snapshot.font_families),
        "instances": [
            {
                "index": item.index,
                "component_id": item.component_id,
                "instance_id": item.instance_id,
                "row_number": item.row_number,
                "copies": item.copies,
                "png": f"images/{item.png_name}",
            }
            for item in snapshot.instances
        ],
        "counts": {
            "instances": len(snapshot.instances),
            "copies": snapshot.total_copies,
            "png": len(snapshot.instances),
            "pages": len(plan.pages),
        },
        "warnings": [
            {
                "code": item.code,
                "severity": item.severity,
                "message": item.message,
                "path": str(item.path) if item.path else None,
                "field": item.field,
                "node_id": item.node_id,
                "source": item.source,
                "sheet": item.sheet,
                "cell": item.cell,
            }
            for item in snapshot.warnings
        ],
    }
    atomic_write(target, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return target
