#!/bin/bash
set -euo pipefail

# Args:
# 1 = job id
# 2 = emin
# 3 = emax
# 4 = ntriggers
# 5 = output dir
# 6 = geometry file
# 7 = particle

if [[ "$#" -ne 7 ]]; then
  echo "ERROR: Expected 7 arguments, got $#"
  echo "Usage: $0 PROCESS_ID EMIN EMAX NTRIGGERS OUTDIR GEOMETRY PARTICLE"
  exit 1
fi

PROCESS_ID="$1"
JOB_ID=$((PROCESS_ID + 1))
EMIN="$2"
EMAX="$3"
NTRIGGERS="$4"
OUTDIR="$5"
GEOMETRY="$6"
PARTICLE="$7"

if [[ ! -f "${GEOMETRY}" ]]; then
  echo "ERROR: Geometry file not found: ${GEOMETRY}"
  exit 1
fi

# Base name
mkdir -p "${OUTDIR}/sources"
BASE_NAME=$(printf "ComPair23_%.0f_%.0f_%04d" "${EMIN}" "${EMAX}" "${JOB_ID}")

SOURCE_FILE="${OUTDIR}/sources/${BASE_NAME}.source"

cat > "${SOURCE_FILE}" <<EOF
Version         1
Geometry        ${GEOMETRY}
PhysicsListEM               LivermorePol
PhysicsListHD               qgsp-bic-hp
StoreSimulationInfo         all
Run SpaceSim
SpaceSim.FileName           ${OUTDIR}/${BASE_NAME}
SpaceSim.Triggers           ${NTRIGGERS}
SpaceSim.Source Linear
Linear.ParticleType         ${PARTICLE}
Linear.Beam                 FarFieldAreaSource 0 60 0 360
Linear.Spectrum             Linear ${EMIN} ${EMAX}
Linear.Flux                 1.0
EOF

echo "Job ID      : ${JOB_ID}"
echo "Emin / Emax : ${EMIN} ${EMAX}"
echo "Output file : ${OUTDIR}/${BASE_NAME}"
echo "Source file : ${SOURCE_FILE}"

export HOME=/home/users/frassa

set +u
source /data2/SIU/set_first_interaction/bin/source-megalib.sh
set -u

echo "which cosima: $(which cosima)"
echo "PWD: $(pwd)"

cosima "${SOURCE_FILE}"