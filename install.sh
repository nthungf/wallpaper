#!/bin/sh
set -eu

# Set this to the raw URL of the directory containing the sync files and bundle.
BASE_URL=${WALLPAPER_SYNC_BASE_URL:-https://raw.githubusercontent.com/nthungf/wallpaper/main}

case $BASE_URL in https://*) ;; *) printf '%s\n' 'BASE_URL must use HTTPS.' >&2; exit 1 ;; esac

for command_name in curl sqlite3; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "$command_name" >&2
    exit 1
  fi
done

default_root=$(pwd)/wallpapers
if [ -r /dev/tty ] && [ -w /dev/tty ]; then
  printf 'Wallpaper folder [%s]: ' "$default_root" >/dev/tty
  IFS= read -r root </dev/tty || root=
else
  root=
fi
root=${root:-$default_root}
case $root in '~/'*) root=${HOME:?}/"${root#~/}" ;; esac
case $root in
  /*) ;;
  *) root=$PWD/$root ;;
esac

if [ -e "$root" ] && [ ! -d "$root" ]; then
  printf 'Wallpaper path exists and is not a folder: %s\n' "$root" >&2
  exit 1
fi
if [ ! -d "$root" ]; then
  if [ ! -r /dev/tty ] || [ ! -w /dev/tty ]; then
    printf 'Wallpaper folder does not exist: %s\nRun the installer interactively to confirm folder creation.\n' "$root" >&2
    exit 1
  fi
  printf 'Folder does not exist: %s\nCreate it? [y/N] ' "$root" >/dev/tty
  IFS= read -r create_answer </dev/tty || create_answer=
  case $create_answer in y|Y|yes|YES|Yes) ;; *) printf '%s\n' 'Folder creation cancelled.' >&2; exit 1 ;; esac
fi
if [ ! -r /dev/tty ] || [ ! -w /dev/tty ]; then
  printf '%s\n' 'Run the installer interactively to review the wallpaper cleanup warning.' >&2
  exit 1
fi
printf 'Warning: sync can remove wallhaven-* images in %s/approved when they are no longer approved. Continue? [y/N] ' "$root" >/dev/tty
IFS= read -r warning_answer </dev/tty || warning_answer=
case $warning_answer in y|Y|yes|YES|Yes) ;; *) printf '%s\n' 'Sync cancelled.' >&2; exit 1 ;; esac
if [ ! -d "$root" ]; then mkdir -p "$root"; fi
mkdir -p "$root/approved"
runner=$(curl -fsSL --proto '=https' --proto-redir '=https' "$BASE_URL/run_sync.sh")
printf '%s\n' "$runner" | sh -s -- "$root" "$BASE_URL"

if [ ! -r /dev/tty ] || [ ! -w /dev/tty ]; then
  printf 'Synced wallpapers to %s/approved.\n' "$root"
  exit 0
fi

printf '\nSet up automatic sync with cron?\n' >/dev/tty
printf '  1) Daily at 03:00 (default)\n  2) Weekly on Sunday at 03:00\n  3) Hourly\n  4) Skip\nChoice [1]: ' >/dev/tty
IFS= read -r schedule </dev/tty || schedule=
schedule=${schedule:-1}
case $schedule in
  1) cron_time='0 3 * * *' ;;
  2) cron_time='0 3 * * 0' ;;
  3) cron_time='0 * * * *' ;;
  4) printf 'Synced wallpapers to %s/approved.\n' "$root"; exit 0 ;;
  *) printf '%s\n' 'Unknown choice; skipping cron setup.'; exit 0 ;;
esac

if ! command -v crontab >/dev/null 2>&1; then
  printf '%s\n' 'crontab is unavailable; initial sync completed without a schedule.'
  printf 'Synced wallpapers to %s/approved; cron was not available.\n' "$root"
  exit 0
fi

quote_arg() {
  escaped=$(printf '%s' "$1" | sed "s/'/'\\\\''/g")
  printf "'%s'" "$escaped"
}
temporary=$(mktemp)
trap 'rm -f "$temporary"' EXIT HUP INT TERM
(crontab -l 2>/dev/null || true) | grep -vF '# wallpaper-sync-managed' > "$temporary" || true
cron_command="curl -fsSL --proto '=https' --proto-redir '=https' $(quote_arg "$BASE_URL/run_sync.sh") | sh -s -- $(quote_arg "$root") $(quote_arg "$BASE_URL")"
printf '%s %s >/dev/null 2>&1 # wallpaper-sync-managed\n' \
  "$cron_time" "$cron_command" >> "$temporary"
crontab "$temporary"
printf 'Synced wallpapers to %s/approved and scheduled automatic sync.\n' "$root"
