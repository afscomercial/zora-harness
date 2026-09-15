# Parallel features in the zora harness

## Context

The harness runs one feature at a time, enforced by two rules that are now partly stale:

- `skills/zora-cycle/SKILL.md:85-88` pins the lane to the **main checkout**, because only
  it is served by local Tilt. Since QA moved to the VPS, remote validation needs a pushed
  commit, not a served checkout.
- The VPS runs **one job at a time**: `qa/qa-job.sh:490-498` takes a single `flock`, and
  `:117-127` refuses to start when ports 10350/5173/27017/30080-30095 are busy. A second
  dispatch bounces back as INCOMPLETE and burns an environment-rerun allowance.

Goal: two features at once — two terminal sessions, two git worktrees, two run folders —
with both QA jobs running concurrently on the first VPS. The QA protocol must also
support additional slots and additional VPS workers through configuration.

User decisions: two sessions; the cycle creates the worktree; **a Linux network namespace
per QA job** so each environment keeps today's exact ports privately; **2 slots**; land
**Stage 1 first**, then Stage 2.

Why namespaces: Tilt's UI and service port-forwards, the web app, and Codex's children
can keep the existing localhost ports inside a private Linux network namespace. This
solves listener collisions, but does not isolate shared filesystem paths, Docker state,
external services, or host privileges. Those require the ownership and isolation contracts
below. Docker remains on the host; cluster creation and build visibility must be tested.

**Developer compatibility requirement:** QA isolation is opt-in. Existing developers must
keep their current Tilt commands, configuration, profiles, ports, staging paths, and
startup behavior without migration. The only planned Pantheon change is configurable
build staging paths with exactly the current defaults. If that cannot preserve ordinary
Tilt behavior, implement filesystem isolation in the harness/VPS instead.

**Scope of this revision:** QA only. Stage 1's lane workflow remains unchanged.

## Reuse, don't rebuild

zora-pantheon already ships worktree tooling in daily use (three worktrees exist today):

- `.agents/skills/worktree-manager/scripts/create-worktree.sh <name> [base] [branch]` —
  resolves the real repo root even from inside a worktree, runs `git worktree add -b
  feat/<name> worktrees/<name> origin/main`, copies `.mcp.json` and every gitignored
  `.env`/`.env.test`, then runs `pnpm install` (required: `worktrees/*` sits outside
  `pnpm-workspace.yaml`).
- `.agents/skills/set-e2e-env` — copies `tilt/data/` and the web-app `.env` when a
  worktree must be served locally. Only needed for the fallback validator, so it is not
  part of lane setup.
- `worktrees/` is already in `.gitignore` and `.tiltignore`.

Already per-checkout, no change needed: `qa/run-codex-qa:125` resolves the repo with
`git rev-parse --show-toplevel` (in a worktree that returns the worktree);
`verdict-check.sh:35` accepts a repo-dir and uses `git -C`. Strengthen job and cluster
identity: the current timestamp-plus-commit ID can collide on same-second submissions,
and truncating a cluster name must not remove its unique suffix.

## Contracts

| Thing | Value |
|---|---|
| Lane worktree | `<pantheon>/worktrees/<slug>`, branch `feat/<slug>` |
| Run folder | `<harness>/runs/<date>-<slug>/` (unchanged) |
| Local-env owner | `<harness>/runs/.local-env-owner.d/` (atomic `mkdir`, gitignored) |
| Job ID | Logical QA request with random uniqueness; persisted before dispatch |
| Attempt ID | Unique execution ID; retries get new attempts, collection does not |
| Worker ID | Stable configured identity for a VPS; initially `vps-1` |
| Slot ID | Worker-local capacity reservation; initially two after validation |
| Environment ID | Unique per attempt; owns cluster, network, processes, staging, and data |
| Resource names | Bounded names derived from environment ID; preserve collision-resistant suffix |
| Slot lock | `$QA_HOME/slots/<slot>.lock`, owned by supervisor through cleanup |
| Admission lock | Worker-wide lock for atomic capacity reservation and maintenance exclusion |
| Build token | One bootstrap build at a time initially; not a total runtime memory guarantee |
| Result location | `<run>/qa/<job-id>/attempts/<attempt-id>/`; update callers and collection together |
| Runner version | 3 → 4, with explicit protocol compatibility checks |

Worker configuration declares SSH destination, QA home, cluster driver, slot count,
network address pool, identity bundles, resource budgets, and phase deadlines. Retain
legacy single-host configuration as a one-worker alias. Start with `QA_SLOTS=1` and
`QA_NET_ISOLATION=none`; enable `netns` and two slots only after acceptance tests pass.
Reject `none` with more than one slot. Two is an initial capacity setting, not a hard cap.

