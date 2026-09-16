"""Deterministic, offline renderer for the Laconic GitHub Pages site."""
# ruff: noqa: E501

from __future__ import annotations

import html
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from laconic.pages.demo import DemoResult, build_demo
from laconic.pages.evidence import PagesEvidence, PagesEvidenceError

SITE_GENERATOR_VERSION: Final = "m23-site-v1"
SITE_FILES: Final = (
    "assets/diagram.svg",
    "assets/site.css",
    "evidence/index.html",
    "evidence/laconic-development.json",
    "index.html",
    "methodology/index.html",
)
_GITHUB: Final = "https://github.com/Mathews-Tom/laconic"
_INSTALL: Final = f"{_GITHUB}#installation"
_LIMITATION_PROSE: Final = {
    "observed_characters_are_not_tokens_or_dollars": (
        "Character reduction is an observed tool-boundary effect, not a token or dollar count."
    ),
    "single_arm_cohort_has_no_counterfactual": (
        "This self-use cohort has no untreated or disabled-Laconic counterfactual."
    ),
    "modelled_avoided_cost_is_not_measured_savings": (
        "Avoided cost is modelled from assumptions; it is not measured or billed savings."
    ),
    "token_density_and_cache_rereads_are_unmeasured_assumptions": (
        "Token density and cache rereads are explicit model inputs, not observed effects."
    ),
    "host_reported_cost_covers_only_hosts_that_report_one": (
        "Host-reported cost covers only sessions whose host records a cost."
    ),
    "fallback_pricing_withholds_dollars_above_25_percent": (
        "Dollar ranges are withheld when fallback-priced cost exceeds 25 percent."
    ),
    "local_laconic_repository_cohort_is_not_generalizable": (
        "This local Laconic-repository cohort does not represent other users or workloads."
    ),
}


class SiteError(ValueError):
    """Raised before a generated site can replace the prior destination."""


@dataclass(frozen=True, slots=True)
class SiteBuild:
    """Content-free receipt for one deterministic render."""

    json_sha256: str
    files: tuple[str, ...]
    demo_raw_chars: int
    demo_visible_chars: int
    backup_cleaned: bool

    def public_summary(self) -> dict[str, Any]:
        return {
            "json_sha256": self.json_sha256,
            "files": list(self.files),
            "demo_raw_chars": self.demo_raw_chars,
            "demo_visible_chars": self.demo_visible_chars,
            "backup_cleaned": self.backup_cleaned,
            "provider_calls": 0,
        }


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PagesEvidenceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_evidence(path: Path) -> PagesEvidence:
    """Load canonical evidence while rejecting duplicate keys and unknown shape."""

    try:
        text = path.read_text(encoding="utf-8")
        payload: Any = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError, PagesEvidenceError) as error:
        raise SiteError("canonical evidence is unreadable") from error
    try:
        evidence = PagesEvidence(payload)
    except PagesEvidenceError as error:
        raise SiteError("canonical evidence is invalid") from error
    if text != evidence.to_json():
        raise SiteError("canonical evidence bytes are not canonical")
    return evidence


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _integer(value: object) -> str:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SiteError("integer display received a non-integer")
    return f"{value:,}"


def _number(value: object, places: int = 2) -> str:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise SiteError("numeric display received a non-number")
    return f"{float(value):,.{places}f}"


def _ratio(part: int, whole: int) -> float:
    return 0.0 if whole == 0 else 100.0 * part / whole


def _nav(prefix: str, active: str) -> str:
    links = (
        ("Product", f"{prefix}index.html", "product"),
        ("Evidence", f"{prefix}evidence/index.html", "evidence"),
        ("Methodology", f"{prefix}methodology/index.html", "methodology"),
    )
    rendered_parts = []
    for label, href, key in links:
        current = ' aria-current="page"' if key == active else ""
        rendered_parts.append(f'<a href="{href}"{current}>{label}</a>')
    rendered = "".join(rendered_parts)
    return (
        '<nav class="site-nav" aria-label="Primary">'
        f'<a class="wordmark" href="{prefix}index.html" aria-label="Laconic home">'
        'laconic<span aria-hidden="true">.</span></a>'
        f'<div class="nav-links">{rendered}'
        f'<a class="nav-install" href="{_INSTALL}">Install</a></div></nav>'
    )


def _shell(
    *, title: str, description: str, active: str, prefix: str, digest: str, body: str
) -> str:
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="{_e(description)}">
  <meta name="laconic-evidence-sha256" content="{digest}">
  <title>{_e(title)}</title>
  <link rel="stylesheet" href="{prefix}assets/site.css">
