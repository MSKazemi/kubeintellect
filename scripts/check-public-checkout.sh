#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# check-public-checkout.sh — run the gates against what a public clone carries.
#
# WHY THIS EXISTS
# ---------------
# This repository is dual-git: `.git` tracks the published subset and a second
# index tracks a superset that also carries the private research materials the
# root .gitignore names (design/, papers/, v5/, .claude/, ...). Every local gate
# runs against the WORKING TREE — the superset. So a test can read a private-tier
# file, be green on a maintainer's machine, and be red on `main`. Twice now.
#
# This script removes the guesswork: it exports HEAD into a throwaway directory
# and runs `make setup` there, so the gates see exactly what GitHub sees.
#
# THE NON-OBVIOUS PART
# --------------------
# The export must be a real git checkout. Twelve tests in the server suite call
# `git ls-files` to decide what to scan, and a bare `tar -x` of `git archive` has
# no index at all — measured, running the suite in one reports TWELVE FAILURES
# THAT DO NOT EXIST ON MAIN. So the export gets an index, and the index is compared to HEAD's
# file list before anything runs. An instrument that manufactures its own
# findings is worse than no instrument.
#
# The export is committed, not merely indexed. `actions/checkout` gives CI a real
# HEAD, so an export without one is *less* faithful than the thing it stands in
# for -- and it fails any test that resolves HEAD, which is a finding the
# instrument manufactured rather than found. The author is read from the source
# repository's own identity (`git var GIT_AUTHOR_IDENT`), so the throwaway commit
# carries exactly the identity every other commit here does, and the standard
# guard hook passes on it rather than being bypassed.
#
# USAGE
#   ./scripts/check-public-checkout.sh              # export HEAD, run `make setup`
#   ./scripts/check-public-checkout.sh --keep       # ...and keep the directory
#   ./scripts/check-public-checkout.sh --export-only <dir>
#                                                   # build the export, run nothing
#
# NOTE: it checks HEAD, not your working tree. Commit first, or you are checking
# the previous state of the code.
# ─────────────────────────────────────────────────────────────────────────────
set -eu

root=$(git rev-parse --show-toplevel)
keep=""
export_only=""
target=""

while [ $# -gt 0 ]; do
  case "$1" in
    --keep) keep=1; shift ;;
    --export-only)
      export_only=1
      [ $# -ge 2 ] || { echo "--export-only needs a directory" >&2; exit 2; }
      target="$2"; shift 2 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$target" ]; then
  target=$(mktemp -d -t ki-public-checkout-XXXXXX)
  [ -n "$keep" ] || trap 'rm -rf "$target"' EXIT
fi
mkdir -p "$target"

# ── 1. the export ────────────────────────────────────────────────────────────
git -C "$root" archive HEAD | tar -x -C "$target"

# ── 2. give it an index ──────────────────────────────────────────────────────
# `-f` because the committed .gitignore matches at least one tracked path
# (v1/charts/.../secrets.yaml); without it that file silently leaves the index
# and the fidelity check below is the only thing that would ever say so.
git -C "$target" init -q
git -C "$target" add -A -f
# Author: the source repository's own identity when git has one, so the standard
# guard hook passes on this commit rather than being bypassed. A CI runner has no
# configured identity at all — `git var` exits 128 there — and this commit exists
# only so the export can resolve HEAD, is never pushed, and dies with the temp
# directory, so a synthetic author is the right answer rather than a failure.
if ident=$(git -C "$root" var GIT_AUTHOR_IDENT 2>/dev/null); then
  ident_name=${ident%% <*}
  ident_email=${ident#*<}; ident_email=${ident_email%%>*}
else
  ident_name="public checkout"
  ident_email="checkout@localhost"
fi
# Passed as environment, not `-c user.*`: GIT_AUTHOR_NAME wins over config, so a
# caller whose environment carries an EMPTY one (a CI runner does) would otherwise
# override whatever identity is chosen above and fail with "empty ident name".
GIT_AUTHOR_NAME="$ident_name"    GIT_AUTHOR_EMAIL="$ident_email" \
GIT_COMMITTER_NAME="$ident_name" GIT_COMMITTER_EMAIL="$ident_email" \
git -C "$target" -c commit.gpgsign=false \
  commit -q -m "export of $(git -C "$root" rev-parse HEAD)"

# ── 3. fidelity, before anything is run in there ─────────────────────────────
#
# `ls-tree HEAD`, NOT `ls-files`: ls-files reads the INDEX, so with anything
# staged it lists files the export — which is of HEAD — cannot contain, and the
# check below aborts on a difference that is entirely the caller's own staging.
git -C "$root"   ls-tree -r --name-only HEAD | sort > "$target/.expected-index"
git -C "$target" ls-tree -r --name-only HEAD | sort > "$target/.actual-index"
if ! diff -q "$target/.expected-index" "$target/.actual-index" >/dev/null; then
  echo "ABORT: the export's index does not match HEAD — any result would be an artifact."
  diff "$target/.expected-index" "$target/.actual-index" | head -20
  exit 1
fi
n=$(wc -l < "$target/.expected-index")
rm -f "$target/.expected-index" "$target/.actual-index"
echo "export: $n tracked path(s) from $(git -C "$root" rev-parse --short HEAD) → $target"
if ! git -C "$root" diff --quiet HEAD -- 2>/dev/null; then
  echo "note: your working tree differs from HEAD — those changes are NOT in this export."
fi

if [ -n "$export_only" ]; then
  exit 0
fi

# ── 4. the gates, exactly as a contributor runs them ─────────────────────────
#
# With a NEUTRAL HOME, because the export alone is not enough to make this
# faithful. `app/core/config.py` reads `~/.kubeintellect/.env` — a machine-global
# file written by `kubeintellect init`, outside the repo and outside any git —
# and the project `.env` only overrides it where both name the same key. The
# export has no project `.env`, so the home file wins outright in here.
#
# Measured 2026-08-29: a self-host walk-through wrote `USE_SQLITE=true` into that
# file, and `tests/test_digest.py::TestDigestBuilder::test_empty_window` went red
# in the export while passing in the working tree — where the repo's own
# `v4/.env` happened to set it back to false. Neither result was CI's: a runner
# has no home config at all. An instrument whose whole claim is "this is what
# GitHub will see" cannot read the developer's home directory.
#
# The uv cache and interpreter store are pointed back at the real home on
# purpose — they are content-addressed build artifacts, not configuration, and
# discarding them would turn a 4-minute check into a download.
fake_home="$target/.public-checkout-home"
mkdir -p "$fake_home"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$HOME/.cache/uv}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$HOME/.local/share/uv/python}"
export HOME="$fake_home"

echo "running \`make setup\` in the export — this is the public checkout's verdict"
cd "$target"
make setup
