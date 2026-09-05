"""The frozen M18 qualification-campaign manifest.

`.docs/DEVELOPMENT_PLAN.md` §6 M18 and the refocus design §9 fix the
campaign's minimums, eligible client version, and required scenarios
before any evidence is collected -- that part is a build-time constant
every manifest must reproduce exactly. But a manifest of static thresholds
alone cannot freeze the actual campaign *population*: which candidate
build was tested, and which of the 10 predeclared sessions ran against
which of the 3 repositories. Without pinning that population up front, an
operator could self-select favorable sessions or repositories after seeing
early results. :func:`build_manifest` therefore also binds one candidate
wheel SHA-256 and an explicit, exactly-10-slot-to-exactly-3-repository-ID
mapping (hashed locally; no path is ever part of the manifest).
:func:`validate_manifest_json` checks the constant part for exact equality
and the population part for the closed shape this contract requires
(contiguous 1..10 slots, exactly 3 distinct repository IDs, one wheel
hash) -- it never compares the population fields to a fixed object, since
they are legitimately different from one campaign to the next.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from laconic.beta.canon import canonical_sha256
from laconic.beta.scenarios import POST_SIGNOFF_SCENARIOS, PRE_SIGNOFF_SCENARIOS

#: Bumped whenever a manifest field is added, removed, or reinterpreted.
MANIFEST_SCHEMA_VERSION = 1

#: `.docs/DEVELOPMENT_PLAN.md` §6 M18 acceptance: "At least 10 ... sessions
#: ... across at least 3 canonical Git roots, with at least 100 eligible
#: observations." The manifest's slot mapping declares exactly this many
#: sessions and repositories, not merely "at least."
MIN_SESSIONS = 10
MIN_REPOSITORIES = 3
MIN_ELIGIBLE_OBSERVATIONS = 100

#: The only OMP client version this qualification campaign may credit.
#: Frozen before results per the M18 design-gate step "Freeze before
#: results: eligible client/version."
ELIGIBLE_OMP_VERSION = "18.1.10"

#: A lowercase SHA-256 hex digest: exactly 64 ``[0-9a-f]`` characters.
_HEX_64 = frozenset("0123456789abcdef")

#: The exact key set a serialized manifest may ever contain.
ALLOWED_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "min_sessions",
        "min_repositories",
        "min_eligible_observations",
        "eligible_omp_version",
        "pre_signoff_scenarios",
        "post_signoff_scenarios",
        "candidate_wheel_sha256",
        "slots",
    }
)


class ManifestValidationError(ValueError):
    """Raised when a candidate manifest is not a valid M18 contract."""


def _is_hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and not set(value) - _HEX_64


@dataclass(frozen=True, slots=True)
class CampaignManifest:
    """The frozen M18 contract every receipt and report binds to by hash.

    ``schema_version`` through ``post_signoff_scenarios`` are build-time
    constants, identical across every campaign this build runs.
    ``candidate_wheel_sha256`` and ``slots`` are this campaign's frozen
    population: the one wheel under test and the exact slot-to-repository
    binding, fixed before any session runs.
    """

    schema_version: int
    min_sessions: int
    min_repositories: int
    min_eligible_observations: int
    eligible_omp_version: str
    pre_signoff_scenarios: tuple[str, ...]
    post_signoff_scenarios: tuple[str, ...]
    candidate_wheel_sha256: str
    slots: tuple[tuple[int, str], ...]
    """``(slot, repository_id)`` pairs, sorted by slot ascending."""

    def repository_id_for_slot(self, slot: int) -> str | None:
        """Return the repository ID this manifest froze for ``slot``, if any."""
        return dict(self.slots).get(slot)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "min_sessions": self.min_sessions,
            "min_repositories": self.min_repositories,
            "min_eligible_observations": self.min_eligible_observations,
            "eligible_omp_version": self.eligible_omp_version,
            "pre_signoff_scenarios": sorted(self.pre_signoff_scenarios),
            "post_signoff_scenarios": sorted(self.post_signoff_scenarios),
            "candidate_wheel_sha256": self.candidate_wheel_sha256,
            "slots": [list(pair) for pair in sorted(self.slots)],
        }


def build_manifest(
    *, candidate_wheel_sha256: str, slots: tuple[tuple[int, str], ...]
) -> CampaignManifest:
    """Build one campaign's manifest: build-time constants plus this
    campaign's frozen wheel hash and slot-to-repository binding.

    Raises :class:`ManifestValidationError` unless ``slots`` names exactly
    :data:`MIN_SESSIONS` contiguous slots starting at 1 and exactly
    :data:`MIN_REPOSITORIES` distinct repository IDs, and
    ``candidate_wheel_sha256`` is a SHA-256 hex digest.
    """
    _require_valid_slots(slots)
    if not _is_hex64(candidate_wheel_sha256):
        raise ManifestValidationError("candidate_wheel_sha256 must be a 64-character hex digest")
    return CampaignManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        min_sessions=MIN_SESSIONS,
        min_repositories=MIN_REPOSITORIES,
        min_eligible_observations=MIN_ELIGIBLE_OBSERVATIONS,
        eligible_omp_version=ELIGIBLE_OMP_VERSION,
        pre_signoff_scenarios=tuple(sorted(PRE_SIGNOFF_SCENARIOS)),
        post_signoff_scenarios=tuple(sorted(POST_SIGNOFF_SCENARIOS)),
        candidate_wheel_sha256=candidate_wheel_sha256,
        slots=tuple(sorted(slots)),
    )


def fingerprint_manifest(manifest: CampaignManifest) -> str:
    """Return the canonical SHA-256 every receipt and report binds to."""
    return canonical_sha256(manifest.to_json())


def _require_int(payload: dict[str, Any], key: str) -> int:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestValidationError(f"{key} must be an int (got {type(value).__name__})")
    return value


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ManifestValidationError(f"{key} must be a non-empty string")
    return value


def _require_scenario_set(payload: dict[str, Any], key: str, expected: frozenset[str]) -> None:
    value = payload[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ManifestValidationError(f"{key} must be a list of strings")
    if set(value) != expected:
        raise ManifestValidationError(
            f"{key} does not match the frozen M18 scenario set: {sorted(expected)}"
        )


def _require_valid_slots(slots: tuple[tuple[int, str], ...]) -> None:
    slot_numbers = [slot for slot, _ in slots]
    if sorted(slot_numbers) != list(range(1, MIN_SESSIONS + 1)):
        raise ManifestValidationError(
            f"slots must declare exactly the contiguous range 1..{MIN_SESSIONS}: "
            f"got {sorted(slot_numbers)}"
        )
    repository_ids = {repository_id for _, repository_id in slots}
    if not all(_is_hex64(repository_id) for repository_id in repository_ids):
        raise ManifestValidationError(
            "every slot's repository_id must be a 64-character hex digest"
        )
    if len(repository_ids) != MIN_REPOSITORIES:
        raise ManifestValidationError(
            f"slots must name exactly {MIN_REPOSITORIES} distinct repository IDs: "
            f"got {len(repository_ids)}"
        )


def _parse_slots(payload: dict[str, Any]) -> tuple[tuple[int, str], ...]:
    value = payload["slots"]
    if not isinstance(value, list):
        raise ManifestValidationError("slots must be a list")
    parsed: list[tuple[int, str]] = []
    for index, entry in enumerate(value):
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or isinstance(entry[0], bool)
            or not isinstance(entry[0], int)
            or not isinstance(entry[1], str)
        ):
            raise ManifestValidationError(f"malformed slots entry at index {index}")
        parsed.append((entry[0], entry[1]))
    if len({slot for slot, _ in parsed}) != len(parsed):
        raise ManifestValidationError("slots must not repeat a slot number")
    return tuple(sorted(parsed))


def validate_manifest_json(payload: Any) -> CampaignManifest:
    """Raise :class:`ManifestValidationError` unless ``payload`` is a valid
    M18 manifest: its build-time constants reproduce this build's frozen
    contract exactly, and its campaign population (wheel hash, slots) has
    the required closed shape. Otherwise return it typed."""
    if not isinstance(payload, dict):
        raise ManifestValidationError("manifest must be a JSON object")
    extra_keys = set(payload) - ALLOWED_MANIFEST_KEYS
    if extra_keys:
        raise ManifestValidationError(f"unallowlisted manifest key(s): {sorted(extra_keys)}")
    missing_keys = ALLOWED_MANIFEST_KEYS - set(payload)
    if missing_keys:
        raise ManifestValidationError(f"missing manifest key(s): {sorted(missing_keys)}")

    schema_version = _require_int(payload, "schema_version")
    min_sessions = _require_int(payload, "min_sessions")
    min_repositories = _require_int(payload, "min_repositories")
    min_eligible_observations = _require_int(payload, "min_eligible_observations")
    eligible_omp_version = _require_str(payload, "eligible_omp_version")
    _require_scenario_set(payload, "pre_signoff_scenarios", PRE_SIGNOFF_SCENARIOS)
    _require_scenario_set(payload, "post_signoff_scenarios", POST_SIGNOFF_SCENARIOS)

    if schema_version != MANIFEST_SCHEMA_VERSION:
        raise ManifestValidationError(
            f"schema_version does not match the frozen M18 contract: "
            f"got {schema_version}, expected {MANIFEST_SCHEMA_VERSION}"
        )
    if min_sessions != MIN_SESSIONS:
        raise ManifestValidationError(
            f"min_sessions does not match the frozen M18 contract: "
            f"got {min_sessions}, expected {MIN_SESSIONS}"
        )
    if min_repositories != MIN_REPOSITORIES:
        raise ManifestValidationError(
            f"min_repositories does not match the frozen M18 contract: "
            f"got {min_repositories}, expected {MIN_REPOSITORIES}"
        )
    if min_eligible_observations != MIN_ELIGIBLE_OBSERVATIONS:
        raise ManifestValidationError(
            f"min_eligible_observations does not match the frozen M18 contract: "
            f"got {min_eligible_observations}, expected {MIN_ELIGIBLE_OBSERVATIONS}"
        )
    if eligible_omp_version != ELIGIBLE_OMP_VERSION:
        raise ManifestValidationError(
            f"eligible_omp_version does not match the frozen M18 contract: "
            f"got {eligible_omp_version!r}, expected {ELIGIBLE_OMP_VERSION!r}"
        )

    candidate_wheel_sha256 = _require_str(payload, "candidate_wheel_sha256")
    if not _is_hex64(candidate_wheel_sha256):
        raise ManifestValidationError("candidate_wheel_sha256 must be a 64-character hex digest")
    slots = _parse_slots(payload)
    _require_valid_slots(slots)

    return CampaignManifest(
        schema_version=schema_version,
        min_sessions=min_sessions,
        min_repositories=min_repositories,
        min_eligible_observations=min_eligible_observations,
        eligible_omp_version=eligible_omp_version,
        pre_signoff_scenarios=tuple(sorted(PRE_SIGNOFF_SCENARIOS)),
        post_signoff_scenarios=tuple(sorted(POST_SIGNOFF_SCENARIOS)),
        candidate_wheel_sha256=candidate_wheel_sha256,
        slots=slots,
    )
