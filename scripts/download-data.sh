#!/usr/bin/env bash
# Downloads the dataset listed in source.txt into the repo's data/ folder (ignored by git).
# Usage (on the node, no sudo needed):  bash scripts/download-data.sh

set -euo pipefail

# Work out the repo folder from where this script lives, so it works from any directory.
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$REPO_DIR/data"

URL="$(head -n 1 "$REPO_DIR/source.txt" | tr -d '[:space:]')"
FILE="$DATA_DIR/$(basename "$URL")"

mkdir -p "$DATA_DIR"

echo "==> Downloading $URL"
echo "    into $FILE"
# -c resumes a half-finished download instead of starting over.
wget -c -O "$FILE" "$URL"

echo "==> Checking the file is a complete gzip archive"
gzip -t "$FILE"

echo "==> Done:"
ls -lh "$FILE"
