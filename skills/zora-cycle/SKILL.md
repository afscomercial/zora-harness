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
| `zora-planner` | Fable | Investigates and returns a file-level plan. Read-only by tool allowlist. |
| `zora-implementer` | Opus | Builds it, test-first, on the lane branch. |
| `zora-validator` | Fable | Tries to prove it does not work, and writes its verdict to disk. |

The validator deliberately runs on a different model from the implementer: a
reviewer from the author's own model shares the author's blind spots. Models and
reasoning effort are pinned in each agent's definition — do not override them per
call without a reason worth stating. The repo's `subagents` skill governs task
sizing; read it before splitting work.

> The `subagents` skill ends by saying the Agent tool cannot set reasoning effort
> per spawn, so `xhigh` requires a workflow. That is no longer true — `effort` is a
> field in agent frontmatter and these three definitions set it. The repo file is
> stale on this point; do not let it push you into a workflow you do not need.

## The run folder

Every run keeps its state on disk, outside both repositories:

```
~/.zora-harness/runs/<YYYY-MM-DD>-<slug>/
├── task.md        the request and acceptance criteria, in the user's words
├── plan.md        the approved plan
├── ledger.md      standing decisions + chronological log
├── evidence/      the validator's evidence files, named by rung
├── verdict.json   the validator's verdict
└── review.md      agent-review's output, when it runs
```

It sits outside zora-pantheon, so nothing ever lands in the repo or trips a Tilt
rebuild, and outside zora-harness, so internal run details never reach that public
repository. **The folder is the memory; your context window is scratch paper.** A
crash, a compaction or a new session resumes from these files.

## The cycle

**0. Open or resume the run folder.**

```bash
RUN=~/.zora-harness/runs/<YYYY-MM-DD>-<slug>
```

If `$RUN/ledger.md` already exists, this is a continuation: read the ledger's head
and tail, find the last completed step, and continue from there. Never restart from
the top. Otherwise `mkdir -p "$RUN/evidence"` and start a new ledger.

**1. Understand the task.** Ask until you could write the acceptance criteria
yourself. For a rich interview, the repo's `grill-me` skill does this well. Resolve
a ClickUp reference with the `clickup` skill first so you are grounded in the actual
ticket. Write `$RUN/task.md`: the request verbatim and the acceptance criteria.

**2. Check the lane before anything runs.**

```bash
git status          # a dirty tree is a hard stop — surface it, never stash it
git branch --show-current
```

Work happens on `feat/<slug>` off `origin/main`, checked out in the **main
checkout** — that is what the user's Tilt cluster and dev servers serve. A worktree
builds and tests fine but nothing running serves its code, so end-to-end validation
there is not possible.

**3. Plan.** Spawn `zora-planner` with the task, the acceptance criteria, absolute
paths to every document it should read, and recent `git log` for the affected paths
when history matters — the planner has no Bash, so it cannot run it. It returns a
plan; it cannot edit.

**4. Approve the plan yourself, then with the user.** Read it as an engineer, not as
a rubber stamp — a bad plan is cheapest to kill here. Check the premise survived
(the planner reports contradictions rather than designing around them), the split is
sensible, and the validation plan is concrete enough to act on. Then get the user's
approval. This is the last checkpoint before autonomous execution. Write the
approved plan to `$RUN/plan.md`.

**5. Implement.** Spawn `zora-implementer` with the approved plan verbatim, absolute
paths, and — if lanes are sequential over the same module — an explicit do-not-touch
list. **One mutating agent at a time.** Parallelism belongs to read-only work
(exploration, review, verification); two agents committing in one checkout will
sweep each other's half-finished files into unrelated commits.

**6. Re-run the gates yourself, then rebase.** Do not advance on the implementer's
self-report.

```bash
turbo check
turbo check:types
CI=true NO_COLOR=1 TURBO_UI=false pnpm turbo test:agentic --filter=<package>
```

Then rebase onto `origin/main` **before** validation, so the validator checks the
code that will actually ship. Run the rebase and the post-rebase gates as separate,
individually-checked steps — a chained command can swallow a mid-rebase conflict.

**7. Validate.** Spawn `zora-validator` with the spec, the diff, the acceptance
criteria and `$RUN` — **not** the implementer's reasoning or its report of success.
The fresh context is the point: an agent that talked itself into a shortcut while
building will accept the same excuse when checking itself. It writes
`$RUN/verdict.json` and `$RUN/evidence/`. If it returns `INCOMPLETE` because Tilt or
a service is down, ask the user to start it (suggest the `! ` prefix) and re-spawn.

**8. Judge.** First check the verdict mechanically, from the repo root:

```bash
bash ~/.claude/skills/zora-cycle/verdict-check.sh "$RUN"
```

Exit 0 means the file says PASS **and** it holds for this exact tree: the current
HEAD, nothing uncommitted, rungs 1–2 ran, and every claimed rung is backed by an
evidence file that exists. Anything else is not a pass, however confident the
validator's message sounded. Then read the evidence yourself and make the call —
this is Fable-tier work and it does not get delegated.

- `FAIL` → hand the findings to a fresh implementer, re-run the gates, re-validate.
  **Two fix rounds at most.** After the second, stop and bring the open findings to
  the user: fix further, accept and document, or re-plan. A third round is usually
  the loop chasing its own tail.
- `INCOMPLETE` → decide whether the gap matters. Never round it up to a pass.
- `PASS` with `verdict-check` exit 0 → proceed, having personally read the diff.

**9. Close.** Open the PR when the fast gates are green; CI is the acceptance gate.
Write the title and body for someone with zero knowledge of how it was built — no
lanes, no charters, no agent vocabulary. If `origin/main` moved since step 6, rebase
again: HEAD moves, so the verdict goes stale by design — re-validate if the incoming
changes touch the diff's files, and record the decision in the ledger either way.

For review, `agent-review`. For a peer-review ping, `ask-slack-review`. For a guided
human pass, offer the repo's `manual-qa`.

**10. Stop.** The cycle ends at a validated, CI-green PR. **Never merge** — that is
the user's decision, made outside this pipeline.

## Rules that keep this honest

- **A PASS is a file, not a sentence.** Advance only on `verdict-check.sh` exit 0.
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

## The ledger

`$RUN/ledger.md`, in two parts: a **standing head** of settled decisions, kept
current, and a **chronological log** of launches (with task ids), the lane's base
commit, and verdicts with their evidence. After a compaction or in a new session,
reground from the head *and* the tail — a tail-only reground lets settled decisions
fade.

A task counts as launched only when its id from the tool result is in the ledger. A
written charter is not a running agent. When the cycle ends, the PR body carries the
durable record for everyone else; the run folder stays behind as yours.
