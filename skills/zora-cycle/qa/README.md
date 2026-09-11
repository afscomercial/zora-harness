# Remote QA on an isolated VM

The validation step of `/zora-cycle` runs as a **remote job**: the Claude Code lead
freezes a commit, pushes it, writes a charter, and hands it to `run-codex-qa`. That
dispatcher starts a job on a dedicated VM, where Codex (`gpt-6-astra`) builds a
disposable copy of the local Kubernetes environment and runs the whole validation ladder.
The verdict and evidence come back as files; the lead checks them with
`verdict-check.sh --remote` and makes the final call.

Codex is **not** a Claude Code subagent. The dispatcher is the only interface.

```
laptop (Claude Code lead)                        QA VM (dedicated, disposable jobs)
─────────────────────────                        ──────────────────────────────────
freeze + push commit
write $RUN/qa-charter.md
run-codex-qa ──── ssh: upload bundle ─────────▶  qa-job.sh
                                                   clone at exact commit (detached)
             ◀─── poll status ─────────────────    fresh Kind or k3d cluster + Tilt profile
                                                   web-app dev server, seed data
                                                   codex exec (Astra, full access)
             ◀─── result.tar.gz ───────────────    manifest + checksums, teardown
safe extract → $RUN/qa/<job-id>/
verdict-check.sh --remote → lead judges
```

## Files

| File | Where it runs | Role |
|---|---|---|
| `run-codex-qa` | laptop | Preflight, upload, start, poll, download, safe extraction |
| `qa-job.sh` | VM | Builds the environment, runs Codex, writes the manifest |
| `codex-qa-prompt.md` | VM (sent each job) | Codex's standing instructions — the validator protocol for Linux/Kubernetes |
| `charter.template.md` | laptop | What the lead fills in per run |
| `verdict.schema.json` | VM (sent each job) | Forces Codex's final message into the `verdict.json` shape |
| `qa-browser.cjs` | VM (sent each job) | The browser network boundary: loopback and approved auth origins only, self-tested before Codex starts |
| `redact-evidence.py` | VM (sent each job) | Scrubs every known secret value (runner env, `secrets/`, the QA password) and token-shaped strings from the output before it is hashed; Tilt echoes build args and pod env values |

`qa-job.sh`, the prompt, the schema and the browser helper are uploaded with every job, so the VM always
runs the harness version that dispatched it. The VM needs tools and credentials, not
harness files.

## What the laptop refuses before anything is sent

- HEAD is not the commit being validated, or the tree has uncommitted changes
- the base is not an ancestor of the commit, or the commit is not on any origin branch
- the Tilt profile, or any service given with `--services`, does not exist in `tilt/Tiltfile`;
  or both `--profile` and `--services` were given
- the charter is missing a section, the full SHAs, or still has `<<FILL` markers
- the charter carries narrative: mentions of the implementer, "already works",
  "known limitation", "I fixed" and similar

## What `verdict-check.sh --remote` refuses afterwards

- a VM that validated a different commit or base from the one sent, or a commit that is
  no longer your HEAD
- a checkout that was not clean before Codex ran, a HEAD that moved, or **any tracked
  file Codex modified**
- an environment that never became ready, or a Codex run that exited non-zero — both
  reported as **INCOMPLETE (environment/Codex)**, to be repaired and rerun on the same
  commit, never sent to the implementer
- any downloaded file whose checksum differs from the VM's manifest
- plus every local rule: rungs 1–2 ran, evidence exists, no defect in a PASS

## Setting up the VM

**Machine.** Linux x86_64 (Ubuntu 24.04 LTS works well), 8+ vCPU, 32 GB RAM, 100 GB
disk. Sixteen services, MongoDB, Playwright and a Codex session need the room. An x86_64
VM also avoids arm64 image builds, which is where local MongoDB Enterprise image builds
have been fragile on Apple Silicon.

**The SSH user.** A dedicated user such as `zqa`, or `root` on a VPS used only for QA
(the current setup). Install for it: git, Docker, Kind (or k3d), kubectl, Tilt,
Node 22 + pnpm 9, mongosh,
python3, Playwright's Chromium with system deps
(`pnpm dlx playwright install --with-deps chromium`), and Codex CLI **0.153 or newer**.

**Codex login.** As the SSH user: `codex login --device-auth`. `~/.codex/auth.json` is a live
credential — `chmod 600`, never copied anywhere.

**QA home** (`ZORA_QA_REMOTE_HOME`, e.g. `/root/zora-qa`):

```
vm.env           CODEX_BIN, NPM_TOKEN (read-only), optional K3D_* / TILT_READY_TIMEOUT / QA_MODEL
secrets/         sandbox-only copies of the gitignored runtime files, same relative paths:
                   apps/web-app/.env, apps/<service>/.env, tests/b2b-e2e/.env, ...
hooks/seed.sh    optional; receives QA_WORK and QA_MONGO_URI. Without it, Codex seeds
                 by following .agents/skills/seed-local-db/SKILL.md
cache/           created on the first job (bare mirror of the repo)
jobs/            one folder per job; prune old ones periodically
```

