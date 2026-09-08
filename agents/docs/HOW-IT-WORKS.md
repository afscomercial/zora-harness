# How the Zora Harness Works

A plan → implement → validate agent harness for the `zora-pantheon` monorepo.
This document covers what orchestrates it, how context is managed, and how the
agents are kept honest.

---

## 1. Orchestration: what's actually driving this

### The pattern

Plain **orchestrator-worker**, the arrangement Anthropic describes in
[Building a multi-agent research system][mars]: one lead coordinates, specialists
run in their own context windows, results come back to the lead.

The lead is **your interactive session**. Not an agent. You are in the loop at the
two points where judgment matters, and there is no lossy relay between you and the
workers.

```
        you
         │
   ┌─────▼─────────┐
   │ lead session  │◄──── every result returns here
   │   (Fable)     │
   └─┬──────┬────┬─┘
     │      │    │
  planner  impl  validator     ← never talk to each other
```

Agents never call each other. Every handoff passes through the lead. That is not a
limitation of the tooling — it is the design (see §3.1).

[mars]: https://www.anthropic.com/engineering/multi-agent-research-system

### Mechanism: Claude Code subagents

Three definitions in `agents/`, symlinked into `~/.claude/agents/`:

| Agent | Model | Effort | Tools |
|---|---|---|---|
| `zora-planner` | `fable` | `high` | `Write`, `Edit`, `NotebookEdit` **denied** |
| `zora-implementer` | `opus` | `xhigh` | all |
| `zora-validator` | `opus` | `xhigh` | all, preloads `agent-browser` |

Model and reasoning effort are pinned in each file's frontmatter. The planner's
read-only status is enforced by `disallowedTools`, not by asking it nicely — the
tool is absent from its context entirely.

Session-wide override, when you want to experiment:

```bash
CLAUDE_CODE_SUBAGENT_MODEL=sonnet claude          # unless frontmatter sets one
CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1                # override frontmatter too
```

### Skills in play

**Written by this harness** (in `skills/`, symlinked to `~/.claude/skills/`):

| Skill | Role |
|---|---|
| `zora-cycle` | The pipeline. Plan → approve → build → gates → validate → judge → PR. |
| `agent-browser` | End-to-end verification against the live Tilt cluster and a real browser. |
| `agent-review` | Review loop with adversarial verification of each finding. |
| `ask-slack-review` | Reader-facing peer-review request. |

The last three fill in skills the repo's own `/orchestrate` references but which were
never written — so installing this harness also makes `/orchestrate` work end to end,
on this machine only.

**Consumed from `zora-pantheon`** (read, never modified):

| Skill | Role |
|---|---|
| `orchestrate` | The full pipeline `zora-cycle` is the light sibling of. |
| `subagents` | Task sizing and model tiers. Governs §2. |
| `tdd` | The Prove-It bug-fix workflow. |
| `grill-me` | The requirements interview. |
| `seed-local-db` | Seeds the local cluster's MongoDB. |
| `review-and-evaluate`, `review-pr` | The review engines `agent-review` wraps. |
| `manual-qa` | Guided human QA over finished work. |
| `clickup` | Ticket resolution. |

### Considered and rejected

**Agent teams** — teammates messaging each other directly, with a shared task list.
Rejected for three concrete reasons:

1. Teams don't isolate teammates in worktrees, and this repo has **one shared local
   environment**: the Tilt cluster and dev servers serve the main checkout, which
   makes the working tree shared state. Two agents committing there sweep each
   other's half-finished files into unrelated commits.
2. Plan → implement → validate is **sequential**. Teams pay off on parallel
   independent exploration, not on a dependency chain.
3. Experimental, no session resumption, and every teammate permission prompt
   bubbles to the lead.

**Dynamic workflows** — a script holding the plan instead of the lead's turn-by-turn
judgment. Genuinely useful for wide fan-out (a codebase-wide audit, a multi-dimension
review). Overkill for a three-step chain.

