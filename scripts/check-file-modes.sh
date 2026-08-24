#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# check-file-modes.sh — enforce one invariant across the active source tree:
#
#     a tracked file is executable if and only if it starts with a shebang.
#
# WHY THIS EXISTS
#
# Ruff's EXE002 ("file is executable but has no shebang") caught 94 library
# modules under v4/ that carried a stray `+x` bit — cleared in #70. But ruff is
# deliberately pinned `<0.16` in v4/pyproject.toml, and EXE002 only became a
# default rule in 0.16. So the CI lint gate cannot see this class of defect at
# all, and the 94 bits would simply accumulate again: they originally arrived in
# bulk from mode-preserving copies (rsync/scp/FAT media), which is exactly the
# kind of drift no reviewer notices in a diff.
#
# This guard is therefore intentionally INDEPENDENT of ruff and of the pin. It
# needs no dependencies, no virtualenv, and no network — just git and coreutils
# — so it stays correct whichever way the ruff upgrade eventually lands.
#
# It also covers the inverse defect (EXE001: a shebang'd script that is NOT
# executable, i.e. a script you cannot actually run), which ruff only reports
# for Python files. Here it applies to every language in the tree.
#
# SCOPE
#
# The frozen generations v1/, v2/ and v3/ are excluded. They are closed to
# changes under ADR-001/002 and are not built by CI, so rewriting ~500 of their
# file modes would be churn against immutable history for no gate benefit. This
# matches the scope statement at the top of .github/workflows/ci.yml.
#
# USAGE
#
#   ./scripts/check-file-modes.sh          # report violations, exit 1 if any
#   ./scripts/check-file-modes.sh --fix    # correct them in place, then report
#   ./scripts/check-file-modes.sh --git-dir DIR
#                                          # read DIR's index instead of the default one
#
#   make check-modes                       # same, from the repo root
#
# WHAT "OK" MEANS, EXACTLY
#
# The invariant is checked over ONE index — the modes recorded in the git
# directory being read — and the summary line now says which index and how many
# files it examined. It used to say "every tracked file outside v1-v3", which is
# a claim about the *tree*: a file tracked in a different index, or absent from
# this checkout, was skipped in silence and still counted as OK. A run that
# examines nothing now FAILS rather than reporting a clean tree it never looked
# at, and files skipped because they are missing from the worktree are counted
# and reported.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

FIX=0
GIT_DIR_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --fix) FIX=1 ;;
    --git-dir) shift; [ $# -gt 0 ] || { echo "--git-dir needs a directory" >&2; exit 2; }
               GIT_DIR_ARG="$1" ;;
    --git-dir=*) GIT_DIR_ARG="${1#--git-dir=}" ;;
    -h|--help) sed -n '2,52p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)     echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

cd "$(git rev-parse --show-toplevel)"

# Which index is being read. Every git invocation below goes through this array,
# so the report, the --fix write-back and the summary line can never disagree
# about which index they are talking about.
GIT=(git)
INDEX_LABEL="the default git index"
if [ -n "$GIT_DIR_ARG" ]; then
  [ -d "$GIT_DIR_ARG" ] || { echo "no such git directory: $GIT_DIR_ARG" >&2; exit 2; }
  GIT=(git --git-dir "$GIT_DIR_ARG")
  INDEX_LABEL="the index of $GIT_DIR_ARG"
fi

# Frozen generations — see SCOPE above.
FROZEN_RE='^v[123]/'

# Escape hatch: paths allowed to be executable WITHOUT a shebang. Intended for
# genuine binaries (a compiled helper, a self-extracting archive). Add a comment
# saying why whenever you add an entry — an unexplained entry here is a silent
# hole in the invariant. Empty is the healthy state.
ALLOW_EXEC_WITHOUT_SHEBANG=(
  # e.g. "tools/bin/some-prebuilt-binary"   # vendored, upstream ships it +x
)

is_allowed() {
  local candidate="$1" allowed
  for allowed in ${ALLOW_EXEC_WITHOUT_SHEBANG[@]+"${ALLOW_EXEC_WITHOUT_SHEBANG[@]}"}; do
    [ "$candidate" = "$allowed" ] && return 0
  done
  return 1
}

has_shebang() {
  # Read exactly two bytes: cheap, and safe on binary files. The `tr -d '\0'`
  # matters — a leading NUL (any binary asset) would otherwise make bash warn
  # "ignored null byte in input" on every such file.
  [ "$(head -c 2 -- "$1" 2>/dev/null | tr -d '\0' || true)" = '#!' ]
}

exec_no_shebang=()
shebang_not_exec=()
examined=0        # files whose bytes were actually read — the denominator of "OK"
absent=0          # tracked here, but not present in this checkout, so unknowable

