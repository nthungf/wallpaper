#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  printf 'Usage: %s WALLPAPER_ROOT BASE_URL\n' "$0" >&2
  exit 2
fi
root=$1
base_url=$2
case $root in '~/'*) root=${HOME:?}/"${root#~/}" ;; esac
case $base_url in https://*) ;; *) printf '%s\n' 'Sync URL must use HTTPS.' >&2; exit 1 ;; esac

for command_name in curl sqlite3; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "$command_name" >&2
    exit 1
  fi
done

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
database=$work/approved-links.sqlite
curl -fsSL --proto '=https' --proto-redir '=https' "$base_url/approved-links.sqlite" -o "$database"
sync_script=$(curl -fsSL --proto '=https' --proto-redir '=https' "$base_url/sync_approved.sh")
printf '%s\n' "$sync_script" | sh - "$database" "$root"
