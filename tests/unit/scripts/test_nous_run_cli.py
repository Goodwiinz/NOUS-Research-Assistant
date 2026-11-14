# mypy: disable-error-code=no-untyped-def

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import nous_run
from scripts.nous.backend_git import SchemaMigration
from scripts.nous.coordination import Claim, ValidationError
from scripts.nous.gitio import TransportFailure
from scripts.nous.preflight import load_runtime_config
from scripts.nous_run import build_parser

RUN_ID = "20260827T040000Z-agent-9f3a1c"
CLAIM_ID = "c-a1b2c3d4e5f6"
HEAD = "23c3551a30ac5d230f68a01b76650c27144571f5"


def test_runtime_config_requires_explicit_machine_and_uses_planned_defaults(tmp_path):
    config = load_runtime_config(
        {"NOUS_MACHINE_ID": "mac-nous", "LOOP_BRIDGE_DIR": str(tmp_path / "bridge")},
        repo_root=tmp_path / "rag",
    )
    assert config.coord_mode == "local"
    assert config.git_remote == "origin"
    assert config.coordination_branch == "nous-coordination"
    assert config.github_repository == "goodwiins/NOUS-Research-Assistant"

    with pytest.raises(ValidationError):
        load_runtime_config({}, repo_root=tmp_path / "rag")


def test_cli_exposes_only_coordination_milestone_commands():
    parser = build_parser()
    subcommands = next(
        action.choices
        for action in parser._actions
        if hasattr(action, "choices") and action.choices
    )
    assert set(subcommands) == {
        "bootstrap",
        "cutover",
        "claim",
        "renew",
        "release",
        "rollback",
        "list",
        "check",
        "migrate",
    }


def test_migrate_cli_requires_coordinate_and_remote_required(monkeypatch, tmp_path):
    config = SimpleNamespace(coord_mode="local")
    monkeypatch.setattr(nous_run, "_config", lambda _repo: config)
    missing_auth = build_parser().parse_args(["migrate", "--to-schema", "2"])
    with pytest.raises(ValidationError, match="authorization"):
        nous_run._dispatch(missing_auth, tmp_path)
    authorized = build_parser().parse_args(
        ["migrate", "--to-schema", "2", "--authorize", "coordinate"]
    )
    with pytest.raises(ValidationError, match="remote-required"):
        nous_run._dispatch(authorized, tmp_path)


def test_migrate_cli_reports_only_schema_and_tip(monkeypatch, tmp_path, capsys):
    tip = "23c3551a30ac5d230f68a01b76650c27144571f5"
    config = SimpleNamespace(
        coord_mode="remote-required", coordination_branch="nous-coordination"
    )

    class Remote:
        def migrate_schema(self, *, target):
            assert target == 2
            return SchemaMigration(1, 2, tip, True)

    monkeypatch.setattr(nous_run, "_config", lambda _repo: config)
    monkeypatch.setattr(nous_run, "_remote", lambda *_args: Remote())
    args = build_parser().parse_args(
        ["migrate", "--to-schema", "2", "--authorize", "coordinate"]
    )
    assert nous_run._dispatch(args, tmp_path) == 0
    assert json.loads(capsys.readouterr().out) == {
        "branch": "nous-coordination",
        "changed": True,
        "from_schema": 1,
        "tip": tip,
        "to_schema": 2,
    }


def test_claim_cli_forwards_prior_fencing_token(monkeypatch, tmp_path):
    seen = {}

    class Backend:
        def claim(self, **kwargs):
            seen.update(kwargs)
            return Claim(
                claim_id=CLAIM_ID,
                run_id=RUN_ID,
                agent="agent",
                machine_id="mac-nous",
                branch="fix/bug",
                area="bug",
                files=(),
                pr=None,
                status="active",
                claimed_at="2026-08-27T04:00:00+00:00",
                expires_at="2026-08-27T07:00:00+00:00",
                renewed_at=None,
                candidate=None,
            )

    config = SimpleNamespace(machine_id="mac-nous")
    monkeypatch.setattr(nous_run, "_config", lambda _repo: config)
    monkeypatch.setattr(nous_run, "_combined", lambda *_args: Backend())
    monkeypatch.setattr(nous_run, "_base_sha", lambda *_args: HEAD)
    args = build_parser().parse_args(
        [
            "claim",
            "--agent",
            "agent",
            "--branch",
            "fix/bug",
            "--area",
            "bug",
            "--run-id",
            RUN_ID,
            "--claim-id",
            CLAIM_ID,
            "--authorize",
            "coordinate",
        ]
    )

    assert nous_run._dispatch(args, tmp_path) == 0
    assert seen["claim_id"] == CLAIM_ID


