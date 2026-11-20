"""Turn a simulation card into a ready-to-submit run directory.

<output_root>/<name>/
    card.ini           copy of the card used
    summary.txt        what was generated
    jobs.submit        HTCondor submit file
    jobs.list          one line per job: name, cosima source file, seed
    run_cosima.sh      worker wrapper (copy)
    inputs/            cropped flux tables / spectra used by the sources
    sources/           one cosima .source per job (+ .done / .failed markers)
    sim/               cosima output: <job>.inc1.id1.sim.gz
    logs/              condor.log + <job>.out / <job>.err
"""

import math
import os
import random
import shutil
import tempfile
import time

from .card import PARTICLES, REPO, CardError
from .fluxtable import FluxTable, integrate_spectrum, spectrum_support

# ---------------------------------------------------------------------------
# FIXED SETTINGS. These are deliberately not in any config file.
#
# The cluster kills jobs that request more than this.
REQUEST_CPUS = 1
REQUEST_MEMORY = "2 GB"
REQUEST_DISK = "200 MB"
#
# Safety net: refuse to create more jobs than this in one run.
MAX_JOBS = 10000
#
# Always store the complete Monte Carlo truth, and only triggered events.
# The stop criterion is always a number of triggers (SpaceSim.Triggers).
STORAGE = [
    ("StoreCalibrated", "true"),
    ("StoreSimulationInfo", "all"),
    ("StoreSimulationInfoIonization", "true"),
    ("DiscretizeHits", "true"),
    ("PreTriggerMode", "full"),
]
# ---------------------------------------------------------------------------

WRAPPER = os.path.join(REPO, "lib", "run_cosima.sh")
RUN = "SpaceSim"


class Source(object):
    """One cosima source block (one particle)."""

    def __init__(self, particle, beam, spectrum, flux, energy_text, extra=""):
        self.particle = particle
        self.name = particle.capitalize()
        self.beam = beam
        self.spectrum = spectrum
        self.flux = flux              # None for flux tables (cosima normalizes them itself)
        self.energy_text = energy_text
        self.extra = extra            # e.g. the integrated flux, for the summary

    def lines(self):
        out = ["%s.Source %s" % (RUN, self.name),
               "%s.ParticleType    %d" % (self.name, PARTICLES[self.particle]),
               "%s.Beam            %s" % (self.name, self.beam),
               "%s.Spectrum        %s" % (self.name, self.spectrum)]
        if self.flux is not None:
            out.append("%s.Flux            %.6g" % (self.name, self.flux))
        return out


class Run(object):
    """A group of parallel jobs. job_sources[i] = the source blocks of job i
    (the same for every job, except in energy-binned runs)."""

    def __init__(self, tag, job_sources, triggers, bins=None):
        jobs = len(job_sources)
        if jobs > triggers:
            raise CardError("%s: more jobs (%d) than triggers (%d)" % (tag, jobs, triggers))
        self.tag = tag
        self.job_sources = job_sources
        self.sources = job_sources[0]
        self.triggers = triggers
        self.bins = bins            # [(particle, [(lo, hi), ...])] for binned runs
        base, rest = divmod(triggers, jobs)
        self.per_job = [base + (1 if i < rest else 0) for i in range(jobs)]


# ------------------------------------------------------------------ build

