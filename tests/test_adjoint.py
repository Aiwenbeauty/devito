import numpy as np
import pytest

from devito import Operator, norm, Function, Grid, SparseFunction
from devito.logger import info
from examples.seismic import demo_model, Receiver
from examples.seismic.acoustic import acoustic_setup
from examples.seismic.tti import tti_setup

presets = {
    'constant': {'preset': 'constant-isotropic'},
    'layers': {'preset': 'layers-isotropic', 'nlayers': 2},
    'layers-tti': {'preset': 'layers-tti', 'nlayers': 2},
}


class TestAdjoint(object):

    @pytest.mark.parametrize('mkey, shape, kernel, space_order, setup_func', [
        # 1 tests with varying time and space orders
        ('layers', (60, ), 'OT2', 12, acoustic_setup),
        ('layers', (60, ), 'OT2', 8, acoustic_setup),
        ('layers', (60, ), 'OT4', 4, acoustic_setup),
        # 2D tests with varying time and space orders
        ('layers', (60, 70), 'OT2', 12, acoustic_setup),
        ('layers', (60, 70), 'OT2', 8, acoustic_setup),
        ('layers', (60, 70), 'OT2', 4, acoustic_setup),
        ('layers', (60, 70), 'OT4', 2, acoustic_setup),
        # 3D tests with varying time and space orders
        ('layers', (60, 70, 80), 'OT2', 8, acoustic_setup),
        ('layers', (60, 70, 80), 'OT2', 6, acoustic_setup),
        ('layers', (60, 70, 80), 'OT2', 4, acoustic_setup), 
        ('layers', (60, 70, 80), 'OT4', 2, acoustic_setup),
        # Constant model in 2D and 3D
        ('constant', (60, 70), 'OT2', 10, acoustic_setup),
        ('constant', (60, 70, 80), 'OT2', 8, acoustic_setup),
        ('constant', (60, 70), 'OT2', 4, acoustic_setup),
        ('constant', (60, 70, 80), 'OT4', 2, acoustic_setup),
        # 2D TTI tests with varying space orders 
        ('layers-tti', (30, 35), 'None', 8, tti_setup), 
        ('layers-tti', (30, 35), 'None', 4, tti_setup),
        # 3D TTI tests with varying space orders
        ('layers-tti', (30, 35, 40), 'None', 8, tti_setup),
        ('layers-tti', (30, 35, 40), 'None', 4, tti_setup),
    ])
    def test_adjoint_F(self, mkey, shape, kernel, space_order, setup_func):
        """
        Adjoint test for the forward modeling operator.
        The forward modeling operator F generates a shot record (measurements)
        from a source while the adjoint of F generates measurments at the source
        location from data. This test uses the conventional dot test:
        < Fx, y> = <x, F^T y>
        """
        tn = 500.  # Final time

        # Create solver from preset
        if setup_func.__name__ == 'acoustic_setup':
            solver = setup_func(shape=shape, spacing=[15. for _ in shape],
                                kernel=kernel, nbl=10, tn=tn,
                                space_order=space_order,
                                **(presets[mkey]), dtype=np.float64)
        else:
            solver = setup_func(shape=shape, spacing=[15. for _ in shape],
                                nbl=10, tn=tn, space_order=space_order,
                                **(presets[mkey]), dtype=np.float64)
                                 
        # Create adjoint receiver symbol
        srca = Receiver(name='srca', grid=solver.model.grid,
                        time_range=solver.geometry.time_axis,
                        coordinates=solver.geometry.src_positions)

        # Run forward and adjoint operators
        if setup_func.__name__ == 'acoustic_setup':
            rec, _, _ = solver.forward(save=False)
        else:
            rec, _, _, _ = solver.forward(save=False)
        solver.adjoint(rec=rec, srca=srca)

        # Adjoint test: Verify <Ax,y> matches  <x, A^Ty> closely
        term1 = np.dot(srca.data.reshape(-1), solver.geometry.src.data)
        term2 = norm(rec) ** 2
        info('<Ax,y>: %f, <x, A^Ty>: %f, difference: %4.4e, ratio: %f'
             % (term1, term2, (term1 - term2)/term1, term1 / term2))
        assert np.isclose((term1 - term2)/term1, 0., atol=1.e-11)

    @pytest.mark.parametrize('space_order', [4, 8, 12])
    @pytest.mark.parametrize('shape', [(60,), (60, 70), (40, 50, 30)])
    def test_adjoint_J(self, shape, space_order):
        """
        Adjoint test for the FWI Jacobian operator.
        The Jacobian operator J generates a linearized shot record (measurements)
        from a model perturbation dm while the adjoint of J generates the FWI gradient
        from an adjoint source (usually data residual). This test uses the conventional
        dot test:
        < Jx, y> = <x ,J^T y>
        """
        tn = 500.  # Final time
        nbl = 10 + space_order / 2
        spacing = tuple([10.]*len(shape))
        # Create solver from preset
        solver = acoustic_setup(shape=shape, spacing=spacing, nlayers=2, vp_bottom=2,
                                nbl=nbl, tn=tn, space_order=space_order,
                                preset='layers-isotropic', dtype=np.float64)

        # Create initial model (m0) with a constant velocity throughout
        model0 = demo_model('layers-isotropic', vp_top=1.5, vp_bottom=1.5,
                            spacing=spacing, space_order=space_order, shape=shape,
                            nbl=nbl, dtype=np.float64, grid=solver.model.grid)

        # Compute the full wavefield u0
        _, u0, _ = solver.forward(save=True, vp=model0.vp)

        # Compute initial born perturbation from m - m0
        dm = (solver.model.vp.data**(-2) - model0.vp.data**(-2))

        du, _, _, _ = solver.born(dm, vp=model0.vp)

        # Compute gradientfrom initial perturbation
        im, _ = solver.gradient(du, u0, vp=model0.vp)

        # Adjoint test: Verify <Ax,y> matches  <x, A^Ty> closely
        term1 = np.dot(im.data.reshape(-1), dm.reshape(-1))
        term2 = norm(du)**2
        info('<Jx,y>: %f, <x, J^Ty>: %f, difference: %4.4e, ratio: %f'
             % (term1, term2, (term1 - term2)/term1, term1 / term2))
        assert np.isclose((term1 - term2)/term1, 0., atol=1.e-12)

    @pytest.mark.parametrize('shape, coords', [
        ((11, 11), [(.05, .9), (.01, .8)]),
        ((11, 11, 11), [(.05, .9), (.01, .8), (0.07, 0.84)])
    ])
    def test_adjoint_inject_interpolate(self, shape, coords, npoints=19):
        """
        Verify that p.inject is the adjoint of p.interpolate for a
        devito SparseFunction p
        """
        grid = Grid(shape)
        a = Function(name="a", grid=grid)
        a.data[:] = 0.
        c = Function(name='c', grid=grid)
        c.data[:] = 27.

        assert a.grid == c.grid
        # Inject receiver
        p = SparseFunction(name="p", grid=grid, npoint=npoints)
        for i, r in enumerate(coords):
            p.coordinates.data[:, i] = np.linspace(r[0], r[1], npoints)
        p.data[:] = 1.2
        expr = p.inject(field=a, expr=p)
        # Read receiver
        p2 = SparseFunction(name="p2", grid=grid, npoint=npoints)
        for i, r in enumerate(coords):
            p2.coordinates.data[:, i] = np.linspace(r[0], r[1], npoints)
        expr2 = p2.interpolate(expr=c)
        Operator(expr + expr2)(a=a, c=c)
        # < P x, y > - < x, P^T y>
        # Px => p2
        # y => p
        # x => c
        # P^T y => a
        term1 = np.dot(p2.data.reshape(-1), p.data.reshape(-1))
        term2 = np.dot(c.data.reshape(-1), a.data.reshape(-1))
        assert np.isclose((term1-term2) / term1, 0., atol=1.e-6)
