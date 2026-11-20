"""Site configuration (config/site.ini) and simulation cards (sims/*.ini)."""

import configparser
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SITE_FILE = os.path.join(REPO, "config", "site.ini")

# name -> cosima ParticleType (MEGAlib MCSource IDs)
PARTICLES = {
    "photon": 1,
    "pho511": 1,       # 511 keV annihilation line (photon with its own flux file)
    "positron": 2,
    "electron": 3,
    "proton": 4,
    "antiproton": 5,
    "neutron": 6,
    "antineutron": 7,
    "muplus": 8,
    "muminus": 9,
    "he4": 21,
}
ALIASES = {
    "gamma": "photon", "e-": "electron", "e+": "positron", "p": "proton",
    "n": "neutron", "mu+": "muplus", "mu-": "muminus", "alpha": "he4",
}
MODES = ("flat", "flux", "cutoff")
SPECTRA = ("flat", "logflat")
BINNINGS = ("none", "linear", "log")


class CardError(Exception):
    pass


def _read_ini(path):
    if not os.path.isfile(path):
        raise CardError("file not found: %s" % path)
    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    try:
        parser.read(path)
    except configparser.Error as e:
        raise CardError("%s: %s" % (path, e))
    return parser


# ------------------------------------------------------------------ site

class Site(object):
    """Things that depend on the cluster/installation (config/site.ini)."""

    def __init__(self, path=SITE_FILE):
        ini = _read_ini(path)
        try:
            self.megalib_setup = ini.get("megalib", "setup")
            self.home = ini.get("megalib", "home")
            self.geometry = ini.get("cosima", "geometry")
            self.physics_em = ini.get("cosima", "physics_em")
            self.physics_hd = ini.get("cosima", "physics_hd")
            self.output_root = ini.get("paths", "output_root")
            flux_pattern = ini.get("paths", "flux_files")
            self.fi_line = ini.get("first_interaction", "line")
        except configparser.Error as e:
            raise CardError("%s: %s" % (path, e))
        self.flux_pattern = flux_pattern if os.path.isabs(flux_pattern) else os.path.join(REPO, flux_pattern)
        self.fi_values = dict((k, v) for k, v in ini.items("first_interaction") if k != "line")
        self.path = path

    def flux_file(self, particle):
        return self.flux_pattern.format(particle=particle)


# ------------------------------------------------------------------ card

CARD_KEYS = {
    "name", "mode", "particles", "triggers", "jobs", "emin", "emax", "theta", "phi",
    "first_interaction", "spectrum", "combine", "geometry", "seed", "output_root", "binning",
}


