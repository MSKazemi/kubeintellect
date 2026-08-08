#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# check-file-modes.sh — enforce one invariant across the active source tree:
#
#     a tracked file is executable if and only if it starts with a shebang.
#
# WHY THIS EXISTS
#
# Ruff's EXE002 ("file is executable but has no shebang") caught 94 library
# modules under v4/ that carried a stray `+x` bit — cleared in #70, closing part
# of #64. But ruff is deliberately pinned `<0.16` in v4/pyproject.toml, and
# EXE002 only became a default rule in 0.16. So the CI lint gate cannot see this
# class of defect at all, and the 94 bits would simply accumulate again: they
# originally arrived in bulk from mode-preserving copies (rsync/scp/FAT media),
# which is exactly the kind of drift no reviewer notices in a diff.
#
# This guard is therefore intentionally INDEPENDENT of ruff and of the pin. It
# needs no dependencies, no virtualenv, and no network — just git and coreutils
# — so it stays correct whichever way the ruff upgrade in #64 lands.
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
#
#   make check-modes                       # same, from the repo root
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

FIX=0
case "${1:-}" in
  --fix) FIX=1 ;;
  "")    ;;
  -h|--help) sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *)     echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
esac

cd "$(git rev-parse --show-toplevel)"

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

# `git ls-files -s` reports the mode recorded in the INDEX, which is what CI and
# every clone actually sees. Reading the filesystem instead would give a
# different answer on checkouts where core.fileMode is false.
while read -r mode _ _ path; do
  case "$path" in ''|*$'\n'*) continue ;; esac
  [[ "$path" =~ $FROZEN_RE ]] && continue
  [ -f "$path" ] || continue          # skip symlinks, gitlinks, deleted entries

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
done < <(git ls-files -s)

n_exec=${#exec_no_shebang[@]}
n_shebang=${#shebang_not_exec[@]}

if [ "$n_exec" -eq 0 ] && [ "$n_shebang" -eq 0 ]; then
  echo "file modes OK — every tracked file outside v1-v3 is executable if and only if it has a shebang"
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
    git update-index --chmod=-x -- "${exec_no_shebang[@]}"
  fi
  if [ "$n_shebang" -gt 0 ]; then
    chmod +x -- "${shebang_not_exec[@]}"
    git update-index --chmod=+x -- "${shebang_not_exec[@]}"
  fi
  echo "fixed: removed +x from $n_exec file(s), added +x to $n_shebang file(s)"
  echo "The mode changes are STAGED (index + working tree). Review with:"
  echo "    git diff --cached --summary"
  exit 0
fi

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

echo "Fix all of the above with:"
echo
echo "    ./scripts/check-file-modes.sh --fix"
echo
echo "If a file genuinely must be executable without a shebang (a compiled binary),"
echo "add it to ALLOW_EXEC_WITHOUT_SHEBANG in this script with a comment saying why."
exit 1
