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
    """A group of parallel jobs that share the same source blocks."""

    def __init__(self, tag, sources, triggers, jobs):
        if jobs > triggers:
            raise CardError("%s: more jobs (%d) than triggers (%d)" % (tag, jobs, triggers))
        self.tag = tag
        self.sources = sources
        self.triggers = triggers
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
        sources = dict((p, make_source(card, site, p, tmp, rundir)) for p in card.particles)
        if card.combine:
            runs = [Run("mix" if len(card.particles) > 1 else card.particles[0],
                        [sources[p] for p in card.particles], card.triggers, card.jobs)]
        else:
            runs = [Run(p, [sources[p]], card.triggers, card.jobs) for p in card.particles]

        jobs = write_sources(card, site, runs, tmp, rundir)
        write_submit(card, site, tmp, rundir, "jobs.submit", "jobs.list")
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
        raise
    return rundir, summary


def make_source(card, site, particle, tmp, rundir):
    if card.mode == "flat":
        return _flat_source(card, particle, card.emin, card.emax)

    table = FluxTable.read(site.flux_file(particle))
    window = "theta %s, phi %s" % (_fmt_range(card.theta), _fmt_range(card.phi))

    if card.mode == "cutoff":
        try:
            if card.is_point:
                support = spectrum_support(*table.spectrum_at(card.phi[0], card.theta[0]))
            else:
                support = table.crop(card.phi, card.theta).energy_support()
        except ValueError as e:
            raise CardError("%s: %s" % (particle, e))
        if support is None:
            raise CardError("%s: the flux file has no flux for %s" % (particle, window))
        return _flat_source(card, particle, support[0], support[1])

    # mode == flux
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


def _flat_source(card, particle, emin, emax):
    if card.spectrum == "flat" and emax / emin > 100:
        card.notes.append("%s: flat spectrum over %.1f decades puts most particles near %g keV "
                          "(spectrum = logflat gives equal counts per decade)"
                          % (particle, math.log10(emax / emin), emax))
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

    jobs = []
    for run in runs:
        for i, n in enumerate(run.per_job):
            job = "%s_%04d" % (run.tag, i + 1)
            body = ["", "Run %s" % RUN,
                    "%s.FileName         %s" % (RUN, os.path.join(rundir, "sim", job)),
                    "%s.Triggers         %d" % (RUN, n)]
            for src in run.sources:
                body += [""] + src.lines()
            with open(os.path.join(tmp, "sources", job + ".source"), "w") as f:
                f.write("\n".join(header + body) + "\n")
            jobs.append((job, os.path.join(rundir, "sources", job + ".source"), base + len(jobs)))

    with open(os.path.join(tmp, "jobs.list"), "w") as f:
        for job in jobs:
            f.write("%s %s %d\n" % job)
    return jobs


def write_submit(card, site, tmp, rundir, submit_name, list_name):
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

log                     = {rundir}/logs/condor.log
output                  = {rundir}/logs/$(mc_job).out
error                   = {rundir}/logs/$(mc_job).err

queue mc_job, mc_source, mc_seed from {rundir}/{jobs_list}
""".format(name=card.name, rundir=rundir, home=site.home, setup=site.megalib_setup,
           cpus=REQUEST_CPUS, memory=REQUEST_MEMORY, disk=REQUEST_DISK, jobs_list=list_name)
    with open(os.path.join(tmp, submit_name), "w") as f:
        f.write(text)


def summarize(card, site, runs, jobs, rundir):
    out = ["Run '%s'  ->  %s" % (card.name, rundir),
           "  mode      : %s" % card.mode,
           "  direction : %s" % card.direction_text(),
           "  geometry  : %s" % card.geometry]
    if card.first_interaction:
        out.append("  first interaction: %s  (%s)" % (
            card.first_interaction, site.fi_line.format(process=site.fi_values[card.first_interaction])))
    out.append("")
    for run in runs:
        label = run.tag if len(run.sources) == 1 else "%s (%s)" % (run.tag, ", ".join(card.particles))
        out.append("  %-12s %d triggers in %d jobs (%d per job)"
                   % (label, run.triggers, len(run.per_job), run.per_job[0]))
        for src in run.sources:
            extra = ("  integrated flux %s" % src.extra) if src.extra else ""
            out.append("      %-10s %s%s" % (src.particle, src.energy_text, extra))
    out.append("")
    out.append("  %d jobs in total, seeds %d..%d" % (len(jobs), jobs[0][2], jobs[-1][2]))
    for note in card.notes + _warnings(card, site):
        out.append("  note: %s" % note)
    return "\n".join(out)


def _warnings(card, site):
    warn = []
    if not os.path.isfile(card.geometry):
        warn.append("geometry not found from this machine: %s" % card.geometry)
    if not os.path.isfile(site.megalib_setup):
        warn.append("MEGAlib setup script not found from this machine: %s" % site.megalib_setup)
    return warn
