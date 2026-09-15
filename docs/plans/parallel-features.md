# Parallel features in the zora harness

Last reconciled: **2026-09-15**. Stage 1 lane workflows are implemented on `main`.
The user subsequently replaced the QA architecture: **harness/VPS-only isolation,
with no Pantheon modifications**. Protocol-5 runtime isolation and real supervisor integration have passed; the worker
is installed with two sandbox slots. Full application QA and capacity acceptance remain pending.

## Goal and current scope

Independent feature worktrees and run folders, with concurrent remote QA on one or
more workers. Two slots are the first VPS target, not a protocol limit. Extra attempts
queue; bootstrap is serialized and ready environments may validate concurrently.

The first QA rollout depended on opt-in Pantheon staging changes. That approach is
superseded. [Pantheon PR #1897](https://github.com/HouseNumbers/zora-pantheon/pull/1897)
is to be closed unmerged. Feature branches must not merge or cherry-pick it for QA.
The replacement runs the original pushed commit and its existing Tilt configuration.

## Delivery tracker

| ID | Feature | Status | Delivered / remaining |
|---|---|---|---|
| L1 | Lane worktree creation, resume, ownership and per-lane gates | Complete | Implemented in `78b1375`; two-lane live verification remains. |
| L2 | Exclusive local fallback environment across lanes | Complete | Existing local ownership claim and served-HEAD checks remain; this fallback is unaffected by mandatory remote sandboxes. |
| Q1 | Worker routing, immutable dispatch, durable queue and reconnection | Retained; v5 verification pending | Keep UUID attempts, persisted routing, FIFO admission, collection lock and cancellation; validate with protocol 5. |
| Q2 | Supervisor ownership, recovery, quarantine and retention | Adapting | Supervisor remains outside a separate sandbox unit; stop and prove sandbox resources gone before slot reuse. |
| Q3 | Pantheon staging prerequisite | Superseded | No Pantheon edits, staging override or capability-marker check; no serial remote fallback. |
| Q4 | Private filesystem, process and network environment | Runtime proof passed | Private `/tmp`, `/var/tmp`, `/run`, `/dev/shm`, HOME/tool state and process/network namespaces. Existing Tilt paths must work unchanged. |
| Q5 | Private Docker daemon and storage | Runtime proof passed | Own socket, daemon, images, containers and build cache per attempt; no host Docker socket access. Verify standard Tilt builds and cleanup. |
| Q6 | Capacity and telemetry | Remeasure | Proposed 13,000 MiB aggregate sandbox budget includes runner/browser, Docker/BuildKit and Kind; keep host headroom and profile admission. Prior split-budget measurements are historical. |
| Q7 | Evidence and protocol compatibility | Adapting | New execution requires protocol 5 and a sandbox; retain checksummed identity and crash redaction. Historical artifacts are not proof of current isolation. |
| Q8 | Test identity and external integration isolation | Retained / partial | Approved matching identity bundles; same account only under explicit concurrent-login policy. External mutable queues/storage/callbacks still need audit. |
| Q9 | First VPS two-slot sandbox rollout | Pending acceptance | Repeat cold bootstrap, two commits, rebuild/prune isolation, crash, queue, retention and full teardown checks. |
| Q10 | Second VPS | Pending | Worker inventory exists; repeat dispatch/collect/cancel and sandbox proof on another physical host. |
| Q11 | Host maintenance and artifact GC | Partial | Installer exclusion exists; ownership-aware historical artifact cleanup and general maintenance interface remain. |

## Replacement QA contract (protocol 5)

- **Unchanged repository:** clone the exact pushed commit, verify clean tracked files,
  and never patch Tilt, scripts, Kubernetes manifests or business code for isolation.
- **Mandatory sandbox:** every new remote attempt receives its own filesystem/process/
  network environment. Unsupported workers/configurations fail explicitly; no legacy
  exclusive host execution or staging-marker bypass is offered.
- **Private defaults:** ordinary `/tmp/tilt-pruned`, `/tmp/tilt-dev-deps-context`, HOME,
  tool runtime state and service ports belong to the sandbox. Shared-path developer
  scripts must not reach another attempt's resources.
- **Private Docker:** the sandbox owns its daemon, socket, data root and build cache.
  Kind creates and loads images through that daemon. Mutable tags, Docker pruning and
  BuildKit cannot mutate the host daemon or a neighbor. No shared host socket mount.
- **Supervision:** the host supervisor owns admission and a separate sandbox unit.
  Persist ownership before creating resources. Aggregate sandbox limits account for
  Docker/BuildKit/Kind as well as runner/browser processes; descendants cannot escape
  accounting through the host daemon. On failure, stop writers before redaction.
- **Capacity:** propose two 13,000 MiB reservations plus 5,000 MiB host headroom for
  the 32,094 MiB VPS. This is a trial budget, not certification. Measure cold-build
  disk growth and aggregate peaks before accepting two simultaneous feature environments.
- **Recovery:** cleanup validates ownership, stops the sandbox, unmounts owned mounts,
  removes private Docker storage/networking and checks absence. Failed proof quarantines
  the slot. Retained sandboxes occupy capacity until release/expiry. Do not prune host
  Docker while performing an attempt's cleanup.
- **Evidence:** immutable job/attempt/worker/environment/commit/charter binding;
  record sandbox identity, actual deployed images, health, memory events and cleanup.
  Finalize and redact the archive before making terminal results collectible.
- **Compatibility:** preserve Stage 1 lanes and their exclusive local-validator fallback.
  The protocol-5 dispatcher and checker reject historical protocol-4 artifacts;
  the historical record below is documentation only.

Implementation sources: [dispatcher](../../skills/zora-cycle/qa/qa-dispatch.py),
[worker](../../skills/zora-cycle/qa/qa-worker.py),
[runner](../../skills/zora-cycle/qa/qa-job.sh),
[installer](../../skills/zora-cycle/qa/install-worker.sh),
[checker](../../skills/zora-cycle/verdict-check.sh), and
[operator guide](../../skills/zora-cycle/qa/README.md).

## Replacement acceptance tracker

Runtime sandbox isolation and real supervisor lifecycle integration have passed on the
VPS, and the protocol-5 worker is installed with two slots. Offline regressions passed:
25 worker, 20 runner, 19 dispatcher, 7 sandbox and 3 extractor tests (74 total).
The sandbox checks are [test-sandbox.py](../../skills/zora-cycle/qa/test-sandbox.py);
its live opt-in proof is [test-sandbox-integration.py](../../skills/zora-cycle/qa/test-sandbox-integration.py).
These results do not certify the full application workload or its capacity.

Two full application attempts are preparing against distinguishable original commits:
`80131b2` (web-app 3.30.0) and `8100c4f` (web-app 3.29.1). Their QA verdicts, concurrent
build/prune behavior and aggregate capacity remain pending. The gates below require
that broader acceptance; historical protocol-4 evidence is not replacement evidence.

| Gate | Required evidence |
|---|---|
| S1 Repository unchanged | Run original Pantheon commits with no staging-support marker or patch; clean tracked-file checks before/after. No new developer instructions. |
| S2 One sandbox | Full five-rung QA and local evidence verification; normal Tilt staging paths, auth, browser and private daemon work. |
| S3 Two different features | Two distinguishable commits validate simultaneously with correct responses, database values and deployed images. |
| S4 Build and cleanup isolation | Rebuild/tag/prune in A's private daemon while B validates; verify B's images, data and responses and host Docker state remain unchanged. |
| S5 Filesystem/process isolation | Same temporary filenames and tool paths coexist without leakage; A's process cleanup cannot terminate B. Confirm mounts, HOME and daemon ownership. |
| S6 Capacity and queue | Measure aggregate cold-build/validation memory and disk for intended profiles. Third full job waits then runs; queue time is not a feature retry. |
| S7 Failure and transport | Cancel/SIGKILL/SSH loss/reboot/partial creation; B remains healthy; sanitize after writers stop and publish a collectible result. |
| S8 Teardown and retention | Prove processes, units, mounts, private Docker data/containers and networking gone; injected failure quarantines; retained slot releases on expiry/operator action. |
| S9 Portability | Repeat selected dispatch/collect/cancel and sandbox proof on a second physical VPS. |

## Stage 1 — Parallel lanes: implemented, not yet live-verified

Delivered in `78b1375` on `main`. Two features plan, implement and gate in separate lane
worktrees. **The rest of this section is the specification as written before
implementation**, kept as the record of what was asked for; the delivered code follows it.
The verification at its end is only partly done — see the checklist there.
QA already supports concurrent attempts; this stage must integrate with the existing
mandatory-sandbox protocol-5 dispatcher, not reintroduce serial QA. Reuse Pantheon’s
`.agents/skills/worktree-manager/scripts/create-worktree.sh` for creation and local
runtime-file setup. Use `.agents/skills/set-e2e-env` only when local fallback needs
a served worktree; do not copy existing Mongo data into remote QA environments.

**`skills/zora-cycle/SKILL.md`**

- Rewrite step 2 as "Open the lane worktree": resolve the main checkout via
  `git rev-parse --path-format=absolute --git-common-dir`; reuse `lane_worktree` from the
  ledger head on resume (`git worktree list` to confirm), else create it by calling
  `create-worktree.sh <slug> origin/main feat/<slug>`. Set `LANE=<pantheon>/worktrees/<slug>`;
  every later git/turbo/pnpm command runs with `cd "$LANE"` or `-C "$LANE"`. The
  dirty-tree hard stop applies to `$LANE`. Say plainly: *`git status` in the main checkout
  is not your lane's status.*
- Replace the main-checkout rationale: QA runs remotely against a
  fresh clone of the pushed commit, so the lane must build and test, not be served; only
  the fallback validator needs a served checkout.
- Step 5: keep the rule but re-scope to **one mutating agent per lane worktree**. Add:
  before starting a second lane, write an ownership split into both ledgers' standing
  heads and give each implementer the other's file list as do-not-touch. Overlapping
  lanes are not parallel lanes — sequence them.
- Step 6: gates run in the lane; a rebase that moves the lockfile may need `pnpm install`.
- Step 8: always pass repo-dir —
  `verdict-check.sh "$ATTEMPT" "$LANE" --remote` (and `"$RUN" "$LANE"` for the
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

**`agents/zora-implementer.md`** — replace the "one shared local environment / main
checkout" framing: you work in a dedicated worktree given as an absolute path; a sibling
worktree may be busy; never `cd` outside it; never run `git worktree`, `git checkout
<branch>` or `git fetch --prune`; the shared local Tilt does not serve your worktree.

**`agents/zora-validator.md`** — state that the fallback drives the single shared local
environment and its fixed ports, runs only in the checkout that environment serves, one at
a time; it must compare `git -C <dir> rev-parse HEAD` against the commit the lead says is
served and return INCOMPLETE on a mismatch rather than validating the wrong tree.

**`skills/agent-browser/SKILL.md`** — one sentence above the port table: these ports
belong to the single shared local environment; exactly one lane may hold it.

**Docs** — `README.md` and `docs/HOW-IT-WORKS.md` (the "why not agent teams"
rationale keeps its other reasons but drops the single-checkout premise),
`docs/HOW-IT-WORKS.md` (quick reference creates/enters the lane), `run-codex-qa`
header.

**Verify Stage 1.** Done:

- ✅ `verdict-check.sh "$RUN" "$LANE"` passes against a lane's HEAD, and the same call
  with the argument omitted from the main checkout reports
  `stale: the verdict is for 494ed756451f, HEAD is 525a4e9c590f` — proving the argument
  matters. Exercised with a synthetic verdict, not yet a real remote job.
- ✅ The `--git-common-dir` snippet resolves the main checkout from a nested directory
  (`apps/web-app`), and `git worktree list` shows the repo's existing lanes.
- ✅ `mkdir runs/.local-env-owner.d` refuses a second claim and is covered by the
  `/runs/` gitignore rule.

Remaining, needs two real lanes and the VPS:

- ⬜ Create two lanes; the main checkout stays clean and `git worktree list` shows both.
- ⬜ Run the gates in both concurrently.
- ⬜ `run-codex-qa --dry-run` from inside lane A passes preflight.
- ⬜ `verdict-check.sh "$ATTEMPT" "$LANE_A" --remote` passes on a real QA job.

---

## Historical protocol-4 rollout — 2026-09-15 (superseded)

The original approach was implemented on `codex/parallel-qa-environments` in both repositories:
[Harness PR #1](https://github.com/afscomercial/zora-harness/pull/1) and
[Pantheon PR #1897](https://github.com/HouseNumbers/zora-pantheon/pull/1897).
That rollout configured two Kind/netns slots sharing the host Docker daemon. Both slots use the existing
QA identity; the user confirmed it supports concurrent logins.

Verified:

- Default Tilt declarations and all 33 generated commands match the base with the
  override unset or empty. Pantheon lint (70 tasks), types (72 tasks), and three real
  Tilt evaluation/staging tests passed. QA changes require no developer configuration.
- Harness regression suite: 20 runner, 18 worker, 15 dispatcher/checker, and three
  extractor tests. Linux CI passes on the review branch.
- Real systemd lifecycle tests passed: duplicate admission, queue, cancellation,
  supervisor SIGKILL, descendant cleanup, reaper packaging and slot release.
- Two real Kind environments served different content on the same localhost port and
  held different values under the same ConfigMap name. Deleting A preserved B.
- A full single-slot Codex run passed all five QA rungs and the local evidence checker.

The overlapping application trial uses two independent attempts at the same Pantheon
commit, with api-gateway, user, loan-application, task, localstack and insurance-provider.
Each tests an identical Mongo fixture ID with its own attempt value, authenticated
browser sessions, staging rebuild isolation and a five-minute stability window.
Both attempts passed all five QA rungs and the local evidence checker. Fifteen
30-second observer samples recorded both attempts validating with healthy Kubernetes
APIs, spanning more than seven minutes. B remained healthy after A's teardown.
Both five-minute stability windows passed all 11 samples. Staging rebuilds in one
scratch root preserved all 1,619 files in the other, including hashes and timestamps.

Process memory peaked at 4.83 GiB (A) and 4.28 GiB (B), below the configured 8,000 MiB
limit; neither reported an OOM. Cluster limits are 5,000 MiB each, slot reservations
13,000 MiB each, and host reserve 5,000 MiB. These measurements cover the selected
six-service pair only. Bootstrap is serialized; QA validation overlaps.

Both manifests report clean teardown. Slot records, attempt units, private work/staging/
scratch directories, Kind containers, network namespaces and owned firewall rules were
released. The pre-existing stopped developer cluster was preserved; the reaper remains
active.

Observed concerns: initial Vite dependency 504s cleared on reload, and mongosh telemetry
errors required a telemetry-disabled or MongoDB-driver sampling path. Both corrected
stability runs passed without service restarts. The pre-existing developer
`pnpm dev:tilt:clean` shortcut still performs global pruning; QA instructions explicitly
prohibit it and direct cleanup through worker cancellation/release.

Scope of this rollout: no second physical VPS, different-commit pair, all-services
capacity certification, live reboot drill or full concurrent Docker image rebuild
stress test has been performed. Inventory/routing, quarantine and retention have
regression coverage; additional live acceptance work remains in the acceptance tracker below.

The staging prerequisite and exclusive-old-commit execution rule are superseded.
Do not merge/cherry-pick the Pantheon PR for the replacement. These recorded results
remain evidence for the earlier implementation only.

### Historical acceptance tracking (not protocol-5 acceptance)

These historical statuses refine the original rollout gates (developer compatibility, single-slot
regression, two commits, data/rebuild isolation, capacity, queueing, crash recovery,
cleanup and portability). **Partial** means the full original
criterion is still open, even where useful live evidence exists.

| Gate | Status | Evidence / remaining verification |
|---|---|---|
| A1 Developer compatibility | Partial | Real Tilt evaluation compared all 33 commands and settings with base, unset and empty; local lint/types passed. Ordinary developer startup/live-rebuild smoke on the supported desktop setup remains. |
| A2 Single-slot regression | Complete for tested services | Full single-slot five-rung QA and evidence checker passed. Invalid multi-slot `none` rejected by regression tests; not every profile certified. |
| A3 Two environments / different commits | Partial | Two real concurrent six-service attempts passed, with own manifests/images. Both used the same commit; different-commit behavior remains. |
| A4 Data and session isolation | Partial | Same Mongo fixture ID held distinct values; authenticated sessions and fresh-context separation passed. External sandbox mutable-state isolation remains. |
| A5 Rebuild isolation | Partial | Actual dependency/service staging rebuild preserved 1,619 neighboring files. Full Docker build/tag/BuildKit races and deployed-image stability during those rebuilds remain. |
| A6 Capacity | Partial | Overlapping validation and bootstrap, memory limits and no-OOM results recorded for one six-service pair. Other profile pairs, repeated cold builds and shared-daemon peaks remain. |
| A7 Queue | Partial | FIFO/idempotence and queued cancellation exercised with real synthetic systemd jobs. A third full application QA waiting behind two active slots and then completing remains. |
| A8 Crash recovery | Partial | Real supervisor SIGKILL, descendant cleanup, reaper packaging and slot release passed. Full two-app failure drill, reboot and interrupted resource creation/cleanup remain. |
| A9 Cleanup failure / retention | Partial | Regression tests cover quarantine, retention and live-unit protection. Live injected cleanup failure and TTL/explicit-release drills remain. |
| A10 Transport | Partial | Reconnect routing/no-redispatch and collection locking have regressions; real duplicate submit and result collection passed. Deliberate SSH-loss/ambiguous-response fault injection during full QA remains. |
| A11 Clean teardown | Complete for tested attempts | Both manifests and host inspection confirmed released slots, units, work/staging/scratch, Kind containers, namespaces and owned firewall rules. Existing stopped developer cluster preserved. |
| A12 Worker portability | Partial | Explicit selection and persisted routing tested locally. Second physical VPS dispatch/collect/cancel remains. |

Regression suites: [runner](../../skills/zora-cycle/qa/test-runner.py),
[worker](../../skills/zora-cycle/qa/test-worker.py),
[dispatcher/checker](../../skills/zora-cycle/qa/test-dispatcher.py),
[extractor](../../skills/zora-cycle/qa/test-extractor.py).
Live helpers: [systemd lifecycle](../../skills/zora-cycle/qa/test-worker-integration.py)
and [Kind/network proof](../../skills/zora-cycle/qa/test-network-integration.sh).
The reusable network script was added after the successful manual proof; that script
itself has syntax validation, not a separately recorded live rerun.

## Remaining work, in suggested order

- [x] Implement mandatory protocol-5 sandbox execution; pass 74 offline regressions, live runtime isolation and real supervisor integration.
- [x] Drain old attempts and install the matching protocol-5 worker/runtime with two sandbox slots.
- [ ] Complete full QA against original Pantheon commits; current application attempts are preparing.
- [ ] Repeat two-slot, different-commit and real Docker rebuild/prune isolation acceptance.
- [ ] Remeasure aggregate memory/disk and certify intended profile pairs; exercise a third queued job.
- [ ] Run crash, SSH-loss, cleanup-failure, retention and reboot recovery drills.
- [ ] Audit external mutable integrations and implement ownership-aware host maintenance/GC.
- [ ] Provision and test a second physical worker; automatic scheduling remains out of scope.
- [ ] Run Stage 1's two-lane/local-fallback live verification; preserve its existing workflow.

Operational rollback: drain/cancel/release attempts and reconcile quarantined ownership
before reinstalling. Reduce to one **sandbox** slot if capacity needs it; do not re-enable
host/serial execution. Never change isolation underneath live attempts.

## Boundaries

- Sandboxes isolate trusted QA workloads on one host kernel. Do not claim protection
  from a compromised host root or expose production credentials.
- Two is configurable; more slots and larger profiles require measured capacity.
- Automatic cross-worker scheduling, parallel k3d and application changes are out of scope.
- Keep credentials/artifacts private and gitignored; checksums prove consistency, not honesty.

## Technical references

- [Kind configuration](https://kind.sigs.k8s.io/docs/user/configuration/)
- [Linux network namespace lifecycle](https://www.man7.org/linux/man-pages/man8/ip-netns.8.html)
- [Docker security](https://docs.docker.com/engine/security/)
