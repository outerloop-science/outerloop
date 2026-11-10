# Storage

Outerloop keeps its whole state in plain files and git branches, with no database anywhere. This page maps what lives where: the agent's workspace, the kernel's run records on a shared filesystem, and the research state in git branches on GitHub. The [kernel page](how-it-works.md) covers the components; the [concurrency page](concurrency.md) covers why each write is safe to repeat.

## The storage map

<figure class="ol-fig">
<svg viewBox="0 0 1600 975" role="img" aria-label="Outerloop storage. The kernel is the only writer. Run state lives as atomic files on a shared filesystem, research state lives in git branches on GitHub, and Slurm job states are a third source of truth. The agent works in its own workspace and reaches the kernel through .outerloop.">
<defs>
<marker id="s-flow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="ah-flow"/></marker>
</defs>

<rect x="30" y="60" width="500" height="96" rx="6" class="box sb"/>
<text x="48" y="90" class="bt" text-anchor="start">Agent session (sandbox)</text>
<text x="48" y="114" class="bl" text-anchor="start">sees only its workspace and run home ·</text>
<text x="48" y="136" class="bl" text-anchor="start">reaches the kernel through .outerloop/</text>

<rect x="580" y="60" width="440" height="96" rx="6" class="box kc"/>
<text x="598" y="90" class="bt" text-anchor="start">Kernel tick</text>
<text x="598" y="114" class="bl" text-anchor="start">the only writer of kernel state · reasons</text>
<text x="598" y="136" class="bl" text-anchor="start">only from these files plus Slurm</text>
<rect x="1170" y="60" width="400" height="96" rx="6" class="box x"/>
<text x="1188" y="90" class="bt" text-anchor="start">Slurm</text>
<text x="1188" y="114" class="bl" text-anchor="start">job states are a third</text>
<text x="1188" y="136" class="bl" text-anchor="start">source of truth</text>

<polyline points="1020,92 1170,92" class="flow" marker-end="url(#s-flow)"/>
<text x="1095" y="84" class="al" text-anchor="middle">sbatch</text>
<polyline points="1170,128 1020,128" class="flow" marker-end="url(#s-flow)"/>
<text x="1095" y="148" class="al" text-anchor="middle">job states</text>

<rect x="30" y="220" width="500" height="556" rx="10" class="box sb"/>
<text x="48" y="250" class="bt" text-anchor="start">Agent workspace</text>
<text x="48" y="272" class="pz" text-anchor="start">inside the sandbox · .outerloop/ is excluded from the diff</text>
<line x1="44" y1="290" x2="516" y2="290" class="rule"/>
<text x="48" y="318" class="mono" text-anchor="start">train.py (allowed paths)</text>
<text x="48" y="340" class="bl" text-anchor="start">the code the agent edits</text>
<rect x="384.6" y="325" width="127" height="22" rx="11" class="chip"/>
<text x="448.3" y="340.5" class="ct" text-anchor="middle">frozen at launch</text>
<line x1="44" y1="356" x2="516" y2="356" class="rule"/>
<text x="48" y="384" class="mono" text-anchor="start">AGENT_MEMORY.md</text>
<text x="48" y="406" class="bl" text-anchor="start">notes on its research line</text>
<rect x="377.2" y="391" width="135" height="22" rx="11" class="chip"/>
<text x="444.6" y="406.5" class="ct" text-anchor="middle">pushed at run end</text>
<line x1="44" y1="422" x2="516" y2="422" class="rule"/>
<text x="48" y="450" class="mono" text-anchor="start">.outerloop/syscall</text>
<text x="48" y="472" class="bl" text-anchor="start">the CLI the agent calls</text>
<rect x="377.2" y="457" width="135" height="22" rx="11" class="chip"/>
<text x="444.6" y="472.5" class="ct" text-anchor="middle">kernel-owned copy</text>

<line x1="44" y1="488" x2="516" y2="488" class="rule"/>
<text x="48" y="516" class="mono" text-anchor="start">.outerloop/syscall.json</text>
<text x="48" y="538" class="bl" text-anchor="start">committed sleep, stop or verdict ·</text>
<text x="48" y="558" class="bl" text-anchor="start">request.json stages first</text>
<rect x="384.6" y="523" width="127" height="22" rx="11" class="chip"/>
<text x="448.3" y="538.5" class="ct" text-anchor="middle">kernel validates</text>

