"""K1 representative-corpus tooling: Stage A metadata feasibility screening.

Governed by `.docs/K1_REPRESENTATIVE_CORPUS_PROTOCOL.md` and authorized by
`.docs/DEVELOPMENT_PLAN_HISTORY.md` H-53. This package implements Stage A
only -- a body-free metadata ledger of historical coding-agent session
files under two explicitly authorized self-owned source roots, plus the
protocol's Stage A stop-condition evaluation.

It never reads a transcript body, prompt, tool argument/result, assistant
response, source file, credential, or title; never calls a provider;
never installs a hook; and never changes a codec setting or K1 threshold.
Stage B (corpus-design freeze) and Stage C (paired-evidence collection)
are separate, subsequent authorizations this package does not perform.
"""

from __future__ import annotations
