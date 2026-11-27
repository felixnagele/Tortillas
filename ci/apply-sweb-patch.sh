#!/usr/bin/env bash
# Apply the Tortillas hooks to a clean sweb checkout.
# If both ways fail (3-way & fuzz) then the patch needs an update for sweb upstream.

set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <patch> <sweb-dir>" >&2
  exit 2
fi

patch_file=$(realpath "$1")
sweb_dir=$2

cd "$sweb_dir"

if git apply --3way "$patch_file"; then
  echo "Applied $(basename "$patch_file") with git apply --3way"
  exit 0
fi

echo "git apply --3way failed; retrying with patch --fuzz=3" >&2
git reset --hard HEAD
git clean -fd
patch -p1 --forward --fuzz=3 <"$patch_file"
echo "Applied $(basename "$patch_file") with patch --fuzz=3"