<line x1="44" y1="576" x2="516" y2="576" class="rule"/>
<text x="48" y="604" class="mono" text-anchor="start">.outerloop/messages.json</text>
<text x="48" y="626" class="bl" text-anchor="start">inbox view · siblings.json too</text>
<rect x="362.4" y="611" width="150" height="22" rx="11" class="chip"/>
<text x="437.2" y="626.5" class="ct" text-anchor="middle">refreshed each wake</text>
<line x1="44" y1="642" x2="516" y2="642" class="rule"/>
<text x="48" y="670" class="mono" text-anchor="start">.outerloop/results/[launch]/</text>
<text x="48" y="692" class="bl" text-anchor="start">files brought back from jobs</text>
<line x1="44" y1="708" x2="516" y2="708" class="rule"/>
<text x="48" y="736" class="mono" text-anchor="start">run home</text>
<text x="48" y="758" class="bl" text-anchor="start">harness config, session to resume</text>
<rect x="451.2" y="743" width="61" height="22" rx="11" class="chip"/>
<text x="481.6" y="758.5" class="ct" text-anchor="middle">per run</text>

<rect x="550" y="220" width="510" height="536" rx="10" class="box k"/>
<text x="568" y="250" class="bt" text-anchor="start">Shared filesystem</text>
<text x="568" y="272" class="pz" text-anchor="start">the operator's state root · private</text>
<line x1="564" y1="290" x2="1046" y2="290" class="rule"/>
<text x="568" y="318" class="mono" text-anchor="start">runs/[run-id]/state.json</text>
<text x="568" y="340" class="bl" text-anchor="start">stage, meter, sealed commit</text>
<rect x="936.8" y="325" width="105" height="22" rx="11" class="chip"/>
<text x="989.4" y="340.5" class="ct" text-anchor="middle">atomic rename</text>
<line x1="564" y1="356" x2="1046" y2="356" class="rule"/>
<text x="568" y="384" class="mono" text-anchor="start">runs/[run-id]/lease.json</text>
<text x="568" y="406" class="bl" text-anchor="start">who may wake this run</text>
<rect x="914.6" y="391" width="127" height="22" rx="11" class="chip"/>
<text x="978.3" y="406.5" class="ct" text-anchor="middle">O_EXCL · expires</text>
<line x1="564" y1="422" x2="1046" y2="422" class="rule"/>
<text x="568" y="450" class="mono" text-anchor="start">runs/[run-id]/inbox/000007.json</text>
<text x="568" y="472" class="bl" text-anchor="start">messages, at least once, in order</text>
<rect x="899.8" y="457" width="142" height="22" rx="11" class="chip"/>
<text x="970.9" y="472.5" class="ct" text-anchor="middle">flock · dedupe key</text>

<line x1="564" y1="488" x2="1046" y2="488" class="rule"/>
<text x="568" y="516" class="mono" text-anchor="start">runs/[run-id]/outbox/</text>
<text x="568" y="538" class="bl" text-anchor="start">replies kept before posting</text>
<rect x="951.6" y="523" width="90" height="22" rx="11" class="chip"/>
<text x="996.8" y="538.5" class="ct" text-anchor="middle">fsync first</text>
<line x1="564" y1="554" x2="1046" y2="554" class="rule"/>
<text x="568" y="582" class="mono" text-anchor="start">runs/baselines/</text>
<text x="568" y="604" class="bl" text-anchor="start">baseline per base commit</text>
<rect x="907.2" y="589" width="135" height="22" rx="11" class="chip"/>
<text x="974.6" y="604.5" class="ct" text-anchor="middle">keyed by base sha</text>
<line x1="564" y1="620" x2="1046" y2="620" class="rule"/>
<text x="568" y="648" class="mono" text-anchor="start">tick lease</text>
<text x="568" y="670" class="bl" text-anchor="start">one kernel acts at a time</text>
<rect x="922.0" y="655" width="120" height="22" rx="11" class="chip"/>
<text x="982.0" y="670.5" class="ct" text-anchor="middle">heartbeat + TTL</text>
<line x1="564" y1="686" x2="1046" y2="686" class="rule"/>
<text x="568" y="714" class="mono" text-anchor="start">transcripts</text>
<text x="568" y="736" class="bl" text-anchor="start">every session, keys redacted</text>

