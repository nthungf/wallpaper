#!/bin/sh
set -eu

usage() {
  printf 'Usage: %s DATABASE [WALLPAPER_ROOT]\n' "$0" >&2
  printf 'Syncs WALLPAPER_ROOT/approved to the approved links in DATABASE.\n' >&2
}

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  usage
  exit 2
fi

bundle=$1
root=${2:-$(pwd)/wallpapers}
approved_dir=$root/approved

if ! command -v sqlite3 >/dev/null 2>&1; then
  printf '%s\n' 'sqlite3 command-line tool is required.' >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  printf '%s\n' 'curl is required.' >&2
  exit 1
fi
if [ ! -f "$bundle" ]; then
  printf 'Database not found: %s\n' "$bundle" >&2
  exit 1
fi

mkdir -p "$approved_dir"
work=$(mktemp -d)
temp_file=
cleanup() {
  if [ -n "$temp_file" ]; then rm -f "$temp_file"; fi
  rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if sqlite3 "$bundle" "SELECT 1 FROM sqlite_master WHERE type='table' AND name='approved_images';" | grep -q '^1$'; then
  query="SELECT id, name, image_url FROM approved_images ORDER BY name;"
elif sqlite3 "$bundle" "SELECT 1 FROM sqlite_master WHERE type='table' AND name='images';" | grep -q '^1$'; then
  query="SELECT id, name, image_url FROM images WHERE status='approved' AND image_url IS NOT NULL ORDER BY name;"
else
  printf '%s\n' 'Database has no approved images table.' >&2
  exit 1
fi

tab=$(printf '\t')
sqlite3 -noheader -separator "$tab" "$bundle" "$query" > "$work/approved.tsv"
if [ ! -s "$work/approved.tsv" ]; then
  printf '%s\n' 'No approved image URLs found.'
fi

total=$(wc -l < "$work/approved.tsv" | tr -d ' ')
done_count=0
failed=0
removed=0
keep_names=$work/approved-names.txt
: > "$keep_names"
while IFS="$tab" read -r image_id name image_url; do
  case $image_id in ''|*[!A-Za-z0-9]*)
    printf 'Skipping invalid image ID: %s\n' "$image_id" >&2
    failed=$((failed + 1))
    continue
  esac
  case $name in
    "wallhaven-$image_id.jpg"|"wallhaven-$image_id.jpeg"|"wallhaven-$image_id.png"|"wallhaven-$image_id.webp") ;;
    *) printf 'Skipping invalid filename: %s\n' "$name" >&2; failed=$((failed + 1)); continue ;;
  esac
  case $image_url in https://w.wallhaven.cc/*) ;;
    *) printf 'Skipping unexpected image URL for %s\n' "$name" >&2; failed=$((failed + 1)); continue ;;
  esac
  printf '%s\n' "$name" >> "$keep_names"

  target=$approved_dir/$name
  if [ ! -f "$target" ]; then
    temp_file=$(mktemp "$approved_dir/.wallpaper.XXXXXX")
    if ! curl -fsSL --proto '=https' --proto-redir '=https' "$image_url" -o "$temp_file"; then
      printf 'Failed to fetch %s\n' "$name" >&2
      failed=$((failed + 1))
      rm -f "$temp_file"
      temp_file=
      continue
    fi
    header=$(od -An -tx1 -N12 "$temp_file" | tr -d ' \n')
    case $name in
      *.jpg|*.jpeg) valid=$(printf '%s' "$header" | grep -c '^ffd8ff') || valid=0 ;;
      *.png) valid=$(printf '%s' "$header" | grep -c '^89504e470d0a1a0a') || valid=0 ;;
      *.webp) valid=$(printf '%s' "$header" | grep -c '^52494646........57454250') || valid=0 ;;
    esac
    if [ "$valid" -ne 1 ]; then
      printf 'Downloaded data is not a valid %s image: %s\n' "${name##*.}" "$name" >&2
      failed=$((failed + 1))
      rm -f "$temp_file"
      temp_file=
      continue
    fi
    mv "$temp_file" "$target"
    temp_file=
  fi
  done_count=$((done_count + 1))
  printf 'Synced %s of %s: %s\n' "$done_count" "$total" "$name"
done < "$work/approved.tsv"

if [ "$failed" -eq 0 ]; then
  for existing in "$approved_dir"/wallhaven-*; do
    [ -f "$existing" ] || continue
    name=${existing##*/}
    case $name in
      wallhaven-*.jpg|wallhaven-*.jpeg|wallhaven-*.png|wallhaven-*.webp) ;;
      *) continue ;;
    esac
    image_id=${name#wallhaven-}
    image_id=${image_id%.*}
    case $image_id in ''|*[!A-Za-z0-9]*) continue ;; esac
    if ! grep -Fqx "$name" "$keep_names"; then
      rm -f "$existing"
      removed=$((removed + 1))
      printf 'Removed no-longer-approved image: %s\n' "$name"
    fi
  done
else
  printf 'Skipped cleanup because %s sync item(s) failed.\n' "$failed"
fi

printf 'Finished: %s approved images available; %s removed; %s failed.\n' "$done_count" "$removed" "$failed"
[ "$failed" -eq 0 ]
