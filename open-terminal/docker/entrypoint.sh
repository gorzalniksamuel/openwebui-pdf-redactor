#!/bin/sh
# Replaces the Open Terminal slim entrypoint (which needs root + NET_ADMIN for its iptables egress filter).
# Runs as uid 1000 with no capabilities:
#  - installs the redaction instructions as ~/AGENTS.md (Open WebUI injects it into every turn)
#  - every 5 minutes: deletes ALL job leftovers (uploads, outputs, sidecars, scratch files, empty dirs) older than
#    REDACT_RETENTION_MIN minutes from the home dir and /tmp (privacy); dotfiles and AGENTS.md are kept
#  - starts the Open Terminal server
set -e
if [ "$(id -u)" = 0 ]; then
  echo "refusing to run as root; the image is meant to run as uid 1000" >&2
  exit 1
fi
export HOME=/home/user
export PATH="/home/user/.local/bin:${PATH}"
cp /opt/redact/AGENTS.md "$HOME/AGENTS.md"
# the skill normally lives in Open WebUI (Workspace -> Skills); REDACT_TERMINAL_SKILL=1 also serves it from here
if [ "${REDACT_TERMINAL_SKILL:-0}" = 1 ]; then
  mkdir -p "$HOME/.agents/skills/pdf-redaction" && cp /opt/redact/skills/pdf-redaction/SKILL.md "$HOME/.agents/skills/pdf-redaction/"
else
  rm -rf "$HOME/.agents/skills/pdf-redaction"
fi
mkdir -p "$HOME/.local/bin"
[ -f "$HOME/.bashrc" ] || cp /etc/skel/.bashrc "$HOME/.bashrc" 2>/dev/null || true
[ -f "$HOME/.profile" ] || cp /etc/skel/.profile "$HOME/.profile" 2>/dev/null || true
RET="${REDACT_RETENTION_MIN:-60}"
export TMPDIR="${TMPDIR:-/tmp}"
(
  while :; do
    find "$HOME" -mindepth 1 -xdev ! -path "$HOME/.*" ! -path "$HOME/AGENTS.md" \
      \( -type f -o -type l \) -mmin +"$RET" -delete 2>/dev/null || true
    find "$HOME" -mindepth 1 -xdev ! -path "$HOME/.*" -type d -empty -mmin +"$RET" -delete 2>/dev/null || true
    find /tmp -mindepth 1 -maxdepth 1 -user "$(id -u)" -mmin +"$RET" -exec rm -rf {} + 2>/dev/null || true
    sleep 300
  done
) &
# Open WebUI uses the server cwd for chat uploads and AGENTS.md; /app (image WORKDIR) is read-only
cd "$HOME"
exec open-terminal "$@"