<rect x="1080" y="220" width="490" height="470" rx="10" class="box k2"/>
<text x="1098" y="250" class="bt" text-anchor="start">Git on GitHub</text>
<text x="1098" y="272" class="pz" text-anchor="start">the target repository · kernel identity</text>
<line x1="1094" y1="290" x2="1556" y2="290" class="rule"/>
<text x="1098" y="318" class="mono" text-anchor="start">main</text>
<text x="1098" y="340" class="bl" text-anchor="start">merged code · next runs start here</text>
<rect x="1424.6" y="325" width="127" height="22" rx="11" class="chip"/>
<text x="1488.3" y="340.5" class="ct" text-anchor="middle">up-to-date merge</text>
<line x1="1094" y1="356" x2="1556" y2="356" class="rule"/>
<text x="1098" y="384" class="mono" text-anchor="start">feat/auto/agent-NN/[run]</text>
<text x="1098" y="406" class="bl" text-anchor="start">one PR branch per submission</text>
<rect x="1446.8" y="391" width="105" height="22" rx="11" class="chip"/>
<text x="1499.4" y="406.5" class="ct" text-anchor="middle">kernel pushes</text>
<line x1="1094" y1="422" x2="1556" y2="422" class="rule"/>
<text x="1098" y="450" class="mono" text-anchor="start">agents/agent-NN</text>
<text x="1098" y="472" class="bl" text-anchor="start">research line + AGENT_MEMORY.md</text>
<rect x="1424.6" y="457" width="127" height="22" rx="11" class="chip"/>
<text x="1488.3" y="472.5" class="ct" text-anchor="middle">kept across runs</text>

<line x1="1094" y1="488" x2="1556" y2="488" class="rule"/>
<text x="1098" y="516" class="mono" text-anchor="start">research-log</text>
<text x="1098" y="538" class="bl" text-anchor="start">BENCHMARKS.md, leaderboard data</text>
<rect x="1424.6" y="523" width="127" height="22" rx="11" class="chip"/>
<text x="1488.3" y="538.5" class="ct" text-anchor="middle">compare-and-swap</text>
<line x1="1094" y1="554" x2="1556" y2="554" class="rule"/>
<text x="1098" y="582" class="mono" text-anchor="start">research-log (reports)</text>
<text x="1098" y="604" class="bl" text-anchor="start">every attempt's report</text>
<line x1="1094" y1="620" x2="1556" y2="620" class="rule"/>
<text x="1098" y="648" class="mono" text-anchor="start">pending → confirmed</text>
<text x="1098" y="670" class="bl" text-anchor="start">only when the merge carries</text>

<polyline points="280,156 280,220" class="flow" marker-end="url(#s-flow)"/>
<text x="292" y="196" class="al" text-anchor="start">edits, notes</text>
<polyline points="800,156 800,220" class="flow" marker-end="url(#s-flow)"/>
<text x="812" y="196" class="al" text-anchor="start">atomic file writes</text>
<polyline points="1000,156 1000,186 1300,186 1300,220" class="flow" marker-end="url(#s-flow)"/>
<text x="1150" y="180" class="al" text-anchor="middle">bot identity</text>
<polyline points="620,156 620,186 420,186 420,220" class="flow" marker-start="url(#s-flow)" marker-end="url(#s-flow)"/>
<text x="520" y="180" class="al" text-anchor="middle">syscall.json in · messages, results out</text>

<rect x="20" y="790" width="1560" height="172" rx="10" class="zone-k"/>
<text x="36" y="816" class="zt" text-anchor="start">CONTAINERS · one image, two places it runs</text>
<rect x="36" y="830" width="370" height="118" rx="6" class="box k2"/>
<text x="50" y="856" class="bt" text-anchor="start">Agent image</text>
<text x="50" y="880" class="bl" text-anchor="start">agent-py312.sif, fetched once from</text>
<text x="50" y="900" class="bl" text-anchor="start">Hugging Face to ~/outerloop-images</text>
<text x="50" y="920" class="bl" text-anchor="start">(or OUTERLOOP_IMAGE)</text>

<rect x="422" y="830" width="370" height="118" rx="6" class="box k2"/>
<text x="436" y="856" class="bt" text-anchor="start">Session container</text>
<text x="436" y="880" class="bl" text-anchor="start">authors and judges run in the image,</text>
<text x="436" y="900" class="bl" text-anchor="start">--containall · binds workspace, run</text>
<text x="436" y="920" class="bl" text-anchor="start">home, harness files read-only</text>

<rect x="808" y="830" width="370" height="118" rx="6" class="box k2"/>
<text x="822" y="856" class="bt" text-anchor="start">Job container (compute node)</text>
<text x="822" y="880" class="bl" text-anchor="start">frozen worktree, node-local scratch</text>
<text x="822" y="900" class="bl" text-anchor="start">--containall --cleanenv · --nv for GPUs</text>
<text x="822" y="920" class="bl" text-anchor="start">uv cache seeded · gone at exit</text>
<rect x="1194" y="830" width="370" height="118" rx="6" class="box k2"/>
<text x="1208" y="856" class="bt" text-anchor="start">Harness installs</text>
<text x="1208" y="880" class="bl" text-anchor="start">Claude Code · Codex · Hermes,</text>
<text x="1208" y="900" class="bl" text-anchor="start">pinned by version and sha256</text>
<text x="1208" y="920" class="bl" text-anchor="start">(harnesses.toml)</text>
</svg>
<figcaption>The diagram shows the three stores and every family of file. Orange chips carry the guarantee each row relies on.</figcaption>
</figure>