# `git ls-files -s` reports the mode recorded in the INDEX, which is what CI and
# every clone actually sees. Reading the filesystem instead would give a
# different answer on checkouts where core.fileMode is false.
while read -r mode _ _ path; do
  case "$path" in ''|*$'\n'*) continue ;; esac
  [[ "$path" =~ $FROZEN_RE ]] && continue
  # Symlinks, gitlinks and entries deleted from this checkout cannot be read, so
  # the invariant is unknowable for them. Counted, not silently dropped: a sparse
  # or partial checkout used to make this gate pass having examined nothing.
  if [ ! -f "$path" ]; then absent=$((absent + 1)); continue; fi
  examined=$((examined + 1))

  case "$mode" in
    100755)
      if ! has_shebang "$path" && ! is_allowed "$path"; then
        exec_no_shebang+=("$path")
      fi
      ;;
    100644)
      if has_shebang "$path"; then
        shebang_not_exec+=("$path")
      fi
      ;;
  esac
done < <("${GIT[@]}" ls-files -s)

n_exec=${#exec_no_shebang[@]}
n_shebang=${#shebang_not_exec[@]}

# A gate that examined nothing is not a gate that passed. This is reachable on a
# sparse/partial checkout and on an empty or wrong --git-dir, and it used to print
# the same confident OK line as a full, clean run.
if [ "$examined" -eq 0 ]; then
  echo "ERROR: examined 0 files from $INDEX_LABEL — nothing was checked."
  echo "This is not a pass. A sparse or partial checkout, or an index that tracks"
  echo "no files outside v1-v3, produces this. Check out the full tree, or point"
  echo "--git-dir at the index you meant."
  exit 1
fi

# Printed on EVERY path from here down, not only the clean one. This note used to live
# inside the OK branch alone, so a run that found violations printed "checked N file(s)"
# with no hint that others had never been examined, and `--fix` printed "fixed: …" and
# exited 0 while the same paths stayed unknowable — a completion claim over an incomplete
# examination. A gap in the denominator does not become less true when the numerator is
# bad news, and hoisting it here means a branch added later cannot forget to say it.
if [ "$absent" -gt 0 ]; then
  echo "note: $absent tracked path(s) were skipped — not regular files in this checkout"
fi

if [ "$n_exec" -eq 0 ] && [ "$n_shebang" -eq 0 ]; then
  # Say what was actually checked. The old line claimed "every tracked file
  # outside v1-v3", which is a claim about the tree rather than about this index.
  echo "file modes OK — $examined file(s) from $INDEX_LABEL are executable if and only if they have a shebang"
  exit 0
fi

if [ "$FIX" -eq 1 ]; then
  # Both halves are required. `chmod` alone fixes only the working tree, and the
  # check above reads the INDEX — so a chmod-only fix leaves the guard still
  # failing and the mode unchanged for everyone who clones. `git update-index
  # --chmod` records the bit in the index without staging any content, which is
  # what actually lands in the commit.
  if [ "$n_exec" -gt 0 ]; then
    chmod -x -- "${exec_no_shebang[@]}"
    "${GIT[@]}" update-index --chmod=-x -- "${exec_no_shebang[@]}"
  fi
  if [ "$n_shebang" -gt 0 ]; then
    chmod +x -- "${shebang_not_exec[@]}"
    "${GIT[@]}" update-index --chmod=+x -- "${shebang_not_exec[@]}"
  fi
  echo "fixed: removed +x from $n_exec file(s), added +x to $n_shebang file(s)"
  echo "The mode changes are STAGED (index + working tree). Review with:"
  echo "    git diff --cached --summary"
  exit 0
fi

echo "checked $examined file(s) from $INDEX_LABEL."
if [ "$n_exec" -gt 0 ]; then
  echo "ERROR: $n_exec tracked file(s) are executable but have no shebang (ruff EXE002)."
  echo "These are source/data files, not programs. The fix is to remove the bit, NOT to add a shebang:"
  echo
  printf '  %s\n' "${exec_no_shebang[@]}"
  echo
fi

if [ "$n_shebang" -gt 0 ]; then
  echo "ERROR: $n_shebang tracked file(s) have a shebang but are not executable (ruff EXE001)."
  echo "These are scripts that cannot be run directly:"
  echo
  printf '  %s\n' "${shebang_not_exec[@]}"
  echo
fi

# The remedy must name the index that was actually checked. A bare `--fix` would
# rewrite the DEFAULT index, which is not where these violations were found.
FIX_HINT="./scripts/check-file-modes.sh --fix"
[ -n "$GIT_DIR_ARG" ] && FIX_HINT="$FIX_HINT --git-dir $GIT_DIR_ARG"
echo "Fix all of the above with:"
echo
echo "    $FIX_HINT"
echo
echo "If a file genuinely must be executable without a shebang (a compiled binary),"
echo "add it to ALLOW_EXEC_WITHOUT_SHEBANG in this script with a comment saying why."
exit 1
