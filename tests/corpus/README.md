# Fixture corpus

A synthetic session corpus in the format `laconic measure` and the replay harness consume. Nothing here comes from a real session: the measurements that motivate Laconic were taken over private transcripts containing proprietary source, which cannot be committed.

## Layout

```
tests/corpus/
├── README.md
├── expected.json            committed expected values
├── session-a-refactor.jsonl 60 assistant turns, claude-sonnet-5
├── session-b-debug.jsonl    40 assistant turns, claude-sonnet-5
└── session-c-review.jsonl   25 assistant turns, claude-opus-4-8
```

A corpus is any directory tree; every `*.jsonl` file beneath it is a transcript, discovered in sorted order so a scan is identical on any machine.

## Transcript schema

One JSON object per line. Unrecognised fields are ignored, so a real Claude Code transcript is a valid corpus member without conversion.

**Assistant record** — carries token usage and the emitted content blocks:

```json
{"type": "assistant",
 "message": {"role": "assistant",
             "model": "claude-sonnet-5",
             "content": [{"type": "text", "text": "..."},
                         {"type": "tool_use", "id": "toolu_0001", "name": "Read",
                          "input": {"path": "src/widget.py"}}],
             "usage": {"input_tokens": 0,
                       "cache_read_input_tokens": 182400,
                       "cache_creation_input_tokens": 4880,
                       "output_tokens": 731}}}
```

**User record** — either a typed prompt or a tool result:

```json
{"type": "user", "message": {"role": "user", "content": "now update the caller"}}
{"type": "user", "message": {"role": "user",
                             "content": [{"type": "tool_result",
                                          "tool_use_id": "toolu_0001",
                                          "content": "..."}]}}
```

Channel attribution: `text` blocks are prose (fenced code is measured separately and excluded from prose), `tool_use.input` is the action channel, `tool_result.content` is the observation channel and is attributed to the tool named by the matching `tool_use.id`, and a string user `content` is the prompt channel.

## Expected values

`expected.json` is the committed decomposition of this corpus. `laconic measure tests/corpus --expect tests/corpus/expected.json` exits non-zero on any difference. Integer counters must match exactly; USD figures match within `COST_TOLERANCE_USD`.

Regenerate after an intentional corpus or accounting change:

```bash
uv run python -c 'import json; from pathlib import Path; \
from laconic.replay.corpus import expectation, scan_corpus; \
Path("tests/corpus/expected.json").write_text(json.dumps(expectation(scan_corpus([Path("tests/corpus")])), indent=2, sort_keys=True) + "\n")'
```

## What the fixture is and is not representative of

The corpus reproduces the *shape* of the real measurement in `docs/overview.md` §2 — that is what makes a channel decomposition run against it meaningful:

| Property | `docs/overview.md` §2 | This fixture |
|---|---:|---:|
| Cache reads, share of spend | 60.3% | 58.7% |
| Cache writes | 26.7% | 27.6% |
| Output tokens | 11.3% | 11.2% |
| Uncached input | 1.7% | 2.4% |
| Turns emitting zero prose | 80.6% | 90.4% |
| Human-facing prose, share of spend | 2.30% | 2.18% |
| Largest observation channel | `Read` | `Read` |

Reads are whale-distributed here as they are in real sessions: a small number of turns pull in most of the observation volume.

It is **not** representative in scale. The corpus is 125 assistant turns across three sessions, against 19,818 turns across 179 sessions for the published figures, and the textual payloads are short stand-ins — the token counters are set to the magnitudes real sessions exhibit (mean resident prefix in the low hundreds of thousands, mean output in the hundreds) rather than to the character length of the synthetic text. A gate number measured against this corpus therefore verifies that the pipeline computes the right thing; it is not a restatement of the published measurement. `DEVELOPMENT_PLAN.md` §2 records that as an open gap against K1.
