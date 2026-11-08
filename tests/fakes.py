"""In-process test doubles for the kernel's seams."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from outerloop.harness import SessionResult
from outerloop.runstate import RunRecord


@dataclass
class FakeHarness:
    """Deterministic in-process harness: records calls, returns a canned result."""

    result: SessionResult
    script: Any = None  # optional callable(brief_text, workspace) for side effects
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)
    supports_resume: bool = True  # a field so tests can exercise the no-resume path

    def run(
        self, brief_text: str, workspace: Path, resume_session_id: str | None = None
    ) -> SessionResult:
        self.calls.append((brief_text, str(workspace), resume_session_id))
        if self.script is not None:
            self.script(brief_text, workspace)
        return self.result


@dataclass
class RecordingDispatcher:
    """Dispatcher that records what would have been woken."""

    dispatched: list[tuple[str, str]] = field(default_factory=list)
    holder_job_id: str = ""  # set to simulate async dispatch

    def dispatch(self, record: RunRecord, reason: str) -> str:
        self.dispatched.append((record.run_id, reason))
        return self.holder_job_id


# The stand-in: `version` and `create` as enroot does them; a `start` with the
# probe's settings file succeeds or fails as $FAKE_LEVEL says this enroot's
# namespaces do, and any other `start` runs the settings file's rc with exactly
# the environment environ() and --env build, as enroot-switchroot does.
FAKE_ENROOT = r"""#!/usr/bin/env bash
here=$(dirname "$0")
# the launcher clears the environment, so the stand-in reads its part here
[ ! -f "$here/settings" ] || source "$here/settings"
log=$here/calls
env | sort > "$here/env.$1"
case $1 in
version) echo "${FAKE_VERSION:-4.2.1}" ;;
create)
    [ -z "${FAKE_CREATE_FAILS-}" ] || { echo "unsquashfs: bad image" >&2; exit 1; }
    echo "create $3 $4" >> "$log"
    mkdir -p "$ENROOT_DATA_PATH/$3/etc"
    ;;
start)
    shift
    flags=() conf='' extra=()
    while :; do
        case $1 in
        --pid | --ipc) flags+=("$1"); shift ;;
        --conf) conf=$2; shift 2 ;;
        --env) extra+=("$2"); shift 2 ;;
        *) break ;;
        esac
    done
    name=$1
    shift
    case $conf in
    */probe.conf)
        echo "probe ${flags[*]}" >> "$log"
        case ${FAKE_LEVEL:-full}:${flags[*]} in
        full:* | pid:--pid | *:) exit 0 ;;
        *) echo "[ERROR] namespaces refused" >&2; exit 1 ;;
        esac
        ;;
    esac
    # record the fds enroot (this stand-in) inherited, so a test can confirm the
    # launcher closed the cleanup pipe before exec (the jailed process must not
    # inherit it): one "<fd> <target>" line per open descriptor
    for fd in /proc/self/fd/*; do
        printf '%s %s\n' "$(basename "$fd")" "$(readlink "$fd" 2>/dev/null)"
    done > "$here/fds.start" 2>/dev/null
    echo "start ${flags[*]}" >> "$log"
    cp "$conf" "$here/jail.conf"
    [ -d "$ENROOT_DATA_PATH/$name" ] || { echo "no container $name" >&2; exit 1; }
    source "$conf"
    mapfile -t lines < <(environ; printf '%s\n' ${extra[@]+"${extra[@]}"})
    exec env -i "${lines[@]}" sh -c "$(declare -f rc | sed '1,2d;$d')" rc "$@"
    ;;
esac
"""


def fake_enroot(directory: Path, **settings: str) -> Path:
    """Write the stand-in enroot into `directory`, with `settings` (FAKE_LEVEL,
    FAKE_VERSION, FAKE_CREATE_FAILS) in the file it reads; returns its path."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "settings").write_text(
        "".join(f"{key}={shlex.quote(value)}\n" for key, value in settings.items())
    )
    enroot = directory / "enroot"
    enroot.write_text(FAKE_ENROOT)
    enroot.chmod(0o755)
    return enroot
