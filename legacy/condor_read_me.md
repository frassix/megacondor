# HTCondor submission for MEGAlib (Cosima)

This setup allows you to submit multiple Cosima simulations to an HTCondor cluster.

------------------------------------------------------------------------
Each job:
* uses the same physical configuration (same energy range, same source)
* produces a **separate output file** (to avoid overwriting)
* runs independently on a worker node
------------------------------------------------------------------------

The most important files are:
* `cosima.submit` → HTCondor submit file
* `run_cosima_condor.sh` → job wrapper script (executed on worker nodes)
* `logs/` → directory containing `.out`, `.err`, `.log` files

------------------------------------------------------------------------

The Cosima input file is generated automatically inside `run_cosima_condor.sh`.

Key parameters you can change are:

Linear.Spectrum Linear EMIN EMAX
SpaceSim.Triggers NTRIGGERS
SpaceSim.FileName OUTDIR/NAME

which can be seen inside the file as:

EMIN      = 1000.0
EMAX      = 6000.0
NTRIGGERS = 10000
OUTDIR    = /data2/SIU/sim/Compair
INTERACTION = PAIR

Also in the same file you can change the number of jobs you want:

queue 16

For the outputfile name and folder you need to check BASE_NAME:

BASE_NAME=$(printf "ComPair23_%.0f_%.0f_%04d" "${EMIN}" "${EMAX}" "${JOB_ID}")

Of course uou can customize this string as needed.

NOTE: Each job initializes MEGAlib and sets his home. You can edit the home:

export HOME=/home/users/<username>

This is required because HTCondor jobs run in a minimal environment.

------------------------------------------------------------------------

## How to submit jobs
From the directory containing `cosima.submit` (/data2/SIU/sim/), run:

condor_submit cosima.submit

You should see:
N job(s) submitted to cluster XXXX.

In order to check the status of your jobs use:

condor_q

and you will get something like:

-- Schedd: node-001.fermi : <172.16.210.1:9618?... @ 04/16/26 07:19:34
OWNER  BATCH_NAME                   SUBMITTED   DONE   RUN    IDLE  TOTAL    JOB_IDS
you    CMD: run_cosima_condor.sh    4/16 07:18  _      16     _     16 2371. 0-15

16 jobs; 0 completed, 0 removed, 0 idle, 16 running, 0 held, 0 

where each column shows the status:
* `I` (Idle) → waiting in queue
* `R` (Running) → executing on a worker
* `H` (Held) → error / blocked
* disappearing from `condor_q` → job finished

You can also see all the different commands using:
condor_q -summary

------------------------------------------------------------------------
Each job produces three files storedi in the logs folder:
* `.out` → standard output (what Cosima prints)
* `.err` → errors
* `.log` → HTCondor job lifecycle


To monitor the jobs live:
tail -f logs/cosima_0.log

Once jobs disappear from `condor_q`, use:

condor_history

or for a specific cluster:

condor_history <ClusterID>

------------------------------------------------------------------------
Each job produces a file like:

ComPair23_<emin>_<emax>_<jobid>.gz

Example:  ComPair23_1000_6000_0001.gz



------------------------------------------------------------------------
------------------------------------------------------------------------

## COMMON ISSUES

### Jobs stuck in Idle
Use this to get more info:

condor_q -better-analyze <jobID>

Typical causes:

* too much requested disk/memory
* no matching worker nodes

### `cosima: command not found`
MEGAlib not sourced correctly.
Check in `.out`:

which cosima

### ROOT crash: `HOME directory not set`
Fix by setting:

export HOME=/home/users/<username>

### Empty `.out` file
Normal if:
* job is still running
* Cosima output is buffered

------------------------------------------------------------------------
------------------------------------------------------------------------
## End