def prepare(card, site):
    """Create the run directory. Returns (run directory, summary text)."""
    rundir = os.path.join(os.path.abspath(card.output_root), card.name)
    if os.path.exists(rundir):
        raise CardError("run directory already exists: %s\n"
                        "Change 'name' in the card, or delete that directory." % rundir)
    if not os.path.isdir(card.output_root):
        os.makedirs(card.output_root)

    # Build everything in a hidden temporary directory and rename it at the end,
    # so a failure never leaves a half-made run directory behind.
    tmp = tempfile.mkdtemp(prefix=".%s." % card.name, dir=card.output_root)
    try:
        for sub in ("inputs", "sources", "sim", "logs"):
            os.mkdir(os.path.join(tmp, sub))
        if card.combine:
            groups = [("mix" if len(card.particles) > 1 else card.particles[0], card.particles)]
        else:
            groups = [(p, [p]) for p in card.particles]

        logdir = os.path.join(rundir, "logs")
        if card.log_dir:
            logdir = os.path.join(os.path.abspath(card.log_dir), card.name)
            if os.path.exists(logdir):
                raise CardError("log directory already exists: %s" % logdir)

        if card.binning == "none":
            if card.jobs * len(groups) > MAX_JOBS:
                raise CardError("this would create %d jobs, more than the limit of %d"
                                % (card.jobs * len(groups), MAX_JOBS))
            sources = dict((p, make_source(card, site, p, tmp, rundir)) for p in card.particles)
            runs = [Run(tag, [[sources[p] for p in group]] * card.jobs, card.triggers)
                    for tag, group in groups]
        else:
            # one energy bin per job
            bins = {}
            for p in card.particles:
                erange = flat_range(card, site, p)
                if card.bin_width:
                    bins[p] = width_bins(erange, card.bin_width)
                else:
                    bins[p] = energy_bins(erange, card.jobs, card.binning)
            check_job_count(card, groups, bins)
            runs = []
            for tag, group in groups:
                if any(bins[p] != bins[group[0]] for p in group):
                    raise CardError("combine = yes with bin_width needs the same energy range for "
                                    "all particles (%s differ): use combine = no" % ", ".join(group))
                runs.append(Run(tag, [[_flat_source(card, p, *b[i]) for p, b in
                                       ((q, bins[q]) for q in group)]
                                      for i in range(len(bins[group[0]]))],
                                card.triggers, [(p, bins[p]) for p in group]))

        if card.log_dir:   # logs live only there, not in the run directory
            os.rmdir(os.path.join(tmp, "logs"))
            os.makedirs(logdir)
        jobs = write_sources(card, site, runs, tmp, rundir)
        write_submit(card, site, tmp, rundir, logdir, "jobs.submit", "jobs.list")
        shutil.copy(WRAPPER, os.path.join(tmp, "run_cosima.sh"))
        os.chmod(os.path.join(tmp, "run_cosima.sh"), 0o755)
        shutil.copy(card.path, os.path.join(tmp, "card.ini"))
        summary = summarize(card, site, runs, jobs, rundir)
        with open(os.path.join(tmp, "summary.txt"), "w") as f:
            f.write(summary + "\n")
        os.chmod(tmp, 0o755)
        os.rename(tmp, rundir)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        if card.log_dir and os.path.isdir(logdir) and not os.listdir(logdir):
            os.rmdir(logdir)
        raise
    return rundir, summary


def flat_range(card, site, particle):
    """Energy range of a flat-spectrum source: emin..emax (flat), or where
    the flux file has flux in the theta/phi window (cutoff)."""
    if card.mode == "flat":
        return card.emin, card.emax
    support = _cutoff_support(card, site, particle)
    lo = support[0] if card.emin is None else max(support[0], card.emin)
    hi = support[1] if card.emax is None else min(support[1], card.emax)
    if lo >= hi:
        raise CardError("%s: emin/emax (%s-%s keV) do not overlap the flux file range %g-%g keV"
                        % ((particle, card.emin, card.emax) + support))
    return lo, hi


def _cutoff_support(card, site, particle):
    table = FluxTable.read(site.flux_file(particle))
    try:
        if card.is_point:
            support = spectrum_support(*table.spectrum_at(card.phi[0], card.theta[0]))
        else:
            support = table.crop(card.phi, card.theta).energy_support()
    except ValueError as e:
        raise CardError("%s: %s" % (particle, e))
    if support is None:
        raise CardError("%s: the flux file has no flux for %s" % (particle, _window(card)))
    return support


def energy_bins(erange, n, binning):
    """n contiguous bins covering erange, equal in width (linear) or in log width (log)."""
    lo, hi = erange
    if binning == "log":
        edges = [lo * (hi / lo) ** (i / float(n)) for i in range(n + 1)]
    else:
        edges = [lo + (hi - lo) * i / float(n) for i in range(n + 1)]
    edges[0], edges[-1] = lo, hi
    return list(zip(edges[:-1], edges[1:]))


def width_bins(erange, width):
    """Linear bins of the given width, with edges on multiples of the width
    (the first and last bin are shorter if the range does not start/end on one)."""
    lo, hi = erange
    tol = 1e-9 * width
    edges = [lo]
    k = int(math.floor(lo / width)) + 1
    while k * width < hi - tol:
        if k * width > lo + tol:
            edges.append(k * width)
        k += 1
    edges.append(hi)
    return list(zip(edges[:-1], edges[1:]))


def check_job_count(card, groups, bins):
    total = sum(len(bins[group[0]]) for _, group in groups)
    if total > MAX_JOBS:
        per = ", ".join("%s %d" % (p, len(bins[p])) for p in card.particles)
        raise CardError("this would create %d jobs (bins: %s), more than the limit of %d.\n"
                        "Use wider bins, narrow the range with emin/emax, or use binning = log."
                        % (total, per, MAX_JOBS))
    for _, group in groups:
        n = len(bins[group[0]])
        if card.triggers < n:
            raise CardError("%d triggers for %d bins: less than one trigger per bin" % (card.triggers, n))


