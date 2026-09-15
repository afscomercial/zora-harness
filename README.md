# zora-harness

A personal plan → implement → validate agent harness for the `zora-pantheon`
monorepo.

![The ten steps of the zora-harness cycle: task and acceptance criteria, clean-branch check, plan (zora-planner on Fable), lead and user approval, implement (zora-implementer on Opus, test-first), lead reruns the gates and rebases, validate (zora-validator on Fable with fresh context — tests, then local APIs and database, then browser), lead judges the evidence, gates and open a PR, stop at green CI without merging. FAIL loops back to implement, at most two rounds; INCOMPLETE returns to validation. Below, the local user scope: zora-harness/ is symlinked by install.sh into ~/.claude/agents and skills, which Claude Code loads against the zora-pantheon main checkout served by Tilt, the service APIs, MongoDB and the web-app.](docs/zora-harness.png)

The diagram shows the original single-checkout, local-validator flow. Work now happens in
a per-feature lane worktree, and validation in the [two-slot remote QA
flow](skills/zora-cycle/qa/README.md).

The real files live here, outside the repo. `install.sh` symlinks them into
`~/.claude/`, which Claude Code reads from **any** working directory — so the
harness is available inside `zora-pantheon` without a single file being added to
that repository. Nothing to gitignore, nothing that can be committed by accident,
no diff for teammates.

## Install

```bash
./install.sh              # link into ~/.claude
./install.sh --uninstall  # unlink (the files here stay)
```

Restart Claude Code afterwards. Re-run after adding an agent or skill.

## What's here

### Agents — `agents/`

| Agent | Model / effort | Role |
|---|---|---|
| `zora-planner` | fable / high | Investigates, returns a file-level plan. Read-only by a `tools` allowlist: no Bash, Write or Edit. |
| `zora-implementer` | opus / xhigh | Builds the plan test-first under the repo's TDD contract. |
| `zora-validator` | fable / high | Local fallback validator, used when the QA VM is unavailable. Tries to prove the change does not work and writes `verdict.json`. Preloads `agent-browser`. |

The lead — your interactive session, on Fable — makes the final call on whether
work is done. That is deliberate: the repo's own `subagents` skill reserves final QA
for Fable tier and says work is never declared done on a lower tier's word alone.

To override a model for one session:
`CLAUDE_CODE_SUBAGENT_MODEL=sonnet` (add `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` to
override the frontmatter too).

### Skills — `skills/`

| Skill | Purpose |
|---|---|
| `zora-cycle` | The pipeline. Plan → approve → implement → gates → validate → judge → PR. Keeps its state in `runs/` (gitignored); a PASS must survive `verdict-check.sh`. Never merges. |
| `agent-browser` | End-to-end verification against the live Tilt cluster, service APIs, MongoDB, and the local web-app in a browser. |
| `agent-review` | The review loop: review once, refute each significant finding before acting, triage, at most two fix rounds. |
| `ask-slack-review` | Drafts a short, reader-facing peer-review request. |

The last three fill in skills that the repo's own `/orchestrate` references but
which were never written — so installing this also makes `/orchestrate` work end to
end, on this machine only.

### Remote QA — `skills/zora-cycle/qa/`

Validation runs as a remote job: the lead freezes and pushes a commit, writes a
charter, and `run-codex-qa` sends an immutable attempt over SSH to a selected worker.
Codex (`gpt-6-astra`) runs the validation ladder; the lead checks the downloaded
verdict and evidence with `verdict-check.sh --remote` before making the final call.

The replacement worker uses **protocol 5 with mandatory per-attempt sandboxes**.
Two slots remain the initial capacity target:

- Each attempt owns a checkout, Kind cluster and database, private process/network
  namespaces, `/tmp`, `/var/tmp`, `/run`, `/dev/shm`, HOME and tool state.
- Each sandbox owns its Docker daemon, socket and storage. Existing Tilt staging
  paths and image tags stay inside that sandbox, including during rebuilds and pruning.
- Bootstrap is serialized to control resource peaks; ready attempts can validate
  concurrently. Further attempts queue. Aggregate sandbox memory includes the
  runner, browser, Docker/BuildKit and Kubernetes; capacity is measured for the tested six-service profile; other combinations need validation.
