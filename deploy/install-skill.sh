#!/usr/bin/env bash
# Install the `sugardaddy-review` Claude skill into ~/.claude/skills on this
# machine. Safe to re-run: it refreshes SKILL.md but never overwrites an
# existing connection.env or the review history.
#
#   bash deploy/install-skill.sh
#
# After the first install, edit the printed connection.env with your serve host.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/skills/sugardaddy-review" && pwd)"
DEST="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}/sugardaddy-review"

mkdir -p "$DEST/history"
cp "$SRC/SKILL.md" "$DEST/SKILL.md"
cp "$SRC/connection.env.example" "$DEST/connection.env.example"

if [[ -f "$DEST/connection.env" ]]; then
  echo "==> kept existing $DEST/connection.env"
else
  cp "$SRC/connection.env.example" "$DEST/connection.env"
  echo "==> created $DEST/connection.env from the example"
  echo "    EDIT IT: set SD_REVIEW_HOST (and check the other values) before first use."
fi

echo "Installed sugardaddy-review skill to $DEST"
echo "Review history (glucose data) stays here and is never committed."
