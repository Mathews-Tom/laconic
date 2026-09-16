"""The report renders the same twice, leaks nothing, and keeps its caveats."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

import laconic.spend.privacy as privacy_module
from laconic.spend.cli import REPORT_HTML, REPORT_JSON, REPORT_MARKDOWN, write_report
from laconic.spend.html import render_html
from laconic.spend.join import join
from laconic.spend.ledger import SessionDecisions
from laconic.spend.omp import SessionUsage, TurnUsage
from laconic.spend.privacy import PrivacyViolationError, validate_report_json
from laconic.spend.report import (
    ALLOWED_REPORT_KEYS,
    GENERATION_BASIS,
    LIMITATIONS,
    PRIVACY_STATUS,
    SOURCE_FRESHNESS,
    SpendReport,
    build_report,
    render_markdown,
    session_hash,
)

MATCHED = "01a078c0-15c2-7000-9389-19db9037f833"
SPEND_ONLY = "01a06d0f-6060-7000-94fc-3301f336bf8b"


def _turn(model: str = "claude-opus-4-8") -> TurnUsage:
    return TurnUsage(
        model=model,
        provider="anthropic",
        input_tokens=4,
        cache_read=87_173,
        cache_write=967,
        output_tokens=218,
        host_cost_usd=0.055,
    )


def _usage(session_id: str, *, nested: bool = False, turns: int = 1) -> SessionUsage:
    return SessionUsage(
        session_id=session_id,
        turns=tuple(_turn() for _ in range(turns)),
        turns_without_usage=0,
        malformed_lines=0,
        unknown_usage_keys=frozenset(),
        nested=nested,
    )


def _decisions(session_id: str) -> SessionDecisions:
    return SessionDecisions(
        session_id=session_id,
        eligible=21,
        emitted=2,
        raw_chars=115_587,
        visible_chars=105_061,
        full_expansions=1,
        span_expansions=0,
    )


def _payload(**kwargs: Any) -> dict[str, Any]:
    composition = join(
        kwargs.pop("usage", [_usage(MATCHED), _usage(SPEND_ONLY, nested=True)]),
        kwargs.pop("decisions", [_decisions(MATCHED)]),
        **kwargs,
    )
    return json.loads(build_report(composition).to_json())


def test_the_report_validates_and_carries_every_limitation() -> None:
    payload = _payload()

    validate_report_json(payload)

    assert tuple(payload["limitations"]) == LIMITATIONS
    assert len(LIMITATIONS) == 11


def test_the_rendering_is_byte_identical_across_two_runs() -> None:
    first = build_report(join([_usage(MATCHED)], [_decisions(MATCHED)]))
    second = build_report(join([_usage(MATCHED)], [_decisions(MATCHED)]))

    assert first.to_json() == second.to_json()
    assert render_markdown(first) == render_markdown(second)
    assert render_html(first) == render_html(second)


def test_the_rendering_disclaims_savings_and_never_asserts_one() -> None:
    rendered = render_markdown(build_report(join([_usage(MATCHED)], [_decisions(MATCHED)])))

    lowered = rendered.lower()
    assert "makes no savings claim" in lowered
    assert "single-arm corpus" in lowered
    assert "no counterfactual exists" in lowered
    # The failure this guards is a claim appearing, not the word appearing:
    # every phrase below asserts a saving rather than denying one.
    for claim in ("saved", "savings of", "we save", "net saving", "% saving", "reduction of"):
        assert claim not in lowered, f"the report asserts a saving: {claim!r}"


def test_a_real_session_id_never_reaches_the_payload() -> None:
    payload = _payload()

    serialized = json.dumps(payload)
    assert MATCHED not in serialized
    assert SPEND_ONLY not in serialized
    assert payload["sessions"][0]["session_hash"] == session_hash(MATCHED)


def test_only_matched_sessions_are_serialized_row_by_row() -> None:
    payload = _payload()

    assert payload["matched_sessions"] == 1
    assert payload["unmatched_spend_sessions"] == 1
    assert [entry["session_hash"] for entry in payload["sessions"]] == [session_hash(MATCHED)]


def test_subagent_sessions_are_counted_apart_from_root_sessions() -> None:
    payload = _payload()

    assert payload["root_sessions"] == 1
    assert payload["nested_sessions"] == 1
    assert payload["sessions_with_spend"] == 2


def test_an_empty_corpus_reports_no_shares_rather_than_four_zeroes() -> None:
    payload = _payload(usage=[], decisions=[])

    validate_report_json(payload)
    assert payload["corpus_shares"] is None
    assert payload["corpus_cost"]["total"] == 0.0


def test_provenance_labels_are_closed_and_privacy_validated() -> None:
    payload = _payload()
    assert payload["generation_basis"] == GENERATION_BASIS
    assert payload["source_freshness"] == SOURCE_FRESHNESS
    assert payload["privacy_status"] == PRIVACY_STATUS

    for field in ("generation_basis", "source_freshness", "privacy_status"):
        mutated = {**payload, field: "private/free text"}
        with pytest.raises(PrivacyViolationError, match=field):
            validate_report_json(mutated)
    leaked = {**payload, "laconic_version": "/Users/owner/private"}
    with pytest.raises(PrivacyViolationError, match="laconic_version"):
        validate_report_json(leaked)


def test_the_allowlist_rejects_an_added_key() -> None:
    payload = _payload()
    payload["cwd"] = "/Users/owner/WorkSpace/private"

    with pytest.raises(PrivacyViolationError, match="unallowlisted report key"):
        validate_report_json(payload)


def test_the_allowlist_rejects_a_removed_key() -> None:
    payload = _payload()
    del payload["codec"]

    with pytest.raises(PrivacyViolationError, match="missing report key"):
        validate_report_json(payload)


def test_the_allowlist_rejects_a_raw_session_id_in_place_of_a_digest() -> None:
    payload = _payload()
    payload["sessions"][0]["session_hash"] = MATCHED

    with pytest.raises(PrivacyViolationError, match="never a session id"):
        validate_report_json(payload)


@pytest.mark.parametrize(
    "leaked",
    [
        "/Users/owner/WorkSpace/laconic",
        "../../etc/passwd",
        "C:\\Users\\owner",
        "a model name with spaces",
        ".hidden",
        "x" * 65,
    ],
)
def test_the_allowlist_rejects_a_path_shaped_model_identifier(leaked: str) -> None:
    payload = _payload()
    payload["unpriced_models"] = [leaked]

    with pytest.raises(PrivacyViolationError, match="unpriced_models"):
        validate_report_json(payload)


def test_a_real_slash_bearing_model_identifier_is_still_accepted() -> None:
    payload = _payload()
    payload["unpriced_models"] = ["~anthropic/claude-opus-latest", "gpt-5.6-terra"]

    validate_report_json(payload)


def test_a_report_that_drops_its_single_arm_caveat_is_rejected() -> None:
    payload = _payload()
    payload["limitations"] = [
        name
        for name in payload["limitations"]
        if name != "single_arm_corpus_every_session_ran_with_the_codec_enabled"
    ]

    with pytest.raises(PrivacyViolationError, match="single-arm caveat"):
        validate_report_json(payload)


def test_a_reordered_limitations_block_is_rejected() -> None:
    payload = _payload()
    payload["limitations"] = list(reversed(payload["limitations"]))

    with pytest.raises(PrivacyViolationError, match="in order"):
        validate_report_json(payload)


def test_a_negative_counter_is_rejected() -> None:
    payload = _payload()
    payload["codec"]["emitted"] = -1

    with pytest.raises(PrivacyViolationError, match="must not be negative"):
        validate_report_json(payload)


def test_a_boolean_smuggled_in_as_a_counter_is_rejected() -> None:
    payload = _payload()
    payload["priced_turns"] = True

    with pytest.raises(PrivacyViolationError, match="must be an integer"):
        validate_report_json(payload)


def test_writing_the_report_produces_the_complete_evidence_bundle(tmp_path: Path) -> None:
    composition = join([_usage(MATCHED)], [_decisions(MATCHED)])

    written = write_report(composition, tmp_path)

    assert written.json_path == tmp_path / REPORT_JSON
    assert written.markdown_path == tmp_path / REPORT_MARKDOWN
    assert written.html_path == tmp_path / REPORT_HTML
    validate_report_json(json.loads(written.json_path.read_text(encoding="utf-8")))
    markdown = written.markdown_path.read_text(encoding="utf-8")
    html = written.html_path.read_text(encoding="utf-8")
    assert "Limitations" in markdown
    assert written.report.sha256 in markdown
    assert written.report.sha256 in html
    assert "modelled_not_measured" in html
    assert "single_arm_corpus_every_session_ran_with_the_codec_enabled" in html
    assert "<script" not in html
    assert "<svg" in html
    assert "https://" not in html and "http://" not in html


def test_writing_twice_produces_identical_bytes(tmp_path: Path) -> None:
    composition = join([_usage(MATCHED)], [_decisions(MATCHED)])

    first = write_report(composition, tmp_path / "a")
    second = write_report(composition, tmp_path / "b")

    assert first.json_path.read_bytes() == second.json_path.read_bytes()
    assert first.markdown_path.read_bytes() == second.markdown_path.read_bytes()
    assert first.html_path.read_bytes() == second.html_path.read_bytes()


def test_a_failing_privacy_check_writes_nothing(tmp_path: Path, monkeypatch: Any) -> None:
    import laconic.spend.report as report_module

    monkeypatch.setattr(report_module, "LIMITATIONS", ("only_one_caveat",))
    composition = join([_usage(MATCHED)], [_decisions(MATCHED)])

    with pytest.raises(PrivacyViolationError):
        write_report(composition, tmp_path / "out")

    assert not (tmp_path / "out" / REPORT_JSON).exists()
    assert not (tmp_path / "out" / REPORT_MARKDOWN).exists()
    assert not (tmp_path / "out" / REPORT_HTML).exists()
    assert not (tmp_path / "out").exists()


def test_a_render_failure_writes_nothing(tmp_path: Path, monkeypatch: Any) -> None:
    import laconic.spend.cli as cli_module

    def fail_render(_report: SpendReport) -> str:
        raise ValueError("render failed")

    monkeypatch.setattr(cli_module, "render_html", fail_render)
    composition = join([_usage(MATCHED)], [_decisions(MATCHED)])

    with pytest.raises(ValueError, match="render failed"):
        write_report(composition, tmp_path / "out")

    assert not (tmp_path / "out").exists()


def test_html_escapes_every_dynamic_identifier() -> None:
    payload = _payload()
    payload["unpriced_models"] = ["evil<script>"]
    report = SpendReport(payload)

    html = render_html(report)
    assert "evil&lt;script&gt;" in html
    assert "evil<script>" not in html


def test_a_replacement_failure_names_the_failed_destination(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    import laconic.spend.cli as cli_module

    original_replace = cli_module.os.replace
    output = tmp_path / "out"

    def fail_html(source: Path, destination: Path) -> None:
        if Path(destination).name == REPORT_HTML:
            raise OSError("synthetic disk failure")
        original_replace(source, destination)

    monkeypatch.setattr(cli_module.os, "replace", fail_html)
    composition = join([_usage(MATCHED)], [_decisions(MATCHED)])

    with pytest.raises(OSError, match=str(output / REPORT_HTML)):
        write_report(composition, output)


def test_no_savings_ratio_is_serialized_at_all() -> None:
    # A tokens-per-avoided-character ratio published beside chars_avoided
    # multiplies back to the matched sessions' whole token volume, which a
    # reader relabels 'tokens saved'. The report carries no such quantity.
    payload = _payload()

    assert "tokens_per_avoided_character" not in payload
    assert not any("per_avoided" in key or "per_character" in key for key in payload)


def test_every_allowlisted_key_is_covered_by_a_shape_check() -> None:
    # The gate's completeness must be enforced, not coincidental: a key
    # added to the allowlist and to no shape group lands in the derived
    # integer group and fails loudly instead of serializing unchecked.
    payload = _payload()
    payload["cwd"] = "/Users/owner/WorkSpace/private"
    ALLOWED_REPORT_KEYS_WITH_LEAK = ALLOWED_REPORT_KEYS | {"cwd"}

    assert "cwd" in ALLOWED_REPORT_KEYS_WITH_LEAK - (
        privacy_module._TOKEN_BLOCK_KEYS
        | privacy_module._COST_BLOCK_KEYS
        | privacy_module._SHARE_BLOCK_KEYS
        | privacy_module._USD_KEYS
        | privacy_module._PERCENT_KEYS
        | privacy_module._IDENTIFIER_LIST_KEYS
        | privacy_module._INLINE_CHECKED_KEYS
    )
    assert (
        privacy_module._INT_KEYS
        | privacy_module._TOKEN_BLOCK_KEYS
        | (
            privacy_module._COST_BLOCK_KEYS
            | privacy_module._SHARE_BLOCK_KEYS
            | privacy_module._USD_KEYS
            | privacy_module._PERCENT_KEYS
            | privacy_module._IDENTIFIER_LIST_KEYS
            | privacy_module._INLINE_CHECKED_KEYS
        )
        == ALLOWED_REPORT_KEYS
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_a_non_finite_dollar_figure_is_refused_before_it_reaches_disk(value: float) -> None:
    payload = _payload()
    payload["corpus_host_cost_usd"] = value

    with pytest.raises(PrivacyViolationError, match="must be finite"):
        validate_report_json(payload)


@pytest.mark.parametrize("value", [-0.01, 100.01])
def test_an_out_of_range_percentage_is_refused_before_rendering(value: float) -> None:
    payload = _payload()
    payload["fallback_priced_cost_share_pct"] = value

    with pytest.raises(PrivacyViolationError, match="negative|exceed"):
        validate_report_json(payload)


def test_a_ledger_bearing_session_with_no_priced_turn_is_reported(tmp_path: Path) -> None:
    composition = join([_usage(MATCHED, turns=0)], [_decisions(MATCHED)])

    payload = json.loads(build_report(composition).to_json())

    validate_report_json(payload)
    assert payload["sessions_with_spend"] == 0
    assert payload["sessions_without_priced_turns"] == 1
    assert payload["codec_active_sessions_without_priced_turns"] == 1
    # Its codec work is counted, not dropped.
    assert payload["codec"]["eligible"] == 21
    assert payload["unmatched_ledger_sessions"] == 0


def test_the_report_refuses_to_write_inside_the_runtime_store(tmp_path: Path) -> None:
    store = tmp_path / "store"
    (store / "sessions").mkdir(parents=True)
    composition = join([_usage(MATCHED)], [_decisions(MATCHED)])

    with pytest.raises(OSError, match="read-only source tree"):
        write_report(composition, store / "spend", data_dir=store)

    assert not (store / "spend").exists()


def test_the_report_refuses_to_write_through_a_symlink(tmp_path: Path) -> None:
    destination = tmp_path / "out"
    destination.mkdir()
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("untouched", encoding="utf-8")
    (destination / REPORT_JSON).symlink_to(elsewhere)
    composition = join([_usage(MATCHED)], [_decisions(MATCHED)])

    with pytest.raises(OSError, match="refusing to write through a symlink"):
        write_report(composition, destination)

    assert elsewhere.read_text(encoding="utf-8") == "untouched"


def test_the_estimate_is_labelled_as_modelled_and_reported_as_a_band() -> None:
    """A dollar figure that loses the word "modelled" reads as a measurement.

    Every input to this estimate except characters-per-token is divided out
    of the corpus's own measured tokens and cost, but that one assumption is
    enough that a point estimate would overstate what is known. The band and
    the `basis` label are the two things that keep the figure honest, so
    both are pinned.
    """
    estimate = _payload()["estimate"]

    assert estimate is not None
    assert estimate["basis"] == "modelled_not_measured"
    assert estimate["avoided_cost_usd_low"] < estimate["avoided_cost_usd_high"]
    assert estimate["chars_per_token_low"] < estimate["chars_per_token_high"]
    assert 0.0 < estimate["avoided_share_pct_low"] < estimate["avoided_share_pct_high"]


def test_the_estimate_prices_cache_re_reads_not_just_one_turn() -> None:
    """The reason tool-boundary removal is worth more than its character count.

    Removed text is never written to the prompt cache, so it is never
    re-read on any later turn. Pricing only the single turn it appeared in
    would understate the effect by the corpus's own re-read multiplier,
    which is the whole mechanism this project's compounding claim rests on.
    """
    payload = _payload()
    estimate = payload["estimate"]
    assert estimate is not None
    tokens = payload["matched_tokens"]

    expected = tokens["cache_read"] / tokens["cache_write"]
    assert estimate["cache_reread_multiplier"] == pytest.approx(expected, rel=1e-9)
    assert expected > 1.0

    # Pin the identity, not an inequality: dropping the re-read term leaves
    # a cost that is still "greater than zero" and can still clear a loose
    # bound by rounding, so the formula itself is what must be asserted.
    write_rate = estimate["effective_cache_write_usd_per_token"]
    read_rate = estimate["effective_cache_read_usd_per_token"]
    assert estimate["avoided_cost_usd_low"] == pytest.approx(
        estimate["tokens_removed_low"] * (write_rate + estimate["reread_credited_low"] * read_rate),
        abs=1e-6,
    )
    assert estimate["avoided_cost_usd_high"] == pytest.approx(
        estimate["tokens_removed_high"]
        * (write_rate + estimate["reread_credited_high"] * read_rate),
        abs=1e-6,
    )
    single_turn = estimate["tokens_removed_low"] * write_rate
    assert estimate["avoided_cost_usd_low"] > 2 * single_turn


def test_a_corpus_with_no_removed_characters_reports_no_estimate() -> None:
    """`None`, not zero.

    Zero would read as "the codec saved nothing"; the truth for a corpus
    that removed nothing measurable is that it cannot say.
    """
    decisions = SessionDecisions(
        session_id=MATCHED,
        eligible=0,
        emitted=0,
        raw_chars=0,
        visible_chars=0,
        full_expansions=0,
        span_expansions=0,
    )
    payload = build_report(join([_usage(MATCHED)], [decisions])).payload

    assert payload["estimate"] is None
    validate_report_json(payload)


def test_an_unlabelled_estimate_is_refused_by_the_privacy_gate() -> None:
    """The mutation this gate exists to catch."""
    payload = _payload()
    assert payload["estimate"] is not None
    payload["estimate"]["basis"] = "measured"

    with pytest.raises(PrivacyViolationError, match="basis"):
        validate_report_json(payload)


def test_the_band_spans_the_re_read_assumption_not_only_the_token_count() -> None:
    """The dominant input must not be held exact.

    The re-read term is most of the modelled per-token price, and the
    corpus average that produces it is inflated for tool output: it is
    dominated by first-turn content re-read on every later turn, while the
    codec removes results that arrive later and are re-read less. Banding
    only characters-per-token while stating that term as fact would imply a
    precision the estimate does not have, and would bias it upward.
    """
    estimate = _payload()["estimate"]
    assert estimate is not None

    assert estimate["reread_credited_low"] < estimate["reread_credited_high"]
    assert estimate["reread_credited_high"] == pytest.approx(
        estimate["cache_reread_multiplier"], rel=1e-9
    )
    # Widening the re-read assumption must widen the band, not just shift
    # it. A bare `>` would clear on rounding noise if the credits were made
    # equal again, so require a margin the token count alone cannot produce.
    token_ratio = estimate["tokens_removed_high"] / estimate["tokens_removed_low"]
    cost_ratio = estimate["avoided_cost_usd_high"] / estimate["avoided_cost_usd_low"]
    assert cost_ratio > token_ratio * 1.2


def test_an_unpriced_model_is_reported_as_a_cost_share_not_just_a_name() -> None:
    """Naming the unpriced models is not enough on its own.

    A reader cannot tell from a list of names whether they are a rounding
    error or most of the bill. The estimate divides its per-token rates
    out of the same modelled cost those models inflate, so a large
    fallback share means the dollars rest on prices nobody published. The
    share survives, because the identical error sits in the numerator and
    the denominator and largely cancels.
    """
    priced = build_report(join([_usage(MATCHED)], [_decisions(MATCHED)])).payload
    assert priced["fallback_priced_cost_share_pct"] == 0.0

    unknown = SessionUsage(
        session_id=MATCHED,
        turns=(_turn("a-model-with-no-list-price"),),
        turns_without_usage=0,
        malformed_lines=0,
        unknown_usage_keys=frozenset(),
    )
    mixed = build_report(join([unknown], [_decisions(MATCHED)])).payload

    assert mixed["fallback_priced_cost_share_pct"] == 100.0
    assert "a-model-with-no-list-price" in mixed["unpriced_models"]
    estimate = mixed["estimate"]
    assert estimate is not None
    assert estimate["fallback_priced_cost_share_pct"] == 100.0


def _no_host_cost(session_id: str) -> SessionUsage:
    """A session whose host records token counters and no cost of its own."""
    return SessionUsage(
        session_id=session_id,
        turns=(dataclasses.replace(_turn(), host_cost_usd=0.0),),
        turns_without_usage=0,
        malformed_lines=0,
        unknown_usage_keys=frozenset(),
        reports_host_cost=False,
    )


def test_the_modelled_total_compared_against_the_host_covers_the_hosts_own_sessions() -> None:
    """The two totals a reader puts side by side must name the same sessions.

    Claude Code records token counters and no per-turn cost. Its sessions
    therefore add modelled dollars and nothing the host total can match, so
    `corpus_cost` over-counts relative to `corpus_host_cost_usd` by exactly
    those sessions. Reading the difference as a pricing disagreement is how
    a whole-corpus ratio gets quoted as a pricing error.
    """
    payload = _payload(
        usage=[_usage(MATCHED), _no_host_cost(SPEND_ONLY)],
        decisions=[_decisions(MATCHED)],
    )
    validate_report_json(payload)

    reporting_only = _payload(usage=[_usage(MATCHED)], decisions=[_decisions(MATCHED)])

    assert payload["priced_sessions_without_host_cost"] == 1
    # The comparable figure prices exactly the sessions the host priced, so
    # it must equal the corpus total of a corpus holding only those.
    assert payload["host_reporting_cost"] == reporting_only["corpus_cost"]
    # ...and must be strictly below the all-sessions total, which is what a
    # figure computed over every session would collapse into.
    assert payload["host_reporting_cost"]["total"] < payload["corpus_cost"]["total"]
    assert payload["corpus_host_cost_usd"] == pytest.approx(0.055)


def test_the_matched_comparison_certifies_that_both_sides_cover_one_set() -> None:
    """`matched_cost` beside `matched_host_cost_usd` needs the same certificate.

    It is zero on today's corpus only because every session the codec has a
    ledger for happens to run on a host that prices turns. The codec installs
    into Claude Code too, so that is a fact about the corpus and not an
    invariant; without the count the matched pair would silently become the
    same incomparable pairing the corpus-level figures already were.
    """
    clean = _payload()
    assert clean["matched_sessions_without_host_cost"] == 0

    mixed = _payload(
        usage=[_no_host_cost(MATCHED), _usage(SPEND_ONLY)],
        decisions=[_decisions(MATCHED)],
    )
    assert mixed["matched_sessions_without_host_cost"] == 1


def test_the_markdown_names_which_sessions_each_total_covers() -> None:
    report = build_report(join([_usage(MATCHED), _no_host_cost(SPEND_ONLY)], [_decisions(MATCHED)]))

    rendered = render_markdown(report)

    assert "Do not read the total above against the host's" in rendered
    assert "| The 1 priced session the host priced |" in rendered
    assert "| All 2 priced sessions |" in rendered
    assert "not available" in rendered


def test_an_all_reporting_corpus_is_not_told_its_totals_are_incomparable() -> None:
    """The directive must not fire when there is nothing to warn about.

    On an OMP-only corpus every priced session reports host cost, so the
    two totals do cover one set. Printing "do not read these against each
    other" above a table showing them equal is a false instruction, and a
    reader who checks it against the table is right to trust the next
    caveat less.
    """
    rendered = render_markdown(build_report(join([_usage(MATCHED)], [_decisions(MATCHED)])))

    assert "Do not read the total above against the host's" not in rendered
    assert "compare directly" in rendered
    # ...and the count that would render as "1 sessions" agrees with its noun.
    assert "1 priced sessions" not in rendered


def test_the_report_discloses_the_cache_lifetime_it_does_not_model() -> None:
    """A measured, signed, deliberately uncorrected modelling gap.

    Anthropic bills a one-hour cache write at twice the input price where a
    five-minute write bills at 1.25x, and `laconic.costs` charges one rate
    for both. On the development corpus 65.4% of cache-write tokens carry a
    one-hour lifetime, so the omission is material -- and it can only make a
    modelled figure *smaller* than the provider's, which is the opposite
    direction from the gap against the host that prompted measuring it. The
    report has to say so rather than leave a reader to assume every billable
    distinction is modelled.
    """
    rendered = render_markdown(build_report(join([_usage(MATCHED)], [_decisions(MATCHED)])))

    assert "cache_writes_are_priced_at_one_rate_although_lifetimes_bill_differently" in LIMITATIONS
    assert "one-hour cache write at twice the input price" in rendered
    assert "under-prices, never over-prices" in rendered
