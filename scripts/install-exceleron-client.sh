#!/usr/bin/env bash
# Install or update the MyUsage Archive component into a Home Assistant
# config directory. Self-contained: fetches the repository itself, then
# copies the one folder HA needs. Needs only bash, cp/find (coreutils) and
# git or curl+tar.
#
#   install-exceleron-client.sh [HA_CONFIG_DIR]                 install or update
#   install-exceleron-client.sh --uninstall [HA_CONFIG_DIR]     remove the component
#   install-exceleron-client.sh --uninstall --purge-archive ... also delete the archives
#
# Uninstall removes custom_components/myusage_archive only. The archive
# databases under <config>/myusage_archive/ are the product (the portal
# forgets 15-minute data after a week) and are kept unless --purge-archive
# is given. Remove the integration in Home Assistant first (Settings ->
# Devices & services -> MyUsage Archive -> Delete), which also drops that
# entry's archive, then uninstall, then restart.
#
# One-liner on the Home Assistant box:
#   curl -fsSL https://raw.githubusercontent.com/ther3zz/MyUsage-Archive/main/scripts/install-exceleron-client.sh \
#     | bash -s -- /root/homeassistant
#
# Options (environment):
#   REF=main         branch or tag to install
#   REPO_URL=...     override the repository URL (GitHub or a Forgejo/Gitea
#                    mirror; both tarball layouts are understood)
#   CACHE_DIR=...    where the checkout lives (default: ~/.cache/exceleron-client)
#   SRC_DIR=...      skip fetching; install from this checkout instead
#   DRY_RUN=1        show what would change, touch nothing (also --dry-run)
#
# When the script is run from inside a checkout, that checkout is used as the
# source (developer mode) and nothing is fetched.
#
# The checkout stays outside the HA config dir: HA scans custom_components/
# for integrations and only wants custom_components/myusage_archive/.
# Restart Home Assistant afterwards.
set -euo pipefail

MODE="install"
PURGE=0
CONFIG_DIR=""
for arg in "$@"; do
  case "$arg" in
    --uninstall)     MODE="uninstall" ;;
    --purge-archive) PURGE=1 ;;
    --dry-run)       DRY_RUN=1 ;;
    -h|--help)
      if [ -f "$0" ]; then sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
      else echo "usage: install-exceleron-client.sh [--uninstall [--purge-archive]] [--dry-run] [HA_CONFIG_DIR]"; fi
      exit 0 ;;
    -*)              echo "unknown option: $arg" >&2; exit 2 ;;
    *)               CONFIG_DIR="$arg" ;;
  esac
done
CONFIG_DIR="${CONFIG_DIR:-/root/homeassistant}"
DRY_RUN="${DRY_RUN:-0}"
REPO_URL="${REPO_URL:-https://github.com/ther3zz/MyUsage-Archive.git}"
REF="${REF:-main}"
CACHE_DIR="${CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/exceleron-client}"
COMPONENT="custom_components/myusage_archive"

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*"; }

# ---------------------------------------------------------------- source

resolve_source() {
  # 1. explicit override
  if [ -n "${SRC_DIR:-}" ]; then
    echo "$SRC_DIR"; return
  fi
  # 2. developer mode: the script lives inside a checkout
  if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    local here
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd || true)"
    if [ -n "$here" ] && [ -f "$here/$COMPONENT/manifest.json" ]; then
      echo "$here"; return
    fi
  fi
  # 3. fetch it
  fetch_repo
  echo "$CACHE_DIR"
}

fetch_repo() {
  mkdir -p "$(dirname "$CACHE_DIR")"
  if command -v git >/dev/null 2>&1; then
    if [ -d "$CACHE_DIR/.git" ]; then
      log "updating $CACHE_DIR ($REF)" >&2
      git -C "$CACHE_DIR" fetch --quiet --depth 1 origin "$REF"
      git -C "$CACHE_DIR" checkout --quiet --force FETCH_HEAD
    else
      log "cloning $REPO_URL ($REF) -> $CACHE_DIR" >&2
      rm -rf "$CACHE_DIR"
      git clone --quiet --depth 1 --branch "$REF" "$REPO_URL" "$CACHE_DIR"
    fi
    return
  fi
  command -v curl >/dev/null 2>&1 || die "need git or curl to fetch the repository"
  local base="${REPO_URL%.git}" tarball
  case "$base" in
    *github.com*) tarball="$base/archive/refs/heads/$REF.tar.gz" ;;  # GitHub layout
    *)            tarball="$base/archive/$REF.tar.gz" ;;             # Forgejo/Gitea layout
  esac
  log "downloading $tarball" >&2
  rm -rf "$CACHE_DIR"
  mkdir -p "$CACHE_DIR"
  curl -fsSL "$tarball" | tar -xz -C "$CACHE_DIR" --strip-components=1
}

# ---------------------------------------------------------------- target

