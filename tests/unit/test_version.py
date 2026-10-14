"""`dsel --version` exits 0 and reports harness provenance (PLAN.md S0)."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from dsel.version import Provenance, provenance

REPO = Path(__file__).resolve().parents[2]

VERSION_LINE = re.compile(r"^dsel \d+\.\d+\.\d+ \(commit (unknown|[0-9a-f]{7}(, dirty)?)\)$")


def test_provenance_in_repo_finds_a_commit() -> None:
    p = provenance(REPO)
    assert p.commit is not None, "running inside the git repo, a commit must resolve"
    assert re.fullmatch(r"[0-9a-f]{7}", p.commit)


def test_provenance_outside_a_repo_degrades(tmp_path: Path) -> None:
    """A tree with no .git must report `unknown`, not raise."""
    p = provenance(tmp_path)
    assert p.commit is None
    assert p.dirty is False
    assert str(p).endswith("(commit unknown)")


def test_provenance_str_marks_dirty() -> None:
    clean = Provenance(version="0.1.0", commit="abc1234", dirty=False)
    dirty = Provenance(version="0.1.0", commit="abc1234", dirty=True)
    assert str(clean) == "dsel 0.1.0 (commit abc1234)"
    assert str(dirty) == "dsel 0.1.0 (commit abc1234, dirty)"


def test_cli_version_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "dsel.cli", "--version"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert VERSION_LINE.match(result.stdout.strip()), result.stdout


def test_unimplemented_subcommand_exits_non_zero() -> None:
    """A stub must not exit 0 and pretend to have done something."""
    result = subprocess.run(
        [sys.executable, "-m", "dsel.cli", "run"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "not implemented" in result.stderr


def test_docker_dependent_commands_fail_with_a_sentence_not_a_traceback() -> None:
    """`env` and `budget` are the first commands a new reader runs, and both
    need the daemon. A Rich traceback here reads as "the harness is broken"
    when the actual state is "Docker Desktop is not started".

    Run with a PATH that has no `docker` on it, which is the same failure a
    reader gets before installing it.
    """
    import os

    env = dict(os.environ)
    env["PATH"] = "/nonexistent"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    for command in ("budget", "env"):
        result = subprocess.run(
            [sys.executable, "-m", "dsel.cli", command],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert result.returncode == 2, result.stdout + result.stderr
        assert "Traceback" not in result.stderr
        assert "docker is not on PATH" in result.stderr, result.stderr
        assert "https://docs.docker.com/desktop/" in result.stderr
