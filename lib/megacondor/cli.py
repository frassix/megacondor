"""megacondor command line."""

import argparse
import os
import random
import re
import shutil
import subprocess
import sys

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

    s = sub.add_parser("resubmit", help="resubmit jobs that did not finish")
    s.add_argument("run")
    s.add_argument("--force", action="store_true",
                   help="resubmit even if jobs of this run are still in the queue")

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
    print(summary)
    print("\nCheck it (e.g. %s/sources/), then submit with:" % rundir)
    print("  ./megacondor submit %s" % rundir)


def cmd_submit(args, site):
    if os.path.isfile(args.target) and args.target.endswith(".ini"):
        card = Card(args.target, site)
        rundir, summary = build.prepare(card, site)
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
    done, failed, pending = classify(jobs)
    queue = condor_queue(os.path.basename(rundir))

    print("Run %s  (%s)" % (os.path.basename(rundir), rundir))
    if not os.path.exists(os.path.join(rundir, "submitted")):
        print("  not submitted yet:  ./megacondor submit %s" % rundir)
    print("  done     %5d / %d" % (len(done), len(jobs)))
    print("  failed   %5d" % len(failed))
    for job, reason in failed[:10]:
        print("             %s: %s   (logs/%s.err)" % (job, reason, job))
    if len(failed) > 10:
        print("             ... and %d more" % (len(failed) - 10))
    print("  pending  %5d   (queued, running, or killed by the cluster)" % len(pending))
    if queue is not None:
        print("  in condor queue: %s" % (", ".join("%d %s" % (n, s) for s, n in sorted(queue.items()))
                                         or "nothing"))
        if not queue and pending:
            print("  -> nothing is queued but %d jobs never finished: ./megacondor resubmit %s"
                  % (len(pending), rundir))


def cmd_resubmit(args, site):
    rundir = resolve_run(args.run, site)
    name = os.path.basename(rundir)
    jobs = read_jobs(rundir)
    done, failed, pending = classify(jobs)
    todo = set(job for job, _ in failed) | set(pending)
    if not todo:
        print("all %d jobs are done, nothing to resubmit" % len(jobs))
        return
    queue = condor_queue(name)
    if queue and not args.force:
        raise CardError("%d jobs of '%s' are still in the condor queue. Wait for them, or remove "
                        "them (condor_rm), or use --force." % (sum(queue.values()), name))

    # Keep partial outputs out of the way (cosima would otherwise write .inc2 files next to them).
    simdir = os.path.join(rundir, "sim")
    stash = os.path.join(simdir, "incomplete")
    for f in sorted(os.listdir(simdir)):
        if f.split(".")[0] in todo:
            if not os.path.isdir(stash):
                os.mkdir(stash)
            shutil.move(os.path.join(simdir, f), os.path.join(stash, f))
    for job in todo:
        marker = os.path.join(rundir, "sources", job + ".failed")
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
    print("resubmitting %d jobs (%d failed, %d never finished)" % (len(todo), len(failed), len(pending)))
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
    path = os.path.abspath(path)
    if not os.path.isfile(os.path.join(path, "jobs.list")):
        raise CardError("no prepared run found at %s" % path)
    return path


def read_jobs(rundir):
    with open(os.path.join(rundir, "jobs.list")) as f:
        return [tuple(line.split()) for line in f if line.strip()]


def classify(jobs):
    done, failed, pending = [], [], []
    for job, source, _seed in jobs:
        mark = source[:-len(".source")]
        if os.path.exists(mark + ".done"):
            done.append(job)
        elif os.path.exists(mark + ".failed"):
            with open(mark + ".failed") as f:
                failed.append((job, f.read().strip() or "unknown error"))
        else:
            pending.append(job)
    return done, failed, pending


CONDOR_STATES = {"1": "idle", "2": "running", "3": "removed", "4": "completed", "5": "held",
                 "6": "transferring", "7": "suspended"}


def condor_queue(batch_name):
    """{state: count} of this run's jobs in the queue, or None if condor_q is unavailable."""
    if not shutil.which("condor_q"):
        return None
    try:
        out = subprocess.check_output(
            ["condor_q", "-constraint", 'JobBatchName == "%s"' % batch_name, "-af", "JobStatus"],
            universal_newlines=True, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, OSError):
        return None
    counts = {}
    for code in out.split():
        state = CONDOR_STATES.get(code, "state " + code)
        counts[state] = counts.get(state, 0) + 1
    return counts


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
