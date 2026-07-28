"""Tests for deterministic renderer components."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import laconic.ledger as ledger_module
from laconic.cli import EXIT_MALFORMED_RECORD, EXIT_NO_CORPUS, EXIT_RENDER_TRACE, main
from laconic.ledger import Ledger, ObservationKind
from laconic.render.templates import render
from laconic.render.view import assemble


def _ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "ledger.db", "render")


def test_assembly_orders_records_by_turn_then_insertion(tmp_path: Path) -> None:
    with _ledger(tmp_path) as ledger:
        first = ledger.register(ObservationKind.FILE, "a.py", "alpha", "a.py", turn=0)
        second = ledger.register(ObservationKind.COMMAND, "pytest", "passed", "passed", turn=1)
        third = ledger.register(ObservationKind.SEARCH, "needle", "match", "match", turn=1)

        trace = assemble(ledger, 1, 2)

    assert [(entry.turn, entry.record.handle) for entry in trace] == [
        (1, first.handle),
        (2, second.handle),
        (2, third.handle),
    ]


def test_assembly_reads_without_mutating_ledger(tmp_path: Path) -> None:
    with _ledger(tmp_path) as ledger:
        record = ledger.register(ObservationKind.FILE, "a.py", "alpha", "a.py", turn=0)

        trace = assemble(ledger, 1, 1)

        assert ledger.get(record.handle) == record

    assert trace[0].record.handle == record.handle


def test_assembly_uses_stored_size_without_decompressing_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _ledger(tmp_path) as ledger:
        record = ledger.register(ObservationKind.FILE, "a.py", "alpha", "a.py", turn=0)

        def fail_decompression(_: bytes) -> str:
            raise AssertionError("trace assembly decompressed a raw payload")

        monkeypatch.setattr(ledger_module, "decompress_raw", fail_decompression)
        trace = assemble(ledger, 1, 1)

    assert trace[0].record.raw_chars == len(record.raw)


@pytest.mark.parametrize("first,last", [(0, 1), (2, 1)])
def test_assembly_rejects_invalid_display_ranges(tmp_path: Path, first: int, last: int) -> None:
    with _ledger(tmp_path) as ledger:
        with pytest.raises(ValueError):
            assemble(ledger, first, last)


def test_templates_render_every_claim_with_its_source_handle(tmp_path: Path) -> None:
    with _ledger(tmp_path) as ledger:
        ledger.register(ObservationKind.FILE, "a.py", "alpha", "a.py", turn=0)
        ledger.register(ObservationKind.COMMAND, "pytest -q", "passed", "passed", turn=1)
        ledger.register(ObservationKind.SEARCH, "needle", "match", "match", turn=1)

        rendered = render(assemble(ledger, 1, 2))

    assert rendered.splitlines() == [
        'Turn 1: read "a.py" (result: 5 chars) [F1]',
        'Turn 2: ran "pytest -q" (result: 6 chars) [B1]',
        'Turn 2: searched "needle" (result: 5 chars) [S1]',
    ]


@pytest.mark.parametrize(
    ("kind", "label"),
    [
        (ObservationKind.FILE, "read"),
        (ObservationKind.COMMAND, "ran"),
        (ObservationKind.SEARCH, "searched"),
        (ObservationKind.FETCH, "fetched"),
        (ObservationKind.OTHER, "observed"),
    ],
)
def test_templates_render_every_observation_kind(
    tmp_path: Path, kind: ObservationKind, label: str
) -> None:
    with _ledger(tmp_path) as ledger:
        record = ledger.register(kind, kind.value, kind.value, kind.value, turn=0)

        rendered = render(assemble(ledger, 1, 1))

    assert rendered == f'Turn 1: {label} "{kind.value}" (result: 1 chars) [{record.handle}]'


@pytest.mark.parametrize("kind", tuple(ObservationKind))
def test_templates_support_all_registered_observation_kinds(
    tmp_path: Path, kind: ObservationKind
) -> None:
    with _ledger(tmp_path) as ledger:
        record = ledger.register(kind, kind.value, kind.value, kind.value, turn=0)

        rendered = render(assemble(ledger, 1, 1))

    assert rendered.endswith(f"[{record.handle}]")


def test_templates_return_no_unprovenanced_text_for_an_empty_trace() -> None:
    assert render(()) == ""


def test_expand_resolves_bare_and_spanned_handles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    transcript = corpus / "session.jsonl"
    records = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "read-1",
                        "name": "Read",
                        "input": {"path": "src/example.py"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "read-1",
                        "content": [
                            {
                                "type": "text",
                                "text": "first line\nsecond line\nthird line",
                            },
                            {"type": "tool_reference", "name": "Read"},
                        ],
                    }
                ],
            },
        },
    ]
    transcript.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )

    assert main(["expand", "F1", "--corpus", str(corpus)]) == 0
    first = capsys.readouterr()
    assert first.out == "first line\nsecond line\nthird line"
    assert f"source transcript: {transcript}" in first.err

    assert main(["expand", "F1:2-3", "--corpus", str(corpus)]) == 0
    assert capsys.readouterr().out == "second line\nthird line"

    assert main(["expand", "F1:4-4", "--corpus", str(corpus)]) == EXIT_RENDER_TRACE
    assert "outside 1-3" in capsys.readouterr().err


def test_expand_rejects_textless_tool_result_blocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "session.jsonl").write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "read-1",
                                "name": "Read",
                                "input": {"path": "src/example.py"},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "read-1",
                                "content": [{"type": "tool_reference", "name": "Read"}],
                            }
                        ],
                    },
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )

    assert main(["expand", "F1", "--corpus", str(corpus)]) == EXIT_RENDER_TRACE
    assert "unsupported tool result" in capsys.readouterr().err


def test_expand_reports_unresolvable_handles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "session.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "unfinished",
                            "name": "Read",
                            "input": {"path": "src/example.py"},
                        }
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert main(["expand", "F1", "--corpus", str(corpus)]) == EXIT_RENDER_TRACE
    assert "unknown handle: F1" in capsys.readouterr().err


def test_expand_rejects_missing_and_malformed_corpora(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["expand", "F1", "--corpus", str(tmp_path / "missing")]) == EXIT_NO_CORPUS
    assert "corpus path does not exist" in capsys.readouterr().err

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    assert main(["expand", "F1", "--corpus", str(corpus)]) == EXIT_NO_CORPUS
    assert "no *.jsonl transcripts found" in capsys.readouterr().err
    (corpus / "session.jsonl").write_text("{not json}\n", encoding="utf-8")

    assert main(["expand", "F1", "--corpus", str(corpus)]) == EXIT_MALFORMED_RECORD
    assert "invalid JSON record" in capsys.readouterr().err


def test_expand_rejects_an_unmatched_tool_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "session.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "missing",
                            "content": "result",
                        }
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert main(["expand", "F1", "--corpus", str(corpus)]) == EXIT_MALFORMED_RECORD
    assert "has no matching tool use" in capsys.readouterr().err
