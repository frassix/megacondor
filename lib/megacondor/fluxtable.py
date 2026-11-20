"""Cosima 3D flux tables (the fluxes/*.dat files).

Format, as read by MEGAlib's MFunction3DSpherical
(Beam FarFieldNormalizedEnergyBeamFluxFunction):

    IP LIN
    PA phi_0 ... phi_n          [deg]
    TA theta_0 ... theta_m      [deg], inside 0..180
    EA E_0 ... E_k              [keV]
    AP i j k value              flux at (phi_i, theta_j, E_k) [#/cm2/s/sr/keV]

Cosima interpolates trilinearly between the nodes and only samples inside
the axis limits. Cropping a table therefore means: keep the nodes inside the
window and add new nodes on the window edges, evaluated with the same
trilinear interpolation. Inside the window the cropped table is the same
function as the original one.
"""

import bisect
import math


class FluxTable(object):

    def __init__(self, phi, theta, energy, values, path=None):
        self.phi = list(phi)
        self.theta = list(theta)
        self.energy = list(energy)
        self.values = list(values)  # flat, index = i + nphi*(j + ntheta*k)
        self.path = path
        self._check()

    # ------------------------------------------------------------------ I/O

    @classmethod
    def read(cls, path):
        axes = {}
        entries = []
        with open(path) as f:
            for line in f:
                tok = line.split()
                if not tok:
                    continue
                key = tok[0]
                if key in ("PA", "TA", "EA"):
                    axes[key] = [float(x) for x in tok[1:]]
                elif key == "AP":
                    entries.append((int(tok[1]), int(tok[2]), int(tok[3]), float(tok[4])))
                elif key == "IP" and tok[1:] != ["LIN"]:
                    raise ValueError("%s: only 'IP LIN' is supported by cosima" % path)
        missing = [k for k in ("PA", "TA", "EA") if k not in axes]
        if missing:
            raise ValueError("%s: missing axis line(s) %s" % (path, ", ".join(missing)))
        nphi, ntheta, nenergy = len(axes["PA"]), len(axes["TA"]), len(axes["EA"])
        values = [0.0] * (nphi * ntheta * nenergy)  # cosima also defaults to 0
        for i, j, k, v in entries:
            if not (0 <= i < nphi and 0 <= j < ntheta and 0 <= k < nenergy):
                raise ValueError("%s: AP index out of range: %d %d %d" % (path, i, j, k))
            values[i + nphi * (j + ntheta * k)] = v
        return cls(axes["PA"], axes["TA"], axes["EA"], values, path)

    def write(self, path, comment=None):
        with open(path, "w") as f:
            if comment:
                f.write("# %s\n" % comment)
            f.write("IP LIN\n")
            f.write("PA %s\n" % " ".join("%.10g" % x for x in self.phi))
            f.write("TA %s\n" % " ".join("%.10g" % x for x in self.theta))
            f.write("EA %s\n" % " ".join("%.10g" % x for x in self.energy))
            for k in range(len(self.energy)):
                for j in range(len(self.theta)):
                    for i in range(len(self.phi)):
                        f.write("AP %d %d %d %.6e\n" % (i, j, k, self._v(i, j, k)))

    def _check(self):
        for name, axis in (("phi", self.phi), ("theta", self.theta), ("energy", self.energy)):
            if len(axis) < 2:
                raise ValueError("%s axis needs at least 2 values" % name)
            if any(b <= a for a, b in zip(axis, axis[1:])):
                raise ValueError("%s axis must be strictly increasing" % name)
        if self.theta[0] < 0 or self.theta[-1] > 180:
            raise ValueError("theta axis must be inside [0, 180]")
        if any(v < 0 for v in self.values):
            raise ValueError("negative flux values are not allowed")

    # ----------------------------------------------------------- evaluation

    def _v(self, i, j, k):
        return self.values[i + len(self.phi) * (j + len(self.theta) * k)]

    def evaluate(self, phi, theta, energy):
        """Trilinear interpolation, identical to MFunction3D::Evaluate."""
        i, fx = _locate(self.phi, phi)
        j, fy = _locate(self.theta, theta)
        k, fz = _locate(self.energy, energy)
        total = 0.0
        for di, wx in ((0, 1 - fx), (1, fx)):
            for dj, wy in ((0, 1 - fy), (1, fy)):
                for dk, wz in ((0, 1 - fz), (1, fz)):
                    total += wx * wy * wz * self._v(i + di, j + dj, k + dk)
        return total

    def integrate(self):
        """Total flux [#/cm2/s], computed exactly like MFunction3DSpherical::Integrate
        (this is the number cosima uses as the source flux)."""
        total = 0.0
        for i in range(len(self.phi) - 1):
            pc = 0.5 * (self.phi[i] + self.phi[i + 1])
            dp = math.radians(self.phi[i + 1] - self.phi[i])
            for j in range(len(self.theta) - 1):
                tc = 0.5 * (self.theta[j] + self.theta[j + 1])
                dt = abs(math.cos(math.radians(self.theta[j])) - math.cos(math.radians(self.theta[j + 1])))
                for k in range(len(self.energy) - 1):
                    ec = 0.5 * (self.energy[k] + self.energy[k + 1])
                    de = self.energy[k + 1] - self.energy[k]
                    total += dp * dt * de * self.evaluate(pc, tc, ec)
        return total

    # ------------------------------------------------------------- windows

    def crop(self, phi_range=None, theta_range=None, energy_range=None):
        """New table restricted to the given (lo, hi) windows (None = keep axis)."""
        phi = _sub_axis(self.phi, phi_range, "phi")
        theta = _sub_axis(self.theta, theta_range, "theta")
        energy = _sub_axis(self.energy, energy_range, "energy")
        values = [self.evaluate(p, t, e) for e in energy for t in theta for p in phi]
        return FluxTable(phi, theta, energy, values, self.path)

    def spectrum_at(self, phi, theta, energy_range=None):
        """Energy spectrum in one direction: lists (E [keV], flux [#/cm2/s/sr/keV])."""
        _inside(self.phi, phi, "phi")
        _inside(self.theta, theta, "theta")
        energy = _sub_axis(self.energy, energy_range, "energy")
        return energy, [self.evaluate(phi, theta, e) for e in energy]

    def energy_support(self):
        """(Emin, Emax) of the energy nodes where the flux is non-zero
        somewhere in the table, or None if the table is empty."""
        nonzero = [k for k in range(len(self.energy))
                   if any(self._v(i, j, k) > 0
                          for i in range(len(self.phi)) for j in range(len(self.theta)))]
        return _support(self.energy, nonzero)