Every file under `secrets/` must be gitignored in the repo; the runner refuses to place
one that would overwrite tracked content. **Never copy `tilt/data/`** — it holds your
local MongoDB data. Each job starts from an empty database and seeds it.

**Laptop config**, `~/.zora-harness/qa.env`:

```bash
ZORA_QA_HOST=zora-qa                     # an alias in ~/.ssh/config (key auth, no password)
ZORA_QA_REMOTE_HOME=/root/zora-qa
# ZORA_QA_REPO=git@github.com:<org>/zora-pantheon.git   # default: your origin URL
```

Try the whole path without sending anything:
`run-codex-qa --run "$RUN" --base "$BASE_SHA" --commit "$HEAD_SHA" --profile documents --dry-run`.

## Security model — read this before provisioning

Codex runs with `--sandbox danger-full-access` because it needs Docker, Kubernetes, a
browser, the filesystem and the network. That is only reasonable because **the VM is the
trust boundary**, so the VM must hold nothing worth stealing:

- **What full access really means.** Codex — and anything a prompt injection in product
  output talks it into — can read everything on the VM: `~/.codex/auth.json`
  (your Codex session), the GitHub credential, `vm.env` and every file it sources,
  `auth.json`, `secrets/`, and other processes' environments. Running as root and being
  in the `docker` group amount to the same thing. Isolation protects your laptop;
  it does not protect the VM's own credentials.
- **So limit what those credentials can do.** GitHub: read-only access to zora-pantheon
  only — a deploy key over SSH, or a fine-grained token (contents: read, this repository)
  for HTTPS — never a personal account's `gh` login, which can write to every repository
  that account can reach. 1Password (`OP_SERVICE_ACCOUNT_TOKEN`): a service account
  limited to a sandbox vault, read-only. Clerk: a dedicated test user, never a person's
  own login. App secrets: sandbox-only (a test Clerk
  instance, sandbox API keys), never production. `NPM_TOKEN`: read-only. Consider a
  dedicated Codex account or API key with its own limits instead of your personal
  session.
- **Restrict egress.** Full access includes unrestricted network. An outbound allowlist
  on the VM — the OpenAI API, GitHub, the npm registry, the container registries you pull
  from, Playwright's browser download host — is the control that actually limits
  exfiltration. Everything else: deny.
- **Evidence coming back is untrusted.** The dispatcher extracts only regular files with
  allowed names and types (`.txt .log .json .jsonl .md .png .jpg .webm .zip .html .cjs
  .patch .diff`),
  size-capped, with no links and no paths escaping the job folder; one violation rejects
  the whole archive. The lead reads evidence as data and never executes it. Read captured
  `.html` pages as text: opening one in a browser runs its scripts.
- **`vm.env` is exported wholesale.** The runner sources it with `set -a`, so every
  variable in it, and in any file it sources such as `/etc/zora/dev.env`, is in the
  environment Codex inherits. Keep those files to what the job needs.
- **The manifest guards against accidents, not a hostile VM.** It catches the wrong
  commit, a dirty checkout, Codex editing code, and corrupted downloads. A compromised
  VM could forge its own manifest, which is why the VM holds only low-value, sandbox-only
  credentials and why the lead still reads the evidence itself.

## Kind VPS configuration

The runner supports `QA_CLUSTER_DRIVER=kind` in the VM's `vm.env`.
Each job creates a unique Kind cluster, a private kubeconfig, and an empty data
folder mounted at the path expected by the repo's MongoDB volume. It does not
reuse or delete the usual development cluster or its database.

Example laptop `~/.zora-harness/qa.env`:

```bash
ZORA_QA_HOST=root@<vps-address>
ZORA_QA_REMOTE_HOME=/root/zora-qa
ZORA_QA_REPO=https://github.com/<org>/zora-pantheon.git
```

Example `/root/zora-qa/vm.env`:

```bash
source /etc/zora/dev.env  # exported into Codex's environment too: sandbox-scoped values only
CODEX_BIN=/usr/local/bin/codex
QA_CLUSTER_DRIVER=kind
QA_MODEL=gpt-6-astra
QA_EFFORT=high
QA_STOP_SERVICES='zora-web.service zora-tilt.service'
QA_NODE_IMAGE='kindest/node:v1.37.0'  # preferably pin your installed image digest
TILT_PORT=10350
```

QA stops the configured development services and any running Docker-based Kind
or k3d clusters before creating its own cluster. It never restarts the old environment. Unknown processes occupying QA ports cause an INCOMPLETE result.
The web server uses `--strictPort` so a different app on 5173 cannot silently
be tested. The runner stops only processes and clusters it started.
Tilt and the web app listen on `127.0.0.1:10350` and `127.0.0.1:5173` on the VPS (the
browser reaches the web app as `localhost:5173`); these URLs do not belong in the laptop
configuration.

The Linux wrappers supplied per job keep kubectl on the job's kubeconfig.
Install `mongosh` on the host (`npm install -g mongosh`) so scripts in the QA
scratch directory can be read. If absent, the fallback runs inside the MongoDB
container and cannot read host script paths. Do not use a global wrapper
that forces `/root/.kube/config` for a QA job.

