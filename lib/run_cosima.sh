#!/bin/bash
# Runs ONE cosima job on a worker node. HTCondor calls it as:
#     run_cosima.sh <source file> <seed>
# MEGALIB_SETUP and HOME come from the submit file (config/site.ini).
#
# On exit it leaves a marker next to the source file:
#     <job>.done     cosima finished and wrote its output
#     <job>.failed   something went wrong (the reason is inside)
set -uo pipefail

SOURCE="$1"
SEED="$2"
MARK="${SOURCE%.source}"
rm -f "${MARK}.done" "${MARK}.failed"

fail() {
    echo "ERROR: $*" >&2
    echo "$*" > "${MARK}.failed"
    exit 1
}

[[ -f "${SOURCE}" ]] || fail "source file not found: ${SOURCE}"
[[ -f "${MEGALIB_SETUP:-}" ]] || fail "MEGAlib setup script not found: ${MEGALIB_SETUP:-<unset>}"
export HOME

set +u
# shellcheck disable=SC1090
source "${MEGALIB_SETUP}"
set -u
command -v cosima > /dev/null || fail "cosima not found after sourcing ${MEGALIB_SETUP}"

OUTPUT=$(awk '$1 ~ /\.FileName$/ { print $2; exit }' "${SOURCE}")

echo "host    : $(hostname)"
echo "start   : $(date)"
echo "cosima  : $(command -v cosima)"
echo "source  : ${SOURCE}"
echo "seed    : ${SEED}"
echo "output  : ${OUTPUT}.inc*.id1.sim.gz"
echo

cosima -s "${SEED}" "${SOURCE}"
RC=$?

[[ ${RC} -eq 0 ]] || fail "cosima exited with code ${RC}"
ls "${OUTPUT}".inc*.sim* > /dev/null 2>&1 || fail "cosima finished but wrote no output file"

date > "${MARK}.done"
echo
echo "done    : $(date)"
