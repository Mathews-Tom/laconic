"""K1 Stage C's real, concrete `laconic.replay.engine.ReplayClient`
implementation.

Lives outside `src/laconic`, mirroring `tools/paired_replay`'s existing
precedent for a concrete client the shipped `laconic` package never
imports: no `pyproject.toml` dependency is added, and this module is
never part of the installable package (`pyproject.toml`
`[tool.hatch.build.targets.sdist] only-include` names `src/laconic`, not
`tools`). `.docs/K1_STAGE_C_LIVE_REPLAY_DESIGN.md` SS3 names this
boundary; SS10 records the owner's decision that this session writes the
concrete client rather than leaving it fully unauthored.
"""

from __future__ import annotations
