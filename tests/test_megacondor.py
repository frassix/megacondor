"""Tests: python3 -m unittest discover -s tests"""

import io
import os
import random
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "lib"))

from megacondor import build, cli  # noqa: E402
from megacondor.card import Card, CardError, Site  # noqa: E402
from megacondor.fluxtable import FluxTable, integrate_spectrum  # noqa: E402

FLUX = os.path.join(REPO, "fluxes", "proton_compair2_cosima.dat")


def read(path):
    with open(path) as f:
        return f.read()


class TestFluxTable(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.table = FluxTable.read(FLUX)

    def test_crop_is_the_same_function(self):
        window = dict(phi_range=(30, 300), theta_range=(10, 75), energy_range=(1500, 3e5))
        cropped = self.table.crop(**window)
        rng = random.Random(1)
        for _ in range(500):
            p, t, e = rng.uniform(30, 300), rng.uniform(10, 75), rng.uniform(1500, 3e5)
            a, b = self.table.evaluate(p, t, e), cropped.evaluate(p, t, e)
            self.assertAlmostEqual(a, b, delta=1e-9 * a)

    def test_full_crop_keeps_integral(self):
        full = self.table.crop((0, 360), (0, 180), (self.table.energy[0], self.table.energy[-1]))
        self.assertAlmostEqual(full.integrate(), self.table.integrate(), delta=1e-12)

    def test_energy_split_is_additive(self):
        lo = self.table.crop(energy_range=(100, 5000)).integrate()
        hi = self.table.crop(energy_range=(5000, 1e6)).integrate()
        both = self.table.crop(energy_range=(100, 1e6)).integrate()
        self.assertAlmostEqual(lo + hi, both, delta=1e-9 * both)

    def test_write_read_roundtrip(self):
        cropped = self.table.crop(theta_range=(0, 60), energy_range=(1000, 1e5))
        with tempfile.NamedTemporaryFile(suffix=".dat", delete=False) as f:
            path = f.name
        try:
            cropped.write(path, "test")
            again = FluxTable.read(path)
            self.assertEqual(again.energy, cropped.energy)
            self.assertAlmostEqual(again.integrate(), cropped.integrate(), delta=1e-6 * cropped.integrate())
        finally:
            os.remove(path)

    def test_spectrum_slice(self):
        e, f = self.table.spectrum_at(0, 0, (1000, 1e5))
        self.assertEqual((e[0], e[-1]), (1000, 1e5))
        self.assertEqual(f[3], self.table.evaluate(0, 0, e[3]))
        self.assertGreater(integrate_spectrum(e, f), 0)

    def test_bad_windows(self):
        with self.assertRaises(ValueError):
            self.table.crop(energy_range=(1, 100))       # below the table
        with self.assertRaises(ValueError):
            self.table.crop(theta_range=(50, 40))

    def test_muons_have_no_upward_flux(self):
        mu = FluxTable.read(os.path.join(REPO, "fluxes", "muminus_compair2_cosima.dat"))
        self.assertIsNone(mu.crop(theta_range=(100, 180)).energy_support())
        self.assertIsNotNone(mu.crop(theta_range=(0, 80)).energy_support())


# ------------------------------------------------------------- cosima lint

GLOBAL = {"Version", "Geometry", "PhysicsListEM", "PhysicsListHD", "StoreCalibrated",
          "StoreSimulationInfo", "StoreSimulationInfoIonization", "DiscretizeHits",
          "PreTriggerMode", "SelectInteractionProcess", "Run"}


def lint_source(test, path):
    """Check a generated .source against the token rules of MEGAlib's MCParameterFile."""
    run, sources, seen = None, set(), {}
    for line in read(path).splitlines():
        tok = line.split()
        if not tok or tok[0].startswith("#"):
            continue
        if "." not in tok[0]:
            test.assertIn(tok[0], GLOBAL, path)
            if tok[0] == "Run":
                run = tok[1]
            continue
        tok = tok[0].split(".", 1) + tok[1:]   # cosima splits "Owner.Key" into two tokens
        owner, key = tok[0], tok[1]
        if owner == run:
            test.assertIn(key, ("FileName", "Triggers", "Source"))
            test.assertEqual(len(tok), 3, line)
            if key == "Source":
                sources.add(tok[2])
            continue
        test.assertIn(owner, sources, line)
        seen.setdefault(owner, {})[key] = tok
    test.assertIsNotNone(run)
    test.assertTrue(sources)
    for name in sources:
        s = seen[name]
        test.assertEqual(len(s["ParticleType"]), 3)
        beam, spec = s["Beam"], s["Spectrum"]
        kind = beam[2]
        if kind == "FarFieldPointSource":
            test.assertEqual(len(beam), 5)
        elif kind == "FarFieldAreaSource":
            test.assertEqual(len(beam), 7)
            test.assertLess(float(beam[3]), float(beam[4]))
            test.assertLess(float(beam[5]), float(beam[6]))
        else:
            test.assertEqual(kind, "FarFieldNormalizedEnergyBeamFluxFunction")
            FluxTable.read(beam[3])
            test.assertEqual(spec[2:], ["NormalizedEnergyBeamFluxFunction"])
            test.assertNotIn("Flux", s)
            continue
        test.assertIn("Flux", s)
        test.assertGreater(float(s["Flux"][2]), 0)
        if spec[2] == "Linear":
            test.assertEqual(len(spec), 5)
            test.assertLess(float(spec[3]), float(spec[4]))
        elif spec[2] == "PowerLaw":
            test.assertEqual(len(spec), 6)
        else:
            test.assertEqual(spec[2], "File")
            dp = [l.split() for l in read(spec[3]).splitlines() if l.startswith("DP")]
            test.assertGreaterEqual(len(dp), 2)
            test.assertTrue(all(len(d) == 3 and float(d[2]) >= 0 for d in dp))


class Sandbox(unittest.TestCase):
    """Each test gets its own output_root, site config, sims dir and fake condor."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, "out")
        self.site_path = os.path.join(self.tmp, "site.ini")
        with open(os.path.join(REPO, "config", "site.ini")) as f:
            text = f.read().replace("output_root = /data2/SIU/sim", "output_root = %s" % self.out)
        with open(self.site_path, "w") as f:
            f.write(text)
        self.site = Site(self.site_path)
        self.old_runs_file, cli.RUNS_FILE = cli.RUNS_FILE, os.path.join(self.tmp, "runs.txt")
        bindir = os.path.join(self.tmp, "bin")
        os.mkdir(bindir)
        fake = os.path.join(bindir, "condor_submit")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\necho \"$(wc -l < $(awk '/^queue/ {print $NF}' $1)) job(s) "
                    "submitted to cluster 4242.\"\n")
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
        self.queue_file = os.path.join(self.tmp, "queue.txt")
        fake_q = os.path.join(bindir, "condor_q")
        with open(fake_q, "w") as f:
            f.write("#!/bin/sh\n[ -f %s ] && cat %s\nexit 0\n" % (self.queue_file, self.queue_file))
        os.chmod(fake_q, 0o755)
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = bindir + os.pathsep + self.old_path

    def tearDown(self):
        os.environ["PATH"] = self.old_path
        cli.RUNS_FILE = self.old_runs_file
        shutil.rmtree(self.tmp)

    def card(self, **kw):
        kw.setdefault("name", "t")
        kw.setdefault("triggers", "1000")
        if "bin_width" not in kw:
            kw.setdefault("jobs", "4")
        path = os.path.join(self.tmp, "%s.ini" % kw["name"])
        with open(path, "w") as f:
            f.write("[simulation]\n" + "".join("%s = %s\n" % kv for kv in kw.items()))
        return path

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--site", self.site_path] + list(args))
        return rc, out.getvalue() + err.getvalue()

    def prepare(self, **kw):
        rundir, _ = build.prepare(Card(self.card(**kw), self.site), self.site)
        return rundir

    def sources(self, rundir):
        d = os.path.join(rundir, "sources")
        return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".source"))


class TestCards(Sandbox):

    def bad(self, message, **kw):
        with self.assertRaises(CardError) as ctx:
            Card(self.card(**kw), self.site)
        self.assertIn(message, str(ctx.exception))

    def test_errors_are_clear(self):
        self.bad("unknown key", mode="flat", particles="photon", emin="1", emax="2", energy="3")
        self.bad("unknown particle", mode="flat", particles="pion", emin="1", emax="2")
        self.bad("needs emin and emax", mode="flat", particles="photon")
        self.bad("emax must be larger", mode="flat", particles="photon", emin="5", emax="2")
        self.bad("only works for photons", mode="flat", particles="photon, proton",
                 emin="1", emax="2", first_interaction="compton")
        self.bad("first_interaction must be", mode="flat", particles="photon",
                 emin="1", emax="2", first_interaction="rayleigh")
        self.bad("no flux file", mode="flux", particles="antiproton")
        self.bad("give a single phi", mode="flat", particles="photon", emin="1", emax="2",
                 theta="30", phi="0 360")
        self.bad("theta must be within", mode="flat", particles="photon", emin="1", emax="2",
                 theta="0 200", phi="0 360")
        self.bad("positive integer", mode="flat", particles="photon", emin="1", emax="2", jobs="2.5")

    def test_aliases_and_scientific_notation(self):
        c = Card(self.card(mode="flat", particles="gamma e- mu+", emin="1", emax="2",
                           triggers="1e6"), self.site)
        self.assertEqual(c.particles, ["photon", "electron", "muplus"])
        self.assertEqual(c.triggers, 1000000)

    def test_templates_are_valid(self):
        for mode in ("flat", "flux", "cutoff"):
            Card(os.path.join(REPO, "templates", mode + ".ini"), self.site)


class TestPrepare(Sandbox):

    def check_run(self, rundir, njobs, triggers):
        jobs = cli.read_jobs(rundir)
        self.assertEqual(len(jobs), njobs)
        self.assertEqual(len(set(j[2] for j in jobs)), njobs, "seeds must be unique")
        total = 0
        for path in self.sources(rundir):
            lint_source(self, path)
            total += int([l for l in read(path).splitlines() if ".Triggers" in l][0].split()[1])
        self.assertEqual(total, triggers)
        submit = read(os.path.join(rundir, "jobs.submit"))
        for line in ("request_cpus            = 1", "request_memory          = 2 GB",
                     "request_disk            = 200 MB"):
            self.assertIn(line, submit)
        for path in self.sources(rundir):
            text = read(path)
            self.assertIn("StoreSimulationInfo            init-only", text)
            self.assertIn("StoreSimulationInfoIonization  false", text)
            self.assertIn("SpaceSim.Triggers", text)
            self.assertNotIn(".Events", text)
            self.assertNotIn(".Time", text)

    def test_flat_onaxis_with_first_interaction(self):
        rundir = self.prepare(mode="flat", particles="photon", emin="100", emax="5000",
                              triggers="1003", jobs="4", first_interaction="compton")
        self.check_run(rundir, 4, 1003)
        text = read(self.sources(rundir)[0])
        self.assertIn("FarFieldPointSource 0 0", text)
        self.assertIn("SelectInteractionProcess COMP", text)

    def test_flat_two_particles_separate_and_logflat(self):
        rundir = self.prepare(mode="flat", particles="photon, proton", emin="100", emax="1e6",
                              theta="0 60", phi="0 360", spectrum="logflat")
        self.check_run(rundir, 8, 2000)
        self.assertIn("PowerLaw 100 1000000 1", read(self.sources(rundir)[0]))

    def test_flux_area_combined(self):
        rundir = self.prepare(mode="flux", particles="proton he4 electron", emin="1000",
                              emax="1e5", theta="0 90", phi="0 360")
        self.check_run(rundir, 4, 1000)
        self.assertEqual(sorted(os.listdir(os.path.join(rundir, "inputs"))),
                         ["electron_flux.dat", "he4_flux.dat", "proton_flux.dat"])
        t = FluxTable.read(os.path.join(rundir, "inputs", "proton_flux.dat"))
        self.assertEqual((t.energy[0], t.energy[-1], t.theta[-1]), (1000, 1e5, 90))

    def test_flux_onaxis_uses_spectrum_file(self):
        rundir = self.prepare(mode="flux", particles="photon", emin="100", emax="1e4", theta="0")
        self.check_run(rundir, 4, 1000)
        self.assertIn("Photon.Spectrum        File ", read(self.sources(rundir)[0]))

    def test_flux_full_range_and_clipping(self):
        card = Card(self.card(mode="flux", particles="photon", emin="1", emax="1e9"), self.site)
        _, summary = build.prepare(card, self.site)
        self.assertIn("clipped", summary)

    def test_flux_no_flux_in_window(self):
        with self.assertRaises(CardError):
            self.prepare(mode="flux", particles="muminus", theta="120 180", phi="0 360")

    def test_cutoff_uses_flux_file_range(self):
        rundir = self.prepare(mode="cutoff", particles="photon neutron pho511", theta="0 90",
                              phi="0 360")
        self.check_run(rundir, 12, 3000)
        text = "".join(read(p) for p in self.sources(rundir))
        self.assertIn("Linear 22.536 89716", text)
        self.assertIn("Linear 89.717 897160000", text)
        self.assertIn("Linear 510.95 511.05", text)

    def test_existing_run_is_never_overwritten(self):
        self.prepare(mode="flat", particles="photon", emin="1", emax="2")
        with self.assertRaises(CardError):
            self.prepare(mode="flat", particles="photon", emin="1", emax="2")

    def test_failed_prepare_leaves_nothing(self):
        with self.assertRaises(CardError):
            self.prepare(mode="flux", particles="proton muminus", theta="120 180", phi="0 360")
        self.assertEqual(os.listdir(self.out), [])


class TestBinning(Sandbox):

    def spectra(self, rundir):
        return [[l.split()[1:] for l in read(p).splitlines() if ".Spectrum" in l]
                for p in self.sources(rundir)]

    def test_linear_bins_one_per_job(self):
        rundir = self.prepare(mode="flat", particles="proton", emin="100", emax="1000",
                              jobs="50", triggers="100000", binning="linear")
        spectra = [s[0] for s in self.spectra(rundir)]
        self.assertEqual(len(spectra), 50)
        self.assertEqual(spectra[0], ["Linear", "100", "118"])
        self.assertEqual(spectra[1], ["Linear", "118", "136"])
        self.assertEqual(spectra[-1], ["Linear", "982", "1000"])
        for a, b in zip(spectra, spectra[1:]):
            self.assertEqual(a[2], b[1], "bins must be contiguous")
        rows = [l.split() for l in read(os.path.join(rundir, "bins.txt")).splitlines()[1:]]
        self.assertEqual(rows[0], ["proton_0001", "proton", "100", "118"])
        for path in self.sources(rundir):
            lint_source(self, path)
            self.assertIn("SpaceSim.Triggers         2000", read(path))

    def test_log_bins_with_cutoff_and_two_particles(self):
        rundir = self.prepare(mode="cutoff", particles="photon neutron", jobs="10",
                              theta="0 60", phi="0 360", binning="log", spectrum="logflat")
        spectra = [s[0] for s in self.spectra(rundir)]
        self.assertEqual(len(spectra), 20)
        photon = spectra[10:]   # files sort as neutron_*, photon_*
        self.assertEqual(photon[0][:2], ["PowerLaw", "22.536"])
        self.assertEqual(photon[-1][2], "89716")
        ratios = [float(s[2]) / float(s[1]) for s in photon]
        for r in ratios:
            self.assertAlmostEqual(r, ratios[0], places=6)

    def test_combined_bins_hold_all_particles(self):
        rundir = self.prepare(mode="flat", particles="photon proton", emin="100", emax="200",
                              jobs="4", combine="yes", binning="linear")
        spectra = self.spectra(rundir)
        self.assertEqual(len(spectra), 4)
        self.assertEqual(spectra[3], [["Linear", "175", "200"], ["Linear", "175", "200"]])

    def test_bin_width_one_job_per_bin(self):
        rundir = self.prepare(mode="cutoff", particles="proton, he4", triggers="1750000",
                              bin_width="1000", emax="100000", theta="0 180", phi="0 360")
        spectra = self.spectra(rundir)
        self.assertEqual(len(spectra), 200)
        self.assertEqual(spectra[0][0], ["Linear", "11.294", "1000"])   # he4 sorts first
        self.assertEqual(spectra[1][0], ["Linear", "1000", "2000"])
        self.assertEqual(spectra[99][0], ["Linear", "99000", "100000"])
        self.assertIn("SpaceSim.Triggers         17500", read(self.sources(rundir)[0]))

    def test_bin_width_rules(self):
        with self.assertRaises(CardError):
            Card(self.card(mode="flat", particles="p", emin="1", emax="9", bin_width="1",
                           jobs="3"), self.site)
        with self.assertRaises(CardError):
            Card(self.card(mode="flat", particles="p", emin="1", emax="9", bin_width="1",
                           binning="log"), self.site)
        self.assertEqual(build.width_bins((0.5, 3.2), 1.0),
                         [(0.5, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 3.2)])

    def test_job_limit(self):
        with self.assertRaises(CardError) as ctx:
            self.prepare(mode="cutoff", particles="proton", triggers="1000000", bin_width="1000",
                         theta="0 180", phi="0 360")
        self.assertIn("897160", str(ctx.exception))
        self.assertEqual(os.listdir(self.out), [])

    def test_cutoff_emin_emax_narrow_the_range(self):
        rundir = self.prepare(mode="cutoff", particles="photon", emin="1", emax="500",
                              theta="0 90", phi="0 360")
        self.assertEqual(self.spectra(rundir)[0][0], ["Linear", "22.536", "500"])

    def test_log_dir(self):
        logs = os.path.join(self.tmp, "repologs")
        rundir = self.prepare(name="lg", mode="flat", particles="photon", emin="1", emax="2",
                              log_dir=logs)
        self.assertEqual(cli.condor_log_of(rundir), os.path.join(logs, "lg.condor.log"))
        self.assertFalse(os.path.lexists(os.path.join(rundir, "logs")))
        self.assertFalse(os.path.lexists(os.path.join(rundir, "condor.log")))

    def test_one_log_per_run_and_mc_info_all(self):
        rundir = self.prepare(mode="flat", particles="photon", emin="1", emax="2", mc_info="all")
        submit = read(os.path.join(rundir, "jobs.submit"))
        self.assertIn("log                     = %s/condor.log" % rundir, submit)
        keys = [l.split("=")[0].strip() for l in submit.splitlines() if "=" in l]
        self.assertNotIn("output", keys)
        self.assertNotIn("error", keys)
        text = read(self.sources(rundir)[0])
        self.assertIn("StoreSimulationInfo            all", text)
        self.assertIn("StoreSimulationInfoIonization  true", text)

    def test_binning_needs_flat_spectrum(self):
        with self.assertRaises(CardError):
            Card(self.card(mode="flux", particles="proton", binning="linear"), self.site)


class TestWrapper(Sandbox):
    """Run lib/run_cosima.sh with a fake MEGAlib whose 'cosima' succeeds or crashes."""

    def run_wrapper(self, cosima_body):
        rundir = self.prepare(mode="flat", particles="photon", emin="1", emax="2", jobs="1")
        fakebin = os.path.join(self.tmp, "megalib", "bin")
        os.makedirs(fakebin)
        cosima = os.path.join(fakebin, "cosima")
        with open(cosima, "w") as f:
            f.write("#!/bin/bash\nOUT=$(awk '$1 ~ /FileName$/ {print $2}' \"$3\")\n" + cosima_body)
        os.chmod(cosima, 0o755)
        setup = os.path.join(self.tmp, "megalib", "source-megalib.sh")
        with open(setup, "w") as f:
            # like the real one: ROOT's thisroot.sh sets SOURCE, others set generic names
            f.write("export PATH=%s:$PATH\nSOURCE=/x/thisroot.sh\nSEED=1\nOUTPUT=\nMARK=/x\n"
                    "RC=0\nfail() { :; }\n" % fakebin)
        job, source, seed = cli.read_jobs(rundir)[0]
        env = dict(os.environ, MEGALIB_SETUP=setup, HOME=self.tmp)
        proc = subprocess.Popen([os.path.join(rundir, "run_cosima.sh"), source, seed], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                universal_newlines=True)
        self.log = proc.communicate()[0]
        return proc.returncode, source[:-len(".source")]

    def test_progress_file_and_quiet_log(self):
        rc, mark = self.run_wrapper(
            'for i in $(seq 1 1000); do echo "Storing event $i of $((3*i)) at t_obs=0.1s"; done\n'
            'echo "Geant4 says something useful"\ntouch "$OUT.inc1.id1.sim.gz"\n')
        self.assertEqual(rc, 0, self.log)
        self.assertTrue(os.path.exists(mark + ".done"))
        self.assertFalse(os.path.exists(mark + ".progress"), "only .done is left behind")
        self.assertFalse(os.path.exists(mark + ".tail"))
        self.assertNotIn("Storing event", self.log)
        self.assertIn("Geant4 says something useful", self.log)
        self.assertEqual(self.log.count("progress:"), 10)
        self.assertIn("progress: 1000 / 1000 triggers (100%)", self.log)

    def test_success_writes_done(self):
        rc, mark = self.run_wrapper('[ "$1" = "-s" ] || exit 3\ntouch "$OUT.inc1.id1.sim.gz"\n')
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(mark + ".done"))

    def test_crash_writes_failed(self):
        rc, mark = self.run_wrapper("echo 'G4Exception: out of memory'\nexit 255\n")
        self.assertEqual(rc, 1)
        failed = read(mark + ".failed")
        self.assertTrue(failed.startswith("cosima exited with code 255\n"))
        self.assertIn("host: ", failed)
        self.assertIn("G4Exception: out of memory", failed)
        self.assertFalse(os.path.exists(mark + ".tail"))

    def test_no_output_is_a_failure(self):
        rc, mark = self.run_wrapper("exit 0\n")
        self.assertIn("no output", read(mark + ".failed"))


class TestWorkflow(Sandbox):

    def queue(self, *entries):
        """What the fake condor_q reports: (state code, source file) pairs."""
        with open(self.queue_file, "w") as f:
            for code, source in entries:
                f.write("%s %s 12345 undefined\n" % (code, source))

    def test_new_submit_status_resubmit(self):
        rc, out = self.run_cli("submit", self.card(name="wf", mode="flat", particles="photon",
                                                   emin="1", emax="2", jobs="5"))
        self.assertEqual(rc, 0, out)
        self.assertIn("5 job(s) submitted", out)
        rundir = os.path.join(self.out, "wf")

        rc, out = self.run_cli("submit", rundir)
        self.assertEqual(rc, 1)
        self.assertIn("already submitted", out)

        # 3 done, job 4 failed (with a partial output), job 5 running at 150 of 200
        src = self.sources(rundir)
        for p in src[:3]:
            open(p[:-7] + ".done", "w").close()
        with open(src[3][:-7] + ".failed", "w") as f:
            f.write("cosima exited with code 255\nhost: node-1\n--- last lines ---\nboom\n")
        open(os.path.join(rundir, "sim", "photon_0004.inc1.id1.sim.gz"), "w").close()
        with open(src[4][:-7] + ".progress", "w") as f:
            f.write("150 200\n")
        self.queue(("2", src[4]))

        rc, out = self.run_cli("status", "wf")
        self.assertIn("75.0%  750 / 1000", out)
        self.assertIn("3 done, 1 running, 1 failed  (of 5)", out)
        self.assertIn("failed photon_0004: cosima exited with code 255\n", out)

        # resubmit leaves the running job alone
        rc, out = self.run_cli("resubmit", "wf")
        self.assertEqual(rc, 0, out)
        self.assertIn("1 job(s) submitted", out)
        self.assertTrue(os.path.exists(os.path.join(rundir, "sim", "incomplete",
                                                    "photon_0004.inc1.id1.sim.gz")))
        self.assertIn("jobs.resubmit1.list", read(os.path.join(rundir, "jobs.resubmit1.submit")))

        # job 4 now waits in the queue, job 5 was removed with condor_rm
        self.queue(("1", src[3]))
        rc, out = self.run_cli("status", "wf")
        self.assertIn("60.0%  600 / 1000", out, "a removed job's partial triggers do not count")
        self.assertIn("3 done, 1 idle, 1 stopped  (of 5)", out)

        rc, out = self.run_cli("resubmit", "wf")
        self.assertEqual(rc, 0, out)
        lines = read(os.path.join(rundir, "jobs.resubmit2.list")).split("\n")
        self.assertEqual([l.split()[0] for l in lines if l], ["photon_0005"])

    def test_resubmit_without_condor_q_needs_force(self):
        self.run_cli("submit", self.card(name="nq", mode="flat", particles="photon",
                                         emin="1", emax="2", jobs="2"))
        os.remove(os.path.join(self.tmp, "bin", "condor_q"))
        rc, out = self.run_cli("resubmit", "nq")
        self.assertEqual(rc, 1)
        self.assertIn("--force", out)
        rc, out = self.run_cli("resubmit", "nq", "--force")
        self.assertEqual(rc, 0, out)

    def test_card_file_works_after_prepare(self):
        card = self.card(name="inside_name", mode="flat", particles="photon", emin="1", emax="2")
        rc, out = self.run_cli("prepare", card)
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli("submit", "t")          # the file name is not the run name
        self.assertEqual(rc, 1)
        self.assertIn("named by the 'name' line", out)
        self.assertIn("inside_name", out)
        rc, out = self.run_cli("status", card)
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli("submit", card)
        self.assertEqual(rc, 0, out)
        self.assertIn("4 job(s) submitted", out)
        with open(card, "a") as f:
            f.write("seed = 7\n")
        rc, out = self.run_cli("submit", card)
        self.assertEqual(rc, 1)
        self.assertIn("different card", out)

    def test_status_by_name_with_own_output_root(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        rc, out = self.run_cli("prepare", self.card(name="far", mode="flat", particles="photon",
                                                    emin="1", emax="2", output_root=elsewhere))
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli("status", "far")
        self.assertEqual(rc, 0, out)
        self.assertIn(os.path.join(elsewhere, "far"), out)

    def test_new_creates_card(self):
        name = "zz_unittest_card"
        target = os.path.join(REPO, "sims", name + ".ini")
        try:
            rc, out = self.run_cli("new", "flux", name)
            self.assertEqual(rc, 0, out)
            self.assertEqual(Card(target, self.site).name, name)
        finally:
            if os.path.exists(target):
                os.remove(target)

    def test_info(self):
        rc, out = self.run_cli("info", "--theta", "0 90", "--phi", "0 360")
        self.assertEqual(rc, 0, out)
        self.assertIn("22.54 - 8.972e+04", out)


if __name__ == "__main__":
    unittest.main()