Retain queue timeout, memory/disk/load probes, and configurable DNS. Numerical admission
thresholds are provisional until measured; do not present them as proven capacity.
Specify separate queue, preparation, validation, and cleanup deadlines. Collection timeout
must not cancel a remote attempt. Reject unsupported drivers/isolation combinations.

---

## Stage 1 — Parallel lanes (harness only)

Usable the same day: two features plan, implement and gate in parallel; QA stays serial.

**`skills/zora-cycle/SKILL.md`**
- Rewrite step 2 as "Open the lane worktree": resolve the main checkout via
  `git rev-parse --path-format=absolute --git-common-dir`; reuse `lane_worktree` from the
  ledger head on resume (`git worktree list` to confirm), else create it by calling
  `create-worktree.sh <slug> origin/main feat/<slug>`. Set `LANE=<pantheon>/worktrees/<slug>`;
  every later git/turbo/pnpm command runs with `cd "$LANE"` or `-C "$LANE"`. The
  dirty-tree hard stop applies to `$LANE`. Say plainly: *`git status` in the main checkout
  is not your lane's status.*
- Delete the stale rationale at `:85-88` and replace with: QA runs remotely against a
  fresh clone of the pushed commit, so the lane must build and test, not be served; only
  the fallback validator needs a served checkout.
- Step 5: keep the rule but re-scope to **one mutating agent per lane worktree**. Add:
  before starting a second lane, write an ownership split into both ledgers' standing
  heads and give each implementer the other's file list as do-not-touch. Overlapping
  lanes are not parallel lanes — sequence them.
- Step 6: gates run in the lane; a rebase that moves the lockfile may need `pnpm install`.
- Step 8: always pass repo-dir —
  `verdict-check.sh "$RUN/qa/<job-id>" "$LANE" --remote` (and `"$RUN" "$LANE"` for the
  fallback).
