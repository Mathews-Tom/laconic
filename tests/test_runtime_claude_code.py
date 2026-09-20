"""Claude Code transforming-hook contract: shape fidelity and fail-open."""

from __future__ import annotations

import io
import json
import multiprocessing
import os
import sqlite3
from pathlib import Path
from typing import Any

from laconic.runtime.claude_code import main, transform

_LONG_OUTPUT = "\n".join(f"line {index}: {'payload ' * 12}" for index in range(400))


def _bash_payload(tmp_path: Path, *, session: str, call: str, **extra: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "stdout": _LONG_OUTPUT,
        "stderr": "",
        "interrupted": False,
        "isImage": False,
        "noOutputExpected": False,
    }
    response.update(extra)
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "session_id": session,
        "tool_use_id": call,
        "cwd": str(tmp_path),
        "tool_input": {"command": "ls -R"},
        "tool_response": response,
    }


def _process_payload(
    working_directory: str,
    *,
    session: str,
    index: int,
    include_tool_use_id: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "session_id": session,
        "cwd": working_directory,
        "tool_input": {"command": f"printf {index}"},
        "tool_response": {
            "stdout": f"{_LONG_OUTPUT}\nunique callback {index}",
            "stderr": "",
            "interrupted": False,
            "isImage": False,
            "noOutputExpected": False,
            "futureField": index,
        },
    }
    if include_tool_use_id:
        payload["tool_use_id"] = f"toolu-{index}"
    return payload


def _transform_in_fresh_process(
    barrier: Any,
    results: Any,
    data_dir: str,
    working_directory: str,
    session: str,
    index: int,
    include_tool_use_id: bool,
) -> None:
    try:
        barrier.wait(timeout=10)
        result = transform(
            _process_payload(
                working_directory,
                session=session,
                index=index,
                include_tool_use_id=include_tool_use_id,
            ),
            data_dir=Path(data_dir),
        )
    except BaseException as error:
        results.put(("error", type(error).__name__))
    else:
        results.put(("result", result))


def _hold_session_lock(
    data_dir: str,
    session: str,
    acquired: Any,
    release: Any,
    crash: bool,
) -> None:
    from laconic.runtime.storage import RuntimeStorage

    with RuntimeStorage(Path(data_dir)).session_lock(session):
        acquired.set()
        if crash:
            os._exit(0)
        release.wait(timeout=10)


def _reference(result: dict[str, Any]) -> str:
    emitted = _updated(result)["stdout"]
    assert isinstance(emitted, str)
    return emitted.split("\n", 1)[0].removeprefix("[laconic ").split(" |", 1)[0]


def _run_concurrent_callbacks(
    tmp_path: Path,
    *,
    session: str,
    include_tool_use_id: bool,
    count: int = 8,
) -> tuple[Path, list[dict[str, Any]]]:
    context = multiprocessing.get_context("spawn")
    data_dir = tmp_path / "data"
    barrier = context.Barrier(count)
    results = context.Queue()
    processes = [
        context.Process(
            target=_transform_in_fresh_process,
            args=(
                barrier,
                results,
                str(data_dir),
                str(tmp_path),
                session,
                index,
                include_tool_use_id,
            ),
        )
        for index in range(count)
    ]
    for process in processes:
        process.start()
    try:
        received = [results.get(timeout=20) for _ in processes]
    finally:
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.kill()
                process.join()
    assert all(process.exitcode == 0 for process in processes)
    assert all(kind == "result" for kind, _ in received)
    transformed = [result for _, result in received]
    assert all(result is not None for result in transformed)
    return data_dir, [result for result in transformed if result is not None]


