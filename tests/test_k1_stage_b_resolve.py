"""Tests for `laconic.k1corpus.resolve`. No test touches a real
`~/.claude`/`~/.codex`/`~/.omp` path; every fixture is synthetic and
`tmp_path`-scoped. Nothing here invokes the resolver against a replay
engine or provider -- it is exercised only by these tests."""

from __future__ import annotations

from pathlib import Path

from laconic.k1corpus.resolve import resolve_session_path
from laconic.k1corpus.stage_a import Provider

_UUID = "41380cc3-ebf5-45c2-b0c6-c5f071f7a319"


def test_resolve_claude_code_single_match(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = home / ".claude" / "projects" / "laconic" / f"{_UUID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{}\n")

    resolved = resolve_session_path(Provider.CLAUDE_CODE, f"claude-code:{_UUID}", home=home)
    assert resolved == path


def test_resolve_codex_single_match(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = (
        home
        / ".codex"
        / "sessions"
        / "2026"
        / "01"
        / "01"
        / f"rollout-2026-01-01T00-00-00-{_UUID}.jsonl"
    )
    path.parent.mkdir(parents=True)
    path.write_text("{}\n")

    resolved = resolve_session_path(Provider.CODEX, f"codex:{_UUID}", home=home)
    assert resolved == path


def test_resolve_omp_single_match(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = (
        home
        / ".omp"
        / "agent"
        / "sessions"
        / "-WorkSpace-AetherForge-laconic"
        / f"2026-01-01T00-00-00-000Z_{_UUID}.jsonl"
    )
    path.parent.mkdir(parents=True)
    path.write_text("{}\n")

    resolved = resolve_session_path(Provider.OMP, f"omp:{_UUID}", home=home)
    assert resolved == path


def test_resolve_returns_none_for_zero_matches(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    resolved = resolve_session_path(Provider.CLAUDE_CODE, f"claude-code:{_UUID}", home=home)
    assert resolved is None


def test_resolve_returns_none_for_missing_storage_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    resolved = resolve_session_path(Provider.CLAUDE_CODE, f"claude-code:{_UUID}", home=home)
    assert resolved is None


def test_resolve_returns_none_for_ambiguous_match_never_guesses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project_a = home / ".claude" / "projects" / "proj-a"
    project_b = home / ".claude" / "projects" / "proj-b"
    project_a.mkdir(parents=True)
    project_b.mkdir(parents=True)
    # Two files that both end up matching the codex-style "*-<uuid>.jsonl"
    # glob is the realistic ambiguity case; construct it directly here.
    (project_a / f"rollout-a-{_UUID}.jsonl").write_text("{}\n")
    (project_b / f"rollout-b-{_UUID}.jsonl").write_text("{}\n")

    resolved = resolve_session_path(Provider.CODEX, f"codex:{_UUID}", home=home)
    assert resolved is None


def test_resolve_returns_none_for_wrong_provider_prefix(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = home / ".claude" / "projects" / "laconic" / f"{_UUID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{}\n")

    # session_id carries the wrong provider prefix for this file's shape.
    resolved = resolve_session_path(Provider.CLAUDE_CODE, f"omp:{_UUID}", home=home)
    assert resolved is None


def test_resolve_returns_none_for_malformed_session_id(tmp_path: Path) -> None:
    home = tmp_path / "home"
    resolved = resolve_session_path(Provider.CLAUDE_CODE, "not-a-valid-id", home=home)
    assert resolved is None
