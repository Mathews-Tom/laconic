#!/usr/bin/env python3
# ruff: noqa: E501
"""Generate the public dashboard demo from its committed synthetic fixture."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any, Final

from laconic.spend.html import render_html
from laconic.spend.join import join
from laconic.spend.ledger import SessionDecisions
from laconic.spend.omp import SessionUsage, TurnUsage
from laconic.spend.report import SpendReport, build_report, render_markdown

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
FIXTURE_ROOT: Final = PROJECT_ROOT / "docs" / "demo" / "fixtures"
DEFAULT_FIXTURE: Final = FIXTURE_ROOT / "evidence-ledger.json"
DEFAULT_OUTPUT: Final = PROJECT_ROOT / "docs" / "demo"
ARTIFACT_NAMES: Final = (
    "spend-composition.json",
    "spend-composition.md",
    "spend-composition.html",
    "dashboard-preview.svg",
)

_FIXTURE_KEYS: Final = frozenset({"fixture_kind", "fixture_version", "session", "decisions"})
_SESSION_KEYS: Final = frozenset(
    {
        "session_id",
        "nested",
        "model",
        "provider",
        "input_tokens",
        "prompt_cache_read_tokens",
        "prompt_cache_write_tokens",
        "output_tokens",
        "host_cost_usd",
    }
)
_DECISION_KEYS: Final = frozenset(
    {
        "eligible",
        "emitted",
        "raw_chars",
        "visible_chars",
        "full_expansions",
        "span_expansions",
    }
)


def _exact_object(name: str, value: Any, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{name} must contain exactly {sorted(keys)}")
    return value


def _non_negative_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _non_negative_number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise ValueError(f"{name} must be a non-negative number")
    return float(value)


def _text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def fixture_path(path: Path) -> Path:
    """Resolve a fixture path and refuse every source outside the fixture tree."""
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(FIXTURE_ROOT.resolve(strict=True)):
        raise ValueError(f"fixture source must be under {FIXTURE_ROOT}")
    return resolved


def report_from_fixture(path: Path) -> SpendReport:
    """Build one canonical spend report from an exact synthetic input schema."""
    source = fixture_path(path)
    payload = _exact_object("fixture", json.loads(source.read_text()), _FIXTURE_KEYS)
    if payload["fixture_kind"] != "laconic_dashboard_synthetic_fixture":
        raise ValueError("fixture_kind must identify the synthetic dashboard fixture")
    if payload["fixture_version"] != 1:
        raise ValueError("unsupported dashboard fixture version")

    session = _exact_object("session", payload["session"], _SESSION_KEYS)
    decisions = _exact_object("decisions", payload["decisions"], _DECISION_KEYS)
    if not isinstance(session["nested"], bool):
        raise ValueError("session.nested must be a bool")

    session_id = _text("session.session_id", session["session_id"])
    usage = SessionUsage(
        session_id=session_id,
        turns=(
            TurnUsage(
                model=_text("session.model", session["model"]),
                provider=_text("session.provider", session["provider"]),
                input_tokens=_non_negative_int("session.input_tokens", session["input_tokens"]),
                cache_read=_non_negative_int(
                    "session.prompt_cache_read_tokens",
                    session["prompt_cache_read_tokens"],
                ),
                cache_write=_non_negative_int(
                    "session.prompt_cache_write_tokens",
                    session["prompt_cache_write_tokens"],
                ),
                output_tokens=_non_negative_int("session.output_tokens", session["output_tokens"]),
                host_cost_usd=_non_negative_number(
                    "session.host_cost_usd", session["host_cost_usd"]
                ),
            ),
        ),
        turns_without_usage=0,
        malformed_lines=0,
        unknown_usage_keys=frozenset(),
        nested=session["nested"],
    )
    decision = SessionDecisions(
        session_id=session_id,
        eligible=_non_negative_int("decisions.eligible", decisions["eligible"]),
        emitted=_non_negative_int("decisions.emitted", decisions["emitted"]),
        raw_chars=_non_negative_int("decisions.raw_chars", decisions["raw_chars"]),
        visible_chars=_non_negative_int("decisions.visible_chars", decisions["visible_chars"]),
        full_expansions=_non_negative_int(
            "decisions.full_expansions", decisions["full_expansions"]
        ),
        span_expansions=_non_negative_int(
            "decisions.span_expansions", decisions["span_expansions"]
        ),
    )
    if decision.emitted > decision.eligible:
        raise ValueError("decisions.emitted must not exceed decisions.eligible")
    if decision.visible_chars > decision.raw_chars:
        raise ValueError("decisions.visible_chars must not exceed decisions.raw_chars")
    return build_report(join([usage], [decision]))


def _preview_svg(report: SpendReport) -> str:
    payload = report.payload
    codec = payload["codec"]
    estimate = payload["estimate"]
    assert isinstance(codec, dict)
    assert isinstance(estimate, dict)
    raw_chars = int(codec["raw_chars"])
    chars_avoided = int(codec["chars_avoided"])
    reduction = 100 * chars_avoided / raw_chars if raw_chars else 0.0
    eligible = int(codec["eligible"])
    emission = 100 * int(codec["emitted"]) / eligible if eligible else 0.0
    low = float(estimate["avoided_cost_usd_low"])
    high = float(estimate["avoided_cost_usd_high"])
    digest = report.sha256

    def e(value: object) -> str:
        return html.escape(str(value), quote=True)

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630" role="img" aria-labelledby="title desc" data-basis="modelled_not_measured" data-json-sha256="{e(digest)}">
  <title id="title">Laconic evidence ledger synthetic fixture</title>
  <desc id="desc">A deterministic preview of the offline evidence dashboard using synthetic values only.</desc>
  <rect width="1200" height="630" fill="#f2eadc"/>
  <rect x="38" y="34" width="1124" height="562" rx="8" fill="#fbf7ef" stroke="#27231f" stroke-width="2"/>
  <rect x="76" y="57" width="505" height="36" rx="3" fill="#a33a2b"/>
  <text x="91" y="82" fill="#fffdf8" font-family="ui-monospace, monospace" font-size="17" font-weight="700" letter-spacing="2">SYNTHETIC FIXTURE · OFFLINE EVIDENCE</text>
  <text x="76" y="139" fill="#211e1a" font-family="Georgia, serif" font-size="49" font-weight="700">Laconic evidence ledger</text>
  <text x="76" y="178" fill="#5d554c" font-family="ui-monospace, monospace" font-size="16">schema 3 · exact-key validated · JSON sha256 {e(digest[:16])}…</text>
  <rect x="76" y="216" width="1048" height="126" rx="4" fill="#16191d"/>
  <path d="M104 307 C230 260 322 316 432 267 S650 291 750 247 S952 269 1096 232" fill="none" stroke="#e55b45" stroke-width="4"/>
  <path d="M104 319 C230 296 322 327 432 300 S650 315 750 282 S952 302 1096 271" fill="none" stroke="#d9c8aa" stroke-width="2" stroke-dasharray="8 8"/>
  <text x="104" y="248" fill="#f0e4d2" font-family="ui-monospace, monospace" font-size="15">SYNTHETIC SCHEMATIC · NOT A MEASURED CAUSAL CURVE</text>
  <g transform="translate(76 378)">
    <rect width="318" height="154" rx="4" fill="#fffdf8" stroke="#c9bda9"/>
    <text x="24" y="35" fill="#6a6157" font-family="ui-monospace, monospace" font-size="14">EMISSION</text>
    <text x="24" y="86" fill="#211e1a" font-family="Georgia, serif" font-size="42" font-weight="700">{emission:.1f}%</text>
    <text x="24" y="122" fill="#6a6157" font-family="ui-monospace, monospace" font-size="14">{e(codec["emitted"])} of {e(codec["eligible"])} synthetic observations</text>
  </g>
  <g transform="translate(416 378)">
    <rect width="318" height="154" rx="4" fill="#fffdf8" stroke="#c9bda9"/>
    <text x="24" y="35" fill="#6a6157" font-family="ui-monospace, monospace" font-size="14">CHARACTER REDUCTION</text>
    <text x="24" y="86" fill="#211e1a" font-family="Georgia, serif" font-size="42" font-weight="700">{reduction:.1f}%</text>
    <text x="24" y="122" fill="#6a6157" font-family="ui-monospace, monospace" font-size="14">{chars_avoided:,} synthetic characters excluded</text>
  </g>
  <g transform="translate(756 378)">
    <rect width="368" height="154" rx="4" fill="#fffdf8" stroke="#a33a2b" stroke-width="2"/>
    <text x="24" y="35" fill="#a33a2b" font-family="ui-monospace, monospace" font-size="14">MODELLED · NOT MEASURED</text>
    <text x="24" y="86" fill="#211e1a" font-family="Georgia, serif" font-size="38" font-weight="700">${low:.2f}–${high:.2f}</text>
    <text x="24" y="122" fill="#6a6157" font-family="ui-monospace, monospace" font-size="14">synthetic model · single arm</text>
  </g>
  <text x="76" y="568" fill="#6a6157" font-family="ui-monospace, monospace" font-size="14">Generated from docs/demo/fixtures/evidence-ledger.json · values are synthetic</text>
</svg>
'''


def artifact_bytes(path: Path = DEFAULT_FIXTURE) -> dict[str, bytes]:
    """Return every public artifact as deterministic bytes without writing."""
    report = report_from_fixture(path)
    markdown = (
        "> **Synthetic fixture.** These values demonstrate the offline dashboard and are not "
        "production evidence. Basis: `modelled_not_measured`.\n\n" + render_markdown(report)
    )
    return {
        "spend-composition.json": report.to_json().encode(),
        "spend-composition.md": markdown.encode(),
        "spend-composition.html": render_html(report, synthetic=True).encode(),
        "dashboard-preview.svg": _preview_svg(report).encode(),
    }


def generate(path: Path = DEFAULT_FIXTURE, output: Path = DEFAULT_OUTPUT) -> None:
    """Write the complete generated demo bundle."""
    artifacts = artifact_bytes(path)
    output.mkdir(parents=True, exist_ok=True)
    for name in ARTIFACT_NAMES:
        (output / name).write_bytes(artifacts[name])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    generate(args.fixture, args.output)
    for name in ARTIFACT_NAMES:
        print(args.output / name)


if __name__ == "__main__":
    main()