Three stores hold everything. The agent works in its own workspace inside the sandbox. The kernel's state root on the shared filesystem is private to the deployment. The target repository on GitHub holds the merged code, the pull request branches and the research ledger. Slurm's job states are a third source of truth, which the kernel reads but never owns.

## The agent workspace

Each run owns one workspace, a git clone of the target at `runs/<run-id>/ws`, and one run home at `runs/<run-id>/ws-home` for the harness's own config and resume state. The agent sees only these two directories, plus the model endpoint it talks to.

The `.outerloop/` directory inside the workspace is the kernel's channel. It holds the syscall CLI the kernel copies in, the staged and committed requests the CLI writes, the message view and fleet snapshot refreshed at each wake, and the artifacts brought back from jobs. The kernel excludes this directory from the git diff, so requests and delivered results never enter a candidate, a scope check or a fingerprint. Judges work on author-written checkouts, so the kernel rebuilds the channel fresh before they run.

## Run state and leases

A run's record is one JSON file, `runs/<run-id>/state.json`, written by atomic rename. A crash mid-write leaves the previous record intact, and the sweep reasons only from these files plus Slurm, never from process memory. The same run directory holds the wake lease, the inbox, the outbox of replies kept before posting, and the redacted transcript of every session. The kernel also appends `trajectory.jsonl`, a [hash-chained event timeline](design/trajectory.md), under a sibling `trajectory.lock`. Beside the run directories, a baseline cache holds the measured baseline that a `cached` contract shares among all attempts on one base commit.

Unknown fields are ignored on read, and new formats for run records are migrated as the kernel reads them, so an upgrade needs no migration step unless the changelog's Upgrading notes for that release name one. That compatibility runs forward only: an older kernel cannot interpret newer record formats, so a rollback past a format change needs its runs drained or their records translated by hand. After a run ends, its workspace and run home are removed after a grace period of 24 hours by default; the record, the report, the transcripts and the ledger entries stay.

## The research ledger

Research state lives on the target repository's `research-log` branch, and only the kernel writes to it, through the configured GitHub identity. The branch carries `results/leader.json` and `BENCHMARKS.md`, the leaderboard and its rendered view. Each pending submission is one JSON file under `results/submissions/`, and every attempt's report sits under `reports/`. Each ledger write is a compare-and-swap on the branch head, retried when the head moved; the [concurrency page](concurrency.md) covers the mechanism.

## Research-line branches

Research lines are a contract option, off by default. When the contract turns them on, each agent slot works on its own branch, `agents/agent-NN`, kept across runs. The branch carries the agent's `AGENT_MEMORY.md` notes and its accumulated code history, merged from the base branch at run start. A submission publishes from a separate branch, `feat/auto/agent-NN/<run>`, one per pull request. Only the kernel pushes either kind of branch.

## Frozen snapshots

Submitted jobs never run from the agent's live workspace. At each launch or measurement the kernel commits the workspace content as a snapshot, parented on the base commit. A temporary ref keeps it reachable, so garbage collection cannot prune it while a job is queued. The job materializes the snapshot by checkout into a worktree on node-local scratch and runs the contract command inside the container. The ref is dropped once the result has been read.

## Container images

One Apptainer image, `agent-py312.sif`, serves the whole system. It is fetched once from Hugging Face to `~/outerloop-images`, or named explicitly with `OUTERLOOP_IMAGE` (see the [environment reference](reference/environment.md)). Contained sessions run inside it with the workspace, the run home and the harness's own files bound in. Compute-node jobs run inside it with a clean environment, GPUs exposed, a seeded `uv` cache and a worktree that is deleted at exit. Harness binaries are pinned by version and SHA-256. A local Claude run can also go without the image, uncontained, as [Install](install.md) describes.

## No database by design

Three properties carried more weight than query convenience.

The first is crash safety. Every change is a whole file written by rename, a lock-guarded append or a compare-and-swap on a branch head, so no step leaves half-written state. The kernel can be killed at any line and resume from what is on disk.

The second is portability. A shared filesystem and git behave the same on a university cluster, on cloud Slurm and on one workstation. The third is auditability. The record is plain files and git history that a human can read without the kernel's help.