def _assert_concurrent_session(
    tmp_path: Path,
    *,
    session: str,
    include_tool_use_id: bool,
) -> None:
    from laconic.runtime.storage import RuntimeStorage

    data_dir, results = _run_concurrent_callbacks(
        tmp_path,
        session=session,
        include_tool_use_id=include_tool_use_id,
    )
    storage = RuntimeStorage(data_dir)
    references = [_reference(result) for result in results]
    assert len(references) == len(set(references)) == 8
    with storage.open_existing_ledger(session) as ledger:
        decisions = ledger.runtime_decisions()
    assert [decision.sequence for decision in decisions] == list(range(1, 9))
    assert len({decision.request_id for decision in decisions}) == 8
    assert {decision.candidate_reference for decision in decisions} == set(references)
    if include_tool_use_id:
        assert {decision.request_id for decision in decisions} == {
            f"toolu-{index}" for index in range(8)
        }
    else:
        assert {decision.request_id for decision in decisions} == {
            f"cc-{index}" for index in range(1, 9)
        }
    with sqlite3.connect(storage.ledger_path(session)) as database:
        observation_count = database.execute("SELECT count(*) FROM observations").fetchone()[0]
    assert observation_count == len(decisions) == 8
    expected = {
        _process_payload(
            str(tmp_path),
            session=session,
            index=index,
            include_tool_use_id=include_tool_use_id,
        )["tool_response"]["stdout"]
        for index in range(8)
    }
    assert {storage.expand(reference) for reference in references} == expected


def _updated(result: dict[str, Any]) -> dict[str, Any]:
    output = result["hookSpecificOutput"]["updatedToolOutput"]
    assert isinstance(output, dict)
    return output


def test_an_unrecognized_key_survives_the_replacement(tmp_path: Path) -> None:
    """Claude Code silently ignores a replacement that misses the tool's shape.

    There is no error and no signal back to the hook, so a replacement
    built from an assumed schema degrades into a silent no-op the moment
    Claude Code adds a field. Real transcripts already carry
    ``noOutputExpected`` and ``gitOperation``, neither of which appears in
    the published example. The adapter must copy the observed response and
    overwrite one field rather than construct a new object.
    """
    payload = _bash_payload(
        tmp_path,
        session="shapes",
        call="toolu_1",
        gitOperation={"pr": {"number": 311}},
        someFutureField=["unknown"],
    )
    original = payload["tool_response"]

    result = transform(payload, data_dir=tmp_path / "data")

    assert result is not None
    updated = _updated(result)
    assert sorted(updated) == sorted(original)
    assert all(updated[key] == original[key] for key in original if key != "stdout")
    assert len(updated["stdout"]) < len(original["stdout"])


def test_a_second_tool_call_in_the_same_session_still_transforms(tmp_path: Path) -> None:
    """Every tool call in a Claude Code session shares one ``session_id``.

    The ledger keys a decision on ``(session_id, request_id)``, so a
    constant request id transforms the first observation of a session and
    then fails open on every one after it -- the failure mode a
    single-payload test cannot see, because it only ever makes one call.
    """
    data_dir = tmp_path / "data"
    first = transform(_bash_payload(tmp_path, session="reuse", call="toolu_1"), data_dir=data_dir)
    second = transform(_bash_payload(tmp_path, session="reuse", call="toolu_2"), data_dir=data_dir)
    third = transform(_bash_payload(tmp_path, session="reuse", call="toolu_3"), data_dir=data_dir)

    assert first is not None
    assert second is not None
    assert third is not None


def test_the_transformed_output_is_exactly_recoverable(tmp_path: Path) -> None:
    """Replacement is only safe because the original is still retrievable."""
    from laconic.runtime.storage import RuntimeStorage

    payload = _bash_payload(tmp_path, session="recover", call="toolu_1")
    data_dir = tmp_path / "data"

    result = transform(payload, data_dir=data_dir)

    assert result is not None
    emitted = _updated(result)["stdout"]
    reference = emitted.split("\n", 1)[0]
    handle = reference.split("/", 1)[1].split(" ", 1)[0]
    with RuntimeStorage(data_dir).open_ledger("recover") as ledger:
        assert ledger.expand(handle) == _LONG_OUTPUT


