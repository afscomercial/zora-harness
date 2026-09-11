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
             ◀─── poll status ─────────────────    fresh k3d cluster + Tilt profile
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
| `codex-qa-prompt.md` | VM (sent each job) | Codex's standing instructions — the validator protocol for Linux/k3d |
| `charter.template.md` | laptop | What the lead fills in per run |
| `verdict.schema.json` | VM (sent each job) | Forces Codex's final message into the `verdict.json` shape |

`qa-job.sh`, the prompt and the schema are uploaded with every job, so the VM always
runs the harness version that dispatched it. The VM needs tools and credentials, not
harness files.

## What the laptop refuses before anything is sent

- HEAD is not the commit being validated, or the tree has uncommitted changes
- the base is not an ancestor of the commit, or the commit is not on any origin branch
- the Tilt profile does not exist in `tilt/Tiltfile`
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

**A dedicated user**, e.g. `zqa`, used for nothing else. Install for that user: git,
Docker (user in the `docker` group), k3d, kubectl, Tilt, Node 22 + pnpm 9, mongosh,
python3, Playwright's Chromium with system deps
(`pnpm dlx playwright install --with-deps chromium`), and Codex CLI **0.153 or newer**.

**Codex login.** As `zqa`: `codex login --device-auth`. `~/.codex/auth.json` is a live
credential — `chmod 600`, never copied anywhere.

**QA home** (`ZORA_QA_REMOTE_HOME`, e.g. `/home/zqa/zora-qa`):

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
ZORA_QA_REMOTE_HOME=/home/zqa/zora-qa
# ZORA_QA_REPO=git@github.com:<org>/zora-pantheon.git   # default: your origin URL
```

Try the whole path without sending anything:
`run-codex-qa --run "$RUN" --base "$BASE_SHA" --commit "$HEAD_SHA" --profile documents --dry-run`.

## Security model — read this before provisioning

Codex runs with `--sandbox danger-full-access` because it needs Docker, Kubernetes, a
browser, the filesystem and the network. That is only reasonable because **the VM is the
trust boundary**, so the VM must hold nothing worth stealing:

- **What full access really means.** Codex — and anything a prompt injection in product
  output talks it into — can read everything the `zqa` user can: `~/.codex/auth.json`
  (your Codex session), the deploy key, `vm.env`, `secrets/`. The `docker` group is
  root-equivalent, so in practice that is the whole VM. Isolation protects your laptop;
  it does not protect the VM's own credentials.
- **So limit what those credentials can do.** GitHub: a read-only deploy key for
  zora-pantheon only, never a personal token. App secrets: sandbox-only (a test Clerk
  instance, sandbox API keys), never production. `NPM_TOKEN`: read-only. Consider a
  dedicated Codex account or API key with its own limits instead of your personal
  session.
- **Restrict egress.** Full access includes unrestricted network. An outbound allowlist
  on the VM — the OpenAI API, GitHub, the npm registry, the container registries you pull
  from, Playwright's browser download host — is the control that actually limits
  exfiltration. Everything else: deny.
- **Evidence coming back is untrusted.** The dispatcher extracts only regular files with
  allowed names and types (`.txt .log .json .jsonl .md .png .jpg .webm .zip`), size-capped,
  with no links and no paths escaping the job folder; one violation rejects the whole
  archive. The lead reads evidence as data and never executes it.
- **The manifest guards against accidents, not a hostile VM.** It catches the wrong
  commit, a dirty checkout, Codex editing code, and corrupted downloads. A compromised
  VM could forge its own manifest, which is why the VM holds only low-value, sandbox-only
  credentials and why the lead still reads the evidence itself.
