#!/usr/bin/env bash
# Link the zora-harness agents and skills into ~/.claude so Claude Code discovers
# them from any working directory — including inside the zora-pantheon repo, which
# never sees a single file from this harness.
#
# Idempotent. Re-run after adding an agent or skill. Pass --uninstall to remove.
set -euo pipefail

HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${HOME}/.claude"

uninstall=false
[[ "${1:-}" == "--uninstall" ]] && uninstall=true

link_one() {
  local src="$1" dest="$2" label="$3"

  if [[ "$uninstall" == true ]]; then
    if [[ -L "$dest" ]]; then
      rm "$dest"; echo "  removed  $label"
    fi
    return
  fi

  # Never clobber a real file or directory that isn't ours.
  if [[ -e "$dest" && ! -L "$dest" ]]; then
    echo "  SKIP     $label — a real file/dir already exists at $dest" >&2
    return
  fi
  if [[ -L "$dest" && "$(readlink "$dest")" != "$src" ]]; then
    echo "  SKIP     $label — symlink exists pointing elsewhere: $(readlink "$dest")" >&2
    return
  fi

  ln -sfn "$src" "$dest"
  echo "  linked   $label"
}

mkdir -p "${CLAUDE_DIR}/agents" "${CLAUDE_DIR}/skills"

echo "agents:"
for f in "${HARNESS}"/agents/*.md; do
  [[ -e "$f" ]] || continue
  link_one "$f" "${CLAUDE_DIR}/agents/$(basename "$f")" "$(basename "$f")"
done

echo "skills:"
for d in "${HARNESS}"/skills/*/; do
  [[ -d "$d" ]] || continue
  name="$(basename "$d")"
  link_one "${d%/}" "${CLAUDE_DIR}/skills/${name}" "${name}/"
done

if [[ "$uninstall" == true ]]; then
  echo
  echo "Uninstalled. The real files remain in ${HARNESS}"
else
  echo
  echo "Installed. Source of truth stays in ${HARNESS}"
  echo "Restart Claude Code (or start a new session) to pick up the changes."
fi