</head>
<body data-json-sha256="{digest}">
  <a class="skip-link" href="#main">Skip to content</a>
  <header>{_nav(prefix, active)}</header>
  <main id="main">{body}</main>
  <footer class="site-footer">
    <p><strong>laconic.</strong> Local by default. Fail open. Exactly recoverable.</p>
    <p><a href="{prefix}evidence/index.html">Evidence</a> · <a href="{prefix}evidence/laconic-development.json">Canonical JSON</a> · <a href="{prefix}methodology/index.html">Methodology</a> · <a href="{_GITHUB}">GitHub</a><br>No telemetry · No hosted service</p>
  </footer>
</body>
</html>
'''


def _observed_strip(payload: dict[str, Any], prefix: str) -> str:
    observed = payload["observed"]
    reduction = _ratio(observed["chars_avoided"], observed["raw_chars"])
    expansions = observed["full_expansions"] + observed["span_expansions"]
    return f'''<section class="observed-strip" aria-labelledby="observed-strip-title">
  <div><p class="data-label observed-label">Observed</p><h2 id="observed-strip-title">While developing Laconic.</h2><p>Actual codec counters, separate from economic modelling.</p></div>
  <dl class="strip-stats">
    <div><dt>Fewer visible chars</dt><dd>{_integer(observed["chars_avoided"])}</dd></div>
    <div><dt>Boundary reduction</dt><dd>{_number(reduction)}%</dd></div>
    <div><dt>Exact expansions</dt><dd>{_integer(expansions)}</dd></div>
  </dl>
  <p class="scope-note">Frozen local Laconic-repository cohort only. Not representative of other users, repositories, hosts, or workloads. <a href="{prefix}evidence/index.html">Inspect the evidence</a>.</p>
</section>'''


def _demo_panel(demo: DemoResult) -> str:
    return f"""<section class="demo-section" id="transformation" aria-labelledby="demo-title">
  <div class="section-heading"><div><p class="eyebrow">Deterministic transformation</p><h2 id="demo-title">A real codec run.<br>Every omitted byte recoverable.</h2></div><p>This committed fixture passes through Laconic's file encoder and exact recovery envelope. The complete envelope is strictly smaller than the original before it is shown.</p></div>
  <div class="demo-grid">
    <article class="terminal-card" aria-labelledby="envelope-title"><p class="terminal-title" id="envelope-title">READ · {_integer(demo.raw_chars)} raw characters</p><pre>{_e(demo.envelope)}</pre><p class="terminal-result"><strong>{_integer(demo.visible_chars)} visible characters · {_number(demo.reduction_pct)}% fewer</strong><br>Original retained locally under <code>{_e(demo.reference)}</code>.</p></article>
    <aside class="recovery-card" aria-labelledby="recovery-title"><p class="eyebrow">Exact recovery proof</p><h3 id="recovery-title">Full and ranged.</h3><dl><div><dt>Full handle</dt><dd><code>{_e(demo.reference)}</code></dd></div><div><dt>Range handle</dt><dd><code>{_e(demo.range_reference)}</code></dd></div><div><dt>Recovered SHA-256</dt><dd><code class="digest">{demo.recovery_sha256}</code></dd></div></dl><pre>{_e(demo.range_preview)}</pre></aside>
  </div>
</section>"""


def _pipeline(prefix: str) -> str:
    return f'''<section class="paper-section" aria-labelledby="system-title">
  <div class="section-heading"><div><p class="eyebrow">The system</p><h2 id="system-title">Reduce first.<br>Recover exactly.</h2></div><p>The host adapter sends a completed tool result to one canonical runtime. The runtime accepts a recovery-bearing envelope only when exact expansion succeeds and the complete visible result is strictly smaller.</p></div>
  <img class="system-diagram" src="{prefix}assets/diagram.svg" alt="Five-step Laconic path from host tool result through runtime, encoder, private ledger, and recovery-bearing envelope.">
