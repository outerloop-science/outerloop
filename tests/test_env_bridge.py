"""OUTERLOOP_* is the public name; AUTORESEARCH_* still works — bridged at every boundary."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import outerloop
from outerloop.cli import START_KEYS, env_file_values

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_python_bridge_copies_new_into_legacy_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("AUTORESEARCH_ZZ_TEST", raising=False)
    monkeypatch.setenv("OUTERLOOP_ZZ_TEST", "from-new")
    outerloop._bridge_legacy_env()
    assert os.environ["AUTORESEARCH_ZZ_TEST"] == "from-new"
    # an explicitly set legacy twin wins (setdefault semantics)
    monkeypatch.setenv("AUTORESEARCH_ZZ_TEST", "explicit")
    monkeypatch.setenv("OUTERLOOP_ZZ_TEST", "ignored")
    outerloop._bridge_legacy_env()
    assert os.environ["AUTORESEARCH_ZZ_TEST"] == "explicit"


def test_env_file_reads_the_public_names_canonically(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("OUTERLOOP_ROOT=/scratch/x\nAUTORESEARCH_ACCOUNT=acct\n")
    env.chmod(0o600)
    got = env_file_values(env, START_KEYS)
    assert got == {"AUTORESEARCH_ROOT": "/scratch/x", "AUTORESEARCH_ACCOUNT": "acct"}


def _bridge_block() -> str:
    text = (SCRIPTS / "tick_chain.sbatch").read_text()
    m = re.search(r"^for _ov in .*?^done\n", text, re.S | re.M)
    assert m, "the shell bridge loop must be present at the chain's entry"
    return m.group(0)


def test_shell_bridge_exports_legacy_twin_and_respects_an_explicit_one() -> None:
    block = _bridge_block()
    run = lambda env: (
        subprocess.run(
            ["bash", "-c", block + '\nprintf "%s" "${AUTORESEARCH_ZZ_TEST:-unset}"'],
            env={**{"PATH": os.environ["PATH"]}, **env},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    assert run({"OUTERLOOP_ZZ_TEST": "from-new"}) == "from-new"
    assert run({"OUTERLOOP_ZZ_TEST": "new", "AUTORESEARCH_ZZ_TEST": "keep"}) == "keep"
    assert run({}) == "unset"


def test_deploy_allowlist_takes_either_spelling() -> None:
    sh = (SCRIPTS / "tick_deploy.sh").read_text()
    assert 'grep -E "^(${_k}|OUTERLOOP_${_k#AUTORESEARCH_})="' in sh
