"""Regression cover for the dev -> qa -> main promotion pipeline.

The guarantee under test: VERSION is BRANCH-OWNED. A version set on dev must
never reach qa or main, and each promotion advances only the target branch's
own sequence (qa 1.45 -> 1.46 even when dev says 10.00).

That property is easy to break by "simplifying" promote.sh into a plain merge,
and the breakage is silent -- promotions keep succeeding while every branch
quietly inherits the wrong version. So it is pinned here with a real git
sandbox rather than mocks.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
SELFTEST = SCRIPTS / "promotion_selftest.sh"
BUMP = SCRIPTS / "bump_version.py"


def _bump(tmp_path, value):
    f = tmp_path / "VERSION"
    f.write_text(value + "\n")
    subprocess.run(["python3", str(BUMP), str(f)], check=True, capture_output=True)
    return f.read_text().strip()


@pytest.mark.parametrize("start,expected", [
    ("1.45", "1.46"),   # the motivating case
    ("1.00", "1.01"),
    ("1.09", "1.10"),   # carry within the minor, padding preserved
    ("0.01", "0.02"),
    (".07", ".08"),     # legacy ".NN" counter
    ("1.99", "1.99"),   # exhausted minor HOLDS; major is set by hand
])
def test_bump_version(tmp_path, start, expected):
    assert _bump(tmp_path, start) == expected


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_promotion_keeps_version_branch_owned():
    """Full dev -> qa -> main cycle in a throwaway repo.

    Covers: dev's 10.00 not leaking into qa, qa advancing 1.45 -> 1.46, main
    advancing on its own line, dev left untouched, a repeat promotion being a
    no-op, and a second cycle advancing qa again.
    """
    res = subprocess.run(["bash", str(SELFTEST)], capture_output=True, text=True)
    assert res.returncode == 0, f"promotion selftest failed:\n{res.stdout}\n{res.stderr}"
    assert "0 failed" in res.stdout
