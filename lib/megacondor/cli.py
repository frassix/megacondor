"""megacondor command line."""

import argparse
import os
import random
import re
import shutil
import subprocess
import sys
import time

from . import build
from .card import PARTICLES, REPO, Card, CardError, Site, _angles
from .fluxtable import FluxTable, integrate_spectrum, spectrum_support

DESCRIPTION = """\
Parallel cosima simulations on HTCondor.

  new <flat|flux|cutoff> <name>   start a new card in sims/<name>.ini
  info                            particles, energy ranges and fluxes of the .dat files
  prepare <card.ini>              build the run directory, do not submit (check it first)
  submit  <card.ini | run>        prepare (if needed) and submit
  status  <run>                   done / failed / pending jobs
  resubmit <run>                  resubmit the jobs that did not finish (new seeds)

<run> is a run name, a run directory, or the card that created it.
"""


def main(argv=None):
    p = argparse.ArgumentParser(prog="megacondor", description=DESCRIPTION,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--site", default=None, help="site config (default: config/site.ini)")
    sub = p.add_subparsers(dest="command", metavar="command")

    s = sub.add_parser("new", help="start a new card from a template")
    s.add_argument("mode", choices=["flat", "flux", "cutoff"])
    s.add_argument("name")

    s = sub.add_parser("info", help="show particles and flux file ranges")
    s.add_argument("--theta", default="0 180", help="one value or 'min max' (deg)")
    s.add_argument("--phi", default=None, help="one value or 'min max' (deg)")

    s = sub.add_parser("prepare", help="build the run directory without submitting")
    s.add_argument("card")

    s = sub.add_parser("submit", help="prepare and submit")
    s.add_argument("target")

    s = sub.add_parser("status", help="show job status of a run")
    s.add_argument("run")
    s.add_argument("--all", action="store_true",
                   help="list every job that is not done, not only the running ones")

    s = sub.add_parser("resubmit", help="resubmit jobs that did not finish")
    s.add_argument("run")
    s.add_argument("--force", action="store_true",
                   help="without condor_q: resubmit every job that is not done")

    args = p.parse_args(argv)
    if not args.command:
        p.print_help()
        return 1
    try:
        site = Site(args.site) if args.site else Site()
        return COMMANDS[args.command](args, site) or 0
    except CardError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


# ---------------------------------------------------------------- commands

def cmd_new(args, site):
    template = os.path.join(REPO, "templates", args.mode + ".ini")
    target = os.path.join(REPO, "sims", args.name + ".ini")
    if os.path.exists(target):
        raise CardError("%s already exists" % target)
    with open(template) as f:
        text = f.read()
    text = re.sub(r"(?m)^name\s*=.*$", "name      = %s" % args.name, text, count=1)
    with open(target, "w") as f:
        f.write(text)
    print("created %s" % os.path.relpath(target))
    print("edit it, then:  ./megacondor prepare %s" % os.path.relpath(target))


def cmd_info(args, site):
    theta = _angles(args.theta, "theta", 0, 180)
    phi = _angles(args.phi or ("0" if len(theta) == 1 else "0 360"), "phi", 0, 360)
    if len(theta) != len(phi):
        raise CardError("give theta and phi both as single values or both as ranges")
    point = len(theta) == 1
    print("Flux files: %s" % site.flux_pattern.replace(REPO + os.sep, ""))
    print("Window    : theta %s, phi %s deg\n" % (" ".join("%g" % t for t in theta),
                                                   " ".join("%g" % x for x in phi)))
    print("%-12s %3s   %-28s %s" % ("particle", "id", "energy range [keV]",
                                    "flux [#/cm2/s/sr]" if point else "flux in window [#/cm2/s]"))
    for name in sorted(PARTICLES, key=lambda n: (PARTICLES[n], n)):
        path = site.flux_file(name)
        if not os.path.isfile(path):
            print("%-12s %3d   (no flux file: flat mode only)" % (name, PARTICLES[name]))
            continue
        table = FluxTable.read(path)
        try:
            if point:
                e, f = table.spectrum_at(phi[0], theta[0])
                support, total = spectrum_support(e, f), integrate_spectrum(e, f)
            else:
                cropped = table.crop(phi, theta)
                support, total = cropped.energy_support(), cropped.integrate()
        except ValueError as err:
            raise CardError("%s: %s" % (name, err))
        rng = "%.4g - %.4g" % support if support else "no flux here"
        print("%-12s %3d   %-28s %.3g" % (name, PARTICLES[name], rng, total))
    print("\nThe energy range is what 'mode = cutoff' simulates (flat spectrum).")


def cmd_prepare(args, site):
    card = Card(args.card, site)
    rundir, summary = build.prepare(card, site)
    remember_run(card.name, rundir)
    print(summary)
    print("\nCheck it (e.g. %s/sources/), then submit with:" % rundir)
    print("  ./megacondor submit %s" % rundir)


def cmd_submit(args, site):
    if os.path.isfile(args.target) and args.target.endswith(".ini"):
        card = Card(args.target, site)
        rundir = os.path.abspath(os.path.join(card.output_root, card.name))
        if os.path.isfile(os.path.join(rundir, "jobs.list")):
            # already prepared: submit it, if it was prepared from this same card
            with open(card.path) as a, open(os.path.join(rundir, "card.ini")) as b:
                if a.read() != b.read():
                    raise CardError("run '%s' already exists (%s) and was prepared from a different "
                                    "card. Change 'name' in the card for a new run." % (card.name, rundir))
        else:
            rundir, summary = build.prepare(card, site)
            remember_run(card.name, rundir)
            print(summary + "\n")
    else:
        rundir = resolve_run(args.target, site)
    marker = os.path.join(rundir, "submitted")
    if os.path.exists(marker):
        raise CardError("this run was already submitted (see %s).\n"
                        "Use './megacondor resubmit' for jobs that did not finish." % marker)
    condor_submit(rundir, "jobs.submit", marker)


def cmd_status(args, site):
    rundir = resolve_run(args.run, site)
    jobs = read_jobs(rundir)
    st = job_states(rundir, jobs)

    print("Run %s  (%s)" % (os.path.basename(rundir), rundir))
    if not os.path.exists(os.path.join(rundir, "submitted")):
        print("  not submitted yet:  ./megacondor submit %s" % rundir)

    # triggers, overall and per particle (or mix): finished jobs + jobs running now
    per_tag = {}
    for job, source, _seed in jobs:
        total = _triggers_in(source)
        state = st["state"][job]
        n = total if state == "done" else (_progress(source, total) if state == "running" else 0)
        tag = job.rsplit("_", 1)[0]
        a, b = per_tag.get(tag, (0, 0))
        per_tag[tag] = (a + n, b + total)
    n_all, t_all = sum(v[0] for v in per_tag.values()), sum(v[1] for v in per_tag.values())
    print("  triggers  %s" % _bar(n_all, t_all))
    if len(per_tag) > 1:
        for tag in sorted(per_tag):
            print("     %-10s %s" % (tag, _bar(*per_tag[tag])))

    counts = st["counts"]
    order = ["done", "running", "idle", "held", "failed", "stopped", "waiting"]
    print("  jobs      %s  (of %d)" % (", ".join("%d %s" % (counts[k], k) for k in order if counts.get(k)),
                                       len(jobs)))
    for job, reason in st["failed"][:10]:
        print("     failed %s: %s" % (job, reason))
    if len(st["failed"]) > 10:
        print("     ... and %d more failed" % (len(st["failed"]) - 10))
    if st["failed"]:
        print("     (cosima's last lines are in sources/<job>.failed)")
    if counts.get("held"):
        print("  -> held jobs: see why with  condor_q -hold ; remove them with condor_rm, then resubmit")
    if st["queue_known"] and (counts.get("failed") or counts.get("stopped")):
        print("  -> %d failed/stopped jobs can be rerun now:  ./megacondor resubmit %s"
              % (counts.get("failed", 0) + counts.get("stopped", 0), os.path.basename(rundir)))
    if not st["queue_known"]:
        print("  (condor_q not available: 'running' means the job started, it may have been removed since)")
    print("  condor log: %s" % condor_log_of(rundir))

    shown = [j for j in jobs if (st["state"][j[0]] != "done" if args.all else st["state"][j[0]] == "running")]
    if shown:
        print("\n  %s jobs:" % ("not finished" if args.all else "running"))
        print_job_table(shown, st, show_state=args.all)


def print_job_table(jobs, st, show_state=False):
    now = time.time()
    rows = [["job"] + (["state"] if show_state else []) +
            ["condor id", "host", "particle", "energy", "spectrum", "progress", "runtime", "left"]]
    for job, source, _seed in jobs:
        q = st["queue"].get(source, {})
        state = st["state"][job]
        particle, energy, spectrum = describe_job(source)
        total = _triggers_in(source)
        n = _progress(source, total) if state == "running" else 0
        runtime = now - q["start"] if q.get("start") and state == "running" else None
        left = runtime * (total - n) / n if runtime and n else None
        frac = float(n) / total if total else 0
        rows.append([job] + ([state] if show_state else []) +
                    [q.get("id", "-"), q.get("host", "-"), particle, energy, spectrum,
                     "[%-10s] %3d%%" % ("#" * int(10 * frac), 100 * frac) if state == "running" else "-",
                     _duration(runtime), _duration(left)])
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("     " + "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


def describe_job(source):
    """(particle(s), energy range, spectrum) of a job, read from its .source file."""
    names, spectra, beams = [], {}, {}
    with open(source) as f:
        for line in f:
            tok = line.split()
            if len(tok) < 2 or "." not in tok[0]:
                continue
            owner, key = tok[0].split(".", 1)
            if key == "Source":
                names.append(tok[1])
            elif key == "Spectrum":
                spectra[owner] = tok[1:]
            elif key == "Beam":
                beams[owner] = tok[1:]
    if not names:
        return "?", "?", "?"
    spec, beam = spectra.get(names[0], []), beams.get(names[0], [])
    kind = spec[0].lower() if spec else "?"
    try:
        if kind == "linear":
            erange, label = (float(spec[1]), float(spec[2])), "flat"
        elif kind == "powerlaw":
            erange, label = (float(spec[1]), float(spec[2])), "logflat" if spec[3] == "1" else "powerlaw"
        elif kind == "file":
            erange, label = _range_of(spec[1], "DP"), "flux"
        elif kind == "normalizedenergybeamfluxfunction":
            erange, label = _range_of(beam[1], "EA"), "flux"
        else:
            erange, label = None, kind
    except (IndexError, ValueError, OSError):
        erange, label = None, kind
    return "+".join(n.lower() for n in names), _energy_range(erange), label


def _range_of(path, key):
    """First and last energy of a spectrum (DP lines) or flux table (EA line)."""
    values = []
    with open(path) as f:
        for line in f:
            tok = line.split()
            if tok and tok[0] == key:
                if key == "EA":
                    return float(tok[1]), float(tok[-1])
                values.append(float(tok[1]))
    return values[0], values[-1]


def _energy(x):
    for unit, scale in (("GeV", 1e6), ("MeV", 1e3), ("keV", 1.0)):
        if x >= scale or unit == "keV":
            return "%.3g" % (x / scale), unit


def _energy_range(erange):
    if not erange:
        return "?"
    (a, ua), (b, ub) = _energy(erange[0]), _energy(erange[1])
    return "%s-%s %s" % (a, b, ub) if ua == ub else "%s %s - %s %s" % (a, ua, b, ub)


def _duration(seconds):
    if seconds is None:
        return "-"
    m = int(seconds) // 60
    return "%dh%02dm" % (m // 60, m % 60) if m >= 60 else "%dm" % m


def _bar(n, total, width=30):
    frac = float(n) / total if total else 0.0
    fill = int(round(frac * width))
    return "[%s%s] %5.1f%%  %d / %d" % ("#" * fill, "." * (width - fill), 100 * frac, n, total)


def job_states(rundir, jobs):
    """State of every job: done / failed (from the wrapper's markers), running /
    idle / held (from condor_q), stopped (neither finished nor in the queue:
    removed or killed). Without condor_q: running (started) / waiting."""
    queue = condor_jobs(os.path.basename(rundir))
    state, failed, counts = {}, [], {}
    for job, source, _seed in jobs:
        mark = source[:-len(".source")]
        if os.path.exists(mark + ".done"):
            s = "done"
        elif os.path.exists(mark + ".failed"):
            s = "failed"
            with open(mark + ".failed") as f:
                failed.append((job, (f.readline().strip() or "unknown error")))
        elif queue is not None:
            s = queue[source]["state"] if source in queue else "stopped"
        else:
            s = "running" if os.path.exists(mark + ".progress") else "waiting"
        state[job] = s
        counts[s] = counts.get(s, 0) + 1
    return {"state": state, "failed": failed, "counts": counts, "queue_known": queue is not None,
            "queue": queue or {}}


def cmd_resubmit(args, site):
    rundir = resolve_run(args.run, site)
    jobs = read_jobs(rundir)
    st = job_states(rundir, jobs)
    if st["queue_known"]:
        rerun = ("failed", "stopped")       # jobs still in the queue are left alone
    else:
        if not args.force:
            raise CardError("condor_q is not available, so I cannot tell which jobs are still "
                            "running. Use --force to resubmit every job that is not done.")
        rerun = ("failed", "running", "waiting")
    todo = set(job for job, s in st["state"].items() if s in rerun)
    if not todo:
        print("nothing to resubmit (%s)" % ", ".join("%d %s" % (n, k) for k, n in st["counts"].items()))
        return

    # Keep partial outputs out of the way (cosima would otherwise write .inc2 files next to them).
    simdir = os.path.join(rundir, "sim")
    stash = os.path.join(simdir, "incomplete")
    for f in sorted(os.listdir(simdir)):
        if f.split(".")[0] in todo:
            if not os.path.isdir(stash):
                os.mkdir(stash)
            shutil.move(os.path.join(simdir, f), os.path.join(stash, f))
    for job in todo:
        for ext in (".failed", ".progress", ".tail"):
            marker = os.path.join(rundir, "sources", job + ext)
            if os.path.exists(marker):
                os.remove(marker)

    n = 1
    while os.path.exists(os.path.join(rundir, "jobs.resubmit%d.list" % n)):
        n += 1
    list_name, submit_name = "jobs.resubmit%d.list" % n, "jobs.resubmit%d.submit" % n
    base = random.SystemRandom().randint(1, 2 ** 30)
    with open(os.path.join(rundir, list_name), "w") as f:
        for i, (job, source, _) in enumerate(j for j in jobs if j[0] in todo):
            f.write("%s %s %d\n" % (job, source, base + i))
    with open(os.path.join(rundir, "jobs.submit")) as f:
        text = f.read().replace("/jobs.list\n", "/%s\n" % list_name)
    with open(os.path.join(rundir, submit_name), "w") as f:
        f.write(text)
    print("resubmitting %d jobs (%d failed, %d stopped)"
          % (len(todo), st["counts"].get("failed", 0), len(todo) - st["counts"].get("failed", 0)))
    if os.path.isdir(stash):
        print("partial outputs moved to %s" % stash)
    condor_submit(rundir, submit_name, os.path.join(rundir, "submitted"))


COMMANDS = {"new": cmd_new, "info": cmd_info, "prepare": cmd_prepare, "submit": cmd_submit,
            "status": cmd_status, "resubmit": cmd_resubmit}


# ----------------------------------------------------------------- helpers

def resolve_run(target, site):
    if os.path.isfile(target) and target.endswith(".ini"):
        card = Card(target, site)
        path = os.path.join(card.output_root, card.name)
    elif os.path.isdir(target):
        path = target
    else:
        path = os.path.join(site.output_root, target)
        if not os.path.isfile(os.path.join(path, "jobs.list")):
            path = known_runs().get(target, path)
    path = os.path.abspath(path)
    if not os.path.isfile(os.path.join(path, "jobs.list")):
        names = sorted(known_runs())
        raise CardError("no prepared run called '%s'.\n"
                        "A run is named by the 'name' line inside the card, not by the card's file name.\n"
                        "You can also give the card itself (sims/<file>.ini) or the run directory.%s"
                        % (target, ("\nRuns prepared here: " + ", ".join(names)) if names else ""))
    return path


RUNS_FILE = os.path.join(REPO, "runs.txt")


def remember_run(name, rundir):
    """Note where a run lives, so 'status <name>' works whatever its output_root."""
    with open(RUNS_FILE, "a") as f:
        f.write("%s %s\n" % (name, rundir))


def known_runs():
    runs = {}
    if os.path.isfile(RUNS_FILE):
        with open(RUNS_FILE) as f:
            for line in f:
                tok = line.split(None, 1)
                if len(tok) == 2:
                    runs[tok[0]] = tok[1].strip()   # the latest entry wins
    return runs


def read_jobs(rundir):
    with open(os.path.join(rundir, "jobs.list")) as f:
        return [tuple(line.split()) for line in f if line.strip()]


def _progress(source, total):
    try:
        with open(source[:-len(".source")] + ".progress") as f:
            return min(int(f.read().split()[0]), total)
    except (ValueError, IndexError, OSError):
        return 0   # not there, or being rewritten right now


def _triggers_in(source):
    with open(source) as f:
        for line in f:
            tok = line.split()
            if tok and tok[0].endswith(".Triggers"):
                return int(tok[1])
    return 0


def condor_log_of(rundir):
    """The run's HTCondor log, as written in its submit file."""
    with open(os.path.join(rundir, "jobs.submit")) as f:
        for line in f:
            tok = line.split("=", 1)
            if tok[0].strip() == "log":
                return tok[1].strip()
    return None


CONDOR_STATES = {"1": "idle", "2": "running", "3": "stopped", "4": "running", "5": "held",
                 "6": "running", "7": "held"}   # removing / completing / transferring / suspended


def condor_jobs(batch_name):
    """{source file: {state, id, start, host}} for this run's jobs in the
    condor queue, or None if condor_q is not available."""
    if not shutil.which("condor_q"):
        return None
    try:
        out = subprocess.check_output(
            ["condor_q", "-constraint", 'JobBatchName == "%s"' % batch_name,
             "-af", "JobStatus", "ClusterId", "ProcId", "JobCurrentStartDate", "RemoteHost",
             "Args", "Arguments"],
            universal_newlines=True, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, OSError):
        return None
    jobs = {}
    for line in out.splitlines():
        tok = line.split()
        sources = [t for t in tok[1:] if t.endswith(".source")]
        if len(tok) < 5 or not sources:
            continue
        start = int(tok[3]) if tok[3].isdigit() else None
        host = tok[4].split("@")[-1].split(".")[0] if tok[4] != "undefined" else "-"
        jobs[sources[0]] = {"state": CONDOR_STATES.get(tok[0], "in queue"),
                            "id": "%s.%s" % (tok[1], tok[2]), "start": start, "host": host}
    return jobs


def condor_submit(rundir, submit_name, marker):
    if not shutil.which("condor_submit"):
        raise CardError("condor_submit not found. Run this on the HTCondor submit node.")
    proc = subprocess.Popen(["condor_submit", submit_name], cwd=rundir,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    out = proc.communicate()[0]
    print(out.rstrip())
    if proc.returncode != 0:
        raise CardError("condor_submit failed")
    with open(marker, "a") as f:
        f.write("%s: %s\n" % (submit_name, out.strip().splitlines()[-1] if out.strip() else "submitted"))
    print("\nFollow it with:  ./megacondor status %s" % rundir)
