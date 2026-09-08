---
name: agent-review
description: Run the review loop over a committed zora-pantheon branch or PR — a scoped review, adversarial verification of each significant finding before acting on it, triage into fix/bank/reject, and repeat until the diff is genuinely satisfactory. Use when the orchestrate pipeline reaches its review stage, or when the user asks for a reviewed-and-adjudicated verdict on a branch rather than a raw list of review comments.
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

## Step 1: Review

Run the repo's own reviewer over the committed branch or PR:

- `review-and-evaluate` — the default. It runs the security-first review and, in
  parallel, judges each finding against branch goals and existing project patterns,
  then synthesizes a decision-ready report. Use this when the question is "should we
  act on this?"
- `review-pr` — the raw rigorous review, when you only want findings.

Both live in `.agents/skills/` and discover branch scope themselves. Let them.

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

## Step 4: Loop

Fix, re-commit, re-review, re-verify. Continue until **you** are satisfied with the
diff.

Reviewer silence is not the bar. A review that returns zero findings is a clean pass
only if its agents all completed — an errored reviewer, an implausibly fast run, or
a wrong agent count means the run is broken, and the fix is to relaunch it, not to
record a pass.

## Output

- The diff's current state and your verdict on it.
- Per finding: the claim, the verification result with citations, the bucket, and
  why.
- The banked list, carried forward intact.
- What you chose not to review, and why.
