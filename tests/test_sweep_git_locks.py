"""The tick chain's pre-deploy lock sweep: stale locks go, fresh or live ones stay."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sweep_git_locks.sh"
HOUR_AGO = time.time() - 3600


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def _repo(tmp_path: Path, name: str = "checkout") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("x\n")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")
    return repo


def _aged_lock(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    os.utime(path, (HOUR_AGO, HOUR_AGO))
    return path


def _sweep(checkout: Path, min_age: str = "10") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), str(checkout), min_age], check=True, capture_output=True, text=True
    )


def test_stale_locks_are_swept_at_any_depth_and_named(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    stale = [
        _aged_lock(repo / ".git" / "index.lock"),
        _aged_lock(repo / ".git" / "refs" / "heads" / "main.lock"),
        # slash-named branches nest their ref locks (terra #231 r1)
        _aged_lock(repo / ".git" / "refs" / "remotes" / "origin" / "release" / "v1" / "x.lock"),
        _aged_lock(repo / ".git" / "logs" / "refs" / "heads" / "a" / "b" / "c" / "d.lock"),
    ]
    out = _sweep(repo)
    assert not any(lock.exists() for lock in stale)
    for lock in stale:
        assert f"swept stale git lock {lock}" in out.stdout


def test_a_fresh_lock_is_left_alone(tmp_path: Path) -> None:
    """A live git op holds its lock for seconds — age alone never qualifies."""
    repo = _repo(tmp_path)
    fresh = repo / ".git" / "HEAD.lock"
    fresh.write_text("")
    out = _sweep(repo)
    assert fresh.exists() and out.stdout == ""


def test_an_aged_lock_under_a_live_git_process_is_left_alone(tmp_path: Path) -> None:
    """A git command working in the checkout owns its locks, however old: the
    sweep waits (a cadence later it runs again). Held live via a git that
    reads stdin until we close it; swept once it is gone."""
    repo = _repo(tmp_path)
    lock = _aged_lock(repo / ".git" / "index.lock")
    # the RELATIVE form, started from the parent: argv names the checkout by
    # basename only and cwd is outside it (terra #231 r4)
    proc = subprocess.Popen(
        ["git", "-C", repo.name, "hash-object", "--stdin"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        cwd=tmp_path,
    )
    try:
        time.sleep(0.3)
        out = _sweep(repo)
        assert lock.exists() and out.stdout == ""
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=30)
    out = _sweep(repo)
    assert not lock.exists() and "swept stale git lock" in out.stdout


def test_a_linked_worktree_is_followed_to_its_git_dir(tmp_path: Path) -> None:
    """In a linked worktree `.git` is a FILE naming the real git dir; stale
    locks live there and in the common dir (terra #231 r1)."""
    repo = _repo(tmp_path)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "wt", str(linked))
    assert (linked / ".git").is_file()
    wt_lock = _aged_lock(repo / ".git" / "worktrees" / "linked" / "HEAD.lock")
    common_lock = _aged_lock(repo / ".git" / "refs" / "heads" / "wt.lock")
    # a git working in the SIBLING (main) worktree owns shared locks too (r3)
    proc = subprocess.Popen(
        ["git", "-C", str(repo), "hash-object", "--stdin"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.3)
        assert _sweep(linked).stdout == "" and common_lock.exists()
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=30)
    out = _sweep(linked)
    assert not wt_lock.exists() and not common_lock.exists()
    assert out.stdout.count("swept stale git lock") == 2


def test_the_age_threshold_means_at_least_that_old(tmp_path: Path) -> None:
    """find truncates ages to whole minutes: a lock 10m30s old must count as
    older than ten minutes (r3), while one 9m30s old must not."""
    repo = _repo(tmp_path)
    just_over = _aged_lock(repo / ".git" / "index.lock")
    t = time.time() - 630
    os.utime(just_over, (t, t))
    just_under = _aged_lock(repo / ".git" / "HEAD.lock")
    t = time.time() - 570
    os.utime(just_under, (t, t))
    _sweep(repo, "10")
    assert not just_over.exists() and just_under.exists()


def test_only_lock_files_are_touched_and_a_non_repo_is_a_no_op(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    keep = repo / ".git" / "FETCH_HEAD"
    keep.write_text("abc")
    os.utime(keep, (HOUR_AGO, HOUR_AGO))
    _sweep(repo)
    assert keep.read_text() == "abc"
    plain = tmp_path / "not-a-checkout"
    plain.mkdir()
    assert _sweep(plain).returncode == 0


def test_a_prefix_named_sibling_checkout_never_blocks_the_sweep(tmp_path: Path) -> None:
    """A live git in `<checkout>-old` is unrelated work: paths match at
    argument boundaries, so the sweep of `<checkout>` proceeds (r5)."""
    repo = _repo(tmp_path, "app")
    sibling = _repo(tmp_path, "app-old")
    lock = _aged_lock(repo / ".git" / "index.lock")
    proc = subprocess.Popen(
        ["git", "-C", str(sibling), "hash-object", "--stdin"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.3)
        out = _sweep(repo)
        assert not lock.exists() and "swept stale git lock" in out.stdout
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=30)


def test_a_worktree_path_with_spaces_still_counts_as_live(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    linked = tmp_path / "linked work tree"
    _git(repo, "worktree", "add", "-q", "-b", "sp", str(linked))
    common_lock = _aged_lock(repo / ".git" / "refs" / "heads" / "sp.lock")
    proc = subprocess.Popen(
        ["git", "hash-object", "--stdin"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        cwd=linked,
    )
    try:
        time.sleep(0.3)
        assert _sweep(repo).stdout == "" and common_lock.exists()
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=30)
    _sweep(repo)
    assert not common_lock.exists()


def test_split_form_git_dir_and_work_tree_from_the_parent_count_as_live(tmp_path: Path) -> None:
    """`--git-dir checkout/.git --work-tree checkout` (split form, relative,
    from the parent) names the checkout only by basename tokens (r6)."""
    repo = _repo(tmp_path, "app")
    lock = _aged_lock(repo / ".git" / "index.lock")
    proc = subprocess.Popen(
        ["git", "--git-dir", "app/.git", "--work-tree", "app", "hash-object", "--stdin"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        cwd=tmp_path,
    )
    try:
        time.sleep(0.3)
        assert _sweep(repo).stdout == "" and lock.exists()
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=30)
    assert "swept stale git lock" in _sweep(repo).stdout
