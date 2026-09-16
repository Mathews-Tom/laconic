"""Closed offline safety audit shared by Pages generation and drift verification."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from laconic.pages.demo import build_demo
from laconic.pages.evidence import PagesEvidence
from laconic.pages.site import SITE_FILES

_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.I,
)
_ULID = re.compile(r"\b[0-7][0-9A-HJKMNP-TV-Z]{25}\b", re.I)
_CREDENTIAL = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,})\b")
_HEX_64 = re.compile(r"\b[0-9a-f]{64}\b", re.I)
_PRIVATE_PATH = re.compile(r"(?:/Users/|/home/|/private/var/folders/|[A-Za-z]:\\\\Users\\\\)")
_ALLOWED_EXTERNAL = (
    "https://github.com/Mathews-Tom/laconic",
    "https://github.com/Mathews-Tom/laconic#installation",
)


class VerificationError(ValueError):
    """Raised when Pages output violates its closed public contract."""


class _PageAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[tuple[str, str]] = []
        self.ids: set[str] = set()
        self.landmarks: set[str] = set()
        self.headings: list[int] = []
        self.h1_count = 0
        self.tables = 0
        self.captions = 0
        self.scripts = 0
        self.external_resources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value for key, value in attrs}
        identifier = values.get("id")
        if identifier:
            self.ids.add(identifier)
        if tag in {"header", "nav", "main", "footer"}:
            self.landmarks.add(tag)
        if re.fullmatch(r"h[1-6]", tag):
            level = int(tag[1])
            self.headings.append(level)
            if level == 1:
                self.h1_count += 1
        if tag == "table":
            self.tables += 1
        elif tag == "caption":
            self.captions += 1
        elif tag == "script":
            self.scripts += 1
        for attribute in ("href", "src"):
            value = values.get(attribute)
            if value is None:
                continue
            self.hrefs.append((tag, value))
            split = urlsplit(value)
            if split.scheme in {"http", "https"} and tag != "a":
                self.external_resources.append(value)


def site_files(root: Path) -> tuple[str, ...]:
    return tuple(sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()))


def _assert_heading_order(path: Path, headings: Sequence[int]) -> None:
    if not headings or headings[0] != 1:
        raise VerificationError(f"{path}: heading order must begin at h1")
    for previous, current in zip(headings, headings[1:], strict=False):
        if current > previous + 1:
            raise VerificationError(f"{path}: heading level jumps from h{previous} to h{current}")


def _resolve_link(site: Path, page: Path, value: str, ids: set[str]) -> None:
    split = urlsplit(value)
    if split.scheme in {"http", "https"}:
        if value not in _ALLOWED_EXTERNAL:
            raise VerificationError(f"{page}: unapproved external link {value}")
        return
    if split.scheme or split.netloc or value.startswith("/"):
        raise VerificationError(f"{page}: link is not project-relative: {value}")
    if not split.path:
        if split.fragment and split.fragment not in ids:
            raise VerificationError(f"{page}: missing local fragment #{split.fragment}")
        return
    target = (page.parent / unquote(split.path)).resolve()
    try:
        target.relative_to(site.resolve())
    except ValueError as error:
        raise VerificationError(f"{page}: link escapes the site: {value}") from error
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        raise VerificationError(f"{page}: unresolved link {value}")
    if split.fragment and target == page.resolve() and split.fragment not in ids:
        raise VerificationError(f"{page}: missing local fragment #{split.fragment}")


def _audit_html(site: Path, path: Path, digest: str) -> None:
    text = path.read_text(encoding="utf-8")
    audit = _PageAudit()
    audit.feed(text)
    if audit.scripts:
        raise VerificationError(f"{path}: browser JavaScript is forbidden")
    if audit.external_resources:
        raise VerificationError(f"{path}: external runtime resources are forbidden")
    if audit.h1_count != 1:
        raise VerificationError(f"{path}: expected exactly one h1")
    if not {"header", "nav", "main", "footer"}.issubset(audit.landmarks):
        raise VerificationError(f"{path}: semantic landmarks are incomplete")
    _assert_heading_order(path, audit.headings)
    if audit.tables != audit.captions:
        raise VerificationError(f"{path}: every table requires a caption")
    if digest not in text:
        raise VerificationError(f"{path}: canonical JSON digest is absent")
    if "Observed" not in text or "modelled_not_measured" not in text:
        raise VerificationError(f"{path}: observed/modelled labels are incomplete")
    for _tag, value in audit.hrefs:
        _resolve_link(site, path, value, audit.ids)


def _audit_svg(path: Path, digest: str) -> None:
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ET.ParseError) as error:
        raise VerificationError("diagram SVG is invalid") from error
    namespace = "{http://www.w3.org/2000/svg}"
    if root.tag != f"{namespace}svg":
        raise VerificationError("diagram root is not SVG")
    if root.find(f"{namespace}title") is None or root.find(f"{namespace}desc") is None:
        raise VerificationError("diagram requires title and description")
    if root.attrib.get("role") != "img" or not root.attrib.get("aria-labelledby"):
        raise VerificationError("diagram requires an accessible image label")
    if root.attrib.get("data-json-sha256") != digest:
        raise VerificationError("diagram digest does not match canonical JSON")
    for element in root.iter():
        for key, value in element.attrib.items():
            if key.endswith("href") and urlsplit(value).scheme:
                raise VerificationError("diagram contains an external reference")


def _privacy_scan(
    site: Path,
    *,
    allowed_hashes: set[str],
    forbidden_markers: Sequence[str],
) -> None:
    for path in site.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if _PRIVATE_PATH.search(text):
            raise VerificationError(f"{path}: private filesystem path detected")
        if _UUID.search(text):
            raise VerificationError(f"{path}: UUID-shaped session identifier detected")
        if _ULID.search(text):
            raise VerificationError(f"{path}: ULID-shaped session identifier detected")
        if _CREDENTIAL.search(text):
            raise VerificationError(f"{path}: credential-shaped value detected")
        for marker in forbidden_markers:
            if marker and marker in text:
                raise VerificationError(f"{path}: forbidden private marker detected")
        unexpected = {value.lower() for value in _HEX_64.findall(text)} - allowed_hashes
        if unexpected:
            raise VerificationError(f"{path}: unexpected 64-hex identifier detected")


def audit_site(
    evidence: PagesEvidence,
    site: Path,
    *,
    forbidden_markers: Sequence[str] = (),
) -> tuple[str, ...]:
    """Audit a rendered tree without mutating or regenerating it."""

    actual_files = site_files(site)
    if actual_files != SITE_FILES:
        raise VerificationError("site file set differs from the closed allowlist")
    public_json = site / "evidence" / "laconic-development.json"
    if public_json.read_text(encoding="utf-8") != evidence.to_json():
        raise VerificationError("site canonical JSON differs from the validated source")
    digest = evidence.sha256
    for relative in ("index.html", "evidence/index.html", "methodology/index.html"):
        _audit_html(site, (site / relative).resolve(), digest)
    _audit_svg(site / "assets" / "diagram.svg", digest)
    css = (site / "assets" / "site.css").read_text(encoding="utf-8")
    if digest not in css or "@media(max-width:390px)" not in css:
        raise VerificationError("stylesheet lacks digest or required mobile breakpoint")
    payload = evidence.payload
    allowed_hashes = {
        digest.lower(),
        payload["manifest_sha256"].lower(),
        payload["source_inventory_sha256"].lower(),
        build_demo().recovery_sha256.lower(),
    }
    _privacy_scan(
        site,
        allowed_hashes=allowed_hashes,
        forbidden_markers=forbidden_markers,
    )
    return actual_files
