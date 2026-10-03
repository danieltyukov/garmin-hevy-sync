#!/bin/sh
# Installs garmin-hevy-sync on macOS and Linux.
#
#   curl -LsSf https://danieltyukov.github.io/garmin-hevy-sync/install.sh | sh
#   curl -LsSf https://danieltyukov.github.io/garmin-hevy-sync/install.sh | sh -s -- --no-setup
#
# Environment:
#   GH_SYNC_VERSION   Install this release (e.g. 0.2.0) instead of the latest.
#   GH_SYNC_SOURCE    Install from this source instead (a path or URL; for testing).
#
# What it does: installs uv (Astral's Python tool manager, into ~/.local/bin) if
# it is missing, uses it to install garmin-hevy-sync in its own environment
# with its own Python 3.12, then starts the interactive setup. Re-running it
# upgrades an existing install; your settings are kept.
set -eu

REPO="danieltyukov/garmin-hevy-sync"

usage() {
  printf '%s\n' \
    'Usage: install.sh [--no-setup]' \
    '' \
    '  --no-setup         Install only; run "garmin-hevy-sync setup" yourself later.' \
    '  GH_SYNC_VERSION    Environment variable that pins a release, e.g. 0.2.0.'
}

SETUP=1
for arg in "$@"; do
  case "$arg" in
    --no-setup) SETUP=0 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || fail "curl is required. Install it with your package manager and run this again."

# ---------------------------------------------------------------------- uv
find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
  elif [ -x "$HOME/.local/bin/uv" ]; then
    printf '%s\n' "$HOME/.local/bin/uv"
  elif [ -x "$HOME/.cargo/bin/uv" ]; then
    printf '%s\n' "$HOME/.cargo/bin/uv"
  fi
}

UV="$(find_uv || true)"
if [ -z "$UV" ]; then
  say "Installing uv, which manages the Python environment for garmin-hevy-sync."
  say "(https://docs.astral.sh/uv/ ; it goes into ~/.local/bin)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="$(find_uv || true)"
  [ -n "$UV" ] || fail "uv was installed but could not be found. Open a new terminal and run this again."
fi

# ------------------------------------------------------------------ source
if [ -n "${GH_SYNC_SOURCE:-}" ]; then
  SOURCE="$GH_SYNC_SOURCE"
else
  VERSION="${GH_SYNC_VERSION:-}"
  if [ -z "$VERSION" ]; then
    # /releases/latest redirects to /releases/tag/vX.Y.Z. Following the
    # redirect avoids the GitHub API and its anonymous rate limit.
    LATEST="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest" 2>/dev/null || true)"
    case "$LATEST" in
      */tag/*) VERSION="${LATEST##*/tag/}" ;;
    esac
  fi
  if [ -n "$VERSION" ]; then
    VERSION="${VERSION#v}"
    SOURCE="https://github.com/$REPO/archive/refs/tags/v$VERSION.tar.gz"
    say "Installing garmin-hevy-sync $VERSION"
  else
    SOURCE="https://github.com/$REPO/archive/refs/heads/main.tar.gz"
    say "Installing garmin-hevy-sync from the main branch"
  fi
fi

case "$SOURCE" in
  http*://*) SPEC="garmin-hevy-sync @ $SOURCE" ;;
  *) SPEC="$SOURCE" ;;
esac
"$UV" tool install --force --python 3.12 "$SPEC"

# Make sure the command is on PATH in new terminals.
"$UV" tool update-shell >/dev/null 2>&1 || true
BIN_DIR="$("$UV" tool dir --bin)"
EXE="$BIN_DIR/garmin-hevy-sync"
[ -x "$EXE" ] || fail "Installed, but $EXE is missing. Run '$UV tool list' to investigate."

say ""
say "Installed: $("$EXE" --version)"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "Open a new terminal (or add $BIN_DIR to PATH) to use the garmin-hevy-sync command." ;;
esac

if [ "$SETUP" -eq 0 ]; then
  say "Next: run 'garmin-hevy-sync setup'."
  exit 0
fi

# The script itself arrives on stdin through the pipe, so the interactive
# setup has to read from the terminal directly.
if (exec </dev/tty) 2>/dev/null; then
  say ""
  exec "$EXE" setup </dev/tty
fi
say "No terminal available for the interactive setup. Run 'garmin-hevy-sync setup' next."
