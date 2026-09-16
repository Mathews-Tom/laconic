"""Single-run execution boundary for the frozen MCP opportunity census."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from laconic.mcp_census.manifest import CensusManifest, ManifestError, freeze_manifest
from laconic.mcp_census.report import OpportunityPrivacyError, OpportunityReport, build_report

DEFAULT_OMP_ROOT: Final = Path.home() / ".omp" / "agent" / "sessions"
DEFAULT_CLAUDE_ROOT: Final = Path.home() / ".claude" / "projects"
DEFAULT_OUTPUT_DIR: Final = Path(".laconic/research/mcp-census")
MANIFEST_JSON: Final = "manifest.json"
REPORT_JSON: Final = "opportunity-report.json"
DISPOSITION_JSON: Final = "mcp-disposition.json"


class CensusExecutionError(RuntimeError):
    """The census cannot execute without weakening its freeze or write boundary."""


@dataclass(frozen=True, slots=True)
class CensusArtifacts:
    manifest_path: Path
    report_path: Path
    disposition_path: Path
    manifest: CensusManifest
    report: OpportunityReport


def _within(candidate: Path, root: Path) -> bool:
    resolved = candidate.expanduser().resolve(strict=False)
    source = root.expanduser().resolve(strict=True)
    return resolved == source or source in resolved.parents


def _check_output(output: Path, roots: dict[str, Path]) -> Path:
    destination = output.expanduser().resolve(strict=False)
    if any(_within(destination, root) for root in roots.values()):
        raise CensusExecutionError("refusing to write census artifacts inside a source root")
    if destination.exists() or destination.is_symlink():
        raise CensusExecutionError(f"census output already exists: {destination}")
    return destination


def _render_bundle(report: OpportunityReport) -> dict[str, str]:
    return {
        REPORT_JSON: report.to_json(),
        DISPOSITION_JSON: report.disposition_json(),
    }


def _write_bundle(directory: Path, bundle: dict[str, str]) -> None:
    temporary = Path(tempfile.mkdtemp(prefix=f".{directory.name}.", dir=directory.parent))
    try:
        for name, text in bundle.items():
            path = temporary / name
            path.write_text(text, encoding="utf-8")
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        for name in bundle:
            os.replace(temporary / name, directory / name)
    finally:
        for path in temporary.iterdir() if temporary.exists() else ():
            path.unlink(missing_ok=True)
        temporary.rmdir()


def execute_census(
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    *,
    omp_root: Path = DEFAULT_OMP_ROOT,
    claude_root: Path = DEFAULT_CLAUDE_ROOT,
) -> CensusArtifacts:
    """Freeze and execute exactly once without a provider or source write."""
    roots = {"omp": omp_root, "claude_code": claude_root}
    destination: Path | None = None
    try:
        destination = _check_output(output_dir, roots)
        manifest_path = destination / MANIFEST_JSON
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir()
        manifest = freeze_manifest(manifest_path, source_roots=roots)
        report = build_report(manifest)
        bundle = _render_bundle(report)
        _write_bundle(destination, bundle)
    except CensusExecutionError:
        raise
    except (ManifestError, OpportunityPrivacyError, OSError, ValueError) as error:
        # Keep a frozen manifest on source/report failure, but no partial derived bundle.
        if destination is not None and destination.is_dir():
            for name in (REPORT_JSON, DISPOSITION_JSON):
                (destination / name).unlink(missing_ok=True)
        raise CensusExecutionError(str(error)) from error
    return CensusArtifacts(
        manifest_path=manifest_path,
        report_path=destination / REPORT_JSON,
        disposition_path=destination / DISPOSITION_JSON,
        manifest=manifest,
        report=report,
    )


def public_json(artifacts: CensusArtifacts) -> str:
    """Return canonical public output for the CLI JSON mode."""
    return (
        json.dumps(
            artifacts.report.disposition,
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    )
