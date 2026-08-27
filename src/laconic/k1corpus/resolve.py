"""K1 Stage B — on-demand resolution of an opaque session ID back to its
real file, for a future, separately-authorized Stage C.

Governed by `.docs/K1_STAGE_B_MANIFEST_DESIGN.md` § 7. Nothing in this
repository invokes this resolver against a replay engine, provider, or
any content-reading code path -- it exists only as infrastructure a
later, separately-authorized Stage C plan may use. It returns a real
file path (never its content) and refuses to guess between ambiguous
candidates.
"""

from __future__ import annotations

from pathlib import Path

from laconic.k1corpus.providers import provider_root
from laconic.k1corpus.stage_a import Provider

#: Per-provider glob pattern for the real filename shape, given a UUID
#: extracted from an opaque `session_id`.
_GLOB_PATTERNS: dict[Provider, str] = {
    Provider.CLAUDE_CODE: "{uuid}.jsonl",
    Provider.CODEX: "*-{uuid}.jsonl",
    Provider.OMP: "*_{uuid}.jsonl",
}


def _uuid_from_session_id(provider: Provider, session_id: str) -> str | None:
    prefix = f"{provider.value}:"
    if not session_id.startswith(prefix):
        return None
    return session_id[len(prefix) :]


def resolve_session_path(
    provider: Provider, session_id: str, *, home: Path | None = None
) -> Path | None:
    """Re-derive the real file for an opaque ``session_id`` (as produced
    by `laconic.k1corpus.providers.extract_session_id`) by globbing the
    provider's real storage root for its expected filename shape.
    Returns ``None`` on a malformed ``session_id``, a missing storage
    root, or zero/more-than-one filename match -- never a guess between
    ambiguous candidates."""
    uuid = _uuid_from_session_id(provider, session_id)
    if uuid is None:
        return None
    root = provider_root(provider, home=home)
    if not root.is_dir():
        return None
    pattern = _GLOB_PATTERNS[provider].format(uuid=uuid)
    matches = sorted(root.rglob(pattern))
    if len(matches) != 1:
        return None
    return matches[0]
