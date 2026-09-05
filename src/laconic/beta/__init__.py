"""M18 beta qualification: manifest freeze, per-session receipts, and the
aggregate campaign report.

Nothing in this package serializes a raw tool observation, subject,
command, path, prompt, credential, tool argument, or real session ID --
only counts, character totals, latencies, and opaque hashes derived from
them. It reads the runtime ledger's content-free decision and expansion
records (`laconic.ledger`, `laconic.runtime.storage`) plus CLI-supplied
operator attestations, and reads raw observation text in memory in exactly
one place: `laconic.beta.receipt` compares a recovered observation against
its stored original to verify byte-exact recovery. See
`docs/runtime-beta-runbook.md`.
"""

from __future__ import annotations
