#!/bin/sh
set -eu

usage() {
  printf 'Usage: %s [WALLPAPER_ROOT]\n' "$0" >&2
}
if [ "$#" -gt 1 ]; then usage; exit 2; fi

if [ "$#" -eq 1 ]; then
  root=$1
  sync_dir=$root/.wallpaper-sync
else
  script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
  if [ "$(basename -- "$script_dir")" = '.wallpaper-sync' ]; then
    sync_dir=$script_dir
  else
    sync_dir=$(pwd)/wallpapers/.wallpaper-sync
  fi
fi
if [ "$(basename -- "$sync_dir")" != '.wallpaper-sync' ] || [ ! -d "$sync_dir" ]; then
  printf 'Sync installation not found: %s\n' "$sync_dir" >&2
  exit 1
fi
root=$(dirname -- "$sync_dir")

if command -v crontab >/dev/null 2>&1; then
  temporary=$(mktemp)
  trap 'rm -f "$temporary"' EXIT HUP INT TERM
  (crontab -l 2>/dev/null || true) | grep -vF '# wallpaper-sync-managed' > "$temporary" || true
  crontab "$temporary"
fi
rm -rf -- "$sync_dir"
printf 'Removed sync scripts and scheduled job. Approved wallpapers remain in %s/approved.\n' "$root"
