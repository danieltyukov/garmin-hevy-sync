#!/usr/bin/env bash
# Install the systemd user timer that runs the sync every 30 minutes.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

mkdir -p "$UNIT_DIR"

# The units are templates carrying an @REPO@ placeholder rather than a literal
# path, so the same files work from any clone location or username. That means
# generating real files here instead of symlinking: re-run this script after
# editing a unit.
for unit in garmin-hevy-sync.service garmin-hevy-sync.timer; do
  rm -f "$UNIT_DIR/$unit"   # may be a symlink from an earlier install
  sed "s#@REPO@#$REPO#g" "$REPO/systemd/$unit" > "$UNIT_DIR/$unit"
  echo "Installed $UNIT_DIR/$unit"
done

systemctl --user daemon-reload
systemctl --user enable --now garmin-hevy-sync.timer

# Without lingering, user units stop when the last session ends. Lingering makes
# the timer run whenever the machine is powered on, logged in or not.
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" != "yes" ]; then
  echo
  echo "Enabling linger so the timer runs even when you are not logged in."
  echo "This needs sudo once:"
  sudo loginctl enable-linger "$USER"
fi

echo
systemctl --user list-timers garmin-hevy-sync.timer --no-pager
