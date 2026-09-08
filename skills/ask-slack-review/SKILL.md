---
name: ask-slack-review
description: Draft a short Slack message asking teammates to review a finished zora-pantheon pull request, written for the reader rather than as a technical changelog. Use when the orchestrate pipeline reaches its closing step, or when the user asks to request a peer review, ping the team about a PR, or announce that work is ready for review.
---

# Ask for a Peer Review in Slack

Draft a message that makes a colleague want to open the PR. Draft it, show it to
the user, and let them send it — never post to a channel without explicit approval.

## Write it for the reader

The audience skims Slack between other work, and often includes people who will
never read the diff. So lead with what changed for the user, not with what changed
in the tree.

- **Before → Now**, from the user's perspective. This is the first line.
- What someone can now do, see, or no longer has to deal with.
- **One concrete verification result**, so the claim is trustworthy — e.g.
  "verified on the local cluster: 11 tasks flagged, 0 duplicates."
- The PR link, and what kind of review you want: a full pass, a look at one risky
  area, or a design opinion.
- Anything a reviewer needs to know before starting — a migration, a shared package
  touched, a decision you want challenged.

Keep it to one screen. Three to six bullets, not twenty. Skip file paths, function
names, schema details, and config flags unless the reviewer genuinely cannot start
without them — in which case, one line naming the area is enough.

## Shape

```
<Before → Now, one or two lines>

<3–6 bullets: what a user can now do / what changed for them>

Verified: <one concrete result>
Review focus: <what you want eyes on>
<PR link>
```

## Before sending

- Confirm the PR is actually open and CI is green — asking for review on a red PR
  wastes the reviewer's time and yours.
- Ask the user which channel or person, and get their approval on the text.
- Match the channel's register. Use the repo's `slack` skills for formatting and
  for sending once approved.
