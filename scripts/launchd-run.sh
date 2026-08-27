#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
venv="$repo_dir/.venv/bin/vidigami"

if [ ! -x "$venv" ]; then
  echo "missing executable: $venv (create the virtualenv and install the project first)" >&2
  exit 1
fi

cd "$repo_dir"
mkdir -p "$repo_dir/logs"

# Keep this sequence fail-fast: a scheduled run must be visibly unsuccessful
# when sync or either integrity/reporting step fails.
"$venv" sync
"$venv" report --format both
"$venv" verify
