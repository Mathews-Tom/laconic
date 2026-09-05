"""Tests for optional narration-provider configuration."""

from __future__ import annotations

import json
from collections.abc import Callable
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

import laconic.cli as cli_module
import laconic.render.narrate as narrate_module
from laconic.cli import EXIT_NARRATION_RESPONSE, main
from laconic.codec.observe import ObservationCodec
from laconic.ledger import Ledger, ObservationKind
from laconic.render.narrate import (
    Narration,
    NarrationConfig,
    NarrationConfigurationError,
    NarrationUnavailableError,
    NoneProvider,
    provider_for,
)
from laconic.render.templates import render_narration
from laconic.render.view import FixtureLedger, assemble


def test_none_provider_returns_no_narration(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db", "narrate") as ledger:
        ledger.register(ObservationKind.FILE, "a.py", "alpha", "a.py", turn=0)
        entries = assemble(ledger, 1, 1)

    assert NoneProvider().narrate(entries) is None


def test_default_configuration_selects_none_provider() -> None:
    assert isinstance(provider_for(NarrationConfig()), NoneProvider)


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: NarrationConfig(provider="ollama"), "requires an endpoint"),
        (
            lambda: NarrationConfig(provider="ollama", endpoint="http://127.0.0.1"),
            "requires a model",
        ),
        (lambda: NarrationConfig(provider="none", model="local"), "does not accept"),
        (
            lambda: NarrationConfig(
                provider="ollama",
                endpoint="localhost",
                model="local",
            ),
            "requires an absolute HTTP",
        ),
        (
            lambda: NarrationConfig(
                provider="ollama",
                endpoint="http://[::1",
                model="local",
            ),
            "requires an absolute HTTP",
        ),
        (lambda: NarrationConfig(timeout_seconds=0), "must be positive"),
    ],
)
def test_configuration_rejects_incomplete_or_invalid_values(
    build: Callable[[], NarrationConfig], message: str
) -> None:
    with pytest.raises(NarrationConfigurationError, match=message):
        build()


def test_ollama_provider_sends_structural_metadata_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Ledger(tmp_path / "ledger.db", "narrate") as ledger:
        ledger.register(ObservationKind.FILE, "a.py", "secret raw payload", "a.py", turn=0)
        entries = assemble(ledger, 1, 1)

    class HttpResponse:
        def __enter__(self) -> HttpResponse:
            return self

        def __exit__(
            self,
            exception_type: type[BaseException] | None,
            exception: BaseException | None,
            traceback: object,
        ) -> None:
            return None

        def read(self) -> bytes:
            return b'{"response": "The trace proceeds through a single observation."}'

    received: list[bytes] = []

    def respond(request: object, timeout: float) -> HttpResponse:
        assert isinstance(request, narrate_module.Request)
        assert timeout == 5.0
        assert request.data is not None
        received.append(request.data)
        return HttpResponse()

    monkeypatch.setattr(narrate_module, "urlopen", respond)
    narration = provider_for(
        NarrationConfig(
            provider="ollama",
            endpoint="http://127.0.0.1",
            model="local",
        )
    ).narrate(entries)

    assert narration is not None
    assert narration.text == "The trace proceeds through a single observation."
    assert narration.source_handles == ("F1",)
    prompt = json.loads(received[0])["prompt"]
    assert "a.py" in prompt
    assert "secret raw payload" not in prompt