- SSH disconnection does not cancel the attempt. `--status`, `--collect` and
  `--cancel` reconnect using the printed attempt directory.
- Ownership-based cleanup, quarantine and the reaper remain in the supervisor.

**No Pantheon changes or staging prerequisite are required.** The harness tests the
original pushed commit with its normal Tilt configuration. New remote execution has
no serial compatibility fallback or staging-support marker. The existing local
validator fallback remains part of the lane workflow when remote QA is unavailable.

Explicit worker inventory supports additional VPSs; automatic balancing is not
implemented. The earlier two-slot six-service proof used the superseded shared-Docker
and Pantheon staging approach. Protocol-5 runtime isolation and supervisor integration now pass, and two sandbox
slots are installed. Scoped isolation now passes for two distinguishable original commits using the
six-service profile: A was removed while B's API, web and Kubernetes remained healthy;
a third job admitted only after slot release and cancelled cleanly. B passed all five rungs, the exact-source remote verdict checker, and clean teardown. Supplemental A browser monitoring had two transient failures (49/51 healthy),
then recovered; capacity beyond this profile and a second VPS remain unverified.

See the [QA setup and operator guide](skills/zora-cycle/qa/README.md),
[detailed flow](docs/HOW-IT-WORKS.md#qa-on-an-isolated-vm), and
[rollout evidence and remaining acceptance work](docs/plans/parallel-features.md).

## Design notes

**Why the validator gets a fresh context.** It receives the spec and the diff, never
the implementer's reasoning. An agent that talks itself into a shortcut while
building will carry the same reasoning into checking its own work and pass itself.
Isolation is the mechanism, not a side effect.

**Why validation runs on a different model.** Fresh context is not enough if the
reviewer shares the author's model — it shares the author's blind spots too. The
implementer is on Opus; QA normally runs on another vendor entirely (OpenAI's Astra on
the QA VM), and the local fallback on Fable. The only rule is that it differs from the
implementer's.

**Why QA runs on a separate VM.** Codex needs Docker, Kubernetes, a browser and the
network to test for real, and an agent with that much access belongs on a machine that
holds nothing valuable — not your laptop. The VM also never has a copy of the
implementer's reasoning, so the fresh-eyes rule holds by construction.

**Why a PASS is a file.** The validator writes `verdict.json` and its evidence into
the run folder, and `skills/zora-cycle/verdict-check.sh` refuses a PASS that is for
an older commit, predates uncommitted changes, skipped the gates or tests, or claims
a rung with no evidence file behind it. What it cannot check is whether the evidence
is honest, so the lead still reads it.

**Why runs live in `runs/`, gitignored.** Outside zora-pantheon, so nothing lands in that
repo and Tilt never sees a new file. Inside this checkout, next to the harness, so they
are easy to find. Gitignored, because this repo is public and run files hold internal data
(screenshots, tenant and user ids, logs), so never `git add -f` anything under `runs/`.

**Why subagents rather than agent teams.** Agent teams don't isolate teammates in
worktrees. This harness does the isolating itself: each feature gets its own lane
worktree, one mutating agent per lane, and remote QA validates each lane from its
pushed commit. Plan → implement → validate remains sequential within a lane; separate
lanes and their QA attempts overlap. Teams earn their cost on parallel independent
exploration — which is what the repo's `review-and-evaluate` and dynamic workflows
already cover.

**Reasoning effort.** The repo's `subagents` skill states that the Agent tool cannot
set effort per spawn. That is stale — `effort` is a valid agent frontmatter field
and all three definitions set it. The repo file is not edited to say so, because
this harness stays out of that repository.

## Relationship to the repo

The harness reads and composes these repository instructions and tools. Its
installation and remote QA require no repository changes:

- `AGENTS.md` — structure, commands, Definition of Done
- `.agents/skills/orchestrate` — the full pipeline `zora-cycle` is the light sibling of
- `.agents/skills/subagents` — task sizing and model tiers
- `.agents/skills/{tdd,grill-me,manual-qa,seed-local-db,review-and-evaluate,review-pr,clickup}`
- `tilt/Tiltfile` — service ports; `tests/b2b-e2e` — the Playwright suite