def test_a_file_read_carries_its_tool_input_to_the_encoder(tmp_path: Path) -> None:
    """The file encoder reads its subject and span hints from ``tool_input``.

    Dropping it leaves the outliner unable to identify the language, which
    does not raise -- it silently produces a weaker encoding that loses the
    strictly-smaller comparison, so the tool result is never transformed.
    """
    source = "\n".join(f"def function_{index}():\n    return {index}" for index in range(300))
    file_fields: dict[str, Any] = {
        "content": source,
        "filePath": str(tmp_path / "module.py"),
        "numLines": source.count("\n") + 1,
        "startLine": 1,
        "totalLines": source.count("\n") + 1,
    }
    response: dict[str, Any] = {"type": "text", "file": file_fields}
    base = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "session_id": "reads",
        "cwd": str(tmp_path),
        "tool_response": response,
    }

    with_input = transform(
        {**base, "tool_use_id": "a", "tool_input": {"file_path": str(tmp_path / "module.py")}},
        data_dir=tmp_path / "with",
    )
    without_input = transform(
        {**base, "tool_use_id": "b", "tool_input": {}},
        data_dir=tmp_path / "without",
    )

    assert with_input is not None, "a real file read must transform"
    file_output = _updated(with_input)["file"]
    assert sorted(file_output) == sorted(file_fields)
    assert file_output["filePath"] == file_fields["filePath"]
    assert len(file_output["content"]) < len(source)
    # Strictly better, not merely no worse: a disjunction would still hold if
    # forwarding regressed and both paths produced the same encoding, which
    # is exactly the regression this test exists to catch.
    assert without_input is None, "without its subject the encoding must lose the size test"
    # `numLines` follows the replacement so the model is never told it
    # received more lines than it can see.
    assert file_output["numLines"] == file_output["content"].count("\n") + 1
    assert file_output["totalLines"] == file_fields["totalLines"]


def test_a_non_textual_read_is_left_alone(tmp_path: Path) -> None:
    """An image read carries no text to shrink and no string to round-trip."""
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "session_id": "image",
        "tool_use_id": "toolu_1",
        "cwd": str(tmp_path),
        "tool_input": {"file_path": str(tmp_path / "shot.png")},
        "tool_response": {"type": "image", "file": {"base64": "iVBORw0KGgo=", "type": "image/png"}},
    }

    assert transform(payload, data_dir=tmp_path / "data") is None


def test_a_malformed_payload_writes_nothing_and_exits_zero(tmp_path: Path) -> None:
    """Fail-open is the safety story: a crash costs compression, not correctness.

    Empty stdout leaves Claude Code's original tool result in place, and a
    zero exit keeps the hook's stderr out of the model's context.
    """
    stdout = io.StringIO()

    code = main(
        ["--data-dir", str(tmp_path / "data")],
        stdin=io.StringIO("not json at all"),
        stdout=stdout,
    )

    assert code == 0
    assert stdout.getvalue() == ""


