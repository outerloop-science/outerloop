"""`outerloop init` — the guided setup.

Collects placement (Slurm or local), the target repo, bot auth, and the
author's model key, then writes `~/.config/outerloop/.env` (plus the credential
files), so a new adopter never hand-edits config or reasons about which
`OUTERLOOP_*` keys to set. Flags fill
answers non-interactively; anything left out is prompted for (a secret via
getpass, never echoed). Auth is the adopter's own GitHub App by default —
`--github-app`, one click via the manifest flow in `appmanifest.py` — with a PAT
as the fallback; either way `resolve_bot_auth` reads the result exactly as
`outerloop start` does. An existing `.env` is never overwritten without asking.

The config location is `cli.ENV_FILE`, the same file `start` reads — one source
of truth, so a rename of the config dir moves both together.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from outerloop.cli import ENV_FILE
from outerloop.image import ensure_image
from outerloop.paths import write_private

CONFIG_DIR = ENV_FILE.parent
DEFAULT_PAT_FILE = CONFIG_DIR / "bot_pat"
API = "https://api.github.com"
# The climbing author's harnesses (attempt.py's --author-backend choices).
AUTHOR_BACKENDS = ("claude", "codex")


@dataclass
class InitAnswers:
    compute: str  # "slurm" | "local"
    target: str  # "owner/repo"
    root: str = ""  # Slurm state root on the shared filesystem
    account: str = ""  # Slurm account (optional; unset -> the default association)
    partition: str = ""  # Slurm partition (optional; unset -> Slurm default)
    author_backend: str = ""  # optional: the climbing author's harness
    author_model: str = ""  # optional
    author_key_file: str = ""  # the author's model key file, when known
    image: str = ""  # the agent image (OUTERLOOP_IMAGE)
    uncontained: bool = False  # --no-image: write OUTERLOOP_IMAGE= so no image is picked up
    author_bin: str = ""  # the author harness binary (claude/codex), absolute, when found


def render_env(
    a: InitAnswers, pat_file: str = "", *, app_file: str = "", bot_login: str = ""
) -> str:
    """The `.env` body for these answers — only the keys that have a value, so
    the file stays minimal and every line means something. Ordered placement →
    target → auth → author to read top-to-bottom like the setup itself. Auth is
    an App file (`--github-app`) or a PAT file, never both."""
    lines = [f"OUTERLOOP_COMPUTE={a.compute}"]
    if a.compute == "slurm":
        lines.append(f"OUTERLOOP_ROOT={a.root}")
        if a.account:  # optional: unset bills the caller's default association
            lines.append(f"OUTERLOOP_ACCOUNT={a.account}")
        if a.partition:  # optional: unset lets Slurm pick its default partition
            lines.append(f"OUTERLOOP_PARTITION={a.partition}")
    if a.image:
        lines.append(f"OUTERLOOP_IMAGE={a.image}")
    elif a.uncontained:
        # the empty value is the off-switch: without it the tick would still
        # pick up an image already at the default path
        lines.append("OUTERLOOP_IMAGE=")
    lines.append(f"OUTERLOOP_TARGET={a.target}")
    if app_file:
        lines.append(f"OUTERLOOP_GITHUB_APP_FILE={app_file}")
    elif pat_file:
        lines.append(f"OUTERLOOP_PAT_FILE={pat_file}")
    if bot_login:
        # every own-comment filter and own-PR scan keys on this login; without
        # it the kernel assumes a default that is not this adopter's identity
        lines.append(f"OUTERLOOP_BOT_LOGIN={bot_login}")
    if a.author_backend:
        lines.append(f"OUTERLOOP_AUTHOR_BACKEND={a.author_backend}")
    if a.author_model:
        lines.append(f"OUTERLOOP_AUTHOR_MODEL={a.author_model}")
    if a.author_key_file:
        lines.append(f"{author_key_env(a.author_backend)}={a.author_key_file}")
    if a.author_bin:
        lines.append(f"{author_bin_env(a.author_backend)}={a.author_bin}")
    return "\n".join(lines) + "\n"


def author_bin_env(backend: str) -> str:
    """The `.env` key naming `backend`'s harness binary (`OUTERLOOP_CLAUDE_BIN`,
    `OUTERLOOP_CODEX_BIN`), the same names `attempt` reads."""
    return f"OUTERLOOP_{(backend or AUTHOR_BACKENDS[0]).upper()}_BIN"


HARNESS_INSTALL = {
    "claude": "npm install -g @anthropic-ai/claude-code "
    "(or: curl -fsSL https://claude.ai/install.sh | bash)",
    "codex": "npm install -g @openai/codex",
}


def locate_harness(backend: str) -> str:
    """The absolute path of `backend`'s CLI on this machine: PATH first, then
    ~/.local/bin (where the native installers put it and where a Slurm job,
    with no login PATH, would not find it); "" when absent. Recorded in .env so
    every job spawns the same binary the operator installed."""
    name = backend or AUTHOR_BACKENDS[0]
    found = shutil.which(name)
    if found:
        return str(Path(found).resolve())
    local = Path.home() / ".local" / "bin" / name
    return str(local) if local.is_file() and os.access(local, os.X_OK) else ""


def _harness_hint(answers: InitAnswers) -> None:
    if not answers.author_bin:
        name = answers.author_backend or AUTHOR_BACKENDS[0]
        print(
            f"  no `{name}` binary found on PATH or in ~/.local/bin — the first climb would end\n"
            f"  with spawn-error. Install it: {HARNESS_INSTALL.get(name, 'see its docs')}\n"
            "  then run `outerloop init --force` so its path is recorded."
        )


def _token_login(token: str) -> str:
    """The login a PAT acts as (GET /user), or "" when GitHub does not say."""
    req = urllib.request.Request(
        f"{API}/user",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return str(json.loads(resp.read()).get("login") or "")
    except (urllib.error.URLError, OSError, ValueError):
        return ""


def _auth_is_fatal(problem: str) -> bool:
    """A credential the loop cannot open pull requests with fails setup; not
    being able to ask GitHub right now (network, 5xx, rate limit) does not."""
    return bool(problem) and not problem.startswith(
        ("could not reach GitHub", "could not read the App", "could not check the App")
    )


def author_key_env(backend: str) -> str:
    """The `.env` key naming `backend`'s model-key file: one rule for every
    backend (`OUTERLOOP_CLAUDE_KEY_FILE`, `OUTERLOOP_CODEX_KEY_FILE`)."""
    return f"OUTERLOOP_{(backend or AUTHOR_BACKENDS[0]).upper()}_KEY_FILE"


def author_key_path(backend: str, *, config_dir: Path = CONFIG_DIR) -> Path:
    """Where `backend`'s model key lives by default: `<config dir>/<backend>_key`,
    the same rule `attempt.resolve_author_key_file` reads."""
    return config_dir / f"{backend or AUTHOR_BACKENDS[0]}_key"


def write_author_key(backend: str, key: str, *, config_dir: Path = CONFIG_DIR) -> Path:
    """Write a pasted model key to its default file, owner-only (0600), no
    trailing newline. Returns the path."""
    path = author_key_path(backend, config_dir=config_dir)
    write_private(path, key)
    return path


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
        write_private(pat_path, token)
        written_pat = pat_path
        pat_file = str(pat_path)
    env_path = config_dir / ENV_FILE.name
    write_private(env_path, render_env(a, pat_file))
    return env_path, written_pat


def _check_repo_access(token: str, target: str) -> str:
    """Can this token write the target repo? "" on success, else a short reason.
    Never raises — a check failure is a warning, not a reason to abandon config."""
    req = urllib.request.Request(
        f"{API}/repos/{target}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        perms = body.get("permissions") or {}
        if not perms.get("push"):
            return f"reaches {target} but lacks write access (it opens PRs)"
        return ""
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return f"{target} not found, or the token cannot see it"
        if exc.code == 401:
            return "the token is invalid or expired (GitHub returned 401)"
        if exc.code == 403:
            return f"the token is not allowed to access {target} (GitHub returned 403)"
        if exc.code == 429 or exc.code >= 500:
            # GitHub's problem, not the credential's: never fatal
            return f"could not reach GitHub: it returned {exc.code} for {target}"
        return f"GitHub returned {exc.code} for {target}"
    except urllib.error.URLError as exc:
        return f"could not reach GitHub: {exc.reason}"


def _check_app_access(provider: Any, target: str) -> str:
    """Can this App write the target repo? "" on success, else a short reason.

    An installation token does not populate a repository's `permissions`
    object, so the PAT check above reads all-false for an App that can push.
    The App is asked the two questions that decide it: is the target among
    the repositories its installation covers, and does the installation carry
    write on contents and pull requests. Never raises."""
    from outerloop.appauth import build_app_jwt

    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    try:
        # Asked as the App itself: GitHub answers with the installation that
        # covers this repository, permissions included, or 404. A public
        # repository would answer a plain read for any token, and the
        # installation's repository list paginates, so neither is asked.
        jwt = build_app_jwt(provider.app_id, time.time(), provider._sign)
        req = urllib.request.Request(
            f"{API}/repos/{target}/installation",
            headers={**headers, "Authorization": f"Bearer {jwt}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                installation = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return f"the App is not installed on {target}; install it there"
            raise
        if int(installation.get("id", 0)) != int(provider.installation_id):
            return (
                f"the App is installed on {target} under installation "
                f"{installation.get('id')}, but the App file records {provider.installation_id}"
            )
        perms = installation.get("permissions") or {}
        missing = [k for k in ("contents", "issues", "pull_requests") if perms.get(k) != "write"]
        if missing:
            return (
                f"the App lacks write on {', '.join(missing)} "
                "(it pushes branches, opens PRs, and files and closes issues)"
            )
        return ""
    except urllib.error.HTTPError as exc:
        if exc.code == 429 or exc.code >= 500:
            return f"could not reach GitHub: it returned {exc.code} while checking the App"
        return f"GitHub returned {exc.code} while checking the App"
    except urllib.error.URLError as exc:
        return f"could not reach GitHub: {exc.reason}"
    except Exception as exc:  # a check failure is a warning, never fails setup
        return f"could not check the App: {exc}"


def validate_pat(pat_file: str, target: str) -> str:
    """Best-effort: can the PAT in `pat_file` write the target repo?"""
    try:
        token = Path(pat_file).expanduser().read_text().strip()
    except OSError as exc:
        return f"could not read {pat_file}: {exc}"
    if not token:
        return f"{pat_file} is empty"
    return _check_repo_access(token, target)


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
        account = args.account or (
            _ask("Slurm account (blank = your default association)") if interactive else ""
        )
        partition = args.partition or (
            _ask("Slurm partition (blank = Slurm default; a,b for a list)") if interactive else ""
        )
    # Author config is part of the full setup, not the focused --github-app run
    # (that one is about auth). When asked, offer the fixed set, not a blank.
    ask_author = interactive and not args.github_app
    backend = args.author_backend or (
        _ask("Author backend (claude or codex)", "claude") if ask_author else ""
    )
    model = args.author_model or (
        _ask("Author model (blank = the backend's default)") if ask_author else ""
    )
    # An explicit --image is recorded here (absolute: jobs read it from their
    # own directory); the published one is fetched in main, after every check
    # that could still end the run.
    image = str(Path(args.image).expanduser().absolute()) if args.image else ""
    answers = InitAnswers(
        compute=compute,
        target=target,
        root=root,
        account=account,
        partition=partition,
        author_backend=backend,
        author_model=model,
        image=image,
        author_bin=locate_harness(backend),
        uncontained=bool(args.no_image) and not image,
    )
    return answers, (args.pat_file or "")


ORG_PROMPT = (
    "Create the App under which account? (the repository's owner is the only one "
    "the App can then be installed on; Enter to accept)"
)


def _owner_type(owner: str) -> str:
    """ "User" or "Organization" for a GitHub account, or "" when the lookup
    fails. GitHub creates Apps at different pages for the two, and a wrong
    guess sends the adopter to a page that cannot create the App."""
    req = urllib.request.Request(
        f"{API}/users/{owner}", headers={"Accept": "application/vnd.github+json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return str(json.loads(resp.read()).get("type", ""))
    except Exception:
        return ""


def _app_failure(answers: InitAnswers, slug: str, problem: str) -> int:
    """The App cannot write the target: say so, keep the credentials, and point
    at the fix and the re-check. Never `start` — the check just failed."""
    print(f"  auth check: FAILED — {problem}", file=sys.stderr)
    print(
        f"outerloop init: the App '{slug}' cannot write {answers.target}. On\n"
        f"  https://github.com/apps/{slug}/installations/new (or the App's page under\n"
        "  Settings > Installations > Configure) install it on that repository and accept\n"
        "  contents, issues and pull-request write access, then run\n"
        "  `outerloop init --force --github-app` to re-check with these credentials.",
        file=sys.stderr,
    )
    return 1


def _github_app_recheck(answers: InitAnswers, app_json: Path) -> int:
    """Re-run Step 3 for an App this machine already created: confirm it can
    write the target, then point the .env at it."""
    from outerloop import appmanifest
    from outerloop.appauth import app_provider_from_file

    slug = app_json.name[len("github_app.") : -len(".json")]
    print(f"Re-checking the existing App '{slug}' ({app_json}) against {answers.target}.")
    try:
        data = json.loads(app_json.read_text())
        if not int(data.get("installation_id") or 0):
            # the earlier run ended before the App was installed: look again
            iid = appmanifest.capture_installation_id(
                int(data["app_id"]), Path(str(data["private_key"])), answers.target.split("/")[0]
            )
            if iid:
                appmanifest.set_installation_id(app_json, iid)
                print(f"  installation id {iid} recorded")
        problem = _check_app_access(app_provider_from_file(app_json), answers.target)
    except Exception as exc:
        problem = f"could not read the App credentials: {exc}"
    env_path = CONFIG_DIR / ENV_FILE.name
    write_private(env_path, render_env(answers, app_file=str(app_json), bot_login=f"{slug}[bot]"))
    if _auth_is_fatal(problem):
        return _app_failure(answers, slug, problem)
    print(f"  auth check: {'ok' if not problem else 'WARNING — ' + problem}")
    print(f"wrote {env_path}")
    _author_key_hint(answers)
    _harness_hint(answers)
    print("next: outerloop start")
    return 0


def _github_app_setup(
    answers: InitAnswers, app_name: str, org: str, *, interactive: bool = True
) -> int:
    """The `--github-app` path: create the adopter's own App via the manifest
    flow, write its creds, help install it, then point the .env at the App file.
    Interactive by nature (a browser click + install), so no `--yes` variant."""
    from outerloop import appmanifest

    existing = sorted(CONFIG_DIR.glob("github_app.*.json"))
    if len(existing) == 1:
        # a re-run after fixing the installation: re-check the App this machine
        # already has rather than creating a second one
        reuse = True
        if interactive:
            answer = _ask(f"Reuse the App credentials at {existing[0]}? (Y/n)", "y")
            reuse = not answer.lower().startswith("n")
        if reuse:
            return _github_app_recheck(answers, existing[0])
    owner = org or answers.target.split("/")[0]
    name = app_name or f"outerloop-{owner}"[:34]  # GitHub caps App names at 34
    code = appmanifest.request_manifest_code(
        name, "https://github.com/outerloop-science/outerloop", org
    )
    if not code:
        print("outerloop init: no code entered — nothing created", file=sys.stderr)
        return 1
    try:
        conversion = appmanifest.convert_manifest(code)
    except ValueError as exc:
        print(f"outerloop init: {exc}", file=sys.stderr)
        return 1
    pem_path, app_json = appmanifest.save_app_creds(conversion, CONFIG_DIR)
    repo = answers.target.split("/", 1)[-1]
    print(f"  created App '{conversion['slug']}'; credentials in {app_json} and {pem_path} (0600)")
    print()
    print(f"Step 2 of 3: install the App on {answers.target}.")
    print(f"  Open {appmanifest.install_url(conversion)}")
    print(f"  choose 'Only select repositories', pick '{repo}', and click 'Install'.")
    input("Press Enter here once GitHub shows the App as installed… ")
    print()
    print(f"Step 3 of 3: checking that the App can write {answers.target}.")
    iid = appmanifest.capture_installation_id(int(conversion["id"]), pem_path, owner)
    for _ in range(2):
        if iid:
            break
        # not installed yet: a rerun of init would stop at the existing config,
        # so ask again here rather than sending the adopter around the loop
        print(
            f"  no installation found yet. Install the App at {appmanifest.install_url(conversion)}"
        )
        input("Press Enter once GitHub shows the App as installed… ")
        iid = appmanifest.capture_installation_id(int(conversion["id"]), pem_path, owner)
    if iid:
        appmanifest.set_installation_id(app_json, iid)
        print(f"  installation id {iid} recorded")
        # Self-verify end to end: mint a real installation token and check it can
        # write the target — this is what confirms the whole flow actually worked.
        from outerloop.appauth import app_provider_from_file

        try:
            problem = _check_app_access(app_provider_from_file(app_json), answers.target)
        except Exception as exc:  # a check failure is a warning, never fails setup
            problem = f"could not read the App credentials: {exc}"
        if _auth_is_fatal(problem):
            write_private(
                CONFIG_DIR / ENV_FILE.name,
                render_env(answers, app_file=str(app_json), bot_login=f"{conversion['slug']}[bot]"),
            )
            return _app_failure(answers, str(conversion["slug"]), problem)
        print(f"  auth check: {'ok' if not problem else 'WARNING — ' + problem}")
    else:
        # the credentials are kept; nothing can run until the App is installed
        write_private(
            CONFIG_DIR / ENV_FILE.name,
            render_env(answers, app_file=str(app_json), bot_login=f"{conversion['slug']}[bot]"),
        )
        return _app_failure(
            answers, str(conversion["slug"]), f"the App is not installed on {answers.target}"
        )
    env_path = CONFIG_DIR / ENV_FILE.name
    write_private(
        env_path,
        render_env(answers, app_file=str(app_json), bot_login=f"{conversion['slug']}[bot]"),
    )
    print(f"wrote {env_path}")
    _author_key_hint(answers)
    _harness_hint(answers)
    print("next: outerloop start")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="outerloop init",
        description="Guided setup: asks for anything not given as a flag, checks the "
        "credentials, and writes ~/.config/outerloop/.env.",
    )
    parser.add_argument("--compute", choices=["slurm", "local"], help="where the loop runs")
    parser.add_argument("--target", help="the repo the agents work on, owner/repo")
    parser.add_argument("--root", help="Slurm state root on the shared filesystem")
    parser.add_argument(
        "--account", help="Slurm account (optional; unset bills your default association)"
    )
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
    parser.add_argument(
        "--author-backend",
        dest="author_backend",
        help=f"climbing author's backend ({' or '.join(AUTHOR_BACKENDS)}; default claude)",
    )
    parser.add_argument("--author-model", dest="author_model", help="climbing author's model")
    image_flags = parser.add_mutually_exclusive_group()  # one or the other, never both
    image_flags.add_argument(
        "--image",
        help="path to an Apptainer image to run sessions and evals in (default: the "
        "published one, downloaded on Linux when apptainer is installed)",
    )
    image_flags.add_argument(
        "--no-image",
        dest="no_image",
        action="store_true",
        help="no image at all: runs stay uncontained even if one is already on disk",
    )
    parser.add_argument(
        "--author-key-file",
        dest="author_key_file",
        help="path to an existing file holding the author's model API key",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true", help="non-interactive: use flags, do not prompt"
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite an existing config without asking"
    )
    args = parser.parse_args(sys.argv[2:] if argv is None else argv)
    interactive = not args.yes

    answers, pat_file = _collect(args, interactive)
    if not answers.target:
        print("outerloop init: a target repo is required (--target owner/repo)", file=sys.stderr)
        return 2
    if answers.compute == "slurm" and not answers.root:
        # the account and the partition are optional: Slurm's defaults apply
        print("outerloop init: Slurm needs --root", file=sys.stderr)
        return 2
    if answers.author_backend and answers.author_backend not in AUTHOR_BACKENDS:
        print(
            f"outerloop init: author backend must be one of {', '.join(AUTHOR_BACKENDS)}",
            file=sys.stderr,
        )
        return 2
    if answers.image and not Path(answers.image).is_file():
        # a typo here would surface as an uncontained loop later
        print(f"outerloop init: --image {answers.image} is not a file", file=sys.stderr)
        return 2

    # Never clobber a working setup silently: a re-run of init on a configured
    # machine must ask (or be told --force). Checked before any App is created.
    env_path = CONFIG_DIR / ENV_FILE.name
    if env_path.exists() and not args.force:
        if not interactive:
            print(f"outerloop init: {env_path} exists; pass --force to overwrite", file=sys.stderr)
            return 1
        if not _ask(f"{env_path} exists — overwrite it? (y/N)", "n").lower().startswith("y"):
            print(
                "outerloop init: kept the existing config (--force skips this check)",
                file=sys.stderr,
            )
            return 1

    # The author's model key: an existing file by flag, or (on the full
    # interactive run, never the focused --github-app one) a hidden paste written
    # to <config>/<backend>_key. Collected after the overwrite check so a
    # declined run never asks for a secret.
    if args.author_key_file:
        key_file = Path(args.author_key_file).expanduser()
        if not key_file.is_file() or not os.access(key_file, os.R_OK):
            print(
                f"outerloop init: --author-key-file {key_file} is not a readable file",
                file=sys.stderr,
            )
            return 2
        # absolute: climb jobs read the key from their own flight directory
        answers.author_key_file = str(key_file.absolute())
    if not answers.author_key_file and interactive and not args.github_app:
        pasted = getpass.getpass(
            f"API key for the {answers.author_backend or AUTHOR_BACKENDS[0]} author "
            "(hidden; blank to set later): "
        ).strip()
        if pasted:
            key_path = write_author_key(answers.author_backend, pasted, config_dir=CONFIG_DIR)
            answers.author_key_file = str(key_path)
            print(f"wrote {key_path} (0600)")

    # The image, only now: every check that could still end the run has passed
    # and the overwrite question is answered, so a download is never wasted.
    # An existing image is used, else the published one is fetched on a machine
    # that can run it (asked first when interactive); --no-image opts out.
    if not answers.image and not args.no_image:
        # the container probe is for local mode: on Slurm the image runs on
        # compute nodes, which the login node cannot speak for
        answers.image = ensure_image(interactive=interactive, probe=(answers.compute == "local"))

    # The App is the recommended credential (scoped, revocable, no plaintext
    # token); the PAT is the fallback. Offer it first when interactive.
    if not args.github_app and not pat_file and interactive:
        choice = _ask(
            "Auth — [app] create your own GitHub App (recommended) or [pat] paste a token",
            "app",
        ).lower()
        if choice.startswith("a"):
            args.github_app = True
    if args.github_app:
        org = args.org or ""
        if not org and interactive:
            # a private App installs only on the account that created it, so
            # the target's owner is the only default that can work
            owner = answers.target.split("/")[0]
            org = _ask(ORG_PROMPT, owner).strip()
        if org:
            kind = _owner_type(org)
            if not kind and interactive:
                answer = _ask(f"Is '{org}' an organization or a user account? [org/user]", "org")
                kind = "User" if answer.strip().lower().startswith("u") else "Organization"
            if kind == "User":
                org = ""  # a user account: GitHub's personal App page, not an org page
        return _github_app_setup(answers, args.app_name or "", org, interactive=interactive)

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
        if _auth_is_fatal(problem):
            # the loop opens pull requests with this token; sending the adopter
            # to `start` would fail later and further from the cause
            print(f"  auth check: FAILED — {problem}", file=sys.stderr)
            print(
                f"outerloop init: fix the token in {effective_pat} (write access to "
                f"{answers.target}), then run `outerloop init --force`",
                file=sys.stderr,
            )
            return 1
        print(f"  auth check: {'ok' if not problem else 'WARNING — ' + problem}")
        if not problem:
            # record the login the kernel will post as: every own-comment and
            # own-PR filter keys on it, and a default that is not this token's
            # identity would hide the adopter's own PRs from the kernel
            try:
                login = _token_login(Path(effective_pat).expanduser().read_text().strip())
            except OSError:
                login = ""
            if login:
                write_private(env_path, render_env(answers, effective_pat, bot_login=login))
                print(f"  posting as {login} (OUTERLOOP_BOT_LOGIN)")
            else:
                print(
                    "  the token's login could not be read — set OUTERLOOP_BOT_LOGIN in "
                    f"{env_path} before `outerloop start` (the tick skips a target without it)"
                )
        else:
            print(
                "  OUTERLOOP_BOT_LOGIN not recorded (the check did not pass) — rerun "
                "`outerloop init --force` with network access, or set it in "
                f"{env_path} (the tick skips a target without it)"
            )
    else:
        print("  no PAT set — add OUTERLOOP_PAT_FILE before the agents can open PRs")
    _author_key_hint(answers)
    _harness_hint(answers)
    print("next: outerloop start")
    return 0


def _author_key_hint(answers: InitAnswers) -> None:
    if not answers.author_key_file:
        print(
            "  no author key set — put it in "
            f"{author_key_path(answers.author_backend, config_dir=CONFIG_DIR)} "
            "before the first climb"
        )
