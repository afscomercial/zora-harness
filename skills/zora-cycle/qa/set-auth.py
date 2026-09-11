#!/usr/bin/env python3
"""Run interactively on the QA VM; never print credentials or put them in arguments.

Use a dedicated Clerk test account, never a person's own login: Codex runs with full
access on the VM and can read whatever this file holds.
"""
import getpass
import json
import os
from pathlib import Path

path = Path(os.environ.get("QA_AUTH_FILE", "/root/zora-qa/auth.json"))
email = input("Clerk test-account email: ").strip()
if not email:
    raise SystemExit("No email supplied; nothing changed.")
password = getpass.getpass("Clerk password (hidden): ")
if not password:
    raise SystemExit("No password supplied; nothing changed.")
os.umask(0o077)
temp = path.with_suffix(".tmp")
with temp.open("w") as f:
    json.dump({"email": email, "password": password}, f)
temp.chmod(0o600)
temp.replace(path)
print(f"Saved QA credentials to {path}. The password was not displayed.")
