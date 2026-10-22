#!/bin/sh
set -eu

# Set this to the raw URL of the directory containing the sync files and bundle.
BASE_URL=${WALLPAPER_SYNC_BASE_URL:-https://raw.githubusercontent.com/OWNER/REPO/main}

if [ "$BASE_URL" = 'https://raw.githubusercontent.com/OWNER/REPO/main' ]; then
  printf '%s\n' 'Set WALLPAPER_SYNC_BASE_URL or edit BASE_URL in install.sh before hosting it.' >&2
  exit 1
fi
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
mkdir -p "$root"
root=$(CDPATH= cd -- "$root" && pwd)
sync_dir=$root/.wallpaper-sync
mkdir -p "$sync_dir" "$root/approved"

curl -fsSL --proto '=https' --proto-redir '=https' "$BASE_URL/sync_approved.sh" -o "$sync_dir/sync_approved.sh"
curl -fsSL --proto '=https' --proto-redir '=https' "$BASE_URL/run_sync.sh" -o "$sync_dir/run_sync.sh"
curl -fsSL --proto '=https' --proto-redir '=https' "$BASE_URL/uninstall.sh" -o "$sync_dir/uninstall.sh"
printf '%s\n' "${BASE_URL%/}" > "$sync_dir/base-url"
sh "$sync_dir/run_sync.sh"

if [ ! -r /dev/tty ] || [ ! -w /dev/tty ]; then
  printf 'Installed in %s. Run %s/uninstall.sh to remove it.\n' "$root" "$sync_dir"
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
  4) printf 'Installed in %s. Run %s/uninstall.sh to remove it.\n' "$root" "$sync_dir"; exit 0 ;;
  *) printf '%s\n' 'Unknown choice; skipping cron setup.'; exit 0 ;;
esac

if ! command -v crontab >/dev/null 2>&1; then
  printf '%s\n' 'crontab is unavailable; initial sync completed without a schedule.'
  printf 'Installed in %s. Run %s/uninstall.sh to remove it.\n' "$root" "$sync_dir"
  exit 0
fi

quote_arg() {
  escaped=$(printf '%s' "$1" | sed "s/'/'\\\\''/g")
  printf "'%s'" "$escaped"
}
temporary=$(mktemp)
trap 'rm -f "$temporary"' EXIT HUP INT TERM
(crontab -l 2>/dev/null || true) | grep -vF '# wallpaper-sync-managed' > "$temporary" || true
printf '%s %s %s >> %s 2>&1 # wallpaper-sync-managed\n' \
  "$cron_time" "$(quote_arg sh)" "$(quote_arg "$sync_dir/run_sync.sh")" "$(quote_arg "$sync_dir/sync.log")" >> "$temporary"
crontab "$temporary"
printf 'Installed in %s and scheduled automatic sync. Run %s/uninstall.sh to remove it.\n' "$root" "$sync_dir"
