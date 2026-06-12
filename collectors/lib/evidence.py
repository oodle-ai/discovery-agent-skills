"""Evidence capture: raw API responses written to disk, redacted, with a manifest.

Layout under the collector's --output-dir:

    evidence/<name>.json    redacted raw API response
    manifest.json           what was written, when, from which endpoint
    summary.json            written separately by lib.summary
"""

from __future__ import annotations

import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .redact import redact


def utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class EvidenceWriter:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = Path(output_dir)
        self.evidence_dir = self.output_dir / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self._manifest: list[dict[str, Any]] = []

    def write(self, name: str, payload: Any, source_api: str | None = None) -> str:
        """Write a redacted evidence file; returns its path relative to output_dir."""
        rel = f"evidence/{name}.json"
        path = self.output_dir / rel
        path.write_text(
            json.dumps(redact(payload), indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        self._manifest.append(
            {
                "name": name,
                "file": rel,
                "source_api": source_api,
                "written_at": utcnow_iso(),
            }
        )
        return rel

    def load(self, name: str) -> Any | None:
        """Load a previously written evidence file (for --report-only reruns)."""
        path = self.evidence_dir / f"{name}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def load_all(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in sorted(self.evidence_dir.glob("*.json")):
            try:
                out[f.stem] = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"WARN: could not load evidence {f}: {exc}")
        return out

    def finalize(self) -> None:
        (self.output_dir / "manifest.json").write_text(
            json.dumps(self._manifest, indent=2) + "\n", encoding="utf-8"
        )

    def tar(self) -> Path:
        out = self.output_dir.with_suffix(".tar.gz")
        with tarfile.open(out, "w:gz") as t:
            t.add(self.output_dir, arcname=self.output_dir.name)
        print(f"compressed: {out}")
        return out
