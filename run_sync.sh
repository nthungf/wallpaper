#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(dirname -- "$script_dir")
base_url=$(cat "$script_dir/base-url")
database=$script_dir/approved-links.sqlite
temporary=$script_dir/.approved-links.sqlite.$$
trap 'rm -f "$temporary"' EXIT HUP INT TERM

case $base_url in https://*) ;; *) printf '%s\n' 'Sync URL must use HTTPS.' >&2; exit 1 ;; esac
curl -fsSL --proto '=https' --proto-redir '=https' "$base_url/approved-links.sqlite" -o "$temporary"
mv "$temporary" "$database"
trap - EXIT HUP INT TERM
sh "$script_dir/sync_approved.sh" "$database" "$root"
