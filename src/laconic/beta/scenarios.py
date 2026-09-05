"""The exact M18 failure-injection and control-surface scenario vocabulary.

Frozen by the M18 pre-implementation design gate (`.docs/DEVELOPMENT_PLAN.md`
§6 M18; refocus design §§9-10) before any campaign evidence exists. A
receipt's ``scenarios`` tag and a report's scenario-coverage fields may only
ever name one of these two closed sets -- never arbitrary free text -- so a
typo or an invented category fails loudly instead of silently widening what
counts as exercised evidence.
"""

from __future__ import annotations

#: Scenarios a receipt may claim before the human review gate is signed.
#: Every one of these must be covered across the campaign's receipts before
#: a report may recommend anything but NO-GO.
PRE_SIGNOFF_SCENARIOS: frozenset[str] = frozenset(
    {
        "engine_absence",
        "spawn_failure",
        "process_crash",
        "malformed_response",
        "timeout",
        "pause",
        "resume",
        "session_switch",
        "branch_tree_navigation",
        "resumed_session",
        "inherited_reference_expansion",
        "candidate_wheel_install",
        "actual_omp_load",
        "status",
        "full_expansion",
        "span_expansion",
        "disablement",
        "candidate_wheel_uninstall",
        "purge_session_preview",
        "purge_older_than_preview",
        "tool_error_passthrough",
        "unsupported_tool_passthrough",
        "mixed_content_passthrough",
        "details_preserved",
    }
)

#: Scenarios that may only be exercised after a human has signed the M18
#: review gate (`.docs/DEVELOPMENT_PLAN.md` §6 M18 human review gate): both
#: real-ledger purge forms. A report with neither covered reports
#: ``human_review_required`` rather than a hard failure, even when every
#: pre-signoff criterion already passed.
POST_SIGNOFF_SCENARIOS: frozenset[str] = frozenset(
    {
        "purge_session_apply",
        "purge_older_than_apply",
    }
)

#: The complete closed vocabulary a receipt's ``scenarios`` field may draw
#: from -- the union of both gates.
ALL_SCENARIOS: frozenset[str] = PRE_SIGNOFF_SCENARIOS | POST_SIGNOFF_SCENARIOS
