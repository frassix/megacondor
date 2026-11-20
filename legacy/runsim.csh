#!/bin/tcsh

# Run cosima for every .source file found in the sources/ subdirectory.
# Usage (from /data2/SIU/sim/bkg/):
#   tcsh runsim.csh
# To run a specific particle only:
#   tcsh runsim.csh photon

set SRCDIR = sources

# If an argument is given, run only that particle
if ( $#argv == 1 ) then
  set files = ( ${SRCDIR}/$1_compair2.source )
else
  set files = ( ${SRCDIR}/*_compair2.source )
endif

foreach srcfile ( $files )

  # Derive a base name for the log (strip directory and extension)
  set base = `basename $srcfile .source`

  echo ""
  echo "============================================"
  echo "Running: $srcfile"
  echo "============================================"

  cosima $srcfile |& tee ${base}.log
  rm -f ${base}.log.gz
  gzip  ${base}.log

end

echo ""
echo "All simulations finished."
exit