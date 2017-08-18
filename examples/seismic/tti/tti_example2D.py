import numpy as np

from devito.logger import warning
from examples.seismic import Model, GaborSource, Receiver
from examples.seismic.tti import AnisotropicWaveSolver


def setup(dimensions=(150, 150), spacing=(15.0, 15.0), tn=750.0,
          time_order=2, space_order=4, nbpml=10):

    ndim = len(dimensions)
    nrec = 101
    origin = (0., 0.)

    # True velocity
    true_vp = np.ones(dimensions) + 1.0
    true_vp[:, int(dimensions[0] / 3):int(2*dimensions[0]/3)] = 3.0
    true_vp[:, int(2*dimensions[0] / 3):int(dimensions[0])] = 4.0

    model = Model(origin, spacing, dimensions, true_vp,
                  nbpml=nbpml,
                  epsilon=.4*np.ones(dimensions),
                  delta=-.1*np.ones(dimensions),
                  theta=-np.pi/7*np.ones(dimensions))

    # Derive timestepping from model spacing
    dt = model.critical_dt
    t0 = 0.0
    nt = int(1 + (tn-t0) / dt)
    time = np.linspace(t0, tn, nt)

    # Define source geometry (center of domain, just below surface)
    src = GaborSource(name='src', ndim=ndim, f0=0.01, time=time)
    src.coordinates.data[0, :] = np.array(model.domain_size) * .5
    src.coordinates.data[0, -1] = model.origin[-1] + 2 * spacing[-1]

    # Define receiver geometry (spread across x, lust below surface)
    rec = Receiver(name='nrec', ntime=nt, npoint=nrec, ndim=ndim)
    rec.coordinates.data[:, 0] = np.linspace(0., model.domain_size[0], num=nrec)
    rec.coordinates.data[:, 1:] = src.coordinates.data[0, 1:]

    return AnisotropicWaveSolver(model, source=src, time_order=time_order,
                                 space_order=space_order, receiver=rec)


def run(dimensions=(150, 150), spacing=(15.0, 15.0), tn=750.0,
        time_order=2, space_order=4, nbpml=10, **kwargs):

    solver = setup(dimensions, spacing, tn, time_order, space_order, nbpml)

    if space_order % 4 != 0:
        warning('WARNING: TTI requires a space_order that is a multiple of 4!')

    rec, u, v, summary = solver.forward(**kwargs)

    return summary.gflopss, summary.oi, summary.timings, [rec, u, v]


if __name__ == "__main__":
    run()