def test_the_entrypoint_emits_one_hook_response_on_stdout(tmp_path: Path) -> None:
    stdout = io.StringIO()

    code = main(
        ["--data-dir", str(tmp_path / "data")],
        stdin=io.StringIO(json.dumps(_bash_payload(tmp_path, session="cli", call="toolu_1"))),
        stdout=stdout,
    )

    assert code == 0
    document = json.loads(stdout.getvalue())
    assert document["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "updatedToolOutput" in document["hookSpecificOutput"]


def test_an_unsupported_event_is_never_transformed(tmp_path: Path) -> None:
    payload = _bash_payload(tmp_path, session="pre", call="toolu_1")
    payload["hook_event_name"] = "PreToolUse"

    assert transform(payload, data_dir=tmp_path / "data") is None


def test_concurrent_fresh_callbacks_keep_same_session_decisions_distinct(tmp_path: Path) -> None:
    """Fresh Claude callback processes must uphold the ledger's writer invariant."""
    _assert_concurrent_session(tmp_path, session="concurrent", include_tool_use_id=True)


def test_concurrent_callbacks_without_tool_use_ids_mint_unique_fallbacks(tmp_path: Path) -> None:
    """Fallback request ids derive from the sequence selected under the session lock."""
    _assert_concurrent_session(tmp_path, session="fallback", include_tool_use_id=False)


def test_a_held_lock_fails_open_before_a_same_session_mutation(tmp_path: Path) -> None:
    """A bounded lock wait preserves the original output rather than queueing work."""
    from laconic.runtime.operator import runtime_storage_status
    from laconic.runtime.storage import RuntimeStorage

    context = multiprocessing.get_context("spawn")
    data_dir = tmp_path / "data"
    session = "timed-out"
    storage = RuntimeStorage(data_dir)
    sidecar = storage.lock_path(session)
    acquired = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_session_lock,
        args=(str(data_dir), session, acquired, release, False),
    )
    holder.start()
    assert acquired.wait(timeout=10)

    stdout = io.StringIO()
    code = main(
        ["--data-dir", str(data_dir)],
        stdin=io.StringIO(
            json.dumps(
                _process_payload(
                    str(tmp_path),
                    session=session,
                    index=0,
                    include_tool_use_id=True,
                )
            )
        ),
        stdout=stdout,
    )

    assert code == 0
    assert stdout.getvalue() == ""
    status = runtime_storage_status(data_dir)
    assert (
        status.sessions,
        status.eligible_observations,
        status.compressed_observations,
    ) == (0, 0, 0)
    release.set()
    holder.join(timeout=10)
    assert holder.exitcode == 0
    assert (
        transform(
            _process_payload(
                str(tmp_path),
                session=session,
                index=1,
                include_tool_use_id=True,
            ),
            data_dir=data_dir,
        )
        is not None
    )
    assert sidecar.exists()


def test_killed_lock_holder_releases_the_persistent_sidecar(tmp_path: Path) -> None:
    """Kernel descriptor cleanup lets the next callback proceed without unlinking."""
    from laconic.runtime.storage import RuntimeStorage

    context = multiprocessing.get_context("spawn")
    data_dir = tmp_path / "data"
    session = "crash-release"
    acquired = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_session_lock,
        args=(str(data_dir), session, acquired, release, True),
    )
    holder.start()
    assert acquired.wait(timeout=10)
    holder.join(timeout=10)
    assert holder.exitcode == 0
    sidecar = RuntimeStorage(data_dir).lock_path(session)
    inode = sidecar.stat().st_ino

    assert (
        transform(
            _process_payload(
                str(tmp_path),
                session=session,
                index=0,
                include_tool_use_id=True,
            ),
            data_dir=data_dir,
        )
        is not None
    )
    assert sidecar.stat().st_ino == inode


def test_different_session_transforms_progress_while_a_lock_is_held(tmp_path: Path) -> None:
    """One busy session must not serialize unrelated Claude callback state."""
    from laconic.runtime.storage import RuntimeStorage

    context = multiprocessing.get_context("spawn")
    data_dir = tmp_path / "data"
    acquired = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_session_lock,
        args=(str(data_dir), "session-a", acquired, release, False),
    )
    holder.start()
    assert acquired.wait(timeout=10)

    payload = _process_payload(
        str(tmp_path),
        session="session-b",
        index=0,
        include_tool_use_id=True,
    )
    result = transform(payload, data_dir=data_dir)

    assert result is not None
    assert RuntimeStorage(data_dir).expand(_reference(result)) == payload["tool_response"]["stdout"]
    release.set()
    holder.join(timeout=10)
    assert holder.exitcode == 0


def test_invalid_session_id_fails_open_without_a_sidecar_or_diagnostic_leak(
    tmp_path: Path,
    capsys: Any,
) -> None:
    raw_session_id = "invalid/session"
    data_dir = tmp_path / "data"
    payload = _process_payload(
        str(tmp_path),
        session=raw_session_id,
        index=0,
        include_tool_use_id=True,
    )
    stdout = io.StringIO()

    code = main(
        ["--data-dir", str(data_dir)],
        stdin=io.StringIO(json.dumps(payload)),
        stdout=stdout,
    )

    assert code == 0
    assert stdout.getvalue() == ""
    assert raw_session_id not in capsys.readouterr().err
    assert not data_dir.exists()
