#!/usr/bin/env bash
# scripts/ci/data_publish.sh — push files to the orphan `data` branch.
#
# Both the AI brain quick refresh and the static-data refresh push to
# the same `data` branch.  When they overlap, naive `git push` will
# lose one of the snapshots.  This helper performs a fetch + rebase +
# retry loop in a *dedicated* clone directory so the workflow's source
# checkout (on `main`) is never disturbed.
#
# Environment:
#   GH_TOKEN          — required, used to authenticate the git push
#   GITHUB_REPOSITORY — required, set automatically inside GitHub Actions
#   DATA_REPO_DIR     — optional, defaults to $RUNNER_TEMP/data-clone
#
# Arguments:
#   $1                — commit-message prefix (e.g. "brain" / "data")
#   $@ (after $1)     — list of source files to copy under data/ on the
#                       target branch.  Their basenames are preserved.
#
# Behaviour:
#   - Clones `data` if not already present in $DATA_REPO_DIR.
#   - Resets to origin/data, overlays the supplied files, commits.
#   - Pushes with up to 5 retries, rebasing on top of origin/data
#     between attempts.  A "no delta" outcome is treated as success.
#
# This script is idempotent — calling it 5× in a row inside one job is
# the supported pattern (see ai_brain.yml and refresh_data.yml).
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: data_publish.sh <commit_prefix> <file1> [file2 ...]" >&2
  exit 2
fi

PREFIX="$1"; shift

: "${GH_TOKEN:?GH_TOKEN must be set}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY must be set}"
RUNNER_TEMP="${RUNNER_TEMP:-/tmp}"
DATA_REPO_DIR="${DATA_REPO_DIR:-$RUNNER_TEMP/data-clone}"
REPO_URL="https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"

# 1) Stage the source files outside any git workspace.
STAGE="$RUNNER_TEMP/data-stage"
rm -rf "$STAGE"
mkdir -p "$STAGE"
for f in "$@"; do
  if [ ! -f "$f" ]; then
    echo "::warning::data_publish: $f does not exist — skipping"
    continue
  fi
  cp "$f" "$STAGE/$(basename "$f")"
done

# 2) Ensure we have a clone of the `data` branch.  Create the orphan
#    branch on the very first run.
if [ ! -d "$DATA_REPO_DIR/.git" ]; then
  if git ls-remote --exit-code --heads "$REPO_URL" data >/dev/null 2>&1; then
    git clone --branch data --depth 1 "$REPO_URL" "$DATA_REPO_DIR"
  else
    echo "::notice::Creating orphan 'data' branch for the first time"
    git clone --depth 1 "$REPO_URL" "$DATA_REPO_DIR"
    pushd "$DATA_REPO_DIR" >/dev/null
    git checkout --orphan data
    git rm -rf . >/dev/null 2>&1 || true
    popd >/dev/null
  fi
fi

cd "$DATA_REPO_DIR"
git config user.name  "forex-data-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

overlay_and_commit() {
  # Sync with remote — but tolerate the case where the orphan branch
  # has never been pushed yet (first-ever run).
  if git fetch origin data --depth=1 2>/dev/null; then
    git reset --hard origin/data
  fi

  mkdir -p data
  for f in "$STAGE"/*; do
    cp "$f" "data/$(basename "$f")"
  done

  git add data/
  if git diff --cached --quiet; then
    echo "::notice::no delta — nothing to push"
    return 100  # sentinel: success-without-push
  fi
  git commit -m "${PREFIX}: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  return 0
}

# 3) Commit + push with rebase-on-conflict retries.
for attempt in 1 2 3 4 5; do
  set +e
  overlay_and_commit
  rc=$?
  set -e
  if [ $rc -eq 100 ]; then
    # Nothing to push this iteration — counts as success.
    exit 0
  fi
  if git push origin data; then
    echo "::notice::pushed on attempt $attempt"
    exit 0
  fi
  echo "::warning::push attempt $attempt rejected — refetching and retrying"
  sleep $(( attempt * 2 ))
done

echo "::error::data_publish: gave up after 5 push attempts"
exit 1