</section>'''


def _fail_open() -> str:
    cards = (
        (
            "Strictly smaller",
            "Laconic counts the complete recovery-bearing envelope, not an optimistic intermediate encoding.",
        ),
        (
            "Raw retained first",
            "The original remains in the private local ledger until recovery and size checks succeed.",
        ),
        (
            "Original on failure",
            "Unsupported tools, errors, malformed shapes, and non-smaller candidates return the original unchanged.",
        ),
    )
    rendered = "".join(f"<article><h3>{title}</h3><p>{text}</p></article>" for title, text in cards)
    return f"""<section class="dark-section" aria-labelledby="contract-title"><div class="section-heading"><div><p class="eyebrow light">Fail-open contract</p><h2 id="contract-title">Compression is optional.<br>Correctness is not.</h2></div><p>Every decision preserves the host's result unless a complete, exactly recoverable, smaller replacement has already been proven.</p></div><div class="contract-grid">{rendered}</div></section>"""


def _install() -> str:
    return f'''<section class="paper-section install-section" aria-labelledby="install-title"><div><p class="eyebrow">Install entry point</p><h2 id="install-title">Use the maintained instructions.</h2><p>The repository README owns installation and host setup. The site does not fork a second guide that can drift.</p></div><a class="button" href="{_INSTALL}">Open installation instructions</a></section>'''


def _estimate_card(estimate: dict[str, Any]) -> str:
    denominator = estimate["denominator"]
    if estimate["status"] == "available":
        headline = (
            f"${_number(estimate['avoided_cost_usd_low'])}–"
            f"${_number(estimate['avoided_cost_usd_high'])}"
        )
        detail = (
            f"{_number(estimate['avoided_share_pct_low'])}–"
            f"{_number(estimate['avoided_share_pct_high'])}% of the named modelled denominator"
        )
    else:
        headline = "Withheld"
        detail = _e(estimate["reason"].replace("_", " "))
    return f"""<article class="modelled-card"><p class="data-label modelled-label">modelled_not_measured</p><h3>{headline}</h3><p>{detail}</p><dl><div><dt>Modelled denominator</dt><dd>${_number(denominator["modelled_usd"])} across {_integer(denominator["modelled_sessions"])} joined sessions</dd></div><div><dt>Host-reported partial denominator</dt><dd>${_number(denominator["host_reported_usd"])} across {_integer(denominator["host_reported_sessions"])} reporting sessions</dd></div><div><dt>Fallback-priced share</dt><dd>{_number(estimate["fallback_priced_cost_share_pct"], 6)}%</dd></div></dl><p class="adjacent-limit">Single-arm model with no counterfactual. Not measured, billed, token, cache, or behavior savings.</p></article>"""


def _claim_boundary(payload: dict[str, Any], prefix: str) -> str:
    observed = payload["observed"]
    return f'''<section class="claim-section" aria-labelledby="claims-title"><div class="section-heading"><div><p class="eyebrow">Claim boundary</p><h2 id="claims-title">What happened.<br>What is modelled.</h2></div><p>Observed character counters and modelled economic implications never share a label, card, or color. Unsupported token, cache, behavior, and general-benefit claims remain out of scope.</p></div><div class="claim-grid"><article class="observed-card"><p class="data-label observed-label">Observed</p><h3>{_integer(observed["chars_avoided"])}</h3><p>fewer characters visible at the tool-result boundary</p><dl><div><dt>Raw → visible</dt><dd>{_integer(observed["raw_chars"])} → {_integer(observed["visible_chars"])}</dd></div><div><dt>Emitted</dt><dd>{_integer(observed["emitted"])}</dd></div></dl></article>{_estimate_card(payload["estimate"])}</div><p class="claim-link"><a href="{prefix}evidence/index.html">Read the frozen evidence ledger</a> and <a href="{prefix}methodology/index.html">its methodology and limitations</a>.</p></section>'''


def _homepage(evidence: PagesEvidence, demo: DemoResult) -> str:
    payload = evidence.payload
    body = f"""<section class="hero" aria-labelledby="hero-title"><div class="hero-copy"><p class="eyebrow">Local context efficiency for coding agents</p><h1 id="hero-title">Smaller tool results.<br>Exact recovery.</h1><p>Laconic transforms noisy file, command, and search results before they enter an agent's context. The original stays in a private local ledger, available by exact full or ranged expansion.</p><div class="hero-actions"><a class="button" href="#transformation">See the transformation</a><a class="button secondary" href="#system-title">How it works</a></div></div><div class="hero-code" aria-label="Laconic recovery envelope example"><p>Strict-smaller decision</p><strong>{_integer(demo.raw_chars)} → {_integer(demo.visible_chars)} characters</strong><code>{_e(demo.reference)}</code><span>Full or ranged exact expansion</span></div></section>{_demo_panel(demo)}{_observed_strip(payload, "")}{_pipeline("")}{_fail_open()}{_install()}{_claim_boundary(payload, "")}"""
    return _shell(
        title="Laconic — Smaller tool results. Exact recovery.",
        description="Laconic is a local, fail-open, exactly recoverable context codec for coding agents.",
        active="product",
        prefix="",
        digest=evidence.sha256,
        body=body,
    )


def _provenance_table(payload: dict[str, Any], digest: str) -> str:
    rows = (
        ("Snapshot", payload["snapshot_id"]),
        ("Inclusive start", payload["inclusive_start"]),
        ("Exclusive end", payload["exclusive_end"]),
        ("Laconic version", payload["laconic_version"]),
        ("Generator version", payload["generator_version"]),
        ("Generator commit", payload["generator_commit"]),
        ("Manifest SHA-256", payload["manifest_sha256"]),
        ("Source inventory SHA-256", payload["source_inventory_sha256"]),
        ("Canonical public JSON SHA-256", digest),
    )
    body = "".join(
        f'<tr><th scope="row">{_e(label)}</th><td><code class="digest">{_e(value)}</code></td></tr>'
        for label, value in rows
    )
    return f'<div class="table-wrap"><table><caption>Frozen snapshot provenance</caption><tbody>{body}</tbody></table></div>'


def _counter_table(payload: dict[str, Any]) -> str:
    observed = payload["observed"]
    rows = (
        ("Eligible observations", observed["eligible"]),
        ("Emitted transforms", observed["emitted"]),
        ("Pass-through decisions", observed["pass_through"]),
        ("Raw characters", observed["raw_chars"]),
        ("Visible characters", observed["visible_chars"]),
        ("Avoided characters", observed["chars_avoided"]),
        ("Full expansions", observed["full_expansions"]),
        ("Ranged expansions", observed["span_expansions"]),
    )
    body = "".join(
        f'<tr><th scope="row">{label}</th><td>{_integer(value)}</td></tr>' for label, value in rows
    )
    reduction = _ratio(observed["chars_avoided"], observed["raw_chars"])
    emission = _ratio(observed["emitted"], observed["eligible"])
    body += f'<tr><th scope="row">Derived boundary reduction</th><td>{_number(reduction)}%</td></tr><tr><th scope="row">Derived emission rate</th><td>{_number(emission)}%</td></tr>'
    return f'<div class="table-wrap"><table><caption>Observed aggregate runtime counters</caption><tbody>{body}</tbody></table></div>'


def _limitations(payload: dict[str, Any]) -> str:
    items = "".join(
        f"<li><code>{_e(name)}</code><span>{_e(_LIMITATION_PROSE[name])}</span></li>"
        for name in payload["limitations"]
    )
    return f'<ul class="limitations">{items}</ul>'


def _evidence_page(evidence: PagesEvidence) -> str:
    payload = evidence.payload
    population = payload["population"]
    body = f"""<section class="route-hero"><p class="eyebrow">Frozen self-use evidence</p><h1>Observed while developing Laconic.</h1><p>One local, closed, repository-scoped cohort. Aggregate runtime counters only. This route does not publish session, path, host, model, provider, prompt, tool-result, or credential data.</p></section>{_observed_strip(payload, "../")}<section class="paper-section stacked" aria-labelledby="counters-title"><div class="section-heading"><div><p class="data-label observed-label">Observed</p><h2 id="counters-title">Boundary counters.</h2></div><p>Percentages below are derived at render time from the integer counters in the canonical public JSON.</p></div>{_counter_table(payload)}<dl class="population"><div><dt>Selected sessions</dt><dd>{_integer(population["selected_sessions"])}</dd></div><div><dt>Root / nested</dt><dd>{_integer(population["root_sessions"])} / {_integer(population["nested_sessions"])}</dd></div><div><dt>Priced / joined</dt><dd>{_integer(population["priced_sessions"])} / {_integer(population["joined_sessions"])}</dd></div></dl></section><section class="model-section" aria-labelledby="model-title"><div class="section-heading"><div><p class="data-label modelled-label">modelled_not_measured</p><h2 id="model-title">Economic implication.</h2></div><p>A single-arm estimate under explicit token-density and cache-reread assumptions. It is not measured or billed savings.</p></div>{_estimate_card(payload["estimate"])}</section><section class="paper-section stacked" aria-labelledby="provenance-title"><div class="section-heading"><div><p class="eyebrow">Integrity</p><h2 id="provenance-title">Digest-bound provenance.</h2></div><p>Every page carries the canonical public JSON digest. Private source identities remain in the sealed, untracked local manifest.</p></div>{_provenance_table(payload, evidence.sha256)}<p><a href="laconic-development.json">Download the canonical public JSON</a>.</p></section><section class="paper-section stacked" aria-labelledby="limits-title"><div class="section-heading"><div><p class="eyebrow">Required limitations</p><h2 id="limits-title">Claims carry their limits.</h2></div><p>The ordered list below comes from the canonical public snapshot. It is not hidden behind interaction.</p></div>{_limitations(payload)}<p><a href="../methodology/index.html">Read definitions, equations, privacy boundaries, and reproduction commands</a>.</p></section>"""
    return _shell(
        title="Evidence — Laconic",
        description="Frozen aggregate evidence observed while developing Laconic, with modelled economics kept separate.",
        active="evidence",
        prefix="../",
        digest=evidence.sha256,
        body=body,
    )


def _methodology_page(evidence: PagesEvidence) -> str:
    payload = evidence.payload
    body = f"""<section class="route-hero"><p class="eyebrow">Methodology and limitations</p><h1>Definitions before claims.</h1><p>Observed characters, modelled_not_measured economics, privacy controls, and unsupported conclusions remain separate by construction.</p></section><section class="paper-section prose" aria-labelledby="cohort-title"><h2 id="cohort-title">Cohort and freeze protocol</h2><p>The exact cohort is <code>{payload["cohort"]}</code>: closed OMP and Claude Code sessions whose allowlisted metadata resolves canonically inside the Laconic repository and whose timestamp falls in <code>[{payload["inclusive_start"]}, {payload["exclusive_end"]})</code>. Discovery reads at most the first 50 records of each transcript and only the top-level <code>cwd</code> and <code>timestamp</code> keys.</p><p>The owner-local extractor seals transcript size, mtime, content digest, selected runtime-row digest, pricing provenance, and generator provenance before aggregation. It rechecks all selected sources before atomically replacing the public JSON. GitHub Actions never performs extraction.</p><h2>Observed character equations</h2><div class="equations" aria-label="Character counter equations"><code>chars_avoided = raw_chars − visible_chars</code><code>boundary_reduction_pct = 100 × chars_avoided ÷ raw_chars</code><code>emission_rate_pct = 100 × emitted ÷ eligible</code></div><p>These are character counts at the tool-result boundary. They are not token counts, provider billing, cache behavior, agent behavior, or task quality.</p><h2>modelled_not_measured economics</h2><p>The estimate converts observed avoided characters through explicit low/high characters-per-token assumptions, an explicit cache-reread multiplier, and pinned model list prices. The model names host-reported and modelled denominators separately. It withholds dollar figures when fallback-priced cost exceeds 25 percent or required inputs are absent.</p><p>This is a single arm. There is no disabled-Laconic counterfactual. A provider never billed the avoided amount.</p><h2>Privacy boundary</h2><p>The public schema is exact-key and aggregate-only. It excludes session identifiers and hashes, filesystem and repository paths, host folders, model and provider names, tool names, prompts, arguments, content, server names, credentials, and free-form notes. The closed cohort constant is the sole repository-identifying value.</p><h2>Public reproduction</h2><pre><code>uv run python scripts/generate_github_pages.py
uv run python scripts/verify_github_pages.py</code></pre><p>These commands read only the committed canonical public JSON and deterministic demo input. They do not require private session stores. The private extractor is intentionally excluded.</p></section><section class="model-section" aria-labelledby="method-limits-title"><div class="section-heading"><div><p class="data-label modelled-label">modelled_not_measured</p><h2 id="method-limits-title">Ordered limitations.</h2></div><p>Observed boundary counters remain reportable; broader economic and behavioral conclusions do not follow.</p></div>{_limitations(payload)}</section>"""
    return _shell(
        title="Methodology — Laconic",
        description="Definitions, equations, privacy boundary, reproduction, and limitations for Laconic self-use evidence.",
        active="methodology",
        prefix="../",
        digest=evidence.sha256,
        body=body,
    )


def _diagram(digest: str) -> str:
    steps = (
        ("01", "Host", "Tool result"),
        ("02", "Runtime", "Classify"),
        ("03", "Codec", "Encode"),
        ("04", "Ledger", "Retain raw"),
        ("05", "Agent", "Envelope"),
    )
    nodes = []
    for index, (number, label, detail) in enumerate(steps):
        x = 20 + index * 216
        nodes.append(
            f'<g transform="translate({x} 35)"><rect width="190" height="116" rx="14"/><text class="number" x="18" y="28">{number} · {label}</text><text class="label" x="18" y="65">{detail}</text></g>'
        )
        if index < len(steps) - 1:
            nodes.append(f'<path d="M {x + 190} 93 H {x + 210}" marker-end="url(#arrow)"/>')
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1090" height="186" viewBox="0 0 1090 186" role="img" aria-labelledby="diagram-title diagram-desc" data-json-sha256="{digest}"><title id="diagram-title">Laconic recoverable transformation pipeline</title><desc id="diagram-desc">A host tool result enters the canonical runtime, passes through a tool-specific codec, stores its original in a private ledger, and reaches the agent as a smaller recovery-bearing envelope.</desc><defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z"/></marker></defs><style>rect{{fill:#ebe5d9;stroke:#cfc7b8}}path{{stroke:#244f40;stroke-width:2;fill:#244f40}}text{{font-family:ui-sans-serif,system-ui,sans-serif;fill:#13231e}}.number{{font:700 12px ui-monospace,monospace;fill:#244f40;text-transform:uppercase}}.label{{font-size:18px;font-weight:800}}</style>{"".join(nodes)}</svg>'''


