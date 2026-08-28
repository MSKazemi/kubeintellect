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
ident=$(git -C "$root" var GIT_AUTHOR_IDENT)          # fails loudly if unconfigured
ident_name=${ident%% <*}
ident_email=${ident#*<}; ident_email=${ident_email%%>*}
git -C "$target" \
  -c "user.name=$ident_name" -c "user.email=$ident_email" -c commit.gpgsign=false \
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
echo "running \`make setup\` in the export — this is the public checkout's verdict"
cd "$target"
make setup