# Escalate only when the config dir is not ours (e.g. /root/homeassistant).
SUDO=""
if [ ! -w "$CONFIG_DIR" ] && [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
fi
$SUDO test -f "$CONFIG_DIR/configuration.yaml" \
  || die "$CONFIG_DIR does not look like a Home Assistant config dir (no configuration.yaml)"

DEST_PARENT="$CONFIG_DIR/custom_components"
DEST="$DEST_PARENT/myusage_archive"
ARCHIVES="$CONFIG_DIR/myusage_archive"

# ------------------------------------------------------------- uninstall

if [ "$MODE" = "uninstall" ]; then
  if $SUDO test -d "$DEST"; then
    log "removing $DEST"
    [ "$DRY_RUN" = "1" ] || $SUDO rm -rf "$DEST"
  else
    log "$DEST is not installed"
  fi
  if $SUDO test -d "$ARCHIVES"; then
    if [ "$PURGE" = "1" ]; then
      log "removing archives in $ARCHIVES (--purge-archive)"
      $SUDO ls -la "$ARCHIVES" 2>/dev/null || true
      [ "$DRY_RUN" = "1" ] || $SUDO rm -rf "$ARCHIVES"
    else
      log "keeping archives in $ARCHIVES (pass --purge-archive to delete them):"
      $SUDO ls -la "$ARCHIVES" 2>/dev/null || true
    fi
  fi
  [ "$DRY_RUN" = "1" ] && echo "dry run: nothing removed"
  echo "restart Home Assistant to finish. If the integration is still configured,"
  echo "delete it under Settings -> Devices & services first (or after the restart)."
  exit 0
fi

# ---------------------------------------------------------------- source

SRC_ROOT="$(resolve_source)"
SRC="$SRC_ROOT/$COMPONENT"
[ -f "$SRC/manifest.json" ] || die "component not found at $SRC"
[ -f "$SRC/vendor/myusage_archive/archive.py" ] || die "vendored library missing in $SRC"

if [ -d "$SRC_ROOT/.git" ] && [ -n "$(git -C "$SRC_ROOT" status --porcelain -- custom_components 2>/dev/null)" ]; then
  echo "warning: uncommitted changes under custom_components/ will be installed" >&2
fi

VERSION="$(sed -n 's/^ *"version": *"\([^"]*\)".*/\1/p' "$SRC/manifest.json")"
COMMIT="$(git -C "$SRC_ROOT" rev-parse --short HEAD 2>/dev/null || echo "$REF")"
log "installing myusage_archive ${VERSION:-?} ($COMMIT) -> $DEST"

# ------------------------------------------------------------------ copy
# Plain cp/find only (no rsync on a stock HA box). The new tree is staged
# next to the destination and swapped in, so HA never sees a half-copied
# folder, and stale files from an older version cannot linger.

if [ "$DRY_RUN" = "1" ]; then
  if $SUDO test -d "$DEST"; then
    echo "would replace $DEST with $SRC; differences:"
    $SUDO diff -rq -x __pycache__ -x '*.pyc' "$SRC" "$DEST" 2>/dev/null \
      || $SUDO diff -rq "$SRC" "$DEST" 2>/dev/null | grep -v __pycache__ || true
  else
    echo "would create $DEST from $SRC"
  fi
  echo "dry run: nothing copied"
  exit 0
fi

STAGE="$DEST.new.$$"
OLD="$DEST.old.$$"
cleanup() { $SUDO rm -rf "$STAGE"; }   # an aborted run must not leave a stray folder
trap cleanup EXIT
$SUDO mkdir -p "$DEST_PARENT"
$SUDO rm -rf "$STAGE"
$SUDO cp -a "$SRC" "$STAGE"
$SUDO find "$STAGE" \( -name __pycache__ -o -name '*.pyc' \) -prune -exec rm -rf {} +
# Match whatever owns custom_components/ so HA can read (and later write
# __pycache__ into) the folder. stat -c rather than chown --reference: the
# HA SSH add-on ships BusyBox, which lacks the latter.
OWNER="$($SUDO stat -c '%u:%g' "$DEST_PARENT" 2>/dev/null || true)"
if [ -n "$OWNER" ]; then
  $SUDO chown -R "$OWNER" "$STAGE"
else
  echo "warning: could not determine owner of $DEST_PARENT; leaving ownership as copied" >&2
fi
$SUDO test -f "$STAGE/vendor/myusage_archive/archive.py" || die "copy incomplete: vendored library missing"

if $SUDO test -d "$DEST"; then
  $SUDO mv "$DEST" "$OLD"
fi
$SUDO mv "$STAGE" "$DEST"
$SUDO rm -rf "$OLD"

log "installed. now restart Home Assistant (Settings -> System -> Restart)."
echo "    first install: Settings -> Devices & services -> Add integration -> MyUsage Archive"
