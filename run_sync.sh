#!/bin/sh
set -eu

if [ "$#" -ne 3 ]; then
  printf 'Usage: %s WALLPAPER_ROOT SCRIPT_BASE_URL BUNDLE_URL\n' "$0" >&2
  exit 2
fi
root=$1
base_url=$2
bundle_url=$3
case $root in '~/'*) root=${HOME:?}/"${root#~/}" ;; esac
case $root in /*) ;; *) root=$PWD/$root ;; esac
case $base_url in https://*) ;; *) printf '%s\n' 'Sync URL must use HTTPS.' >&2; exit 1 ;; esac
case $bundle_url in https://*) ;; *) printf '%s\n' 'Bundle URL must use HTTPS.' >&2; exit 1 ;; esac

for command_name in curl sqlite3; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "$command_name" >&2
    exit 1
  fi
done

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
database=$work/approved-links.sqlite
curl -fsSL --proto '=https' --proto-redir '=https' \
  -H 'Accept: application/vnd.github.raw+json' "$bundle_url" -o "$database"
sync_script=$(curl -fsSL --proto '=https' --proto-redir '=https' "$base_url/sync_approved.sh")
printf '%s\n' "$sync_script" | sh -s -- "$database" "$root"
