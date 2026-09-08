---
name: zora-cycle
description: Run the plan → implement → validate development cycle for a zora-pantheon feature or fix, using the zora-planner, zora-implementer, and zora-validator agents with local end-to-end verification against the Tilt cluster. Use when the user asks to build a feature in zora-pantheon through the agent harness, or invokes /zora-cycle. Stops at a validated branch or PR and never merges.
---

# The Zora Development Cycle

Carry one task from a description to a validated change, through three specialist
agents. You are the lead: you decide, you delegate, you judge what comes back. You
do not write the feature code yourself.

This is the lighter sibling of the repo's `/orchestrate`. Use `/orchestrate` when
the task warrants the full pipeline — ClickUp lifecycle, interview, review loop,
Slack. Use this when you want the three-role cycle without the ceremony.

## The roles

| Agent | Model | Does |
|---|---|---|
| `zora-planner` | Fable | Investigates and returns a file-level plan. Read-only. |
| `zora-implementer` | Opus | Builds it, test-first, on the lane branch. |
| `zora-validator` | Opus | Tries to prove it does not work. |

Models and reasoning effort are pinned in each agent's definition — do not override
them per call without a reason worth stating. The repo's `subagents` skill governs
task sizing; read it before splitting work.

> The `subagents` skill ends by saying the Agent tool cannot set reasoning effort
> per spawn, so `xhigh` requires a workflow. That is no longer true — `effort` is a
> field in agent frontmatter and these three definitions set it. The repo file is
> stale on this point; do not let it push you into a workflow you do not need.

## The cycle

**1. Understand the task.** Ask until you could write the acceptance criteria
yourself. For a rich interview, the repo's `grill-me` skill does this well. Resolve
a ClickUp reference with the `clickup` skill first so you are grounded in the actual
ticket.

**2. Check the lane before anything runs.**

```bash
git status          # a dirty tree is a hard stop — surface it, never stash it
git branch --show-current
```

Work happens on `feat/<slug>` off `origin/main`, checked out in the **main
checkout** — that is what the user's Tilt cluster and dev servers serve. A worktree
builds and tests fine but nothing running serves its code, so end-to-end validation
there is not possible.

**3. Plan.** Spawn `zora-planner` with the task, the acceptance criteria, and
absolute paths to every document it should read. It returns a plan; it cannot edit.

**4. Approve the plan yourself, then with the user.** Read it as an engineer, not as
a rubber stamp — a bad plan is cheapest to kill here. Check the premise survived
(the planner reports contradictions rather than designing around them), the split is
sensible, and the validation plan is concrete enough to act on. Then get the user's
approval. This is the last checkpoint before autonomous execution.

**5. Implement.** Spawn `zora-implementer` with the approved plan verbatim, absolute
paths, and — if lanes are sequential over the same module — an explicit do-not-touch
list. **One mutating agent at a time.** Parallelism belongs to read-only work
(exploration, review, verification); two agents committing in one checkout will
sweep each other's half-finished files into unrelated commits.

**6. Re-run the gates yourself.** Do not advance on the implementer's self-report.

```bash
turbo check
turbo check:types
CI=true NO_COLOR=1 TURBO_UI=false pnpm turbo test:agentic --filter=<package>
```

**7. Validate.** Spawn `zora-validator` with the spec and the diff — **not** the
implementer's reasoning or its report of success. The fresh context is the point: an
agent that talked itself into a shortcut while building will accept the same excuse
when checking itself. Give it the acceptance criteria and let it work its own ladder.

**8. Judge.** Read the validator's evidence and make the call yourself. This is
Fable-tier work and it does not get delegated — the repo's own policy is that work
is never declared done on a lower tier's word alone.

- `FAIL` → hand the findings back to a fresh implementer, then re-validate. Loop.
- `INCOMPLETE` → decide whether the gap matters. Never round it up to a pass.
- `PASS` → proceed, having personally read the diff.

**9. Close.** Open the PR when the fast gates are green; CI is the acceptance gate.
Write the title and body for someone with zero knowledge of how it was built — no
lanes, no charters, no agent vocabulary. Rebase onto `origin/main` first, and run the
rebase and the post-rebase gates as separate, individually-checked steps.

For review, `agent-review`. For a peer-review ping, `ask-slack-review`. For a guided
human pass, offer the repo's `manual-qa`.

**10. Stop.** The cycle ends at a validated, CI-green PR. **Never merge** — that is
the user's decision, made outside this pipeline.

## Rules that keep this honest

- **Verify, never trust.** Re-run gates yourself before advancing on any agent's
  self-report. Check a "the environment is broken" blocker against the primary
  source — a `curl` against the gateway or a `mongosh` query settles it in seconds.
- **A clean pass requires a completed run.** Zero findings from a run that errored,
  skipped steps, or finished implausibly fast is a broken run. Relaunch it.
- **Primary sources win.** Live files, `git log`, and the actual spec beat skills,
  which beat your recollection.
- **Never `git stash`.** Shelve with `git diff > x.patch` or a WIP commit.
- **The environment is the user's.** Never start or stop Tilt, never kill a process
  by name, never kill a listener to free a port. Ask, and suggest the `! ` prefix.
- **Bank out-of-scope findings** with repro evidence. Never fix them inline, never
  drop them.
- **Read shared-package diffs personally.** `packages/*` changes cross every
  dependent service and outrun any single reviewer's scope.

## Keeping a ledger

For anything longer than a single pass, keep a ledger in the scratchpad: a standing
head of settled decisions plus a chronological log of launches (with task ids), the
lane's base commit, and verdicts with their evidence. After a compaction, reground
from the head *and* the tail — a tail-only reground lets settled decisions fade.

A task counts as launched only when its id from the tool result is in the ledger. A
written charter is not a running agent.
