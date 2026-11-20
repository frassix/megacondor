#!/bin/bash
# Runs ONE cosima job on a worker node. HTCondor calls it as:
#     run_cosima.sh <source file> <seed>
# MEGALIB_SETUP and HOME come from the submit file (config/site.ini).
#
# Next to the source file it keeps:
#     <job>.progress "<triggers so far> <triggers wanted>", updated every 0.5%
#     <job>.done     cosima finished and wrote its output
#     <job>.failed   something went wrong (the reason is inside)
# cosima prints one "Storing event ..." line per trigger: those go to the
# progress file instead of the log, which only gets a line every 10%.
#
# All our variables are MEGACONDOR_* and are fixed *before* the MEGAlib setup
# script is sourced: that script (and ROOT's thisroot.sh, which sets SOURCE)
# defines generic variables that would otherwise overwrite ours.
set -uo pipefail

readonly MEGACONDOR_SOURCE="$1"
readonly MEGACONDOR_SEED="$2"
readonly MEGACONDOR_MARK="${MEGACONDOR_SOURCE%.source}"
readonly MEGACONDOR_SETUP="${MEGALIB_SETUP:-}"

megacondor_fail() {
    echo "ERROR: $*" >&2
    echo "$*" > "${MEGACONDOR_MARK}.failed"
    exit 1
}

rm -f "${MEGACONDOR_MARK}.done" "${MEGACONDOR_MARK}.failed" "${MEGACONDOR_MARK}.progress"
[[ -f "${MEGACONDOR_SOURCE}" ]] || megacondor_fail "source file not found: ${MEGACONDOR_SOURCE}"
[[ -f "${MEGACONDOR_SETUP}" ]] || megacondor_fail "MEGAlib setup script not found: ${MEGACONDOR_SETUP:-<unset>}"
readonly MEGACONDOR_OUTPUT=$(awk '$1 ~ /\.FileName$/ { print $2; exit }' "${MEGACONDOR_SOURCE}")
[[ -n "${MEGACONDOR_OUTPUT}" ]] || megacondor_fail "no FileName in ${MEGACONDOR_SOURCE}"
readonly MEGACONDOR_TRIGGERS=$(awk '$1 ~ /\.Triggers$/ { print $2; exit }' "${MEGACONDOR_SOURCE}")
export HOME

set +u
# shellcheck disable=SC1090
source "${MEGACONDOR_SETUP}"
set -u
command -v cosima > /dev/null || megacondor_fail "cosima not found after sourcing ${MEGACONDOR_SETUP}"

echo "host    : $(hostname)"
echo "start   : $(date)"
echo "cosima  : $(command -v cosima)"
echo "source  : ${MEGACONDOR_SOURCE}"
echo "seed    : ${MEGACONDOR_SEED}"
echo "output  : ${MEGACONDOR_OUTPUT}.inc*.id1.sim.gz"
echo

echo "0 ${MEGACONDOR_TRIGGERS}" > "${MEGACONDOR_MARK}.progress"
cosima -s "${MEGACONDOR_SEED}" "${MEGACONDOR_SOURCE}" 2>&1 | awk \
    -v total="${MEGACONDOR_TRIGGERS}" -v file="${MEGACONDOR_MARK}.progress" '
    BEGIN { step = int(total / 200); if (step < 1) step = 1; tenth = 1 }
    /^Storing event [0-9]+ of / {
        n = $3
        if (n - last >= step || n >= total) { print n, total > file; close(file); last = n }
        while (n * 10 >= tenth * total && tenth <= 10) {
            printf "progress: %d / %d triggers (%d%%)\n", n, total, 10 * tenth; tenth++
        }
        fflush(); next
    }
    { print; fflush() }'
MEGACONDOR_RC=${PIPESTATUS[0]}

[[ ${MEGACONDOR_RC} -eq 0 ]] || megacondor_fail "cosima exited with code ${MEGACONDOR_RC}"
ls "${MEGACONDOR_OUTPUT}".inc*.sim* > /dev/null 2>&1 || megacondor_fail "cosima finished but wrote no output file"

date > "${MEGACONDOR_MARK}.done"
echo
echo "done    : $(date)"
