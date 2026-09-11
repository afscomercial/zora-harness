#!/usr/bin/env python3
"""Scrub secrets from a QA job's output before it is hashed and sent to the laptop.

Tilt echoes Docker build arguments and pod environment values into its log, and
Codex's event log records whatever it read. This replaces every secret value the
runner knows about, plus token-shaped strings, in every file under the output folder.

    redact-evidence.py <out-dir> <qa-home>

Known values come from the runner's environment (vm.env and anything it sources),
the files under <qa-home>/secrets/, and the QA account's password. Only names and
counts are printed, never values.
"""
import json
import os
import re
import sys

SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE|(^|_)KEY$|API_KEY")
SHAPES = [
    ("npm token", re.compile(rb"npm_[A-Za-z0-9]{36}")),
    ("1Password token", re.compile(rb"ops_[A-Za-z0-9_-]{40,}")),
    ("Clerk secret key", re.compile(rb"sk_(?:test|live)_[A-Za-z0-9]{20,}")),
    ("JWT", re.compile(rb"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
]
MIN_LENGTH = 8  # shorter values would match ordinary text


def known_values(home):
    values, public = {}, set()

    def add(name, value):
        value = (value or "").strip().strip('"').strip("'")
        if len(value) < MIN_LENGTH or value.startswith("$"):
            return
        # PUBLIC_* values ship to the browser by design; a value one of them also holds
        # is not a secret, and scrubbing it would garble every log line that uses it.
        if name.startswith("PUBLIC_"):
            public.add(value)
        else:
            values.setdefault(value, name)

    for name, value in os.environ.items():
        if SECRET_NAME.search(name):
            add(name, value)
    # os.walk, not glob: glob skips dotfiles, and these are mostly `.env` files.
    secret_files = [os.path.join(root, n) for root, _, names in os.walk(os.path.join(home, "secrets")) for n in names]
    for path in secret_files:
        if not os.path.isfile(path):
            continue
        for line in open(path, errors="ignore"):
            m = re.match(r"^\s*(?:export\s+)?([A-Za-z0-9_]+)\s*=\s*(.*)$", line)
            if m and SECRET_NAME.search(m.group(1)):
                add(m.group(1), m.group(2))
    auth_file = os.environ.get("QA_AUTH_FILE") or os.path.join(home, "auth.json")
    try:
        add("QA account password", json.load(open(auth_file))["password"])
    except (OSError, ValueError, KeyError):
        pass
    return {value: name for value, name in values.items() if value not in public}


def main(out, home):
    values = known_values(home)
    # Longest first, so a value that contains another is replaced whole.
    ordered = sorted(values.items(), key=lambda kv: len(kv[0]), reverse=True)
    counts, files_changed = {}, 0
    for root, _, names in os.walk(out):
        for n in names:
            path = os.path.join(root, n)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            data = original = open(path, "rb").read()
            for value, name in ordered:
                needle = value.encode()
                if needle in data:
                    counts[name] = counts.get(name, 0) + data.count(needle)
                    data = data.replace(needle, f"<redacted:{name}>".encode())
            for label, pattern in SHAPES:
                data, hits = pattern.subn(f"<redacted:{label}>".encode(), data)
                if hits:
                    counts[label] = counts.get(label, 0) + hits
            if data != original:
                with open(path, "wb") as f:
                    f.write(data)
                files_changed += 1
    summary = ", ".join(f"{name} x{n}" for name, n in sorted(counts.items())) or "nothing found"
    print(f"redacted {files_changed} file(s) against {len(values)} known secret value(s): {summary}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: redact-evidence.py <out-dir> <qa-home>")
    main(sys.argv[1], sys.argv[2])
