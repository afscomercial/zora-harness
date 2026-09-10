# zora-harness

A personal plan → implement → validate agent harness for the `zora-pantheon`
monorepo.

![The ten steps of the zora-harness cycle: task and acceptance criteria, clean-branch check, plan (zora-planner on Fable), lead and user approval, implement (zora-implementer on Opus, test-first), lead reruns the gates and rebases, validate (zora-validator on Fable with fresh context — tests, then local APIs and database, then browser), lead judges the evidence, gates and open a PR, stop at green CI without merging. FAIL loops back to implement, at most two rounds; INCOMPLETE returns to validation. Below, the local user scope: zora-harness/ is symlinked by install.sh into ~/.claude/agents and skills, which Claude Code loads against the zora-pantheon main checkout served by Tilt, the service APIs, MongoDB and the web-app.](docs/zora-harness.png)

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
| `zora-validator` | fable / high | Tries to prove the change does not work, on a different model from the implementer. Writes `verdict.json`. Preloads `agent-browser`. |

The lead — your interactive session, on Fable — makes the final call on whether
work is done. That is deliberate: the repo's own `subagents` skill reserves final QA
for Fable tier and says work is never declared done on a lower tier's word alone.

To override a model for one session:
`CLAUDE_CODE_SUBAGENT_MODEL=sonnet` (add `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` to
override the frontmatter too).

### Skills — `skills/`

| Skill | Purpose |
|---|---|
| `zora-cycle` | The pipeline. Plan → approve → implement → gates → validate → judge → PR. Keeps its state in `~/.zora-harness/runs/`; a PASS must survive `verdict-check.sh`. Never merges. |
| `agent-browser` | End-to-end verification against the live Tilt cluster, service APIs, MongoDB, and the local web-app in a browser. |
| `agent-review` | The review loop: review once, refute each significant finding before acting, triage, at most two fix rounds. |
| `ask-slack-review` | Drafts a short, reader-facing peer-review request. |

The last three fill in skills that the repo's own `/orchestrate` references but
which were never written — so installing this also makes `/orchestrate` work end to
end, on this machine only.

## Design notes

**Why the validator gets a fresh context.** It receives the spec and the diff, never
the implementer's reasoning. An agent that talks itself into a shortcut while
building will carry the same reasoning into checking its own work and pass itself.
Isolation is the mechanism, not a side effect.

**Why the validator runs on a different model.** Fresh context is not enough if the
reviewer shares the author's model — it shares the author's blind spots too. The
implementer is on Opus and the validator on Fable. Sonnet is the cheaper swap if cost
matters; the only rule is that it differs from the implementer's.

**Why a PASS is a file.** The validator writes `verdict.json` and its evidence into
the run folder, and `skills/zora-cycle/verdict-check.sh` refuses a PASS that is for
an older commit, predates uncommitted changes, skipped the gates or tests, or claims
a rung with no evidence file behind it. What it cannot check is whether the evidence
is honest, so the lead still reads it.

**Why runs live in `~/.zora-harness/runs/`.** Outside zora-pantheon, so nothing lands
in that repo and Tilt never sees a new file; outside this repo, so internal run
details never reach a public GitHub page.

**Why subagents rather than agent teams.** Agent teams don't isolate teammates in
worktrees, and this repo has one shared local environment: the Tilt cluster and dev
servers serve the main checkout, so the working tree is shared state and only one
mutating agent can run at a time. Plan → implement → validate is sequential anyway.
Teams earn their cost on parallel independent exploration — which is what the repo's
`review-and-evaluate` and dynamic workflows already cover.

**Reasoning effort.** The repo's `subagents` skill states that the Agent tool cannot
set effort per spawn. That is stale — `effort` is a valid agent frontmatter field
and all three definitions set it. The repo file is not edited to say so, because
this harness stays out of that repository.

## Relationship to the repo

Reads and composes, never edits:

- `AGENTS.md` — structure, commands, Definition of Done
- `.agents/skills/orchestrate` — the full pipeline `zora-cycle` is the light sibling of
- `.agents/skills/subagents` — task sizing and model tiers
- `.agents/skills/{tdd,grill-me,manual-qa,seed-local-db,review-and-evaluate,review-pr,clickup}`
- `tilt/Tiltfile` — service ports; `tests/b2b-e2e` — the Playwright suite
