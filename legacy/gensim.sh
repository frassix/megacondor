#!/bin/tcsh



foreach id ( 9 8 3 2 6 4 21 1 )

if ( $id == 9 ) then
  set par = muminus
else if ( $id == 8 ) then
  set par = muplus
else if ( $id == 3 ) then
  set par = electron
else if ( $id == 2 ) then
  set par = positron
else if ( $id == 6 ) then
  set par = neutron
else if ( $id == 4 ) then
  set par = proton
else if ( $id == 21 ) then
  set par = he4
else if ( $id == 1 ) then
  set par = photon
endif

echo $par $id

cat compair2XXX.source | sed s/XXX/$par/g | sed s/YYY/$id/g > sim.source

cosima sim.source |& tee ${par}compair2.log
rm -f ${par}compair2.log.gz
gzip ${par}compair2.log

end

exit



