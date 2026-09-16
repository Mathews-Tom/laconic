"""Frozen, read-only MCP opportunity census.

This research surface measures whether the already-shipped fallback codec has
enough local MCP headroom to justify a separate runtime milestone. It never
changes host configuration, runtime behavior, or source transcripts.
"""

from laconic.mcp_census.cli import DEFAULT_OUTPUT_DIR, CensusArtifacts, execute_census
from laconic.mcp_census.manifest import CensusManifest, freeze_manifest, load_manifest
from laconic.mcp_census.report import OpportunityReport, load_report, validate_disposition

__all__ = [
    "DEFAULT_OUTPUT_DIR",
    "CensusArtifacts",
    "CensusManifest",
    "OpportunityReport",
    "execute_census",
    "freeze_manifest",
    "load_manifest",
    "load_report",
    "validate_disposition",
]