> The repo's `subagents` skill says the Agent tool cannot set reasoning effort per
> spawn, so `xhigh` requires a workflow. **That is stale** — `effort` is a valid
> agent frontmatter field and all three definitions set it. Don't let that line push
> you into a workflow you don't need. The repo file is not corrected because this
> harness stays out of that repository.

### Where the files live

Real files in `housenumbers/zora-harness/`, outside the repo. `install.sh` symlinks
them into `~/.claude/`, which Claude Code reads from **any** working directory — so
the harness works inside `zora-pantheon` without a single file being added to it.
Nothing to gitignore, nothing committable by accident, no diff for teammates.

---

## 2. Context management

### The budget

From the repo's `subagents` skill: every model here has a **1,000,000-token** window,
and quality degrades noticeably past **~33% fill (~330k)**. Past that point you get
hallucinations, silently dropped requirements, and confident wrong answers.

**Design each task to land near 33% when it finishes** — not to race toward it. The
target is the end state of a healthy task, not a ceiling.

Soft rule: an agent genuinely near the end when it crosses 33% should finish.
Interrupting mid-task is worse than mild degradation at the finish line. What must
be avoided is an agent still doing substantive work at 50%+.

### Estimate before spawning

Add up the real consumers:

| Consumer | Typical cost |
|---|---|
| Startup — system prompt, task prompt, preloaded skills | 20–60k before any work |
| Reading — chars ÷ 4. Design docs and source sweeps | 30–60k |
| **Iteration** — every tool result replays into context on later turns | **100k+** |
| Output — generated code and reasoning | usually dominated by input |

Iteration is the silent killer. A build-until-green loop replays every failed test
run, every `git diff`, every stack trace, on every subsequent turn.

If the honest sum lands well past 330k, split the task before spawning.

### What starts fresh

**Every agent, every time.** Subagents don't inherit the lead's conversation. That's
the default, and this harness leans on it rather than working around it.

**The validator's freshness is the load-bearing one.** It receives:

- ✅ the spec / acceptance criteria
- ✅ the diff (`git diff <base>...HEAD`), which it reads itself
- ❌ the implementer's reasoning
- ❌ the implementer's report of success
- ❌ the conversation where a trade-off got rationalized

An agent that talks itself into a shortcut while building — *"this edge case
probably can't happen"* — will carry the same reasoning into checking its own work
and pass itself. Stripping that out is the mechanism, not a side effect. It is also
why agents never hand off directly to each other: the lead is what performs the
strip.

### What carries over

| Handoff | Carried | Deliberately dropped |
|---|---|---|
| Lead → planner | Task, acceptance criteria, absolute paths to docs to read | — |
| Planner → lead | The plan, as its final message | Its exploration transcript |
| Lead → implementer | Approved plan **verbatim**, absolute paths, do-not-touch list | Planner's reasoning |
| Implementer → lead | Files changed, tests added, verbatim gate output, divergences | Its transcript |
| Lead → validator | Spec + diff + acceptance criteria | **The implementer's reasoning and claims** |
| Validator → lead | Verdict, rungs executed, evidence, not-verified list | — |

**Absolute paths, always.** A relative path resolves differently — or not at all —
in an agent's working directory.

### The ledger

For anything longer than one pass, the lead keeps a ledger in the scratchpad,
structured as two parts:

- **A standing-directives head** — settled policy, kept current.
- **A chronological log** — launches with their task ids, the lane's base commit,
  verdicts with their evidence.

After a compaction, **reground from the head *and* the tail**. A tail-only reground
lets settled decisions quietly fade, and they come back as re-litigated questions or,
worse, as reversed decisions nobody noticed.

A task counts as **launched** only when its id from the tool result is in the ledger.
A written charter is not a running agent.

When a long effort concludes, externalize the durable record — decisions,
divergences, audit results — into the PR body before the scratchpad is cleaned up.

### When to start a fresh agent

- **A task is too big for one healthy context** → split at a natural seam (by
  package, by service, by layer) before spawning, not after.
- **An agent is approaching ~33% with substantial work left** → have it checkpoint
  its state to disk, then hand to a fresh agent with a summary and that file.
  **Never resume by replaying a giant transcript.**