- Step 9/10: PR from the lane branch; after merge or abandonment, `git worktree remove`
  (or the repo's `prune-worktrees.sh`) and note it in the ledger.
- Ledger head records `lane_slug`, `lane_branch`, `lane_worktree`, `base_commit` and
  `other_lanes:` with owned paths, so a compacted session knows which checkout it owns.
- New honesty rule: *your lane is a worktree; never run a mutating command against the
  main checkout.*

**Local environment exclusivity** (step 7's fallback branch). A markdown procedure cannot
hold a `flock` across tool calls, so use an atomic directory: `mkdir
runs/.local-env-owner.d` (fails if held), write slug, lane and timestamp inside, remove it
in the same step once the validator returns. Never steal it; if it is older than six
hours, surface it as stale and ask. Only the owner may ask the user to point local Tilt at
its lane (via `set-e2e-env` plus a user-run Tilt restart — the harness still never starts
or stops Tilt).

**`agents/zora-implementer.md:61-65`** — replace the "one shared local environment / main
checkout" framing: you work in a dedicated worktree given as an absolute path; a sibling
worktree may be busy; never `cd` outside it; never run `git worktree`, `git checkout
<branch>` or `git fetch --prune`; the shared local Tilt does not serve your worktree.

**`agents/zora-validator.md:38`** — state that the fallback drives the single shared local
environment and its fixed ports, runs only in the checkout that environment serves, one at
a time; it must compare `git -C <dir> rev-parse HEAD` against the commit the lead says is
served and return INCOMPLETE on a mismatch rather than validating the wrong tree.

**`skills/agent-browser/SKILL.md:62-72`** — one sentence above the port table: these ports
belong to the single shared local environment; exactly one lane may hold it.

**Docs** — `README.md:91-96` and `docs/HOW-IT-WORKS.md:95-98` (the "why not agent teams"
rationale keeps its other reasons but drops the single-checkout premise),
`docs/HOW-IT-WORKS.md:457` (quick reference creates/enters the lane), `run-codex-qa:12`
header.

**Verify Stage 1**: create two lanes; the main checkout stays clean and `git worktree list`
shows both; run gates in both concurrently; `run-codex-qa --dry-run` from inside lane A
passes preflight (proves worktree resolution); `verdict-check.sh <job> "$LANE_A" --remote`
passes on a real job, and the same call from the main checkout without a repo-dir
correctly reports a stale commit — proving the argument matters.

---

## Stage 2 — Concurrent QA with a worker pool contract

### 2a. Durable dispatch and queue — harness

- Add a worker inventory and explicit `--worker` selection. Initially use SSH and a
  worker-local queue; a central service or automatic scheduler is not required.
- Persist job ID, attempt ID, selected worker, commit, base, charter hash, and bundle hash
  before sending anything. Atomically create the remote attempt directory and submit
  idempotently: repeating a submission returns the existing attempt, never a second process.
- Add status, collect, and cancel operations addressed by the persisted attempt and worker.
  Reconnection collects the original attempt. An ambiguous SSH response requires status
  reconciliation; it must not automatically dispatch elsewhere and duplicate execution.
- Model execution as `queued → preparing → validating → packaging → terminal`, with
  cancellation/infra failure recorded separately from the QA verdict. Slot lifecycle is
  `free → reserved → cleaning → free`, or `retained`/`quarantined` when not reusable.
- Queue fairly using persisted submission order. Queue wait does not spend a feature-fix
  or environment-rerun allowance. Queue expiry reports capacity timeout with Codex not run.
- Write status atomically, with phase timestamps and supervisor liveness. Missing/stale
  status alone is insufficient evidence that resources are safe to delete.
- Update `zora-cycle`, dispatch output, ledger references, and checker calls for attempt
  directories, while keeping historical result directories collectable/checkable.
- Serialize mirror initialization, refresh, and cloning against mirror mutation; verify
  both commits exist. Avoid copies that depend on objects later pruned from the mirror.
- Pin the worker protocol/supervisor version. Per-job uploaded runner bundles must not
  reinterpret another active version's locks or resource ownership.

### 2b. Ownership, supervision, cleanup — harness and VPS

- Use a systemd unit per attempt for runner/agent/process supervision. Keep the slot
  reservation through teardown; prevent descendants from inheriting supervisor lock FDs.
  Do not assume `kill -9` of the shell releases locks or kills its children.
- Persist ownership before resource creation: environment ID, attempt, worker, slot,
  unit, cluster, namespace/veth/rules, paths, and creation time. Label resources where
  supported and reconcile partial creation against this record.
- A worker reaper runs on startup and periodically, including after reboot. It acquires
  the target slot lock, rechecks ownership and unit liveness, then cleans only that owner.
  Establish one documented lock order for admission, slots, and maintenance to avoid deadlock.
- Stop the attempt's processes (including Codex/Chromium), remove its cluster, then remove
  networking and private directories. Namespace deletion alone does not kill processes.
  Collect crash evidence before destructive cleanup. Make all steps repeatable.
- If cleanup cannot prove the environment is gone, quarantine the slot; do not free it.
  `QA_KEEP_CLUSTER=1` retains its slot and storage with an explicit TTL/release operation.
- Remove global cluster stopping from concurrent job startup. Provisioning can disable
  old development units once; individual jobs must not stop shared services or neighbors.
- Keep global Docker pruning outside job execution. Maintenance acquires an exclusion
  lock, confirms no active/retained environments need the resources, and prevents admissions
  until finished. Directory pruning requires ownership checks, not status checks alone.

### 2c. Opt-in Tilt staging isolation — small Pantheon change

The current `tilt/Tiltfile` uses `/tmp/tilt-dev-deps-context` and `/tmp/tilt-pruned`.
Linux network namespaces share those paths. A startup build token does not protect
against later Tilt rebuilds or validation-time commands.

- Introduce an optional staging-root variable (proposed `ZORA_TILT_STAGING_ROOT`). When
  unset, resolve to exactly today's existing paths and generated build commands. When
  set, derive both staging locations beneath that root and propagate them to every
  related build script. Audit all shared temporary paths before considering this complete.
- The harness sets a host-visible absolute directory unique to the attempt. Quote paths
  correctly and validate the override. Keep it out of source-watched paths to avoid loops.
- Do not change default profiles, dependency selection, image behavior, ports, Kubernetes
  manifests, application logic, or developer startup instructions. No developer migration.
- Audit shared mutable Docker tags/custom builds. Verify concurrent builds deploy the
  intended images; record deployed image IDs/digests. If serialization is needed for a
  shared mutation, cover every occurrence, including validation-time rebuilds.
- Land the backward-compatible Pantheon prerequisite before enabling concurrent QA.
  A worker must detect commits lacking staging isolation and queue them for exclusive
  execution. Do not patch the frozen checkout to hide missing support.
- If the override cannot preserve default behavior, use private staging mounts on the VPS
  instead; prove host Docker build-context/mount visibility before enabling that fallback.

### 2d. Kind network isolation — harness and VPS

- Implement Kind first and configure the concurrent worker explicitly to use it. The
  current runner defaults to k3d; preserve its serial path but reject concurrent k3d until
  API routing, registry names/ports, storage, and teardown have their own implementation/tests.
- Create an environment-owned netns/veth pair using addresses reserved from the worker
  pool. Check subnet overlap and actual Docker networks; do not hardcode `172.18.0.0/16`.
- Bring loopback up, configure routes/forwarding and narrowly scoped firewall rules, and
  use reachable DNS resolvers rather than the host-only systemd stub. Tag all rules by
  environment ownership. Verify DNS and outbound dependency access before bootstrap.
- Wrap Tilt, its readiness poller, web app/probes, seed hooks, browser self-test, Codex
  and descendants, and Tilt teardown in the same network namespace. Keep supervision
  outside that wrapper. Review shell/environment propagation explicitly.
- Create Kind on the host, retain per-checkout data mounts, and bind its API to the
  reserved reachable host-side address. Read the actual endpoint from the generated
  kubeconfig; Kind's default host API port is random, not necessarily 6443.
- Probe authenticated `/readyz` using that kubeconfig's endpoint, CA, and credentials from
  inside the namespace. Do not use `curl -k` as the readiness/identity check.
- Retain fixed private localhost service ports. Scan for stale listeners within the
  namespace. Do not expose QA service ports publicly.
- Keep a private kubeconfig per attempt and check the expected context explicitly in
  every runner operation. Network isolation prevents collisions, not privileged access
  to other environments.

### 2e. Capacity and failure attribution

Read-only VPS snapshot during planning: 8 CPUs, 32,094 MiB total RAM, 30,907 MiB available,
335 GB free disk, no swap, cgroup v2, and no running Docker containers. This idle snapshot
supports trying two environments; it does not establish their peak requirements.

- Atomically reserve profile-specific capacity under the worker admission lock. Combine
  configured reservations and OS headroom probes so two arrivals cannot both claim the
  same available memory. Reserve host/Docker overhead and retained-environment capacity.
- Initially serialize dependency installation and bootstrap builds through Tilt readiness.
  Validation also runs tests/builds/browsers: account for those peaks and apply runtime
  budgets; the startup token alone is not a memory bound.
- Measure and configure limits/accounting for both host job processes and Kind containers.
  Docker-created containers do not automatically belong to the runner's systemd cgroup.
  Include shared daemon/BuildKit consumption in worker headroom.
- Record peak memory, CPU usage, disk growth, wait times, and phase durations by supported
  service profile. Reject or queue a profile that does not fit alongside existing reservations.
- Record pod OOMKilled/restarts, relevant container/process cgroup events, and worker
  pressure over the attempt's time window. Attribute incidents to the correct environment.
- Infrastructure exhaustion produces INCOMPLETE with an infrastructure reason and retained
  evidence. A feature's reproducible excessive memory use can be a product failure;
  do not automatically downgrade every OOM to INCOMPLETE. Ambiguous cases retain the
  finding and require a controlled rerun with adequate capacity.

### 2f. Test identity and external dependencies

- Allocate a matching bundle per worker/slot: `QA_AUTH_FILE`, `QA_IDENTITY_FILE`, and
  sandbox integration configuration. Seed the same identity used for browser sign-in.
- No silent shared-auth fallback for concurrent authenticated QA. Shared accounts require
  an explicitly verified concurrency policy; otherwise report configuration unavailable.
- Keep Mongo data, browser profiles, downloads, scratch, and evidence private per attempt.
- Audit external mutable services: queues, caches, object storage prefixes, callbacks,
  webhooks, and sandbox providers. Isolate by environment ID, use test doubles where
  appropriate, or reserve exclusive access. Separate clusters alone do not isolate them.
- Preserve existing shared web-cache disabling and browser boundary checks. Keep credentials
  out of charters/evidence and preserve redaction before packaging.

### 2g. Evidence and checker contract

- Extend dispatch and remote manifest with job/attempt/worker/slot/environment identity,
  exact commit/base, charter hash, runner bundle hash, selected services/profile, deployed
  image identities, queue/build waits, and infrastructure health observations.
- Update `verdict-check.sh` to compare those fields against the persisted expected dispatch
  and reject missing/mismatched identity for the new protocol. Retain explicit legacy checks
  for old manifests; never silently accept an incomplete new manifest as legacy.
- Preserve existing clean-checkout, commit, rung, and evidence checks. Hashes establish
  artifact consistency, not protection from host-root tampering.
- Keep acceptance criteria and required rungs in the charter; routing metadata belongs in
  dispatch. Update the verdict schema only as needed for explicit infrastructure attribution.
- Update the QA prompt with the environment's context/paths and scoped operations. Document
  shared-host trust honestly: prompts do not enforce isolation against a root agent.
- Publish terminal state only after the result archive is complete and atomically available.
  Cleanup outcome remains visible separately; a valid verdict must not conceal a leaked slot.

### 2h. Verification and rollout gates

Mocked Linux tests (`qa/test-runner.py`) retain a stable `# --- entrypoint ---` split.
Cover allocation fairness, concurrent admission, duplicate dispatch, inherited FDs,
phase expiry, cancellation, version mismatch, stale ownership, reaper/admission races,
quarantine, retention, maintenance exclusion, identity bundle matching, network wrappers,
endpoint discovery, manifest mismatch rejection, and safe collection. Preserve existing
extractor tests. Mocks are necessary but cannot prove Linux/Docker isolation.

Real acceptance tests:

1. **Developer compatibility:** with no QA override, verify current staging paths and
   generated build commands are unchanged; run ordinary Tilt startup/build/rebuild smoke
   checks on the supported developer setup. No new configuration or migration is allowed.
2. **Single-slot regression:** existing serial QA completes with current ports, profiles,
   authentication, and verdict checks; `none` with multiple slots is rejected.
3. **Two environments:** different commits return distinguishable expected behavior, both
   finish their required rungs, and dispatch/manifests identify their own deployed images.
4. **Data isolation:** the same fixture ID holds different values in each database;
   browser sessions and external sandbox state do not leak between attempts.
5. **Rebuild isolation:** rebuild A during B's validation, including staging operations;
   B's deployed images, data, and responses remain correct.
6. **Capacity:** test simultaneous arrivals and overlapping validation-time test/build
   peaks for supported profiles. Record measurements; tune budgets before enabling two slots.
7. **Queue:** a third job waits fairly and starts after capacity is released. Queue time
   does not consume the feature's retry allowance.
8. **Crash recovery:** kill A's runner and separately its supervisor; verify descendants
   and resources are reconciled, B remains healthy, and A's slot is not reused prematurely.
   Also test reboot recovery and interruption during resource creation/cleanup.
9. **Cleanup failure/retention:** injected cleanup failure quarantines the slot; kept
   clusters retain capacity until explicit release or TTL cleanup succeeds.
10. **Transport:** disconnect SSH, then collect the original attempt without redispatch;
    repeat an ambiguous submission and prove there is still one execution.
11. **Clean teardown:** no attempt-owned processes, cluster containers, networking/rules,
    or private work/staging paths remain after ordinary cleanup.
12. **Worker portability:** test inventory/routing against two logical workers initially;
    before claiming real multi-VPS support verified, run the same protocol on a second VPS.
    Collection and cancellation must use the recorded worker even if the default changes.

Delivery order:

1. Worker/attempt identity, supervised durable queue, status/collect/cancel; one slot.
2. Ownership/recovery, identity bundles, and backward-compatible staging isolation.
3. Kind network isolation, resource measurements, and two-slot acceptance tests.
4. Second-worker provisioning and end-to-end portability verification when available.

Rollback: stop new admissions, drain or clean active environments, then configure one
slot with `QA_NET_ISOLATION=none`. Do not switch isolation underneath live attempts.

## Risks and boundaries

- Both agents initially run as root on a shared Docker host. This is collision isolation
  among trusted jobs, not a security boundary. A Docker socket proxy alone cannot contain
  host root. Stronger isolation requires a separate VM or a separately designed restricted
  execution boundary. Suspicious evidence is rerun alone.
- Capacity measurements may show some profile pairs cannot fit; queue them or route to
  another worker rather than reducing QA coverage to achieve concurrency.
- Shared external systems and mutable build state require explicit inventory and tests;
  neither netns nor separate Kubernetes clusters eliminates those dependencies.
- Default Tilt compatibility is a release gate. No change to other developers' regular
  workflow is acceptable for this QA feature.

## Out of scope

Changes to Stage 1's lane design; two local developer Tilt clusters; application business
logic; automatic cross-worker scheduling; concurrent k3d until separately implemented;
strong isolation against hostile/root jobs. Provisioning a second VPS can follow the
initial rollout, but worker identity/configuration is included now. More than two slots
requires measured capacity, not a hardcoded runner limit.

## Technical references

- [Kind configuration](https://kind.sigs.k8s.io/docs/user/configuration/): API endpoint and port behavior.
- [Linux network namespace lifecycle](https://www.man7.org/linux/man-pages/man8/ip-netns.8.html): namespace deletion and remaining processes.
- [Docker security](https://docs.docker.com/engine/security/): shared daemon and host privilege implications.
