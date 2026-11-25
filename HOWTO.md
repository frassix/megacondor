# How to run simulations

All commands are run from the repository folder on the HTCondor submit node.
Every command takes the card file (`sims/my_run.ini`). Only `condor_rm` needs the run's
name, which is the `name = ...` line inside the card (not the file name).

## Once

1. Get the code:
   ```bash
   git pull
   ```
2. Check `config/site.ini`: MEGAlib setup script, `home`, geometry, `output_root`.

## Every simulation

1. Create a card (`flat`, `flux` or `cutoff`):
   ```bash
   ./megacondor new cutoff my_run
   ```
2. Edit `sims/my_run.ini` (examples below).
3. Build the jobs and read the summary it prints:
   ```bash
   ./megacondor prepare sims/my_run.ini
   ```
4. Submit:
   ```bash
   ./megacondor submit sims/my_run.ini
   ```
5. Follow it (Ctrl-C to stop watching). It lists the running jobs with particle, energy,
   spectrum, progress and time left; add `--all` to list every unfinished job:
   ```bash
   watch -n 60 ./megacondor status sims/my_run.ini
   ```
6. Results: `<output_root>/<name>/sim/*.sim.gz`. For binned runs, `bins.txt` says which file is which energy bin.

To change anything after `prepare`: edit the card, give it a new `name`, and start again from step 3.

## When something goes wrong

| `status` says | Do                                                                                   |
|---------------|--------------------------------------------------------------------------------------|
| `failed`      | read `<output_root>/<name>/sources/<job>.failed`, fix the cause, then `./megacondor resubmit sims/my_run.ini` |
| `stopped`     | `./megacondor resubmit sims/my_run.ini`                                              |
| `held`        | `condor_q -hold` to see why, then remove them and resubmit (below)                    |

Remove jobs from the queue (replace `hadrons` with the `name` in your card):
```bash
condor_rm -constraint 'JobBatchName == "hadrons"'
```
```bash
condor_rm -constraint 'JobBatchName == "hadrons" && JobStatus == 5'
```
The first removes the whole run, the second only its held jobs. Then rerun what is missing:
```bash
./megacondor resubmit sims/my_run.ini
```

## Example cards

Flat photons, on-axis, 1-10 MeV, Compton first interaction:
```ini
[simulation]
name      = photons_compton
mode      = flat
particles = photon
triggers  = 100000
jobs      = 20
emin      = 1000
emax      = 10000
theta     = 0
first_interaction = compton
```

Flat protons, 100-1000 keV, one 18 keV bin per job:
```ini
[simulation]
name      = protons_binned
mode      = flat
particles = proton
triggers  = 100000
jobs      = 50
emin      = 100
emax      = 1000
binning   = linear
```

Realistic background mix, 1-100 MeV, whole sky:
```ini
[simulation]
name      = bkg_mix
mode      = flux
particles = proton, he4, electron, positron
triggers  = 1000000
jobs      = 100
emin      = 1000
emax      = 100000
theta     = 0 180
phi       = 0 360
```

Protons and alphas over the range of the flux files, equal counts per decade, outputs and logs elsewhere:
```ini
[simulation]
name        = hadrons
mode        = cutoff
particles   = proton, he4
triggers    = 1750000
jobs        = 100
spectrum    = logflat
theta       = 0 180
phi         = 0 360
output_root = /data2/SIU/sim/bkg/output
log_dir     = logs
```

## Card keys

| Key | Values |
|-----|--------|
| `name` | run name (new name for every run) |
| `mode` | `flat` · `flux` · `cutoff` |
| `particles` | `photon pho511 electron positron proton neutron he4 muplus muminus` |
| `triggers` | per particle (per whole mix with `combine = yes`) |
| `jobs` | per particle |
| `emin`, `emax` | keV (required in `flat`; optional in `flux`/`cutoff` to narrow the range) |
| `spectrum` | `flat` · `logflat` |
| `binning` | `none` · `linear` · `log` (one energy bin per job) |
| `bin_width` | keV, linear bins of that width (then no `jobs`) |
| `theta`, `phi` | `0` on-axis · one value point source · `min max` range (deg) |
| `first_interaction` | `any` · `photo` · `compton` · `pair` (photons only) |
| `combine` | `yes` · `no` (all particles in the same jobs) |
| `mc_info` | `init-only` · `all` |
| `output_root`, `log_dir`, `geometry`, `seed` | optional |