- **After an interruption, with a dirty tree** → launch a continuation agent whose
  first step is diffing working tree against last commit to classify done / partial /
  untouched, then finishing. Never a blind restart.
- **After an interruption, with a clean tree at a known commit** → relaunch the
  charter unedited.
- **An agent is still addressable** → resume it by message from its last checkpoint,
  keeping its context, rather than restarting cold.

Before assuming an agent is dead: **positively enumerate what is alive.** Subagents
run inside the harness, not as OS processes — `ps` silence proves nothing. Liveness
evidence is the transcript under the session's `subagents/` directory: a recent mtime
or growing line count means alive. Until death is established, never mutate anything
the agent shares — above all the lane branch's working tree.

### Keeping the lead lean

The lead's context is the scarcest thing in the system, because it is the only one
that spans the whole job. Protect it: delegate reading, take **distilled conclusions
rather than raw file contents**, store durable state in the ledger, and never read
what a subagent can read for you.

### Measuring occupancy

Transcripts record per-turn token usage. Never read the JSONL wholesale — it
overflows your own window. Extract just the numbers; the last turn's
`input_tokens + cache_creation_input_tokens + cache_read_input_tokens` is that
agent's current occupancy. The script is in the repo's `subagents` skill.

Its "empirical calibration" section is **empty**. Fill it in — one line per finished
task recording what the agent did, how many files it touched, and where it landed as
a percentage of the window. Those entries are what turn the sizing rules above from
theory into judgment specific to this repo.

---

## 3. Keeping agents practical

The two failure modes that waste the most time are an agent **inventing a bug and
fixing it**, and an agent **optimizing something nobody asked about**. Both come from
the same root: an agent that would rather produce a plausible deliverable than report
that there was nothing to do.

Every mechanism below exists to make "nothing needed doing" a *safe and expected*
answer.

### 3.1 Structural: judgment stays at the top

Results always return to the lead, never agent-to-agent. Every finding, plan, and
claim passes a checkpoint that can catch a fabrication before it becomes work.
Chain the agents directly and a hallucination in step one becomes committed code by
step three with nobody in between.

Design and adjudication are lead-tier work. When the evidence under a design changes
— a stale reference, a corrected spec — the design is **redone at the lead tier**,
never handed down with a "re-verify your citations" note attached.

### 3.2 Stop on contradiction

Every agent carries an explicit clause:

> If the charter's factual premise — a claimed bug, a claimed missing feature, a
> spec citation — doesn't survive verification against the primary source, **halt
> that item and report with citations** instead of inventing.

And the counterpart that makes it usable:

> **"Verified already correct, no change made" is a valid, expected outcome.**

Without that second sentence the first one doesn't work. An agent told only to stop
on contradiction still feels the pull to produce *something*, so it finds a smaller
thing to fix. Naming the null result as a success removes the pull.

The planner's output format has a dedicated **Premise check** section for exactly
this, so a failed premise is a first-class deliverable rather than a footnote.

### 3.3 Refute before you act

In `agent-review`, before acting on any significant finding — anything that would
change shipped behavior or reverse a design decision — one verifier is spawned and
charged with **refuting** it:

> Here is a claim about this code. Try to prove it wrong. Read the primary sources.
> Report CONFIRMED with citations, or REFUTED with citations. **"The observation is
> accurate but the verdict is wrong" is a valid and common answer.**

That last sentence catches the most common review failure: a reviewer notices
something genuinely unusual and draws the wrong conclusion. The code is odd but
correct, the pattern is deliberate, the "missing" check happens upstream.

One verifier per finding — a refutation attempt, not a voting panel. Trivial findings
(typos, obvious mistakes) skip it.

**And then read the source yourself** before directing any fix that reverses a prior
ruling or claims an inconsistency with a spec. Reviewers fabricate plausible
citations. So do verifiers. Open the cited line and look.

### 3.4 Evidence, or it didn't happen

The validator's contract is that every claim carries its evidence: the command and
its **verbatim output**, the query and what came back, the screenshot path, the
`file:line`.

"The endpoint works correctly" is worthless. `curl` output plus the `mongosh` query
showing the persisted document is a finding.

