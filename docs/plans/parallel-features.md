# Parallel features in the zora harness

Last reconciled: **2026-09-15**, against the QA implementation, regression tests and
recorded VPS rollout. Status describes delivered code and observed tests, not a claim
that every original acceptance criterion has passed.

## Goal and current scope

Goal: independent feature worktrees and run folders, with concurrent remote QA on one
or more VPS workers. The first VPS now has **two Kind/network-namespace QA slots**.
Bootstrap is serialized; ready attempts validate concurrently. More attempts queue.

**Delivery order changed:** the original proposal put Stage 1 (local lane workflow)
first. The user subsequently scoped implementation to QA, so Stage 2 was delivered
first. Stage 1 remains pending. Two-slot QA does not mean the cycle now automatically
creates or coordinates parallel feature worktrees.

**Developer compatibility:** the Pantheon changes are opt-in. With
`ZORA_TILT_STAGING_ROOT` unset or empty, existing generated build commands, paths,
profiles, ports and pruning behavior remain unchanged. The implementation also guards
repair scripts and disables global Tilt pruning in QA mode. No application business
logic or Kubernetes manifests changed.

## Feature delivery tracker

Legend: **Complete** = delivered within the stated scope; **Partial** = code or some
coverage exists, but named work remains; **Pending** = not delivered. Live verification
is tracked separately so mocked coverage is not confused with a VPS acceptance test.

| ID | Feature | Status | Delivered / remaining |
|---|---|---|---|
| L1 | Lane worktree creation, resume, ownership and per-lane gates | Pending | Stage 1 below; main-checkout cycle rules remain. |
| L2 | Exclusive local fallback environment across lanes | Pending | Proposed ownership directory and served-commit check are not added. |
| Q1 | Worker inventory and explicit routing | Complete | `ZORA_QA_WORKERS_FILE`, `--worker`, legacy host/home alias, recorded reconnect destination. No automatic balancing. |
| Q2 | Immutable dispatch and durable queue | Complete | UUID job/attempt IDs, atomic upload, hash binding, idempotent submit, FIFO admission and queue timeout. |
| Q3 | Reconnect, collect and cancel | Complete | Original attempt survives SSH loss; per-attempt collection lock prevents concurrent downloads overwriting evidence. |
| Q4 | Supervision and resource ownership | Complete | Per-attempt systemd unit, slot lock, ownership records, bounded deadlines and descendant cleanup. |
| Q5 | Recovery, quarantine and retention | Complete | Boot/periodic reaper, cleanup quarantine, retained slot TTL and explicit release. Live reboot/retention drills remain. |
| Q6 | Maintenance and installation exclusion | Partial | Installer takes admission lock, refuses active/retained reservations and backs up configuration. General drain/prune command and safe historical artifact GC remain unimplemented. |
| Q7 | Opt-in Pantheon staging | Complete | Validated private staging root, unchanged defaults, QA-only pruning/repair guards; old commits run exclusively. |
| Q8 | Kind networking and private tool state | Complete | Per-attempt netns/veth/rules/DNS, private kubeconfig and Tilt state, host-side Kind API, fixed private service ports. |
| Q9 | Capacity admission and runtime limits | Complete | Atomic reservations, memory/disk/load probes, optional profile reservation overrides, process/cluster limits, bootstrap token and Turbo concurrency. Capacity certification is limited to the tested pair. |
| Q10 | Capacity/health telemetry | Partial | Process peak/CPU/cgroup events, disk-free snapshot, admission reasons, queue wait and pod/image evidence. Full per-profile time series, disk growth, phase/build-wait reporting and shared BuildKit attribution remain. |
| Q11 | Authentication and local data isolation | Complete | Matching slot auth/seed bundles; explicit shared-account policy; separate databases/browser sessions/scratch/evidence. Same account authorized and tested. |
| Q12 | External mutable integration isolation | Partial | Browser boundary, sandbox configuration and prompt restrictions retained. No comprehensive external queues/buckets/callbacks audit or environment-ID partitioning delivered. |
| Q13 | Verdict and evidence integrity | Complete | Protocol-v4 identity/hash checks, explicit legacy handling, clean checkout/rung checks, crash redaction and final cleanup status. |
| Q14 | QA documentation and harness CI | Complete | Markdown/HTML guides updated; GitHub workflow runs shell syntax and 55 offline regressions. It does not dispatch feature QA or access the VPS. |
| Q15 | First VPS two-slot deployment | Complete | Installed and live-tested two concurrent six-service attempts; all five rungs, evidence validation and clean teardown passed. |
| Q16 | Second physical VPS | Pending | Inventory/protocol support exists; second-host provisioning and live portability are not performed. |
| Q17 | Explicit worker DNS configuration | Pending | Network helper copies the host upstream resolver configuration and removes loopback stubs. A configurable per-worker resolver list is not implemented. |

