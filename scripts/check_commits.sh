#!/bin/sh
# Check commit messages against cliff.toml.
#
#   check_commits.sh <message-file>   commit-msg hook: the message being written
#   check_commits.sh <git-range>      CI: every commit in the range, e.g. origin/master..HEAD
#
# git-cliff fails on a message that is not `type(scope)?: description`
# (require_conventional) and files an unknown type under "Unrecognised type",
# which this script also rejects. GIT_CLIFF names the executable.
set -u

cliff=${GIT_CLIFF:-git-cliff}

if [ $# -eq 1 ] && [ -f "$1" ]; then
    message=$(grep -v '^#' "$1")
    case $message in
    "Merge "* | 'Revert "'* | "fixup! "* | "squash! "*) exit 0 ;;
    esac
    output=$($cliff --with-commit "$message" HEAD..HEAD 2>&1)
else
    output=$($cliff "$@" 2>&1)
fi
status=$?

if [ "$status" -ne 0 ] || printf '%s\n' "$output" | grep -q '^### Unrecognised type'; then
    cat >&2 <<'EOF'
commit message rejected: use `type(scope)?: description` in lower case, with
type one of feat, fix, perf, refactor, docs, test, bench, build, ci, chore,
revert. Add `!` before the colon for a breaking change. See cliff.toml.
EOF
    exit 1
fi