def make_source(card, site, particle, tmp, rundir):
    if card.mode in ("flat", "cutoff"):
        emin, emax = flat_range(card, site, particle)
        if card.spectrum == "flat" and emax / emin > 100:
            card.notes.append("%s: flat spectrum over %.1f decades puts most particles near %g keV "
                              "(spectrum = logflat gives equal counts per decade)"
                              % (particle, math.log10(emax / emin), emax))
        return _flat_source(card, particle, emin, emax)

    # mode == flux
    table = FluxTable.read(site.flux_file(particle))
    window = _window(card)
    erange = _clip_energy(card, table, particle)
    try:
        if card.is_point:
            energy, flux = table.spectrum_at(card.phi[0], card.theta[0], erange)
            total = integrate_spectrum(energy, flux)
            if total <= 0:
                raise CardError("%s: zero flux at %s in %g-%g keV" % ((particle, window) + erange))
            name = "%s_spectrum.dat" % particle
            _write_spectrum(os.path.join(tmp, "inputs", name), energy, flux,
                            "%s spectrum at %s from %s [keV, #/cm2/s/sr/keV]"
                            % (particle, window, table.path))
            return Source(particle, _beam(card), "File %s" % os.path.join(rundir, "inputs", name),
                          total, "%g - %g keV  flux file" % erange,
                          "%.3g /cm2/s/sr" % total)
        cropped = table.crop(card.phi, card.theta, erange)
    except ValueError as e:
        raise CardError("%s: %s" % (particle, e))
    total = cropped.integrate()
    if total <= 0:
        raise CardError("%s: zero flux in %s, %g-%g keV" % ((particle, window) + erange))
    name = "%s_flux.dat" % particle
    cropped.write(os.path.join(tmp, "inputs", name),
                  "%s cropped to %s, %g-%g keV" % ((table.path, window) + erange))
    return Source(particle,
                  "FarFieldNormalizedEnergyBeamFluxFunction %s" % os.path.join(rundir, "inputs", name),
                  "NormalizedEnergyBeamFluxFunction", None,
                  "%g - %g keV  flux file" % erange, "%.3g /cm2/s" % total)


def _window(card):
    return "theta %s, phi %s" % (_fmt_range(card.theta), _fmt_range(card.phi))


def _flat_source(card, particle, emin, emax):
    if card.spectrum == "logflat":
        spectrum = "PowerLaw %.10g %.10g 1" % (emin, emax)   # dN/dE ~ 1/E = flat in log(E)
    else:
        spectrum = "Linear %.10g %.10g" % (emin, emax)
    return Source(particle, _beam(card), spectrum, 1.0,
                  "%g - %g keV  %s" % (emin, emax, card.spectrum))


def _beam(card):
    if card.is_point:
        return "FarFieldPointSource %g %g" % (card.theta[0], card.phi[0])
    return "FarFieldAreaSource %g %g %g %g" % (card.theta + card.phi)


def _clip_energy(card, table, particle):
    lo, hi = table.energy[0], table.energy[-1]
    emin = lo if card.emin is None else card.emin
    emax = hi if card.emax is None else card.emax
    if emin >= hi or emax <= lo:
        raise CardError("%s: requested %g-%g keV is outside the flux file range %g-%g keV"
                        % (particle, emin, emax, lo, hi))
    if emin < lo or emax > hi:
        card.notes.append("%s: energy range clipped to the flux file: %g-%g keV"
                          % (particle, max(emin, lo), min(emax, hi)))
    return max(emin, lo), min(emax, hi)


def _write_spectrum(path, energy, flux, comment):
    with open(path, "w") as f:
        f.write("# %s\nIP LIN\n" % comment)
        for e, v in zip(energy, flux):
            f.write("DP %.10g %.6e\n" % (e, v))
        f.write("EN\n")


def _fmt_range(vals):
    return "%g..%g" % vals if len(vals) == 2 else "%g" % vals[0]


# ---------------------------------------------------------------- writers