def test_unreachable_provider_degrades_the_view(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(request: object, timeout: float) -> None:
        raise URLError("connection refused")

    monkeypatch.setattr(narrate_module, "urlopen", unavailable)
    corpus = Path(__file__).parent / "corpus"
    deterministic_args = [
        "research",
        "view",
        "--turns",
        "1-5",
        "--corpus",
        str(corpus),
        "--provider",
        "none",
    ]
    provider_args = [
        "research",
        "view",
        "--turns",
        "1-5",
        "--corpus",
        str(corpus),
        "--provider",
        "ollama",
        "--provider-endpoint",
        "http://127.0.0.1",
        "--provider-model",
        "local",
    ]

    assert main(deterministic_args) == 0
    deterministic = capsys.readouterr().out
    assert main(provider_args) == 0
    provider_result = capsys.readouterr()

    assert provider_result.out == deterministic
    assert "showing deterministic output" in provider_result.err


def test_deterministic_only_bypasses_optional_provider_configuration(
    capsys: pytest.CaptureFixture[str],
) -> None:
    corpus = Path(__file__).parent / "corpus"

    assert (
        main(
            [
                "research",
                "view",
                "--turns",
                "1-5",
                "--corpus",
                str(corpus),
                "--deterministic-only",
                "--provider",
                "ollama",
            ]
        )
        == 0
    )

    result = capsys.readouterr()
    assert result.out
    assert "deterministic-only mode" in result.err
    assert "invalid narration provider" not in result.err


def test_http_exception_degrades_the_view(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class HttpResponse:
        def __enter__(self) -> HttpResponse:
            return self

        def __exit__(
            self,
            exception_type: type[BaseException] | None,
            exception: BaseException | None,
            traceback: object,
        ) -> None:
            return None

        def read(self) -> bytes:
            raise IncompleteRead(b"partial", 1)

    def respond(request: object, timeout: float) -> HttpResponse:
        return HttpResponse()

    monkeypatch.setattr(narrate_module, "urlopen", respond)
    corpus = Path(__file__).parent / "corpus"
    assert (
        main(
            [
                "research",
                "view",
                "--turns",
                "1-5",
                "--corpus",
                str(corpus),
                "--provider",
                "ollama",
                "--provider-endpoint",
                "http://127.0.0.1",
                "--provider-model",
                "local",
            ]
        )
        == 0
    )

    result = capsys.readouterr()
    assert result.out
    assert "showing deterministic output" in result.err


def test_malformed_provider_response_fails_with_a_dedicated_exit_code(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class HttpResponse:
        def __enter__(self) -> HttpResponse:
            return self

        def __exit__(
            self,
            exception_type: type[BaseException] | None,
            exception: BaseException | None,
            traceback: object,
        ) -> None:
            return None

        def read(self) -> bytes:
            return b"not json"

    def respond(request: object, timeout: float) -> HttpResponse:
        return HttpResponse()

    monkeypatch.setattr(narrate_module, "urlopen", respond)
    corpus = Path(__file__).parent / "corpus"
    assert (
        main(
            [
                "research",
                "view",
                "--turns",
                "1-5",
                "--corpus",
                str(corpus),
                "--provider",
                "ollama",
                "--provider-endpoint",
                "http://127.0.0.1",
                "--provider-model",
                "local",
            ]
        )
        == EXIT_NARRATION_RESPONSE
    )

    result = capsys.readouterr()
    assert result.out
    assert "invalid narration response" in result.err
    assert "Traceback" not in result.err


def test_server_error_degrades_without_printing_provider_control_bytes(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(request: object, timeout: float) -> None:
        raise HTTPError(
            "http://127.0.0.1",
            500,
            "\x1b[5A\x1b[2Kforged resolved fact",
            None,
            None,
        )

    monkeypatch.setattr(narrate_module, "urlopen", unavailable)
    corpus = Path(__file__).parent / "corpus"
    assert (
        main(
            [
                "research",
                "view",
                "--turns",
                "1-5",
                "--corpus",
                str(corpus),
                "--provider",
                "ollama",
                "--provider-endpoint",
                "http://127.0.0.1",
                "--provider-model",
                "local",
            ]
        )
        == 0
    )

    result = capsys.readouterr()
    assert result.out
    assert "\x1b" not in result.err
    assert "HTTPError" in result.err
    assert "forged resolved fact" not in result.err
    assert "showing deterministic output" in result.err


def test_client_error_fails_loudly_without_provider_reason(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def rejected(request: object, timeout: float) -> None:
        raise HTTPError("http://127.0.0.1", 404, "unknown model", None, None)

    monkeypatch.setattr(narrate_module, "urlopen", rejected)
    corpus = Path(__file__).parent / "corpus"
    assert (
        main(
            [
                "research",
                "view",
                "--turns",
                "1-5",
                "--corpus",
                str(corpus),
                "--provider",
                "ollama",
                "--provider-endpoint",
                "http://127.0.0.1",
                "--provider-model",
                "local",
            ]
        )
        == EXIT_NARRATION_RESPONSE
    )

    result = capsys.readouterr()
    assert result.out
    assert "HTTP 404" in result.err
    assert "unknown model" not in result.err


def test_unavailable_provider_is_distinct_from_invalid_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Ledger(tmp_path / "ledger.db", "narrate") as ledger:
        ledger.register(ObservationKind.FILE, "a.py", "alpha", "a.py", turn=0)
        entries = assemble(ledger, 1, 1)

    def unavailable(request: object, timeout: float) -> None:
        raise URLError("connection refused")

    monkeypatch.setattr(narrate_module, "urlopen", unavailable)
    provider = provider_for(
        NarrationConfig(
            provider="ollama",
            endpoint="http://127.0.0.1",
            model="local",
        )
    )

    with pytest.raises(NarrationUnavailableError, match="is unavailable"):
        provider.narrate(entries)


def test_generated_narration_is_visually_distinct_and_control_safe() -> None:
    rendered = render_narration(
        Narration(
            text="The work moves to verification.\x1b[1A\nNo outcome is asserted.",
            source_handles=("F1", "B2"),
        )
    )

    assert rendered.splitlines() == [
        "--- generated narration (not resolved facts; sources: [F1] [B2]) ---",
        "> The work moves to verification.\\u001b[1A",
        "> No outcome is asserted.",
        "--- end generated narration ---",
    ]
    assert "\x1b" not in rendered


def test_view_narration_never_writes_ledger_or_agent_context(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class HttpResponse:
        def __enter__(self) -> HttpResponse:
            return self

        def __exit__(
            self,
            exception_type: type[BaseException] | None,
            exception: BaseException | None,
            traceback: object,
        ) -> None:
            return None

        def read(self) -> bytes:
            return b'{"response": "A generated connective sentence."}'

    def respond(request: object, timeout: float) -> HttpResponse:
        return HttpResponse()

    original_loader = cli_module.load_fixture_ledger

    def load_with_post_import_guards(paths: list[Path]) -> FixtureLedger:
        fixture = original_loader(paths)

        def reject_encode(
            self: ObservationCodec,
            tool_name: str,
            subject: str,
            raw: str,
            tool_input: object,
            *,
            turn: int,
        ) -> None:
            raise AssertionError("narration attempted to enter the agent context")

        def reject_register(self: Ledger, *args: object, **kwargs: object) -> None:
            raise AssertionError("narration attempted to mutate the ledger")

        monkeypatch.setattr(ObservationCodec, "encode", reject_encode)
        monkeypatch.setattr(Ledger, "register", reject_register)
        return fixture

    monkeypatch.setattr(narrate_module, "urlopen", respond)
    monkeypatch.setattr(cli_module, "load_fixture_ledger", load_with_post_import_guards)
    corpus = Path(__file__).parent / "corpus"
    assert (
        main(
            [
                "research",
                "view",
                "--turns",
                "1-5",
                "--corpus",
                str(corpus),
                "--provider",
                "ollama",
                "--provider-endpoint",
                "http://127.0.0.1",
                "--provider-model",
                "local",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "--- generated narration (not resolved facts; " in output
    assert "> A generated connective sentence." in output