def _css(digest: str) -> str:
    return f"""/* canonical-json-sha256: {digest} */
:root{{--ink:#13231e;--forest:#244f40;--paper:#f4f0e7;--paper-2:#ebe5d9;--white:#fffdf8;--lime:#c9ed76;--amber:#e8aa50;--line:#cfc7b8;--muted:#5c6b64;--code:#091914;--max:1180px}}
*{{box-sizing:border-box}}html{{scroll-behavior:auto}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.55}}a{{color:inherit;text-underline-offset:.2em}}a:focus-visible{{outline:3px solid var(--amber);outline-offset:3px}}.skip-link{{position:fixed;left:12px;top:12px;z-index:10;transform:translateY(-180%);background:var(--white);padding:12px 16px;border:2px solid var(--ink)}}.skip-link:focus{{transform:none}}header{{border-bottom:1px solid var(--line);background:rgba(244,240,231,.98)}}.site-nav{{max-width:var(--max);min-height:72px;margin:auto;padding:10px 28px;display:flex;align-items:center;justify-content:space-between;gap:24px}}.wordmark{{font-size:22px;font-weight:900;letter-spacing:-.05em;text-decoration:none;min-height:44px;display:flex;align-items:center}}.wordmark span{{color:var(--forest)}}.nav-links{{display:flex;align-items:center;gap:8px}}.nav-links a{{min-height:44px;padding:11px 12px;display:inline-flex;align-items:center;font-size:14px;font-weight:750;text-decoration:none;border-radius:999px}}.nav-links a[aria-current=page]{{background:var(--paper-2)}}.nav-install{{background:var(--ink);color:white}}main{{overflow:hidden}}.hero{{max-width:var(--max);margin:auto;display:grid;grid-template-columns:1.15fr .85fr;min-height:560px}}.hero-copy{{padding:88px 44px 64px}}.eyebrow,.data-label{{margin:0;font:800 11px/1.3 ui-monospace,SFMono-Regular,Consolas,monospace;letter-spacing:.13em;text-transform:uppercase;color:var(--forest)}}h1,h2,h3,p{{overflow-wrap:anywhere}}h1{{font-size:clamp(3.25rem,7vw,5.5rem);line-height:.92;letter-spacing:-.07em;margin:20px 0 24px}}h2{{font-size:clamp(2.3rem,5vw,3.7rem);line-height:1;letter-spacing:-.055em;margin:10px 0 18px}}h3{{font-size:1.35rem;line-height:1.15}}.hero-copy>p:not(.eyebrow){{max-width:660px;font-size:18px;color:var(--muted)}}.hero-actions{{display:flex;flex-wrap:wrap;gap:10px;margin-top:30px}}.button{{display:inline-flex;align-items:center;justify-content:center;min-height:48px;padding:12px 18px;border-radius:999px;background:var(--ink);color:white;font-weight:800;text-decoration:none}}.button.secondary{{background:transparent;color:var(--ink);border:1px solid var(--line)}}.hero-code{{background:var(--code);color:#dce9e2;padding:64px 34px;display:flex;flex-direction:column;justify-content:center;gap:18px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace}}.hero-code p,.hero-code span{{color:#9fb2a9}}.hero-code strong{{color:var(--lime);font-size:26px}}.hero-code code{{padding:14px;border:1px solid #39574b;border-radius:9px;overflow-wrap:anywhere}}.demo-section,.paper-section,.claim-section,.model-section,.dark-section{{padding:76px max(24px,calc((100vw - var(--max))/2 + 44px))}}.demo-section{{background:var(--white);border-top:1px solid var(--line)}}.section-heading{{max-width:var(--max);margin:0 auto 38px;display:grid;grid-template-columns:.8fr 1.2fr;gap:50px;align-items:end}}.section-heading>p{{color:var(--muted);font-size:15px}}.demo-grid,.claim-grid{{max-width:var(--max);margin:auto;display:grid;grid-template-columns:1.15fr .85fr;gap:18px}}.terminal-card{{background:var(--code);color:#dce9e2;border-radius:16px;padding:28px;min-width:0}}.terminal-title{{color:#91a89d;font:700 12px ui-monospace,monospace}}pre{{max-width:100%;overflow:auto;padding:18px;border-radius:10px;background:#10251e;color:#dce9e2;font:12px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap}}.terminal-result{{padding-top:16px;border-top:1px solid #39574b;color:#9fb2a9}}.terminal-result strong{{color:var(--lime)}}.recovery-card{{padding:28px;border:1px solid var(--line);border-radius:16px;background:var(--paper)}}dl{{margin:0}}dl>div{{padding:12px 0;border-top:1px solid currentColor}}dt{{font-size:12px;color:var(--muted)}}dd{{margin:4px 0 0;font-weight:750}}code,.digest{{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}}.observed-strip{{background:var(--ink);color:white;padding:30px max(24px,calc((100vw - var(--max))/2 + 30px));display:grid;grid-template-columns:1fr 2fr;gap:26px}}.observed-strip h2{{font-size:24px;letter-spacing:-.03em;margin:5px 0}}.observed-strip>div>p:last-child,.scope-note{{color:#b7c5be}}.strip-stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}.strip-stats>div{{border-left:1px solid #486057;padding:5px 16px}}.strip-stats dt{{color:#adbbb4;text-transform:uppercase;letter-spacing:.08em;font-size:10px}}.strip-stats dd{{color:var(--lime);font-size:clamp(22px,3vw,32px);letter-spacing:-.04em}}.scope-note{{grid-column:1/-1;border-top:1px solid #354b42;padding-top:15px;font-size:12px}}.observed-label{{display:inline-block;background:var(--lime);color:var(--ink);border-radius:999px;padding:6px 9px}}.modelled-label{{display:inline-block;background:var(--amber);color:#3c2c18;border-radius:999px;padding:6px 9px}}.paper-section{{background:var(--paper)}}.system-diagram{{display:block;max-width:var(--max);width:100%;height:auto;margin:auto}}.dark-section{{background:var(--ink);color:white}}.dark-section .section-heading>p{{color:#bdcbc4}}.eyebrow.light{{color:var(--lime)}}.contract-grid{{max-width:var(--max);margin:auto;display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}.contract-grid article{{padding:24px;border:1px solid #486057;border-radius:14px}}.contract-grid p{{color:#bdcbc4}}.install-section{{display:flex;max-width:var(--max);margin:auto;align-items:center;justify-content:space-between;gap:30px}}.claim-section{{background:var(--white);border-block:1px solid var(--line)}}.observed-card,.modelled-card{{padding:28px;border-radius:16px}}.observed-card{{background:var(--ink);color:white}}.observed-card h3{{font-size:clamp(38px,6vw,62px);color:var(--lime);letter-spacing:-.06em}}.observed-card dt{{color:#b5c4bd}}.modelled-card{{background:#fff1d7;border:1px solid #d6aa63;color:#50391d}}.modelled-card h3{{font-size:clamp(34px,5vw,54px);letter-spacing:-.055em}}.modelled-card dt{{color:#725938}}.adjacent-limit{{font-weight:800;border-top:2px solid var(--amber);padding-top:16px}}.claim-link{{max-width:var(--max);margin:24px auto 0}}.route-hero{{max-width:var(--max);margin:auto;padding:76px 44px}}.route-hero h1{{font-size:clamp(3rem,7vw,5rem)}}.route-hero>p:last-child{{max-width:780px;color:var(--muted);font-size:17px}}.stacked{{max-width:none}}.table-wrap{{max-width:var(--max);margin:auto;overflow-x:auto}}table{{width:100%;border-collapse:collapse;background:var(--white)}}caption{{text-align:left;font-weight:850;font-size:17px;padding:15px;border:1px solid var(--line);border-bottom:0}}th,td{{text-align:left;padding:14px 16px;border:1px solid var(--line);vertical-align:top}}th{{width:34%}}.population{{max-width:var(--max);margin:22px auto 0;display:grid;grid-template-columns:repeat(3,1fr);gap:16px}}.population>div{{padding:16px;border:1px solid var(--line);border-radius:12px}}.model-section{{background:#fff1d7;border-block:1px solid #d6aa63}}.model-section>.modelled-card{{max-width:var(--max);margin:auto}}.limitations{{max-width:var(--max);margin:auto;padding:0;list-style:none;display:grid;grid-template-columns:repeat(2,1fr);gap:12px}}.limitations li{{padding:18px;border:1px solid var(--line);border-radius:12px;background:var(--white)}}.limitations code{{display:block;font-size:11px;color:var(--forest);margin-bottom:8px}}.limitations span{{color:var(--muted)}}.prose{{max-width:920px;margin:auto}}.prose h2{{font-size:34px;margin-top:52px}}.equations{{display:grid;gap:10px}}.equations code{{display:block;padding:16px;background:var(--ink);color:var(--lime);border-radius:8px}}.site-footer{{background:var(--ink);color:#b6c4bd;padding:28px max(24px,calc((100vw - var(--max))/2 + 28px));display:flex;justify-content:space-between;gap:30px;font-size:13px}}.site-footer strong{{color:white}}.site-footer p:last-child{{text-align:right}}
.button,.nav-links a,.site-footer a{{display:inline-flex;align-items:center;min-width:44px;min-height:44px}}
@media(max-width:850px){{.site-nav{{padding-inline:18px}}.nav-links>a:not(.nav-install){{display:none}}.hero{{grid-template-columns:1fr}}.hero-copy{{padding:64px 24px 48px}}.hero-code{{min-height:330px;padding:42px 24px}}.section-heading,.demo-grid,.claim-grid{{grid-template-columns:1fr}}.observed-strip{{grid-template-columns:1fr}}.strip-stats{{grid-template-columns:1fr}}.strip-stats>div{{border-left:0;border-top:1px solid #486057;padding:12px 0}}.contract-grid,.limitations,.population{{grid-template-columns:1fr}}.install-section,.site-footer{{flex-direction:column;align-items:flex-start}}.site-footer p:last-child{{text-align:left}}.route-hero{{padding:58px 24px}}th{{width:auto}}}}
@media(max-width:390px){{h1{{font-size:3rem}}.demo-section,.paper-section,.claim-section,.model-section,.dark-section{{padding-inline:18px}}.terminal-card,.recovery-card,.observed-card,.modelled-card{{padding:20px}}.nav-install{{padding-inline:10px}}}}
@media(prefers-reduced-motion:reduce){{*{{scroll-behavior:auto!important}}}}
"""


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _publish(stage: Path, destination: Path) -> bool:
    backup = destination.with_name(f".{destination.name}.previous")
    if destination.is_symlink():
        raise SiteError("site destination must not be a symlink")
    if backup.is_symlink():
        raise SiteError("site backup must not be a symlink")
    if backup.exists():
        if not backup.is_dir():
            raise SiteError("site backup is not an owned directory")
        try:
            shutil.rmtree(backup)
        except OSError as error:
            raise SiteError("stale site backup blocks publication") from error
    if not destination.exists():
        stage.rename(destination)
        return True
    destination.rename(backup)
    try:
        stage.rename(destination)
    except OSError as error:
        try:
            backup.rename(destination)
        except OSError as rollback_error:
            raise SiteError("site publication and rollback failed") from rollback_error
        raise SiteError("site publication failed; prior site restored") from error
    try:
        shutil.rmtree(backup)
    except OSError:
        return False
    return True


