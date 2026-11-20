# megacondor

Parallel [cosima](https://github.com/zoglauer/megalib) simulations on HTCondor, set up from one small card.

```bash
./megacondor new flat my_photons          # 1. creates sims/my_photons.ini
nano sims/my_photons.ini                  # 2. edit it
./megacondor submit sims/my_photons.ini   # 3. builds the jobs and submits them
./megacondor status my_photons            # 4. follow it
```

Use `prepare` instead of `submit` to build everything and look at it first.

## The three modes

| `mode`   | Spectrum                                   | Energy range                                  | Typical use                 |
|----------|--------------------------------------------|-----------------------------------------------|-----------------------------|
| `flat`   | flat (or `logflat`)                        | `emin`..`emax`                                | response, effective area    |
| `flux`   | realistic, from `fluxes/*.dat`             | `emin`..`emax` (empty = whole file)           | background, real rates      |
| `cutoff` | flat (or `logflat`)                        | the range covered by `fluxes/*.dat` (`emin`/`emax` can narrow it) | background, flat in energy |

`./megacondor info` shows the energy range and flux of every particle (`--theta 0 90` for a window).

## The card

One `[simulation]` section. Templates with comments are in `templates/`.

| Key                 | Meaning                                                                | Default         |
|---------------------|------------------------------------------------------------------------|-----------------|
| `name`              | run name; output goes to `<output_root>/<name>/`                       | required        |
| `mode`              | `flat`, `flux` or `cutoff`                                             | required        |
| `particles`         | one or more: `photon pho511 electron positron proton neutron he4 muplus muminus` | required |
| `triggers`          | triggers to simulate, summed over all jobs (`1e6` is fine)             | required        |
| `jobs`              | parallel jobs                                                          | 1               |
| `emin`, `emax`      | energy range in keV                                                    | flat: required  |
| `theta`             | `0` = on-axis, one value = point source, `min max` = range (deg)       | `0`             |
| `phi`               | one value with a single theta, `min max` with a theta range            | `0` / `0 360`   |
| `spectrum`          | `flat` or `logflat` (equal counts per decade), flat and cutoff modes   | `flat`          |
| `binning`           | `none`, `linear` or `log`: one energy bin per job (flat and cutoff modes) | `none`       |
| `bin_width`         | linear bins of this width in keV, one job per bin (replaces `jobs`)    |                 |
| `output_root`       | where the run directory goes                                           | site.ini        |
| `log_dir`           | put the condor logs in `<log_dir>/<name>/` (relative = inside the repo) | in the run dir |
| `first_interaction` | photons only: `any`, `photo`, `compton`, `pair`                        | `any`           |
| `combine`           | `yes`: all particles in one simulation, at their real relative rates   | flux: `yes`, else `no` |
| `geometry`          | another `.geo.setup`                                                   | ComPair (site.ini) |
| `seed`              | first seed (each job gets `seed + n`)                                  | random          |

**Several particles.** With `combine = no` every particle gets its own `jobs` jobs and `triggers` triggers.
With `combine = yes` there is one set of jobs simulating the mix; in flux mode the mix has the real
relative rates from the `.dat` files.

**Energy bins.** With `binning = linear` (or `log`) the energy range is split into `jobs` contiguous bins
and each job simulates one bin, with `triggers / jobs` triggers. Example: `emin = 100`, `emax = 1000`,
`jobs = 50` gives 100-118, 118-136, ..., 982-1000 keV. `bins.txt` in the run directory maps each job
(and so each output file) to its bin. With `binning = log` the bins have equal width in log(E).
With `bin_width = 1000` you get 1 MeV bins with edges on whole MeV and one job per bin.
A run may not have more than 10000 jobs (see `sims/hadrons.ini` for a worked example).

**Direction.** theta is the angle the particle comes *from*, measured from the instrument axis (cosima far field).
In flux mode the `.dat` table is cut to the theta/phi window, so the angular distribution stays realistic.
A single direction uses the spectrum of the `.dat` file in that direction.

## Always the same (not configurable)

* **1 CPU, 2 GB memory, 200 MB disk per job.** The cluster kills jobs that ask for more.
* **Full Monte Carlo information**: `StoreSimulationInfo all` and `StoreSimulationInfoIonization true`.
* **Triggers only**: every run stops after a number of triggers, and only triggered events are stored.
* **A different seed for every job.** cosima's default seed is the clock, so jobs that start in the same second would produce identical events.

These are set in `lib/megacondor/build.py` under `FIXED SETTINGS`.

## Site settings: `config/site.ini`

Paths that may change: the MEGAlib setup script, `HOME` for the jobs, the default geometry, the output root
(it must be on a disk the workers can see), the flux files, and the keyword for `first_interaction`.

`first_interaction` only works with the patched MEGAlib (`set_first_interaction`). It writes the line
`SelectInteractionProcess <PHOT|COMP|PAIR>` into the source file. cosima silently ignores keywords it does not
know, so check that this matches the patched build.

## What a run looks like

```
<output_root>/<name>/
├── card.ini          the card used (for the record)
├── summary.txt       what was generated
├── jobs.submit       HTCondor submit file
├── jobs.list         job name, source file, seed
├── inputs/           flux tables / spectra cut to your window (flux mode)
├── sources/          one .source per job, plus .done / .failed markers
├── sim/              <job>.inc1.id1.sim.gz
└── logs/             condor.log, <job>.out, <job>.err
```

An existing run directory is never overwritten: change `name` for a new run.

## Commands

| Command                              | What it does                                                    |
|--------------------------------------|-----------------------------------------------------------------|
| `new <flat\|flux\|cutoff> <name>`    | copy a template to `sims/<name>.ini`                            |
| `info [--theta ..] [--phi ..]`       | particles, energy ranges and fluxes of the `.dat` files         |
| `prepare <card>`                     | build the run directory, do not submit                          |
| `submit <card \| run>`               | prepare (if needed) and `condor_submit`                         |
| `status <run>`                       | done / failed / pending jobs, and what is in the queue          |
| `resubmit <run>`                     | resubmit failed and lost jobs with new seeds                    |

`<run>` is a run name, a run directory or its card.

## When things go wrong

| Symptom                                   | Look at / do                                                         |
|-------------------------------------------|----------------------------------------------------------------------|
| jobs stay idle                            | `condor_q -better-analyze <cluster>`                                 |
| `status` shows failed jobs                | the reason is printed; details in `logs/<job>.err` and `.out`        |
| pending jobs but nothing in the queue     | killed by the cluster (often memory): `./megacondor resubmit <run>`  |
| `cosima not found`                        | `[megalib] setup` in `config/site.ini`                               |
| ROOT complains about `HOME`               | `[megalib] home` in `config/site.ini`                                |

## Repository

```
megacondor          the command
config/site.ini     cluster paths
templates/          one commented card per mode
sims/               your cards
fluxes/             ComPair2 background flux tables (cosima 3D format)
lib/                python code + run_cosima.sh (runs one job on a worker)
tests/              python3 -m unittest discover -s tests
legacy/             the old scripts, for reference
```