Implementation sources: [dispatcher](../../skills/zora-cycle/qa/qa-dispatch.py),
[worker](../../skills/zora-cycle/qa/qa-worker.py),
[runner](../../skills/zora-cycle/qa/qa-job.sh),
[network helper](../../skills/zora-cycle/qa/qa-network.sh),
[installer](../../skills/zora-cycle/qa/install-worker.sh),
[checker](../../skills/zora-cycle/verdict-check.sh), and
[operator guide](../../skills/zora-cycle/qa/README.md).

## Implemented QA contracts

| Contract | Actual implementation |
|---|---|
| Job / attempt | UUID logical request and execution IDs, persisted locally before upload. A new dispatch creates a new attempt; collection does not. |
| Environment | Environment ID equals attempt ID. Kind name is `zora-qa-` plus 24 SHA-256 hex characters; netns name is `qa-` plus 10. |
| Result location | `<run>/qa/<job-id>/attempts/<attempt-id>/`; use its absolute path as `$ATTEMPT`. |
| Remote layout | `$QA_HOME/jobs/<attempt-id>/`, immutable `in/`, ownership/status and final artifacts. Private work/staging/scratch are removed after normal cleanup. |
| Worker routing | Laptop inventory supplies SSH destination and QA home. Worker-local `worker.json` supplies identity, slots, driver, budgets and deadlines. |
| Compatibility | Runner/protocol v4; explicit old-manifest checks retained. Unsupported driver/isolation combinations are rejected. |
| Admission | `$QA_HOME/.admission.lock`, persisted reservations and submission order; slot locks are held through cleanup and not inherited by children. |
| Bootstrap | `$QA_HOME/.qa-build.lock` covers repository mirror refresh/clone, install, build and readiness. It is released before Codex validation. |
| State | `queued → preparing → validating → packaging → done/failed/cancelled`. Product PASS/FAIL/INCOMPLETE is separate from execution and cleanup state. |
| Recovery | `zora-qa-<attempt>.service`; reaper runs after boot and every 60 seconds, rechecks liveness under ownership locks and cleans only the recorded attempt. |
| Retention | `QA_KEEP_CLUSTER=1` keeps reservation/storage until TTL or `release`. Cleanup failure quarantines capacity. |
| Collection | Terminal result archive is finalized after cleanup/redaction. `.collection.lock` serializes local download, extraction and metadata publication. |

The worker source defaults to **one slot, isolation `none`, driver `kind`** for a new
installation. The deployed VPS configuration overrides this to **two slots, `netns`,
`kind`**. `none` with multiple slots and parallel k3d are rejected. The runner retains
its legacy serial path; this is not a parallel k3d implementation.

Commits without the staging-support marker are admitted only when all slots are idle
and hold an exclusive reservation. They run with `QA_SLOTS=1` and isolation `none`.
Feature branches need the Pantheon prerequisite merged or cherry-picked for parallel
execution; no frozen checkout is patched to manufacture support.

### Changes made during implementation

| Original proposal / issue | Implemented resolution |
|---|---|
| Stage 1 before QA | QA-only scope delivered first; lane orchestration deferred. |
| Single lock rejects a second QA job | Durable supervisor queue with two slots; waiting does not consume a feature retry. |
| Network isolation alone | Added private staging, kubeconfig, Helm/kubectl wrappers and Tilt state via `TILT_DEV_DIR` plus private XDG paths. Shared Tilt state caused a real Helm endpoint failure before this fix. |
| Create Kind within job netns | Kind creation enters the host network via `nsenter --net=/proc/1/ns/net`; its API binds the slot's reachable host-side address. Readiness uses the generated kubeconfig with authentication. |
| Staging override only | Added QA-only automatic-pruning disablement, removal of Tilt's manual cleaner and refusal guards in both repair scripts. Default behavior stays unchanged. |
| Assume inherited login environment | systemd units explicitly set HOME/USER/LOGNAME; authoritative attempt variables are exported after sourcing `vm.env`. This fixed mirror authentication. |
| Initial resource split | A real run hit the initial 6 GB process limit. Retuned to 8,000 MiB process + 5,000 MiB cluster, with `TURBO_CONCURRENCY=2` and sequential QA gates. |
| Runner publishes terminal immediately | Managed runner hands off packaging; supervisor adds telemetry, cleanup state and redaction before final publication. Failed redaction withholds raw evidence. |
| Independent collectors | Added a persistent per-attempt collection lock after observing a download/replacement race. |
| Generic global-prune prohibition | Prompt/operator guides explicitly name `pnpm dev:tilt:clean`; that pre-existing developer shortcut remains unchanged and must not be used by QA. |
| Full telemetry and maintenance service | Only the metrics and installer exclusion listed in Q6/Q10 are delivered; remaining work is tracked below. |

### Capacity configuration and measurement limits

The planning snapshot was 8 CPUs, 32,094 MiB RAM, no swap and approximately 335 GB free
disk. The deployed budgets below passed the selected six-service pair; they are not
an all-services capacity guarantee.