def write_sources(card, site, runs, tmp, rundir):
    base = card.seed or random.SystemRandom().randint(1, 2 ** 30)
    header = ["# Generated by megacondor from %s on %s" % (card.path, time.strftime("%Y-%m-%d %H:%M")),
              "",
              "Version         1",
              "Geometry        %s" % card.geometry,
              "PhysicsListEM   %s" % site.physics_em,
              "PhysicsListHD   %s" % site.physics_hd,
              ""]
    header += ["%-30s %s" % kv for kv in STORAGE]
    if card.first_interaction:
        header += ["", site.fi_line.format(process=site.fi_values[card.first_interaction])]

    jobs, bin_rows = [], []
    for run in runs:
        for i, n in enumerate(run.per_job):
            job = "%s_%04d" % (run.tag, i + 1)
            body = ["", "Run %s" % RUN,
                    "%s.FileName         %s" % (RUN, os.path.join(rundir, "sim", job)),
                    "%s.Triggers         %d" % (RUN, n)]
            if run.bins:
                body[:0] = ["", "# Energy bin %d of %d" % (i + 1, len(run.per_job))]
                bin_rows += ["%s %s %.10g %.10g" % ((job, p) + b[i]) for p, b in run.bins]
            for src in run.job_sources[i]:
                body += [""] + src.lines()
            with open(os.path.join(tmp, "sources", job + ".source"), "w") as f:
                f.write("\n".join(header + body) + "\n")
            jobs.append((job, os.path.join(rundir, "sources", job + ".source"), base + len(jobs)))

    with open(os.path.join(tmp, "jobs.list"), "w") as f:
        for job in jobs:
            f.write("%s %s %d\n" % job)
    if bin_rows:
        with open(os.path.join(tmp, "bins.txt"), "w") as f:
            f.write("# job particle emin_keV emax_keV\n" + "\n".join(bin_rows) + "\n")
    return jobs


def write_submit(card, site, tmp, rundir, logdir, submit_name, list_name):
    text = """\
# HTCondor submit file generated by megacondor - do not edit by hand.
batch_name              = {name}
universe                = vanilla
executable              = {rundir}/run_cosima.sh
arguments               = $(mc_source) $(mc_seed)
environment             = "HOME='{home}' MEGALIB_SETUP='{setup}'"

should_transfer_files   = YES
when_to_transfer_output = ON_EXIT

# Fixed: jobs that ask for more are killed by the cluster.
request_cpus            = {cpus}
request_memory          = {memory}
request_disk            = {disk}

log                     = {logdir}/condor.log
output                  = {logdir}/$(mc_job).out
error                   = {logdir}/$(mc_job).err

queue mc_job, mc_source, mc_seed from {rundir}/{jobs_list}
""".format(name=card.name, rundir=rundir, logdir=logdir, home=site.home, setup=site.megalib_setup,
           cpus=REQUEST_CPUS, memory=REQUEST_MEMORY, disk=REQUEST_DISK, jobs_list=list_name)
    with open(os.path.join(tmp, submit_name), "w") as f:
        f.write(text)


def summarize(card, site, runs, jobs, rundir):
    out = ["Run '%s'  ->  %s" % (card.name, rundir),
           "  logs      : %s" % os.path.join(os.path.abspath(card.log_dir), card.name)
           if card.log_dir else None,
           "  mode      : %s" % card.mode,
           "  direction : %s" % card.direction_text(),
           "  geometry  : %s" % card.geometry]
    if card.first_interaction:
        out.append("  first interaction: %s  (%s)" % (
            card.first_interaction, site.fi_line.format(process=site.fi_values[card.first_interaction])))
    out.append("")
    for run in runs:
        label = run.tag if len(run.sources) == 1 else "%s (%s)" % (run.tag, ", ".join(card.particles))
        if run.bins:
            out.append("  %-12s %d triggers in %d energy bins, one job each (%d per bin)"
                       % (label, run.triggers, len(run.per_job), run.per_job[0]))
            for p, bins in run.bins:
                shown = ["%g-%g" % b for b in bins]
                if len(shown) > 4:
                    shown = shown[:2] + ["..."] + shown[-1:]
                out.append("      %-10s %s bins, %s in each: %s keV  (all bins: bins.txt)"
                           % (p, card.binning, card.spectrum, ", ".join(shown)))
            continue
        out.append("  %-12s %d triggers in %d jobs (%d per job)"
                   % (label, run.triggers, len(run.per_job), run.per_job[0]))
        for src in run.sources:
            extra = ("  integrated flux %s" % src.extra) if src.extra else ""
            out.append("      %-10s %s%s" % (src.particle, src.energy_text, extra))
    out.append("")
    out.append("  %d jobs in total, seeds %d..%d" % (len(jobs), jobs[0][2], jobs[-1][2]))
    for note in card.notes + _warnings(card, site):
        out.append("  note: %s" % note)
    return "\n".join(line for line in out if line is not None)


def _warnings(card, site):
    warn = []
    if not os.path.isfile(card.geometry):
        warn.append("geometry not found from this machine: %s" % card.geometry)
    if not os.path.isfile(site.megalib_setup):
        warn.append("MEGAlib setup script not found from this machine: %s" % site.megalib_setup)
    return warn