def test_base_sha_fetches_develop_through_a_unique_private_ref(monkeypatch, tmp_path):
    calls = []

    class FakeGitIO:
        def __init__(self, repo_root):
            assert repo_root == tmp_path

        def fetch_branch(self, remote, branch, temp_ref):
            calls.append(("fetch", remote, branch, temp_ref))
            return HEAD

        def delete_local_ref(self, temp_ref):
            calls.append(("delete", temp_ref))

        def run_git(self, *_args, **_kwargs):
            raise AssertionError("base SHA must not depend on FETCH_HEAD")

    monkeypatch.setattr(nous_run, "GitIO", FakeGitIO)
    config = SimpleNamespace(git_remote="origin")

    assert nous_run._base_sha(config, tmp_path) == HEAD
    temp_ref = calls[0][3]
    assert temp_ref.startswith("refs/nous/tmp/develop-")
    assert calls == [
        ("fetch", "origin", "develop", temp_ref),
        ("delete", temp_ref),
    ]


def test_production_backend_records_recoverable_held_in_error_claim(
    monkeypatch, tmp_path, capsys
):
    claim = Claim(
        claim_id=CLAIM_ID,
        run_id=RUN_ID,
        agent="agent",
        machine_id="mac-nous",
        branch="fix/bug",
        area="bug",
        files=(),
        pr=None,
        status="active",
        claimed_at="2026-08-27T04:00:00+00:00",
        expires_at="2026-08-27T07:00:00+00:00",
        renewed_at=None,
        candidate=None,
    )

    class Remote:
        def claim(self, **_kwargs):
            return claim

        def release(self, **_kwargs):
            raise TransportFailure("release unavailable")

    class Local:
        def __init__(self, bridge_dir):
            assert bridge_dir == tmp_path / "bridge"

        def claim_legacy(self, **_kwargs):
            raise TransportFailure("local unavailable")

    config = SimpleNamespace(
        coord_mode="remote-required",
        loop_bridge_dir=tmp_path / "bridge",
        receipt_dir=tmp_path / "receipts",
    )
    monkeypatch.setattr(nous_run, "_remote", lambda *_args: Remote())
    monkeypatch.setattr(nous_run, "LocalBackend", Local)

    with pytest.raises(TransportFailure, match="release unavailable"):
        nous_run._combined(config, tmp_path).claim(
            run_id=RUN_ID,
            agent="agent",
            machine_id="mac-nous",
            branch="fix/bug",
            area="bug",
            files=(),
            candidate=None,
            pr=None,
            ttl_seconds=10_800,
            base_sha=HEAD,
            evidence_head_sha=HEAD,
        )

    recovery = json.loads(capsys.readouterr().err)
    receipt = config.receipt_dir / RUN_ID / "held-in-error.json"
    assert recovery == {
        "claim_id": CLAIM_ID,
        "receipt": str(receipt),
        "run_id": RUN_ID,
        "state": "held-in-error",
    }
    assert json.loads(receipt.read_text(encoding="utf-8")) == recovery | {
        "agent": "agent",
        "area": "bug",
        "branch": "fix/bug",
        "expires_at": "2026-08-27T07:00:00+00:00",
        "machine_id": "mac-nous",
        "schema": 1,
    }


def test_held_in_error_still_exposes_fencing_when_receipt_write_fails(
    monkeypatch, tmp_path, capsys
):
    claim = Claim(
        claim_id=CLAIM_ID,
        run_id=RUN_ID,
        agent="agent",
        machine_id="mac-nous",
        branch="fix/bug",
        area="bug",
        files=(),
        pr=None,
        status="active",
        claimed_at="2026-08-27T04:00:00+00:00",
        expires_at="2026-08-27T07:00:00+00:00",
        renewed_at=None,
        candidate=None,
    )

    def fail_open(*_args, **_kwargs):
        raise OSError("read-only receipt directory")

    monkeypatch.setattr(nous_run.os, "open", fail_open)
    nous_run._record_held_in_error(tmp_path, claim)

    assert json.loads(capsys.readouterr().err) == {
        "claim_id": CLAIM_ID,
        "receipt": None,
        "run_id": RUN_ID,
        "state": "held-in-error",
    }


def test_held_in_error_fsyncs_each_new_directory_entry(monkeypatch, tmp_path):
    claim = Claim(
        claim_id=CLAIM_ID,
        run_id=RUN_ID,
        agent="agent",
        machine_id="mac-nous",
        branch="fix/bug",
        area="bug",
        files=(),
        pr=None,
        status="active",
        claimed_at="2026-08-27T04:00:00+00:00",
        expires_at="2026-08-27T07:00:00+00:00",
        renewed_at=None,
        candidate=None,
    )
    receipt_root = tmp_path / "nested" / "receipts"
    opened = {}
    synced = []
    real_open = nous_run.os.open
    real_fsync = nous_run.os.fsync

    def record_open(path, flags, mode=0o777):
        descriptor = real_open(path, flags, mode)
        opened[descriptor] = Path(path)
        return descriptor

    def record_fsync(descriptor):
        synced.append(opened[descriptor])
        real_fsync(descriptor)

    monkeypatch.setattr(nous_run.os, "open", record_open)
    monkeypatch.setattr(nous_run.os, "fsync", record_fsync)
    nous_run._record_held_in_error(receipt_root, claim)

    run_dir = receipt_root / RUN_ID
    assert synced[:3] == [tmp_path, tmp_path / "nested", receipt_root]
    assert synced[-1] == run_dir
