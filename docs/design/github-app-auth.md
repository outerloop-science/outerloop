# GitHub App auth: installation tokens replace the bot PAT

The kernel authenticates to GitHub as a bot account holding a long-lived
fine-grained PAT (`FileTokenProvider` over `~/.config/autoresearch/bot_pat`).
This note designs its replacement by a **GitHub App**: the kernel mints
short-lived installation tokens from the App's private key. It is the concrete
form of the identity seam in [external.md](external.md) ("an App
installation-token provider replaces one constructor call, not the callers").

## Why (and why now)

The public flip is the driver. A PAT is tied to a real account with broad
reach; leaked from a public-repo CI run, its blast radius is that whole
account. An App's installation tokens are **scoped** to only the installed
repositories with **fine-grained permissions**, **short-lived** (~1h,
auto-minted), **revocable** per installation, and carry **higher rate limits**
(~15k/hr per installation vs 5k/hr for a PAT). The one durable secret becomes
the App private key, which never travels to CI.

The 2026-09-01 workflow audit found the `pull_request_target` fork-exfil gap
already closed (same-repo guards on every secret-bearing PR job; the
reviewer/verifier workflows are `workflow_dispatch`, unreachable by forks), so
this App migration is the top remaining pre-flip credential item, not the
exfil fix.

## The provider

`AppInstallationTokenProvider` satisfies the existing `TokenProvider` protocol
(`token() -> str`), so it drops in where `FileTokenProvider` is constructed and
no caller changes. Each `token()`:

1. **Mint a JWT** signed RS256 with the App private key: `iss` = App ID,
   `iat` backdated 60s (clock skew), `exp` ≤ 10 min (GitHub's cap; we use
   ~9 min). The JWT authenticates *as the App*.
2. **Exchange it** at `POST /app/installations/{installation_id}/access_tokens`
   (Bearer JWT) for a **short-lived installation token** (`ghs_…`, ~1h) scoped
   to the installation's repos and permissions.
3. **Cache** the token until a refresh margin (5 min) before its `expires_at`,
   so steady-state adds no per-call network cost; re-mint only across the
   margin.

Two collaborators are injected so the provider is fully testable without a key
or network (and so the crypto dependency stays out of the hot path):

- a **signer** `Callable[[bytes], bytes]` (RS256 over the signing input) —
  production signer built lazily from the PEM via `cryptography`; tests inject
  a fake.
- a **transport** for the token-exchange POST — same pattern as `GitHubClient`,
  and the default rides the same auth-stripping opener (a redirect that changes
  host loses the `Authorization` header, so the App JWT can never be forwarded
  off `api.github.com`).

## Configuration

Three values, mirroring the PAT handling:

- **App ID** and **Installation ID**: non-secret integers (config keys).
- **Private key** (`.pem`) on the orchestrator host, `chmod 600`, never
  committed — the same custody the PAT file already has (`FileTokenProvider`
  enforces the mode; the key loader enforces it too).

## The crypto dependency

GitHub App JWTs require RS256, which the stdlib cannot sign. The production
signer uses `cryptography` (well-audited, ubiquitous). It is added to a new
`app-auth` optional extra and `uv lock`-ed **at cutover**, not now — the
scaffold injects the signer, so the provider and its tests build and run
green without the dependency. The default signer lazy-imports `cryptography`
and raises a clear "install the app-auth extra" error if absent.

## Git compatibility

Installation tokens authenticate git over HTTPS exactly as the PAT does — the
`x-access-token:<token>` extraheader in `_git_env` is unchanged. Because
tokens are short-lived, `git_network` already resolves the credential
per-invocation (it calls `auth.token()` each time), so a token that expired
between operations is simply re-minted on the next call. No git-path change.

## Redaction across refreshes

Call sites today build a fixed secrets tuple from one `bot_auth.token()`
snapshot — safe with an immortal PAT, stale the moment the App provider mints
a refresh mid-run. The provider therefore remembers every token it has ever
issued (`issued()`), and **cutover must rebuild redaction sets from
`issued()` at write time** (report publish, session output, best-effort
logging), not from a value captured at construction.

## Identity

Commits and comments shift from the `agentic-learning-bot` account to
`<app-name>[bot]`. Cosmetic but permanent — the App name is chosen once as the
bot's durable identity.

## Staged cutover (not a hot swap)

Swapping the live fleet's identity mid-campaign is an ops event:

1. **Build + unit-test** the provider (this note's scaffold) behind no live
   wiring.
2. **Validate on a throwaway repo**: point a provider instance at a scratch
   installation, confirm git push/fetch, a PR, a comment, and a
   `workflow_dispatch` all succeed with a minted token.
3. **Cut over in a quiet window**: a config flag selects the provider; keep the
   PAT provider as a one-cycle fallback, then retire it.

## Out of scope

The advisory-review workflows' `checkout_ssh_key` is a separate deploy key,
not the bot PAT; migrating it (if ever) is independent of this change.

## Permissions the App declares

Least privilege for the kernel's operations: **Contents** RW (git, board
publish, merge), **Pull requests** RW (create/comment/review/arm), **Issues**
RW (comments, labels), **Actions** RW (`workflow_dispatch` the review/verify
workflows), **Commit statuses** R (merge-on-green gate), **Metadata** R
(mandatory). Not granted: Workflows (agents are scope-gated to the solver
path, never `.github/`), Administration, Members.

## Registration metadata

The values entered when the App is created (recorded so the App can be
re-created identically). It authenticates server-to-server only, so the whole
user-authorization/OAuth half of the form is off: no callback URL, no device
flow, no user-authorization-on-install. The webhook is inactive — the kernel
polls each tick rather than receiving events — so no webhook URL, secret, or
event subscriptions. Installation is restricted to **only this account**
(`agentic-learning-ai-lab`); no organization, account, or enterprise
permissions are requested.

- **Name**: `Outerloop Autoresearch` → bot identity `outerloop-autoresearch[bot]`.
- **Homepage**: `https://outerloop.science`.
- **Description**: "Autonomous research agents working in an outer loop. Opens
  pull requests, comments, and pushes branches as the Outerloop kernel on
  research repositories. Agents launch and evaluate their own training runs on
  compute clusters, iterating on their results without human dispatch.
  Developed by the Agentic Learning AI Lab at NYU."