class Card(object):
    """A simulation card: everything that defines one run."""

    def __init__(self, path, site):
        ini = _read_ini(path)
        if ini.sections() != ["simulation"]:
            raise CardError("%s: the card must contain exactly one [simulation] section" % path)
        raw = dict(ini.items("simulation"))
        unknown = sorted(set(raw) - CARD_KEYS)
        if unknown:
            raise CardError("%s: unknown key(s): %s  (valid: %s)"
                            % (path, ", ".join(unknown), ", ".join(sorted(CARD_KEYS))))
        self.path = os.path.abspath(path)
        self.notes = []
        get = lambda key, default=None: raw.get(key, "").strip() or default

        self.name = _required(get("name"), "name")
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$", self.name):
            raise CardError("name may only contain letters, digits and _ . + - (got %r)" % self.name)

        self.mode = _choice(get("mode"), MODES, "mode")
        self.particles = _particles(_required(get("particles"), "particles"))
        self.triggers = _positive_int(_required(get("triggers"), "triggers"), "triggers")
        self.jobs = _positive_int(get("jobs", "1"), "jobs")
        self.spectrum = _choice(get("spectrum", "flat"), SPECTRA, "spectrum")
        self.binning = _choice(get("binning", "none"), BINNINGS, "binning")
        if self.binning != "none" and self.mode == "flux":
            raise CardError("binning works with flat spectra only (mode = flat or cutoff)")
        self.combine = _yesno(get("combine", "yes" if self.mode == "flux" else "no"), "combine")
        self.geometry = get("geometry", site.geometry)
        self.output_root = get("output_root", site.output_root)
        self.seed = _positive_int(get("seed"), "seed") if get("seed") else None

        # energy
        self.emin = _number(get("emin"), "emin") if get("emin") else None
        self.emax = _number(get("emax"), "emax") if get("emax") else None
        if self.mode == "flat" and (self.emin is None or self.emax is None):
            raise CardError("mode = flat needs emin and emax (keV)")
        if self.mode == "cutoff" and (self.emin is not None or self.emax is not None):
            self.notes.append("emin/emax are ignored in cutoff mode (the range comes from the flux files)")
            self.emin = self.emax = None
        if self.emin is not None and self.emin <= 0:
            raise CardError("emin must be > 0 keV")
        if self.emin is not None and self.emax is not None and self.emax <= self.emin:
            raise CardError("emax must be larger than emin")
        if self.mode == "flux" and get("spectrum"):
            self.notes.append("spectrum is ignored in flux mode (the shape comes from the flux files)")

        # direction
        self.theta = _angles(get("theta", "0"), "theta", 0, 180)
        default_phi = "0" if len(self.theta) == 1 else "0 360"
        self.phi = _angles(get("phi", default_phi), "phi", 0, 360)
        if len(self.theta) == 1 and len(self.phi) == 2:
            if self.theta[0] == 0:
                self.phi = (0.0,)   # on-axis: phi is irrelevant
            else:
                raise CardError("a single theta is a point source: give a single phi too, or a theta range")
        if len(self.theta) == 2 and len(self.phi) == 1:
            raise CardError("with a theta range give a phi range too (e.g. phi = 0 360)")

        # first interaction (photons only)
        fi = get("first_interaction", "any").lower()
        if fi in ("any", "all", "none"):
            self.first_interaction = None
        else:
            if fi not in site.fi_values:
                raise CardError("first_interaction must be one of: any, %s"
                                % ", ".join(sorted(site.fi_values)))
            not_photons = [p for p in self.particles if PARTICLES[p] != 1]
            if not_photons:
                raise CardError("first_interaction only works for photons, not for: %s"
                                % ", ".join(not_photons))
            self.first_interaction = fi

        if self.mode in ("flux", "cutoff"):
            for p in self.particles:
                if not os.path.isfile(site.flux_file(p)):
                    raise CardError("no flux file for %s (looked for %s)" % (p, site.flux_file(p)))

    @property
    def is_point(self):
        return len(self.theta) == 1

    def direction_text(self):
        if self.is_point:
            if self.theta[0] == 0:
                return "on-axis (theta = 0)"
            return "point source at theta = %g, phi = %g deg" % (self.theta[0], self.phi[0])
        return "theta %g..%g deg, phi %g..%g deg" % (self.theta + self.phi)


# --------------------------------------------------------------- parsing

def _required(value, key):
    if value is None:
        raise CardError("missing required key: %s" % key)
    return value


def _choice(value, choices, key):
    value = _required(value, key).lower()
    if value not in choices:
        raise CardError("%s must be one of: %s (got %r)" % (key, ", ".join(choices), value))
    return value


def _yesno(value, key):
    v = value.lower()
    if v in ("yes", "true", "1", "on"):
        return True
    if v in ("no", "false", "0", "off"):
        return False
    raise CardError("%s must be yes or no (got %r)" % (key, value))


def _number(value, key):
    try:
        return float(value)
    except ValueError:
        raise CardError("%s must be a number (got %r)" % (key, value))


def _positive_int(value, key):
    x = _number(value, key)
    if x != int(x) or x < 1:
        raise CardError("%s must be a positive integer (got %r)" % (key, value))
    return int(x)


def _split(value):
    return [v for v in re.split(r"[,\s]+", value.strip()) if v]


def _particles(value):
    names = []
    for raw in _split(value):
        name = ALIASES.get(raw.lower(), raw.lower())
        if name not in PARTICLES:
            raise CardError("unknown particle %r. Known: %s" % (raw, ", ".join(sorted(PARTICLES))))
        if name in names:
            raise CardError("particle listed twice: %s" % name)
        names.append(name)
    return names


def _angles(value, key, lo, hi):
    vals = tuple(_number(v, key) for v in _split(value))
    if len(vals) not in (1, 2):
        raise CardError("%s takes one value (point source) or two values (range), got %r" % (key, value))
    if any(v < lo or v > hi for v in vals):
        raise CardError("%s must be within %g..%g deg (got %r)" % (key, lo, hi, value))
    if len(vals) == 2 and vals[1] <= vals[0]:
        raise CardError("%s range must be 'min max' with min < max (got %r)" % (key, value))
    return vals