# --------------------------------------------------------------- helpers

def spectrum_support(energy, flux):
    return _support(energy, [k for k, f in enumerate(flux) if f > 0])


def integrate_spectrum(energy, flux):
    """Trapezoidal integral (exact for a linearly interpolated spectrum)."""
    return sum(0.5 * (flux[k] + flux[k + 1]) * (energy[k + 1] - energy[k])
               for k in range(len(energy) - 1))


def _support(axis, nonzero):
    if not nonzero:
        return None
    lo, hi = nonzero[0], nonzero[-1]
    if lo == hi:  # a single non-zero node: the flux lives on the neighbouring bins
        lo, hi = max(lo - 1, 0), min(hi + 1, len(axis) - 1)
    return axis[lo], axis[hi]


def _locate(axis, x):
    """Lower node index (clamped like cosima) and fractional position."""
    i = bisect.bisect_right(axis, x) - 1
    i = min(max(i, 0), len(axis) - 2)
    return i, (x - axis[i]) / (axis[i + 1] - axis[i])


def _tol(lo, hi):
    return 1e-9 * max(abs(lo), abs(hi), 1.0)


def _inside(axis, x, name):
    if x < axis[0] - _tol(axis[0], axis[-1]) or x > axis[-1] + _tol(axis[0], axis[-1]):
        raise ValueError("%s = %g is outside the table (%g .. %g)" % (name, x, axis[0], axis[-1]))


def _sub_axis(axis, window, name):
    if window is None:
        return list(axis)
    lo, hi = window
    if hi <= lo:
        raise ValueError("%s window must have min < max (got %g, %g)" % (name, lo, hi))
    _inside(axis, lo, name)
    _inside(axis, hi, name)
    tol = _tol(lo, hi)
    inner = [a for a in axis if lo + tol < a < hi - tol]
    return [lo] + inner + [hi]
