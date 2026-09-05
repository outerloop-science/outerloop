"""`outerloop init` — the guided setup.

Collects placement (Slurm or local), the target repo, and bot auth, then writes
`~/.config/autoresearch/.env` (and the PAT file), so a new adopter never
hand-edits config or reasons about which `AUTORESEARCH_*` keys to set. Flags fill
answers non-interactively; anything left out is prompted for (a secret via
getpass, never echoed). The App-manifest auth path — one-click creation of the
adopter's own GitHub App — is a later stage; today init writes a PAT you paste or
point at, and `resolve_bot_auth` reads it exactly as `outerloop start` does.

The config location is `cli.ENV_FILE`, the same file `start` reads — one source
of truth, so a rename of the config dir moves both together.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from outerloop.cli import ENV_FILE

CONFIG_DIR = ENV_FILE.parent
DEFAULT_PAT_FILE = CONFIG_DIR / "bot_pat"
API = "https://api.github.com"


@dataclass
class InitAnswers:
    compute: str  # "slurm" | "local"
    target: str  # "owner/repo"
    root: str = ""  # Slurm state root on the shared filesystem
    account: str = ""  # Slurm account (required for Slurm)
    partition: str = ""  # Slurm partition (optional; unset -> Slurm default)
    author_backend: str = ""  # optional: the climbing author's harness
    author_model: str = ""  # optional


def render_env(a: InitAnswers, pat_file: str = "", *, app_file: str = "") -> str:
    """The `.env` body for these answers — only the keys that have a value, so
    the file stays minimal and every line means something. Ordered placement →
    target → auth → author to read top-to-bottom like the setup itself. Auth is
    an App file (`--github-app`) or a PAT file, never both."""
    lines = [f"AUTORESEARCH_COMPUTE={a.compute}"]
    if a.compute == "slurm":
        lines.append(f"AUTORESEARCH_ROOT={a.root}")
        lines.append(f"AUTORESEARCH_ACCOUNT={a.account}")
        if a.partition:  # optional: unset lets Slurm pick its default partition
            lines.append(f"AUTORESEARCH_PARTITION={a.partition}")
    lines.append(f"AUTORESEARCH_TARGET={a.target}")
    if app_file:
        lines.append(f"AUTORESEARCH_GITHUB_APP_FILE={app_file}")
    elif pat_file:
        lines.append(f"AUTORESEARCH_PAT_FILE={pat_file}")
    if a.author_backend:
        lines.append(f"AUTORESEARCH_AUTHOR_BACKEND={a.author_backend}")
    if a.author_model:
        lines.append(f"AUTORESEARCH_AUTHOR_MODEL={a.author_model}")
    return "\n".join(lines) + "\n"


def write_config(
    a: InitAnswers, token: str, pat_file: str, *, config_dir: Path = CONFIG_DIR
) -> tuple[Path, Path | None]:
    """Write the PAT file (only when a token is pasted) and the `.env`, both
    owner-only (0600) — `start`/`tick_deploy` refuse a group/world-readable
    `.env`, and a token file must never be wider. Returns (env_path, pat_path)."""
    config_dir.mkdir(parents=True, exist_ok=True)
    written_pat: Path | None = None
    if token:
        pat_path = config_dir / DEFAULT_PAT_FILE.name
        # printf-style: no trailing newline — the deploy `cat`s this file into
        # the git credential, and a trailing newline rides along and breaks auth.
        pat_path.write_text(token)
        pat_path.chmod(0o600)
        written_pat = pat_path
        pat_file = str(pat_path)
    env_path = config_dir / ENV_FILE.name
    env_path.write_text(render_env(a, pat_file))
    env_path.chmod(0o600)
    return env_path, written_pat


def validate_pat(pat_file: str, target: str) -> str:
    """Best-effort: can this token read the target repo? Returns "" on success,
    else a short reason. Never raises — a check failure is a warning, not a
    reason to abandon a written config."""
    try:
        token = Path(pat_file).expanduser().read_text().strip()
    except OSError as exc:
        return f"could not read {pat_file}: {exc}"
    if not token:
        return f"{pat_file} is empty"
    req = urllib.request.Request(
        f"{API}/repos/{target}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        perms = body.get("permissions") or {}
        if not perms.get("push"):
            return f"the token reads {target} but lacks write access (it opens PRs)"
        return ""
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return f"{target} not found, or the token cannot see it"
        return f"GitHub returned {exc.code} for {target}"
    except urllib.error.URLError as exc:
        return f"could not reach GitHub: {exc.reason}"


def _ask(prompt: str, default: str = "", *, required: bool = False) -> str:
    """One interactive prompt with an optional default; re-asks while a required
    answer is blank."""
    suffix = f" [{default}]" if default else ""
    while True:
        got = input(f"{prompt}{suffix}: ").strip() or default
        if got or not required:
            return got
        print("  (required)")


def _collect(args: argparse.Namespace, interactive: bool) -> tuple[InitAnswers, str]:
    """Merge flags with prompts (when interactive) into answers + a PAT-file
    path. `args.pat_file` names an existing file; otherwise, interactively, a
    pasted token is returned separately to be written 0600."""
    compute = args.compute or (_ask("Compute: slurm or local", "slurm") if interactive else "slurm")
    compute = compute.lower()
    target = args.target or (_ask("Target repo (owner/repo)", required=True) if interactive else "")
    root = account = partition = ""
    if compute == "slurm":
        root = args.root or (
            _ask("Slurm state root (shared filesystem)", required=True) if interactive else ""
        )
        account = args.account or (_ask("Slurm account", required=True) if interactive else "")
        partition = args.partition or (
            _ask("Slurm partition (blank = Slurm default; a,b for a list)") if interactive else ""
        )
    backend = args.author_backend or (
        _ask("Author backend (blank to set later)") if interactive else ""
    )
    model = args.author_model or (_ask("Author model (blank to set later)") if interactive else "")
    answers = InitAnswers(
        compute=compute,
        target=target,
        root=root,
        account=account,
        partition=partition,
        author_backend=backend,
        author_model=model,
    )
    return answers, (args.pat_file or "")


def _github_app_setup(answers: InitAnswers, app_name: str, org: str) -> int:
    """The `--github-app` path: create the adopter's own App via the manifest
    flow, write its creds, help install it, then point the .env at the App file.
    Interactive by nature (a browser click + install), so no `--yes` variant."""
    from outerloop import appmanifest

    owner = org or answers.target.split("/")[0]
    name = app_name or f"outerloop-{owner}"[:34]  # GitHub caps App names at 34
    code = appmanifest.run_manifest_flow(
        name, "https://github.com/outerloop-science/outerloop", org
    )
    if not code:
        print(
            "outerloop init: no manifest code received (the browser flow timed out "
            "or was cancelled)",
            file=sys.stderr,
        )
        return 1
    try:
        conversion = appmanifest.convert_manifest(code)
    except ValueError as exc:
        print(f"outerloop init: {exc}", file=sys.stderr)
        return 1
    pem_path, app_json = appmanifest.save_app_creds(conversion, CONFIG_DIR)
    print(f"created App '{conversion['slug']}' — wrote {app_json} and {pem_path} (0600)")
    print(f"install it on {answers.target}: {appmanifest.install_url(conversion)}")
    input("press Enter once you've installed the App… ")
    iid = appmanifest.capture_installation_id(int(conversion["id"]), pem_path, owner)
    if iid:
        appmanifest.set_installation_id(app_json, iid)
        print(f"  installation id {iid} recorded")
    else:
        print(
            f"  no installation found yet — install the App, then set installation_id in {app_json}"
        )
    env_path = CONFIG_DIR / ENV_FILE.name
    env_path.write_text(render_env(answers, app_file=str(app_json)))
    env_path.chmod(0o600)
    print(f"wrote {env_path}")
    print("next: outerloop start")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="outerloop init", description="guided setup for outerloop"
    )
    parser.add_argument("--compute", choices=["slurm", "local"], help="where the loop runs")
    parser.add_argument("--target", help="the repo the agents work on, owner/repo")
    parser.add_argument("--root", help="Slurm state root on the shared filesystem")
    parser.add_argument("--account", help="Slurm account")
    parser.add_argument(
        "--partition", help="Slurm partition (optional; blank = default; a,b = list)"
    )
    parser.add_argument(
        "--pat-file", dest="pat_file", help="path to an existing file holding a PAT"
    )
    parser.add_argument(
        "--github-app",
        dest="github_app",
        action="store_true",
        help="create your own GitHub App in a browser (one click) instead of a PAT",
    )
    parser.add_argument("--app-name", dest="app_name", help="name for the created GitHub App")
    parser.add_argument("--org", help="create the App under this org (default: your account)")
    parser.add_argument("--author-backend", dest="author_backend", help="climbing author's backend")
    parser.add_argument("--author-model", dest="author_model", help="climbing author's model")
    parser.add_argument(
        "--yes", "-y", action="store_true", help="non-interactive: use flags, do not prompt"
    )
    args = parser.parse_args(sys.argv[2:] if argv is None else argv)
    interactive = not args.yes

    answers, pat_file = _collect(args, interactive)
    if not answers.target:
        print("outerloop init: a target repo is required (--target owner/repo)", file=sys.stderr)
        return 2
    if answers.compute == "slurm" and not (answers.root and answers.account):
        print("outerloop init: Slurm needs --root and --account", file=sys.stderr)
        return 2

    if args.github_app:
        return _github_app_setup(answers, args.app_name or "", args.org or "")

    token = ""
    if not pat_file and interactive:
        # re-collect only the token here so _collect stays pure of getpass in tests
        token = getpass.getpass(
            "Paste a GitHub PAT with write access to the target (hidden; blank to skip): "
        ).strip()

    env_path, pat_path = write_config(answers, token, pat_file, config_dir=CONFIG_DIR)
    print(f"wrote {env_path}")
    if pat_path:
        print(f"wrote {pat_path} (0600)")
    effective_pat = str(pat_path) if pat_path else pat_file
    if effective_pat:
        problem = validate_pat(effective_pat, answers.target)
        print(f"  auth check: {'ok' if not problem else 'WARNING — ' + problem}")
    else:
        print("  no PAT set — add AUTORESEARCH_PAT_FILE before the agents can open PRs")
    print("next: outerloop start")
    return 0
