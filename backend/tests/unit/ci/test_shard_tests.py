"""Contracts for the integration-test sharding used by the Test Pipeline.

The Integration Tests job runs one matrix leg per shard. The legs must cover
every test file exactly once, or a test silently stops running in CI.
"""

import importlib.util
import sys
from pathlib import Path
from typing import cast

import pytest
import yaml  # type: ignore[import-untyped]  # CI lint gate installs no types-PyYAML

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "shard_tests.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "test-pipeline.yml"

_spec = importlib.util.spec_from_file_location("shard_tests", SCRIPT)
assert _spec is not None and _spec.loader is not None
shard_tests = importlib.util.module_from_spec(_spec)
sys.modules["shard_tests"] = shard_tests
_spec.loader.exec_module(shard_tests)


def _tree(root: Path, sizes: dict[str, int]) -> None:
    for name, size in sizes.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * size, encoding="utf-8")


SIZES = {
    "test_big.py": 900,
    "test_mid.py": 400,
    "test_small.py": 100,
    "ai/test_nested.py": 300,
    "ai/test_tiny.py": 10,
    "conftest.py": 50,  # never collected as a test file
    "helpers.py": 50,
}


@pytest.mark.parametrize("total", [1, 2, 3, 5, 8])
def test_every_test_file_runs_in_exactly_one_shard(tmp_path: Path, total: int) -> None:
    _tree(tmp_path, SIZES)
    expected = sorted(tmp_path.rglob("test_*.py"))

    seen: list[Path] = []
    for shard in range(1, total + 1):
        seen.extend(shard_tests.shard_files(tmp_path, shard, total))

    assert sorted(seen) == expected
    assert len(seen) == len(set(seen))


def test_split_is_deterministic(tmp_path: Path) -> None:
    _tree(tmp_path, SIZES)
    first = shard_tests.shard_files(tmp_path, 2, 3)
    assert shard_tests.shard_files(tmp_path, 2, 3) == first


def test_heaviest_files_are_spread_across_shards(tmp_path: Path) -> None:
    _tree(tmp_path, SIZES)
    heaviest = {shard_tests.shard_files(tmp_path, shard, 2)[0].name for shard in (1, 2)}
    assert "test_big.py" in heaviest
    # the big file does not share a shard with the next-largest one
    big_shard = next(
        s
        for s in (1, 2)
        if tmp_path / "test_big.py" in shard_tests.shard_files(tmp_path, s, 2)
    )
    assert tmp_path / "test_mid.py" not in shard_tests.shard_files(
        tmp_path, big_shard, 2
    )


def test_more_shards_than_files_leaves_empty_shards(tmp_path: Path) -> None:
    _tree(tmp_path, {"test_only.py": 10})
    assert shard_tests.shard_files(tmp_path, 1, 3) == [tmp_path / "test_only.py"]
    assert shard_tests.shard_files(tmp_path, 2, 3) == []


@pytest.mark.parametrize("shard,total", [(0, 3), (4, 3), (1, 0), (-1, 2)])
def test_invalid_shard_arguments_are_rejected(
    tmp_path: Path, shard: int, total: int
) -> None:
    with pytest.raises(ValueError):
        shard_tests.shard_files(tmp_path, shard, total)


def test_cli_prints_one_path_per_line_and_rejects_bad_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _tree(tmp_path, {"test_a.py": 10, "test_b.py": 20})
    assert shard_tests.main([str(tmp_path), "--shard", "1", "--of", "1"]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert printed == sorted(p.as_posix() for p in tmp_path.glob("test_*.py"))

    assert shard_tests.main([str(tmp_path), "--shard", "5", "--of", "2"]) == 2
    assert (
        shard_tests.main([str(tmp_path / "missing"), "--shard", "1", "--of", "2"]) == 2
    )


def _integration_job() -> dict:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    return cast(dict, workflow["jobs"]["integration-tests"])


def test_integration_job_is_a_sharded_matrix_that_fails_closed() -> None:
    job = _integration_job()
    strategy = job["strategy"]
    shards = strategy["matrix"]["shard"]
    # fail-fast stays off so one red shard does not hide the others' results
    assert strategy["fail-fast"] is False
    assert shards == list(range(1, len(shards) + 1)) and len(shards) >= 2
    assert "matrix.shard" in job["name"]

    step = next(
        step
        for step in job["steps"]
        if "backend/tests/integration" in str(step.get("run", ""))
    )
    # the leg count comes from the matrix, never a second hard-coded number
    assert "strategy.job-total" in str(step["env"]["SHARDS"])
    assert "matrix.shard" in str(step["env"]["SHARD"])
    assert "shard_tests.py" in step["run"]
    assert "backend/tests/integration/ \\" not in step["run"]  # no whole-dir run
