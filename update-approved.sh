#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$script_dir"

python3 sync_approved.py export approved-links.sqlite

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git add approved-links.sqlite
  printf '%s\n' 'Updated and staged approved-links.sqlite. Commit and push to publish it.'
else
  printf '%s\n' 'Updated approved-links.sqlite.'
fi
