#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

[[ -f maubot.yaml ]] || { echo "Missing maubot.yaml" >&2; exit 1; }

id=$(grep -E '^\s*id:\s*' maubot.yaml | head -1 | sed -E 's/^\s*id:\s*//')
version=$(grep -E '^\s*version:\s*' maubot.yaml | head -1 | sed -E 's/^\s*version:\s*//')
[[ -n "$id" && -n "$version" ]] || { echo "Could not read id/version" >&2; exit 1; }

mkdir -p build
out="build/${id}-v${version}.mbp"
if command -v mbc >/dev/null 2>&1; then
    echo "Using mbc build -> $out"
    if mbc build -o "$out"; then
        echo "Built: $out"
        exit 0
    fi
    echo "mbc build failed; building manually." >&2
fi

rm -f "$out" "${out}.zip"
zip -j "${out}.zip" maubot.yaml base-config.yaml plex.py bot.py
mv "${out}.zip" "$out"
echo "Built: $out"