def render_site(evidence_path: Path, destination: Path) -> SiteBuild:
    """Render complete deterministic output, then replace the site as one tree."""

    evidence = load_evidence(evidence_path)
    demo = build_demo()
    stage = destination.with_name(f".{destination.name}.build")
    if stage.is_symlink():
        raise SiteError("site build destination must not be a symlink")
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    try:
        _write(stage / "index.html", _homepage(evidence, demo))
        _write(stage / "evidence" / "index.html", _evidence_page(evidence))
        _write(stage / "methodology" / "index.html", _methodology_page(evidence))
        _write(stage / "assets" / "site.css", _css(evidence.sha256))
        _write(stage / "assets" / "diagram.svg", _diagram(evidence.sha256))
        _write(
            stage / "evidence" / "laconic-development.json",
            evidence.to_json(),
        )
        files = tuple(
            sorted(str(path.relative_to(stage)) for path in stage.rglob("*") if path.is_file())
        )
        if files != SITE_FILES:
            raise SiteError("generated site file set is incomplete")
        from laconic.pages.audit import VerificationError, audit_site

        try:
            audit_site(evidence, stage)
        except VerificationError as error:
            raise SiteError("staged site failed safety audit") from error
        backup_cleaned = _publish(stage, destination)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise
    return SiteBuild(
        json_sha256=evidence.sha256,
        files=files,
        demo_raw_chars=demo.raw_chars,
        demo_visible_chars=demo.visible_chars,
        backup_cleaned=backup_cleaned,
    )
