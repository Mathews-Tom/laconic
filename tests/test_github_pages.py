"""The public product site is deterministic, recoverable, and privacy-safe."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from laconic.pages.demo import build_demo
from laconic.pages.evidence import PagesEvidence
from laconic.pages.site import SITE_FILES, SiteError, load_evidence, render_site
from laconic.pages.verify import VerificationError, verify_site

FIXTURE = Path("tests/fixtures/pages/evidence.json")


def _file_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def test_demo_invokes_real_codec_and_proves_full_and_ranged_recovery() -> None:
    first = build_demo()
    second = build_demo()

    assert first == second
    assert first.visible_chars == len(first.envelope)
    assert first.visible_chars < first.raw_chars
    assert first.reference == "pages-demo/F1"
    assert first.range_reference == "pages-demo/F1:1-12"
    assert "laconic_expand" in first.envelope
    assert "outline:" in first.outline
    assert first.range_preview.startswith('"""A deterministic recovery ledger')


def test_rendered_site_is_byte_deterministic_and_verifies_offline(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_result = render_site(FIXTURE, first)
    second_result = render_site(FIXTURE, second)

    assert first_result == second_result
    assert first_result.files == SITE_FILES
    assert _file_bytes(first) == _file_bytes(second)
    assert verify_site(FIXTURE, first) == {
        "status": "verified",
        "json_sha256": first_result.json_sha256,
        "files": list(SITE_FILES),
        "provider_calls": 0,
    }


def test_routes_render_canonical_observed_modelled_and_provenance_values(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    result = render_site(FIXTURE, site)
    home = (site / "index.html").read_text()
    evidence = (site / "evidence" / "index.html").read_text()
    methodology = (site / "methodology" / "index.html").read_text()

    assert home.index("Smaller tool results") < home.index("Observed")
    assert home.index("Deterministic transformation") < home.index("Observed")
    assert "6,000" in home
    assert "60.00%" in home
    assert "$1.00–$2.00" in home
    assert "modelled_not_measured" in home
    assert "Single-arm model with no counterfactual" in home
    assert "10,000" in evidence and "4,000" in evidence
    assert "80.00%" in evidence
    assert "m23-pages-v1" in evidence
    assert result.json_sha256 in home
    assert result.json_sha256 in evidence
    assert result.json_sha256 in methodology
    assert "observed_characters_are_not_tokens_or_dollars" in evidence
    assert "GitHub Actions never performs extraction" in methodology


def test_withheld_estimate_renders_reason_without_dollar_range(tmp_path: Path) -> None:
    payload = load_evidence(FIXTURE).payload
    payload["estimate"] = {
        "basis": "modelled_not_measured",
        "status": "withheld",
        "reason": "fallback_price_share_above_25_percent",
        "denominator": payload["estimate"]["denominator"],
        "fallback_priced_cost_share_pct": 26.0,
    }
    evidence_path = tmp_path / "withheld.json"
    evidence_path.write_text(PagesEvidence(payload).to_json())
    site = tmp_path / "site"

    render_site(evidence_path, site)

    evidence_html = (site / "evidence" / "index.html").read_text()
    assert "Withheld" in evidence_html
    assert "fallback price share above 25 percent" in evidence_html
    assert "$1.00–$2.00" not in evidence_html
    assert "modelled_not_measured" in evidence_html


def test_invalid_or_duplicate_evidence_never_replaces_prior_site(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    marker = site / "prior.txt"
    marker.write_text("prior site\n")
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"provider_calls":0,"provider_calls":0}\n')

    with pytest.raises(SiteError):
        render_site(invalid, site)

    assert marker.read_text() == "prior site\n"
    assert _file_bytes(site) == {"prior.txt": b"prior site\n"}


def test_staged_audit_failure_preserves_prior_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from laconic.pages import site as module

    site = tmp_path / "site"
    site.mkdir()
    marker = site / "prior.txt"
    marker.write_text("prior site\n")
    original = module._homepage
    monkeypatch.setattr(
        module,
        "_homepage",
        lambda evidence, demo: original(evidence, demo).replace(
            "</main>", "<script>unsafe()</script></main>"
        ),
    )

    with pytest.raises(SiteError, match="safety audit"):
        render_site(FIXTURE, site)

    assert _file_bytes(site) == {"prior.txt": b"prior site\n"}


def test_symlink_destination_is_rejected_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    marker = target / "owned.txt"
    marker.write_text("owned\n")
    site = tmp_path / "site"
    site.symlink_to(target, target_is_directory=True)

    with pytest.raises(SiteError, match="must not be a symlink"):
        render_site(FIXTURE, site)

    assert site.is_symlink()
    assert marker.read_text() == "owned\n"


def test_backup_cleanup_failure_is_reported_without_failing_after_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from laconic.pages import site as module

    site = tmp_path / "site"
    render_site(FIXTURE, site)
    original_rmtree = module.shutil.rmtree

    def fail_backup(path: Path) -> None:
        if path.name == ".site.previous":
            raise OSError("simulated cleanup failure")
        original_rmtree(path)

    monkeypatch.setattr(module.shutil, "rmtree", fail_backup)

    result = render_site(FIXTURE, site)

    assert result.backup_cleaned is False
    assert (site / "index.html").is_file()
    assert (tmp_path / ".site.previous").is_dir()


def test_verifier_rejects_generated_drift_and_private_markers(tmp_path: Path) -> None:
    site = tmp_path / "site"
    render_site(FIXTURE, site)
    page = site / "index.html"
    page.write_text(page.read_text().replace("Smaller tool results", "Changed headline", 1))

    with pytest.raises(VerificationError, match="generated drift"):
        verify_site(FIXTURE, site)

    render_site(FIXTURE, site)
    with pytest.raises(VerificationError, match="forbidden private marker"):
        verify_site(FIXTURE, site, forbidden_markers=("Exact recovery",))


def test_generated_surface_has_no_scripts_external_assets_or_unexpected_identifiers(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    result = render_site(FIXTURE, site)
    verify_site(FIXTURE, site)

    combined = "\n".join(path.read_text() for path in site.rglob("*") if path.is_file())
    assert "<script" not in combined.lower()
    assert "@import" not in combined.lower()
    assert "url(http" not in combined.lower()
    assert "/Users/" not in combined
    assert "/home/" not in combined
    assert "PRIVATE CONTENT" not in combined
    assert result.json_sha256 in combined
    assert (
        json.loads((site / "evidence" / "laconic-development.json").read_text())["provider_calls"]
        == 0
    )