This cuts both ways — it stops false failures as much as false passes. An agent that
must paste the actual output cannot report a bug it did not observe.

Corollaries:

- **A 2xx is not evidence.** The claim is that data landed correctly; check the
  document.
- **A clean pass requires a completed run.** Zero findings from a run that errored,
  skipped rungs, or finished implausibly fast is a *broken run*, not a pass.
  Relaunch it.
- **`INCOMPLETE` is a legitimate verdict.** Explicitly ranked alongside PASS and
  FAIL so that "I couldn't finish" never has to disguise itself as success.
- **Reconcile numbers exactly.** Baseline + delta = measured, identical across two
  runs. A mismatch is a hard stop, not noise.

### 3.5 Scope discipline

**Bank, don't fix.** Real problems found outside the task's scope go into a running
list with repro evidence, for a dedicated pass. Never fixed inline, never dropped.
Both agent outputs have a required **Out of scope** section, which gives the agent
somewhere to put the itch without acting on it.

This is the direct antidote to scope creep. An agent with no legitimate outlet for an
observation either suppresses it (losing real information) or acts on it (blowing
the diff). A banking list is the third option.

**Minimum code that passes the test.** The TDD cycle's GREEN phase is defined as the
*minimum* implementation. Refactoring is a named third step with tests staying green
— not an open invitation.

**Do-not-touch lists.** When sequential lanes share a module, each charter names the
files the others own.

**Shared packages get read personally.** Any `packages/*` change gets the lead's own
diff read before shipping, regardless of who approved it — the blast radius crosses
every dependent service and exceeds any single reviewer's scope.

### 3.6 Grounding priority

When state or policy is uncertain, in strict order:

1. **Primary sources** — live files, `git log`, the actual spec, a real query against
   the running environment.
2. **Live skills.**
3. **Ledger notes or compacted memory.**

Applied concretely: check an agent's *"the environment is broken / the data is gone"*
blocker against the primary source before acting on it. A direct `mongosh` query or a
`curl` against the gateway settles it in seconds, and it is wrong often enough to be
worth the ten seconds every time.

When two sources disagree, the charter **names the conflict for adjudication** — it
never silently picks one.

### 3.7 Verify, never trust

The lead re-runs the gates itself before advancing on any agent's self-report:

```bash
turbo check
turbo check:types
CI=true NO_COLOR=1 TURBO_UI=false pnpm turbo test:agentic --filter=<package>
```

Also: verify mechanical batch edits landed by **grepping the expected before/after
state**, not by exit code. And audit PR-body citations ("per spec X", "per source Y
at file:line") against the actual cited location — agents produce plausible but
fabricated attributions, and a PR body is where they are least likely to be checked.

### 3.8 The honest limitation

Everything in §3 is **prose**. Nothing executes it.

The `agentic-sdlc` framework states the problem precisely in its eighth invariant:

> *A process rule must be executable or CI-verified, because a rule nothing checks
> decays into a lie.*

By that standard this harness is unverified. Nothing confirms the validator actually
climbed all five rungs, that the implementer really ran the gates it pasted, or that
the planner didn't quietly design around a contradiction. The rules hold only while
the model chooses to follow them, and the failure is **silent**.

The mitigations that do exist are real but partial:

- The repo's **TDD hooks** genuinely block completion on failing tests — that one is
  executable.
- `disallowedTools` on the planner is enforced by the harness, not by prose.
- The **evidence requirement** makes fabrication visible to a reading human, since a
  claim without pasted output is conspicuous.

The known next step is a machine-checkable verdict artifact the validator must emit,
so that PASS becomes a file rather than a sentence. Until then: **read the evidence,
don't skim the verdict.**

---

## Quick reference

```bash
cd ~/Documents/housenumbers/zora-pantheon
git status                    # clean tree required
claude
/zora-cycle <task>
```

With Tilt and the web-app running in their own terminals if the change is
user-facing:

```
! pnpm dev:tilt:full
! pnpm web-dev
```

Two deliberate stops: **approve the plan**, and **make the final call**. One possible
interruption: the validator asking you to start Tilt. The cycle never merges.
