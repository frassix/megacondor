#!/bin/bash
set -euo pipefail

# Args:
# 1 = process id
# 2 = emin start [keV]
# 3 = bin width [keV]
# 4 = ntriggers per bin
# 5 = output dir
# 6 = geometry file
# 7 = interaction SelectInteractionProcess    ${INTERACTION}

if [[ "$#" -ne 7 ]]; then
  echo "ERROR: Expected 7 arguments, got $#"
  echo "Usage: $0 PROCESS_ID EMIN_START BIN_WIDTH NTRIGGERS_PER_BIN OUTDIR GEOMETRY INTERACTION"
  exit 1
fi

PROCESS_ID="$1"
BIN_ID=$((PROCESS_ID + 1))

EMIN_START="$2"
BIN_WIDTH="$3"
NTRIGGERS="$4"
OUTDIR="$5"
GEOMETRY="$6"
INTERACTION="$7"

case "${INTERACTION}" in
  Photo|PHOT|Photoelectric|Compton|COMP|Pair|PAIR|ALL)
    ;;
  *)
    echo "ERROR: Unknown interaction '${INTERACTION}'"
    exit 1
    ;;
esac

if [[ ! -f "${GEOMETRY}" ]]; then
  echo "ERROR: Geometry file not found: ${GEOMETRY}"
  exit 1
fi

BIN_MIN=$(awk -v emin="${EMIN_START}" -v w="${BIN_WIDTH}" -v p="${PROCESS_ID}" \
  'BEGIN { printf "%.6f", emin + p*w }')

BIN_MAX=$(awk -v emin="${EMIN_START}" -v w="${BIN_WIDTH}" -v p="${PROCESS_ID}" \
  'BEGIN { printf "%.6f", emin + (p+1)*w }')

mkdir -p "${OUTDIR}/sources"

BASE_NAME=$(printf "ComPair23_%s_%.0f_%.0f_%04d" "${INTERACTION}" "${BIN_MIN}" "${BIN_MAX}" "${BIN_ID}")
SOURCE_FILE="${OUTDIR}/sources/${BASE_NAME}.source"

cat > "${SOURCE_FILE}" <<EOF
Version         1
Geometry        ${GEOMETRY}
PhysicsListEM               LivermorePol
StoreSimulationInfo         false
Run SpaceSim
SpaceSim.FileName           ${OUTDIR}/${BASE_NAME}
SpaceSim.Triggers           ${NTRIGGERS}
SpaceSim.Source Linear
Linear.ParticleType         1
Linear.Beam                 FarFieldAreaSource 0 60 0 360
Linear.Spectrum             Linear ${BIN_MIN} ${BIN_MAX}
Linear.Flux                 1.0
EOF

echo "Bin ID      : ${BIN_ID}"
echo "Energy bin  : ${BIN_MIN} ${BIN_MAX} keV"
echo "Triggers    : ${NTRIGGERS}"
echo "Output file : ${OUTDIR}/${BASE_NAME}"
echo "Source file : ${SOURCE_FILE}"

export HOME=/home/users/frassa

set +u
source /data2/SIU/megalib_compair2_photo/bin/source-megalib.sh
set -u

echo "which cosima: $(which cosima)"
echo "PWD: $(pwd)"

cosima "${SOURCE_FILE}"
