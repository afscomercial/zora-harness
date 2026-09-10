---
name: agent-review
description: Run the review loop over a committed zora-pantheon branch or PR — a scoped review, adversarial verification of each significant finding before acting on it, triage into fix/bank/reject, and at most two fix rounds before the rest goes to the user. Use when the orchestrate pipeline reaches its review stage, or when the user asks for a reviewed-and-adjudicated verdict on a branch rather than a raw list of review comments.
---

# The Review Loop

A review produces claims. This skill turns claims into adjudicated decisions —
because acting on an unverified finding is how a correct implementation gets
"fixed" into a broken one.

The repo already has the review engines. This is the loop around them.

## Step 0: Commit first

Commit the work on the branch before reviewing anything. A review of an
uncommitted tree silently reviews the wrong thing — the diff the reviewer computes
does not match the diff you are about to ship, and nobody notices until later.

```bash
git status                      # must be clean
git log --oneline -3            # confirm what is actually being reviewed
```

## Step 1: Review — once

Run the repo's own reviewer over the committed branch or PR:

- `review-and-evaluate` — the default. It runs the security-first review and, in
  parallel, judges each finding against branch goals and existing project patterns,
  then synthesizes a decision-ready report. Use this when the question is "should we
  act on this?"
- `review-pr` — the raw rigorous review, when you only want findings.

Both live in `.agents/skills/` and discover branch scope themselves. Let them.

Run it **once**. Never re-run a reviewer, or ask one to "look again" or "find more":
that raises the count and drowns the signal, and the extra findings are mostly
invented — which is how a one-line change reaches round seven.

## Step 2: Verify before you act

**Before acting on any significant finding, spawn one verifier charged with
refuting it.** Significant means: it would change shipped behavior, or it would
reverse a design decision. Typos and obvious mistakes skip this.

One verifier per finding — a refutation attempt, not a vote. Its charter:

> Here is a claim about this code: `<finding>`. Try to prove it wrong. Read the
> primary sources — the actual file at the cited line, the actual spec, the actual
> test. Report CONFIRMED with citations, or REFUTED with citations. "The observation
> is accurate but the verdict is wrong" is a valid and common answer.

That last sentence carries the weight. A reviewer routinely notices something real
and draws the wrong conclusion from it — the code is unusual but correct, the
pattern is deliberate, the "missing" check happens upstream.

**Read the source yourself before directing any fix that reverses a prior ruling or
claims an inconsistency with a spec.** Reviewers fabricate plausible citations. So
do verifiers. Open the cited location and look.

## Step 3: Triage

Every finding lands in exactly one bucket, with its evidence recorded:

- **Fix now** — confirmed, in scope, worth the change.
- **Bank** — real, but out of scope. Record it with repro evidence for a dedicated
  pass. Never fix it inline; never drop it.
- **Reject** — refuted, or a deliberate choice. Record why, so the next review does
  not resurrect it.

Findings against `packages/*` get a personal diff read before shipping regardless of
who confirmed them. A shared package's blast radius exceeds any single service's
review scope, and the reviewer only saw one service.

## Step 4: Fix — two rounds at most

Fix the **Fix now** bucket, re-commit, then close the round: re-run the gates and
re-verify the findings you just fixed. A round does **not** re-run the full review
hunting for new findings.

**Two rounds, then stop.** After the second, anything still open goes to the user
with its evidence: fix further, accept and document it, or re-plan. A documented
known issue is a better outcome than a third round.

A review that returns zero findings is a clean pass only if its agents all
completed — an errored reviewer, an implausibly fast run, or a wrong agent count
means the run is broken; relaunch it rather than recording a pass. Zero findings from
a run that did complete is a real result, not a failure to look.

## Output

Inside a `/zora-cycle` run, write this to `$RUN/review.md`; otherwise report it in
chat.

- The diff's current state and your verdict on it.
- Per finding: the claim, the verification result with citations, the bucket, and
  why.
- Rounds used, and anything left open for the user.
- The banked list, carried forward intact.
- What you chose not to review, and why.
