"""Failure-path evidence and resource cleanup for the live proof runner."""

import argparse
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts.validation import r8_live_proof as proof


@pytest.mark.parametrize(
    "failure_at", ["stop", "event", "summary", "excerpts", "flush", "dispose"]
)
async def test_cleanup_attempts_flush_and_dispose_preserving_primary_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure_at: str
) -> None:
    primary = RuntimeError("scenario failed")
    cleanup_error = OSError("cleanup failed")
    recorder = proof.Recorder(tmp_path)
    fleet = MagicMock()
    fleet.summary.return_value = []
    fleet.excerpts.return_value = "worker log"
    engine = MagicMock()
    engine.dispose = AsyncMock()
    flush = MagicMock(wraps=recorder.flush)
    monkeypatch.setattr(recorder, "flush", flush)
    if failure_at == "stop":
        fleet.stop_all.side_effect = cleanup_error
    elif failure_at == "event":
        monkeypatch.setattr(recorder, "event", MagicMock(side_effect=cleanup_error))
    elif failure_at == "summary":
        fleet.summary.side_effect = cleanup_error
    elif failure_at == "excerpts":
        fleet.excerpts.side_effect = cleanup_error
    elif failure_at == "flush":
        flush.side_effect = cleanup_error
    elif failure_at == "dispose":
        engine.dispose.side_effect = cleanup_error
    monkeypatch.setattr(proof, "Recorder", lambda _out: recorder)
    monkeypatch.setattr(proof, "Fleet", lambda _scratch: fleet)
    monkeypatch.setattr(proof, "create_async_engine", lambda _url: engine)
    monkeypatch.setattr(proof, "async_sessionmaker", MagicMock())
    monkeypatch.setattr(proof, "init_field_encryption", lambda: None)
    monkeypatch.setattr(proof, "pick_fixtures", AsyncMock(side_effect=primary))
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused")

    with pytest.raises(RuntimeError) as caught:
        await proof.run(argparse.Namespace(out=str(tmp_path), scratch=str(tmp_path)))

    assert caught.value is primary
    flush.assert_called_once()
    engine.dispose.assert_awaited_once()
    fleet.summary.assert_called_once()
    fleet.excerpts.assert_called_once()
    if failure_at != "flush":
        assert json.loads((tmp_path / "checks.json").read_text()) == []
    if failure_at != "excerpts":
        assert (tmp_path / "log-excerpts.txt").read_text() == "worker log"
    if failure_at == "stop":
        assert not recorder.timeline


async def test_cleanup_failure_without_primary_error_is_reported_after_disposal(
    tmp_path: Path,
) -> None:
    fleet = MagicMock()
    failure = OSError("children could not stop")
    fleet.stop_all.side_effect = failure
    fleet.summary.return_value = []
    fleet.excerpts.return_value = "worker log"
    recorder = proof.Recorder(tmp_path)
    engine = MagicMock()
    engine.dispose = AsyncMock()

    with pytest.raises(OSError) as caught:
        await proof.finalize_proof(fleet, recorder, tmp_path, engine, None)

    assert caught.value is failure
    assert json.loads((tmp_path / "checks.json").read_text()) == []
    engine.dispose.assert_awaited_once()


async def test_successful_cleanup_writes_evidence(tmp_path: Path) -> None:
    fleet = proof.Fleet(tmp_path)
    recorder = proof.Recorder(tmp_path)
    recorder.check("scenario", True)
    engine = MagicMock()
    engine.dispose = AsyncMock()

    await proof.finalize_proof(fleet, recorder, tmp_path, engine, None)

    assert json.loads((tmp_path / "checks.json").read_text())[0]["ok"] is True
    assert json.loads((tmp_path / "timeline.json").read_text())[0]["event"] == (
        "all_children_stopped"
    )
    assert json.loads((tmp_path / "log-summary.json").read_text()) == {}
    engine.dispose.assert_awaited_once()
