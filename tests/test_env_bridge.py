"""OUTERLOOP_* is the name; the pre-rename AUTORESEARCH_* still works for one
release, bridged at every boundary (process env, .env file, chain scripts)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import outerloop
from outerloop.cli import START_KEYS, env_file_values

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_python_bridge_copies_legacy_into_new_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("OUTERLOOP_ZZ_TEST", raising=False)
    monkeypatch.setenv("AUTORESEARCH_ZZ_TEST", "from-legacy")
    outerloop._bridge_legacy_env()
    assert os.environ["OUTERLOOP_ZZ_TEST"] == "from-legacy"
    # an explicitly set new name wins (setdefault semantics)
    monkeypatch.setenv("OUTERLOOP_ZZ_TEST", "explicit")
    monkeypatch.setenv("AUTORESEARCH_ZZ_TEST", "ignored")
    outerloop._bridge_legacy_env()
    assert os.environ["OUTERLOOP_ZZ_TEST"] == "explicit"


def test_env_file_reads_either_spelling_canonically(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("OUTERLOOP_ROOT=/scratch/x\nAUTORESEARCH_ACCOUNT=acct\n")
    env.chmod(0o600)
    got = env_file_values(env, START_KEYS)
    assert got == {"OUTERLOOP_ROOT": "/scratch/x", "OUTERLOOP_ACCOUNT": "acct"}


def test_env_file_prefers_the_new_spelling_whatever_the_order(tmp_path: Path) -> None:
    """A file edited across the rename can hold both spellings; the OUTERLOOP_
    line wins even when the legacy line comes later (terra, #302). Among
    lines of one spelling the last still wins."""
    env = tmp_path / ".env"
    env.write_text(
        "OUTERLOOP_ROOT=/new\nAUTORESEARCH_ROOT=/old\n"
        "AUTORESEARCH_ACCOUNT=old\nOUTERLOOP_ACCOUNT=new1\nOUTERLOOP_ACCOUNT=new2\n"
        "AUTORESEARCH_PARTITION=p1\nAUTORESEARCH_PARTITION=p2\n"
    )
    env.chmod(0o600)
    got = env_file_values(env, START_KEYS)
    assert got["OUTERLOOP_ROOT"] == "/new"
    assert got["OUTERLOOP_ACCOUNT"] == "new2"
    assert got["OUTERLOOP_PARTITION"] == "p2"


def _bridge_block() -> str:
    text = (SCRIPTS / "tick_chain.sbatch").read_text()
    m = re.search(r"^for _ov in .*?^done\n", text, re.S | re.M)
    assert m, "the shell bridge loop must be present at the chain's entry"
    return m.group(0)


def test_shell_bridge_exports_new_twin_and_respects_an_explicit_one() -> None:
    block = _bridge_block()
    run = lambda env: (
        subprocess.run(
            ["bash", "-c", block + '\nprintf "%s" "${OUTERLOOP_ZZ_TEST:-unset}"'],
            env={**{"PATH": os.environ["PATH"]}, **env},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    assert run({"AUTORESEARCH_ZZ_TEST": "from-legacy"}) == "from-legacy"
    assert run({"AUTORESEARCH_ZZ_TEST": "old", "OUTERLOOP_ZZ_TEST": "keep"}) == "keep"
    assert run({}) == "unset"


def test_deploy_allowlist_takes_either_spelling_new_first() -> None:
    sh = (SCRIPTS / "tick_deploy.sh").read_text()
    new = sh.index('_line=$(grep -E "^${_k}=" "$ENV_FILE"')
    old = sh.index(
        '[ -n "$_line" ] || _line=$(grep -E "^AUTORESEARCH_${_k#OUTERLOOP_}=" "$ENV_FILE"'
    )
    assert new < old  # the legacy grep only fills an absent canonical key