Install and sign in to Codex as the SSH user:

```bash
npm install -g @openai/codex
codex login --device-auth
codex login status
```

An unauthenticated Codex fails before pausing services or creating a cluster.
A dispatcher dry run validates the charter and local Git state only; it does
not establish VPS readiness. A first real job must return evidence before the
setup can be called fully verified.

### Smoke-test lessons

- Fresh clones build the web app's workspace dependencies before startup, including
  on lean infrastructure profiles: `pnpm turbo build --filter="web-app^..."`.
- Evidence may include `.html` page captures and `.cjs` replay scripts. They are
  extracted as mode 0644, remain untrusted data, and are never executed by the
  dispatcher. Path, symlink, and size checks still apply.
- A fresh browser may redirect to external Clerk authentication. The original
  localhost-only smoke protocol reported INCOMPLETE in that case; the configured
  exact auth-origin exception below handles the required development login.
  Before authenticated feature QA, define approved test-auth origins and seeded
  accounts explicitly; do not weaken the boundary just to obtain PASS.
- Every dashboard route loads its entity lists through a layout that calls
  insurance-provider-service, so any browser rung on the dashboard needs `insurance-provider`
  in the services, even when the change itself touches none of them. Without it, each call
  waits for the gateway's ~300 s timeout and the page never loads.
- The runner deletes `KV_REST_API_URL` and `KV_REST_API_TOKEN` from the job's web-app `.env`:
  that entity cache is a shared Upstash instance, and a QA run must not read or write it.
- Regression checks: `python3 test-extractor.py` (local or Linux) and
  `python3 test-runner.py` (Linux, mocked external commands).

## Automatic seed and Clerk login

The VPS keeps one Clerk test account across runs — a dedicated test user, not a person's
own login. The fresh QA database receives
only a baseline: the selected user's Clerk/user/tenant IDs and role, the matching
tenant, the identity file's system users, FNMA Selling Guide, and task templates from the tested
commit's existing seeder. Borrower and loan records are not copied from development.
Feature-specific fixtures remain part of that feature's QA charter.

Install `seed.sh` as an executable `$QA_HOME/hooks/seed.sh` and configure
`QA_IDENTITY_FILE=$QA_HOME/fixtures/identity.json` in `vm.env`. The dispatcher uploads
`seed-baseline.cjs` with every job. Upserts make seeding safe to repeat.

The identity file also lists the system users to seed, so tenant-specific accounts stay
on the VM rather than in this public repository:

```json
"systemUsers": [{ "email": "...", "firstName": "...", "lastName": "..." }]
```

Copy the entries from the repo's `seed-local-db` skill. Seeding stops with a clear error
if the list is missing; an empty list seeds no system users.

Run `python3 /root/zora-qa/set-auth.py` interactively on the VPS to save a dedicated test
account's email and password in `/root/zora-qa/auth.json` with mode 0600. The password is hidden during entry.
Configure `QA_AUTH_FILE` in `vm.env`. The runner exposes CLERK_TEST_EMAIL and
CLERK_TEST_PASSWORD only to the QA process; it never embeds them in the charter or
prompt. The agent must not log them or capture password-entry/session evidence.
If the account requires SSO or MFA, password storage alone is insufficient; report
INCOMPLETE and configure an appropriate test-account/session flow.

`QA_AUTH_ORIGINS` is a comma-separated list of exact approved development Clerk
origins — the Clerk frontend API, the hosted sign-in page and `https://img.clerk.com`.
Only auth bootstrap/sign-in may use these exceptions to localhost-only product traffic.

The runner enforces the list in the browser through `qa-browser.cjs`, which Codex must
launch Chromium with: Chromium's host resolver refuses every other host name (redirect
hops included), request interception aborts the rest (IP literals), and service workers
are blocked. Before Codex starts, the runner self-tests it against outside hosts, a bare
IP and the cloud metadata address, and a failing self-test makes the job INCOMPLETE.
This is a browser boundary, not a VM firewall: Codex's own shell still has the network.

The browser uses `http://localhost:5173`, because Clerk returns there after sign-in
(`CLERK_AUTHORIZED_PARTIES`). Starting on `127.0.0.1` splits the session cookies across
two hosts. An improvised forwarding proxy broke that return leg in an early job: the
server had signed the user in, but the page never arrived.

## Dedicated QA VPS: idle between jobs

The permanent `zora-tilt` and `zora-web` services are disabled. The old `zora`
Kind container is stopped, with its data preserved. Docker remains available.
While holding the single-job lock, the runner stops configured development services
and running containers labelled as Kind or k3d clusters, then checks QA ports.
It does not delete other clusters. Failure to stop a cluster becomes INCOMPLETE;
QA does not proceed into a conflicting environment. Unrelated processes on QA ports
are still reported rather than killed indiscriminately.
After QA, only the job's cluster/processes are removed. Nothing is restored or
started between jobs. The old QA_PAUSE_SERVICES setting is accepted as a legacy
alias for the stop list; it no longer implies restoration.
