# QA charter

<!--
  Written by the Claude Code lead, from facts only. Replace every <<FILL: ...>> marker.
  Never include the implementer's reasoning, claims that the feature already works, or
  excuses for known limitations. run-codex-qa refuses a charter that does.
-->

## Commits

- Base: <<FILL: full base sha>>
- Feature: <<FILL: full feature sha>>

## Task

<<FILL: the request, in the user's words, from task.md>>

## Acceptance criteria

<<FILL: numbered criteria, each provable by a command or an observable state>>

## Environment

- Tilt profile or services: <<FILL: the profile name, or the service list given to --services>>
- Relevant services: <<FILL: e.g. api-gateway, loan-application>>
- Test accounts: <<FILL: seeded personas from seed-local-db, and what each may do>>
- Entry point: <<FILL: web-app route, API endpoint, or test command>>

## Required rungs

- 1 Static gates: required
- 2 Tests: required. Packages: <<FILL: affected packages>>
- 3 API calls and MongoDB state: <<FILL: required, or skipped and why>>
- 4 Browser (Playwright): <<FILL: required, or skipped and why>>
- 5 Adversarial cases: required. Focus: <<FILL: permissions, empty data, malformed input, boundaries, regressions to watch>>

## Evidence

Write every file to `$QA_EVIDENCE_DIR`, named by rung first, and reference it in the
verdict as `evidence/<file>`.
