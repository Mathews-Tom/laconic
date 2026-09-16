"""Produce a local spend-composition report from two read-only sources."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from laconic.runtime.storage import resolve_data_dir
from laconic.spend.claude_code import DEFAULT_SESSION_DIR as DEFAULT_CLAUDE_SESSION_DIR
from laconic.spend.claude_code import load_sessions as load_claude_code_sessions
from laconic.spend.html import render_html
from laconic.spend.join import Composition, join
from laconic.spend.ledger import scan_store
from laconic.spend.omp import load_sessions
from laconic.spend.privacy import validate_report_json
from laconic.spend.report import DEFAULT_OUTPUT_DIR, SpendReport, build_report, render_markdown

#: Where OMP keeps the default profile's sessions.
DEFAULT_SESSION_DIR = Path.home() / ".omp" / "agent" / "sessions"

#: Serialized artifact names, written side by side.
REPORT_JSON = "spend-composition.json"
REPORT_MARKDOWN = "spend-composition.md"
REPORT_HTML = "spend-composition.html"


@dataclass(frozen=True, slots=True)
class WrittenReport:
    """Where a generated report landed."""

    json_path: Path
    markdown_path: Path
    html_path: Path
    report: SpendReport


def measure(
    session_dirs: list[Path] | None = None,
    data_dir: Path | None = None,
    claude_session_dirs: list[Path] | None = None,
) -> Composition:
    """Read every source and join them. No source is written to.

    Both hosts the codec runs on are scanned. Reading only OMP would make
    the report describe a fraction of the codec's own coverage while
    presenting itself as the whole picture.
    """
    sessions = load_sessions(session_dirs or [DEFAULT_SESSION_DIR])
    sessions += load_claude_code_sessions(claude_session_dirs or [DEFAULT_CLAUDE_SESSION_DIR])
    scan = scan_store(data_dir)
    return join(sessions, scan.sessions, damaged_ledgers=scan.damaged_ledgers)


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        resolved_root = root.resolve()
    except OSError:
        return False
    return candidate == resolved_root or resolved_root in candidate.parents


def _check_destination(destination: Path, session_dirs: list[Path], data_dir: Path | None) -> None:
    """Refuse to write inside either source tree.

    Both sources are declared read-only, and the default output directory is
    relative to the working directory -- so running this from inside the
    runtime store, or passing ``--output`` at it, would create directories
    and files in a tree this command promises not to touch.
    """
    resolved = destination.resolve()
    protected: list[Path] = [resolve_data_dir(data_dir), *(session_dirs or [DEFAULT_SESSION_DIR])]
    for root in protected:
        if _is_within(resolved, root):
            raise OSError(f"refusing to write a report inside a read-only source tree: {root}")


def _atomic_write_bundle(directory: Path, bundle: dict[str, str]) -> None:
    destinations = {name: directory / name for name in bundle}
    for path in destinations.values():
        if path.is_symlink():
            raise OSError(f"refusing to write through a symlink: {path}")
    temporary_paths: dict[str, Path] = {}
    try:
        for name, content in bundle.items():
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{name}.",
                suffix=".tmp",
                dir=directory,
            )
            temporary_path = Path(temporary)
            temporary_paths[name] = temporary_path
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        for name, destination in destinations.items():
            try:
                os.replace(temporary_paths[name], destination)
            except OSError as error:
                raise OSError(f"failed to replace {destination}: {error}") from error
    finally:
        for path in temporary_paths.values():
            path.unlink(missing_ok=True)


def write_report(
    composition: Composition,
    output_dir: Path | None = None,
    *,
    session_dirs: list[Path] | None = None,
    data_dir: Path | None = None,
) -> WrittenReport:
    """Validate and write the JSON, Markdown, and HTML evidence bundle.

    The privacy allowlist runs against the exact canonical JSON before the
    destination exists. Every derived artifact renders in memory first; only
    then are all three destinations replaced atomically one by one.
    """
    report = build_report(composition)
    json_text = report.to_json()
    validate_report_json(json.loads(json_text))
    markdown_text = render_markdown(report)
    html_text = render_html(report)

    destination = DEFAULT_OUTPUT_DIR if output_dir is None else output_dir
    _check_destination(destination, session_dirs or [], data_dir)
    destination.mkdir(parents=True, exist_ok=True)
    bundle = {
        REPORT_JSON: json_text,
        REPORT_MARKDOWN: markdown_text,
        REPORT_HTML: html_text,
    }
    _atomic_write_bundle(destination, bundle)
    return WrittenReport(
        json_path=destination / REPORT_JSON,
        markdown_path=destination / REPORT_MARKDOWN,
        html_path=destination / REPORT_HTML,
        report=report,
    )