| Setting | Current configured/default value |
|---|---|
| Slots on first VPS | 2 |
| Slot reservation | 13,000 MiB; optional `profiles` reservation overrides supported |
| Process / Kind container memory | 8,000 / 5,000 MiB |
| Host reserve / minimum free disk | 5,000 MiB / 60 GiB |
| Turbo task concurrency | 2 |
| Queue / total execution deadline | 7,200 / 14,400 seconds |
| Preparation / validation deadline | 5,400 / 7,200 seconds |
| Cleanup deadline / retention TTL | 240 / 21,600 seconds |
| Address allocation | `subnet_base` is `10.77`; one private /30 per slot |

Runtime limits are worker-wide, while profile overrides change admission reservations.
Shared Docker/BuildKit use is covered by headroom rather than independently metered
per-attempt cgroups. An OOM is preserved as evidence; reproducible feature memory
regressions must not automatically be dismissed as infrastructure failures.

## Stage 1 — Parallel lanes: not implemented in this rollout

Future work: two features plan, implement and gate in separate lane worktrees.
QA already supports concurrent attempts; this stage must integrate with the existing
protocol-v4 dispatcher, not reintroduce serial QA. Reuse Pantheon’s
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

**Verify Stage 1**: create two lanes; the main checkout stays clean and `git worktree list`
shows both; run gates in both concurrently; `run-codex-qa --dry-run` from inside lane A
passes preflight (proves worktree resolution); `verdict-check.sh "$ATTEMPT" "$LANE_A" --remote`
passes on a real job, and the same call from the main checkout without a repo-dir
correctly reports a stale commit — proving the argument matters.

---

## QA rollout validation — 2026-09-15

Implementation is on `codex/parallel-qa-environments` in both repositories:
[Harness PR #1](https://github.com/afscomercial/zora-harness/pull/1) and
[Pantheon PR #1897](https://github.com/HouseNumbers/zora-pantheon/pull/1897).
The VPS worker is configured with two Kind/netns slots. Both slots use the existing
QA identity; the user confirmed it supports concurrent logins.

Verified:

- Default Tilt declarations and all 33 generated commands match the base with the
  override unset or empty. Pantheon lint (70 tasks), types (72 tasks), and three real
  Tilt evaluation/staging tests passed. QA changes require no developer configuration.
- Harness regression suite: 20 runner, 18 worker, 14 dispatcher/checker, and three
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

Keep ordinary developer workflows unchanged. Older Pantheon commits without the
staging override run exclusively until the prerequisite is merged or cherry-picked.

## Acceptance tracking

These statuses refine the original rollout gates (developer compatibility, single-slot
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

- [ ] **QA acceptance:** run distinguishable commits and a Docker rebuild in A while B
  validates; verify B's responses, image IDs and data before and after (A3/A5).
- [ ] **QA capacity/queue:** certify intended profile pairs and a third queued full QA;
  add repeatable profile measurements and missing timing/disk/shared-build metrics (Q10/A6/A7).
- [ ] **QA recovery:** live two-environment crash, SSH-loss, cleanup-failure, retention
  and reboot drills; ensure B stays healthy and capacity is never reused early (A8–A10).
- [ ] **External integrations:** inventory mutable sandbox resources and partition,
  stub or reserve exclusive access before certifying features that depend on them (Q12/A4).
- [ ] **Maintenance:** implement an admission-excluded drain/maintenance interface and
  ownership-aware artifact cleanup. Do not re-enable global per-job pruning (Q6).
- [ ] **Developer smoke:** ordinary desktop Tilt startup and live rebuild without override (A1).
- [ ] **DNS configuration:** add an explicit resolver override and tests for workers
  whose host resolver configuration is unsuitable (Q17).
- [ ] **Multi-VPS:** provision a second worker and verify routing, collection and cancellation
  after changing the default worker (Q16/A12). Automatic scheduling remains out of scope.
- [ ] **Parallel lanes:** implement Stage 1 worktree/ledger/local-fallback coordination
  and its two-lane verification; reuse current QA attempt paths (L1/L2).

Operational rollback: drain/cancel/release attempts and resolve quarantined ownership
before reconfiguring with the installer. One slot with `netns` retains isolation;
`none` is an explicit serial compatibility option. Never change isolation under live jobs.
There is currently no dedicated `drain` command; use the operator workflow.

## Boundaries

- Trusted root agents share the Docker host. Namespaces and ownership prevent accidental
  collisions; they do not establish a hostile-code security boundary.
- Two is a configuration choice, not a hardcoded protocol limit. Raising it requires
  measured capacity; no claim of additional-slot or all-profile certification is made.
- Automatic cross-worker scheduling, parallel k3d, two local developer Tilt clusters
  and application business changes are outside this implementation.
- Keep credentials and QA artifacts under private, gitignored run/runtime paths. The
  public plan records summaries; checksums prove consistency, not host-root honesty.

## Technical references

- [Kind configuration](https://kind.sigs.k8s.io/docs/user/configuration/)
- [Linux network namespace lifecycle](https://www.man7.org/linux/man-pages/man8/ip-netns.8.html)
- [Docker security](https://docs.docker.com/engine/security/)
