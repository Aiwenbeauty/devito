from abc import ABC

import numpy as np
import cgen as c
from sympy import Or, Max

from devito.data import FULL
from devito.ir import (DummyEq, Conditional, Dereference, Expression, ExpressionBundle,
                       List, ParallelIteration, ParallelBlock, ParallelTree, Prodder,
                       Block, FindSymbols, FindNodes, Return, VECTORIZED, Transformer,
                       IsPerfectIteration, retrieve_iteration_tree, filter_iterations)
from devito.mpi.routines import IrecvCall, IsendCall
from devito.symbolics import CondEq, INT, ccode
from devito.passes.iet.engine import iet_pass
from devito.tools import as_tuple, is_integer, prod
from devito.types import PointerArray, Symbol, NThreadsMixin

__all__ = ['Constructs', 'LanguageSpecializer', 'HostPragmaParallelizer',
           'DeviceAwarePragmaParallelizer', 'is_on_device']


class Constructs(dict):

    def __getitem__(self, k):
        if k not in self:
            raise NotImplementedError("Must implement `lang[%s]`" % k)
        return super().__getitem__(k)


class LanguageSpecializer(ABC):

    """
    Abstract base class defining a series of methods capable of specializing
    an IET for a certain target language (e.g., C, C+OpenMP).
    """

    lang = Constructs()
    """
    Relevant constructs of the target language.
    """

    def __init__(self, sregistry, platform):
        """
        Parameters
        ----------
        sregistry : SymbolRegistry
            The symbol registry, to access the symbols appearing in an IET.
        platform : Platform
            The underlying platform.
        """
        self.sregistry = sregistry
        self.platform = platform

    @iet_pass
    def initialize(self, iet):
        """
        An `iet_pass` which transforms an IET such that the target language
        runtime is properly initialized.
        """
        return iet, {}


class Parallelizer(LanguageSpecializer):

    """
    Specializer capable of generating shared-memory parallel IETs.
    """

    _Region = ParallelBlock
    """
    The IET node type to be used to construct a parallel region.
    """

    _Iteration = ParallelIteration
    """
    The IET node type to be used to construct a parallel Iteration.
    """

    _Prodder = Prodder
    """
    The IET node type to be used to construct concurrent prodders.
    """

    def __init__(self, key, sregistry, platform):
        """
        Parameters
        ----------
        key : callable, optional
            Return True if an Iteration can and should be parallelized, False otherwise.
        sregistry : SymbolRegistry
            The symbol registry, to access the symbols appearing in an IET.
        platform : Platform
            The underlying platform.
        """
        super().__init__(sregistry, platform)
        if key is not None:
            self.key = key
        else:
            self.key = lambda i: False

    @property
    def ncores(self):
        return self.platform.cores_physical

    @property
    def nhyperthreads(self):
        return self.platform.threads_per_core

    @iet_pass
    def make_parallel(self, iet):
        """
        An `iet_pass` which transforms an IET for shared-memory parallelism.
        """
        return iet, {}

    @iet_pass
    def make_simd(self, iet):
        """
        An `iet_pass` which transforms an IET for SIMD parallelism.
        """
        return iet, {}


class PragmaParallelizer(Parallelizer):

    """
    Specializer capable of generating shared-memory parallel IETs using a
    language based on pragmas.
    """

    def __init__(self, sregistry, options, platform):
        """
        Parameters
        ----------
        sregistry : SymbolRegistry
            The symbol registry, to access the symbols appearing in an IET.
        options : dict
             The optimization options. Accepted: ['par-collapse-ncores',
             'par-collapse-work', 'par-chunk-nonaffine', 'par-dynamic-work', 'par-nested']
             * 'par-collapse-ncores': use a collapse clause if the number of
               available physical cores is greater than this threshold.
             * 'par-collapse-work': use a collapse clause if the trip count of the
               collapsable Iterations is statically known to exceed this threshold.
             * 'par-chunk-nonaffine': coefficient to adjust the chunk size in
               non-affine parallel Iterations.
             * 'par-dynamic-work': use dynamic scheduling if the operation count per
               iteration exceeds this threshold. Otherwise, use static scheduling.
             * 'par-nested': nested parallelism if the number of hyperthreads per core
               is greater than this threshold.
        platform : Platform
            The underlying platform.
        """
        key = lambda i: i.is_ParallelRelaxed and not i.is_Vectorized
        super().__init__(key, sregistry, platform)

        self.collapse_ncores = options['par-collapse-ncores']
        self.collapse_work = options['par-collapse-work']
        self.chunk_nonaffine = options['par-chunk-nonaffine']
        self.dynamic_work = options['par-dynamic-work']
        self.nested = options['par-nested']

    @property
    def nthreads(self):
        return self.sregistry.nthreads

    @property
    def nthreads_nested(self):
        return self.sregistry.nthreads_nested

    @property
    def nthreads_nonaffine(self):
        return self.sregistry.nthreads_nonaffine

    @property
    def threadid(self):
        return self.sregistry.threadid

    def _find_collapsable(self, root, candidates):
        collapsable = []
        if self.ncores >= self.collapse_ncores:
            for n, i in enumerate(candidates[1:], 1):
                # The Iteration nest [root, ..., i] must be perfect
                if not IsPerfectIteration(depth=i).visit(root):
                    break

                # Loops are collapsable only if none of the iteration variables appear
                # in initializer expressions. For example, the following two loops
                # cannot be collapsed
                #
                # for (i = ... )
                #   for (j = i ...)
                #     ...
                #
                # Here, we make sure this won't happen
                if any(j.dim in i.symbolic_min.free_symbols for j in candidates[:n]):
                    break

                # Also, we do not want to collapse SIMD-vectorized Iterations
                if i.is_Vectorized:
                    break

                # Would there be enough work per parallel iteration?
                nested = candidates[n+1:]
                if nested:
                    try:
                        work = prod([int(j.dim.symbolic_size) for j in nested])
                        if work < self.collapse_work:
                            break
                    except TypeError:
                        pass

                collapsable.append(i)
        return collapsable

    @classmethod
    def _make_tid(cls, tid):
        return c.Initializer(c.Value(tid._C_typedata, tid.name), cls.lang['thread-num'])

    def _make_reductions(self, partree, collapsed):
        if not any(i.is_ParallelAtomic for i in collapsed):
            return partree

        # Collect expressions inducing reductions
        exprs = FindNodes(Expression).visit(partree)
        exprs = [i for i in exprs if i.is_Increment and not i.is_ForeignExpression]

        reduction = [i.output for i in exprs]
        if (all(i.is_Affine for i in collapsed)
                or all(not i.is_Indexed for i in reduction)):
            # Implement reduction
            mapper = {partree.root: partree.root._rebuild(reduction=reduction)}
        else:
            # Make sure the increment is atomic
            mapper = {i: i._rebuild(pragmas=self.lang['atomic']) for i in exprs}

        partree = Transformer(mapper).visit(partree)

        return partree

    def _make_threaded_prodders(self, partree):
        mapper = {i: self._Prodder(i) for i in FindNodes(Prodder).visit(partree)}
        partree = Transformer(mapper).visit(partree)
        return partree

    def _make_partree(self, candidates, nthreads=None):
        assert candidates
        root = candidates[0]

        # Get the collapsable Iterations
        collapsable = self._find_collapsable(root, candidates)
        ncollapse = 1 + len(collapsable)

        # Prepare to build a ParallelTree
        if all(i.is_Affine for i in candidates):
            bundles = FindNodes(ExpressionBundle).visit(root)
            sops = sum(i.ops for i in bundles)
            if sops >= self.dynamic_work:
                schedule = 'dynamic'
            else:
                schedule = 'static'
            if nthreads is None:
                # pragma ... for ... schedule(..., 1)
                nthreads = self.nthreads
                body = self._Iteration(schedule=schedule, ncollapse=ncollapse,
                                       **root.args)
            else:
                # pragma ... parallel for ... schedule(..., 1)
                body = self._Iteration(schedule=schedule, parallel=True,
                                       ncollapse=ncollapse, nthreads=nthreads,
                                       **root.args)
            prefix = []
        else:
            # pragma ... for ... schedule(..., expr)
            assert nthreads is None
            nthreads = self.nthreads_nonaffine
            chunk_size = Symbol(name='chunk_size')
            body = self._Iteration(ncollapse=ncollapse, chunk_size=chunk_size,
                                   **root.args)

            niters = prod([root.symbolic_size] + [j.symbolic_size for j in collapsable])
            value = INT(Max(niters / (nthreads*self.chunk_nonaffine), 1))
            prefix = [Expression(DummyEq(chunk_size, value, dtype=np.int32))]

        # Create a ParallelTree
        partree = ParallelTree(prefix, body, nthreads=nthreads)

        collapsed = [partree] + collapsable

        return root, partree, collapsed

    def _make_parregion(self, partree, parrays):
        arrays = [i for i in FindSymbols().visit(partree) if i.is_Array]

        # Detect thread-private arrays on the heap and "map" them to shared
        # vector-expanded (one entry per thread) Arrays
        heap_private = [i for i in arrays if i._mem_heap and i._mem_local]
        heap_globals = []
        for i in heap_private:
            if i in parrays:
                pi = parrays[i]
            else:
                pi = parrays.setdefault(i, PointerArray(name=self.sregistry.make_name(),
                                                        dimensions=(self.threadid,),
                                                        array=i))
            heap_globals.append(Dereference(i, pi))
        if heap_globals:
            prefix = List(header=self._make_tid(self.threadid),
                          body=heap_globals + list(partree.prefix),
                          footer=c.Line())
            partree = partree._rebuild(prefix=prefix)

        return self._Region(partree)

    def _make_guard(self, partree, collapsed):
        # Do not enter the parallel region if the step increment is 0; this
        # would raise a `Floating point exception (core dumped)` in some OpenMP
        # implementations. Note that using an OpenMP `if` clause won't work
        cond = [CondEq(i.step, 0) for i in collapsed if isinstance(i.step, Symbol)]
        cond = Or(*cond)
        if cond != False:  # noqa: `cond` may be a sympy.False which would be == False
            partree = List(body=[Conditional(cond, Return()), partree])
        return partree

    def _make_nested_partree(self, partree):
        # Apply heuristic
        if self.nhyperthreads <= self.nested:
            return partree

        # Note: there might be multiple sub-trees amenable to nested parallelism,
        # hence we loop over all of them
        #
        # for (i = ... )  // outer parallelism
        #   for (j0 = ...)  // first source of nested parallelism
        #     ...
        #   for (j1 = ...)  // second source of nested parallelism
        #     ...
        mapper = {}
        for tree in retrieve_iteration_tree(partree):
            outer = tree[:partree.ncollapsed]
            inner = tree[partree.ncollapsed:]

            # Heuristic: nested parallelism is applied only if the top nested
            # parallel Iteration iterates *within* the top outer parallel Iteration
            # (i.e., the outer is a loop over blocks, while the nested is a loop
            # within a block)
            candidates = []
            for i in inner:
                if self.key(i) and any(is_integer(j.step-i.symbolic_size) for j in outer):
                    candidates.append(i)
                elif candidates:
                    # If there's at least one candidate but `i` doesn't honor the
                    # heuristic above, then we break, as the candidates must be
                    # perfectly nested
                    break
            if not candidates:
                continue

            # Introduce nested parallelism
            subroot, subpartree, _ = self._make_partree(candidates, self.nthreads_nested)

            mapper[subroot] = subpartree

        partree = Transformer(mapper).visit(partree)

        return partree

    def _make_parallel(self, iet):
        mapper = {}
        parrays = {}
        for tree in retrieve_iteration_tree(iet):
            # Get the parallelizable Iterations in `tree`
            candidates = filter_iterations(tree, key=self.key)
            if not candidates:
                continue

            # Outer parallelism
            root, partree, collapsed = self._make_partree(candidates)
            if partree is None or root in mapper:
                continue

            # Nested parallelism
            partree = self._make_nested_partree(partree)

            # Handle reductions
            partree = self._make_reductions(partree, collapsed)

            # Atomicize and optimize single-thread prodders
            partree = self._make_threaded_prodders(partree)

            # Wrap within a parallel region
            parregion = self._make_parregion(partree, parrays)

            # Protect the parallel region if necessary
            parregion = self._make_guard(parregion, collapsed)

            mapper[root] = parregion

        iet = Transformer(mapper).visit(iet)

        # The new arguments introduced by this pass
        args = [i for i in FindSymbols().visit(iet) if isinstance(i, (NThreadsMixin))]
        for n in FindNodes(Dereference).visit(iet):
            args.extend([(n.array, True), n.parray])

        return iet, {'args': args, 'includes': [self.lang['header']]}

    @iet_pass
    def make_parallel(self, iet):
        return self._make_parallel(iet)


class HostPragmaParallelizer(PragmaParallelizer):

    @property
    def simd_reg_size(self):
        return self.platform.simd_reg_size

    @iet_pass
    def make_simd(self, iet):
        mapper = {}
        for tree in retrieve_iteration_tree(iet):
            candidates = [i for i in tree if i.is_Parallel]

            # As long as there's an outer level of parallelism, the innermost
            # PARALLEL Iteration gets vectorized
            if len(candidates) < 2:
                continue
            candidate = candidates[-1]

            # Add SIMD pragma
            aligned = [j for j in FindSymbols('symbolics').visit(candidate)
                       if j.is_DiscreteFunction]
            if aligned:
                simd = self.lang['simd-for-aligned']
                simd = as_tuple(simd(','.join([j.name for j in aligned]),
                                self.simd_reg_size))
            else:
                simd = as_tuple(self.lang['simd-for'])
            pragmas = candidate.pragmas + simd

            # Add VECTORIZED property
            properties = list(candidate.properties) + [VECTORIZED]

            mapper[candidate] = candidate._rebuild(pragmas=pragmas, properties=properties)

        iet = Transformer(mapper).visit(iet)

        return iet, {}


class DeviceAwarePragmaParallelizer(PragmaParallelizer):

    def __init__(self, sregistry, options, platform):
        super().__init__(sregistry, options, platform)

        self.gpu_fit = options['gpu-fit']
        self.par_disabled = options['par-disabled']

    @classmethod
    def _make_sections_from_imask(cls, f, imask):
        datasize = cls._map_data(f)
        if imask is None:
            imask = [FULL]*len(datasize)
        assert len(imask) == len(datasize)
        sections = []
        for i, j in zip(imask, datasize):
            if i is FULL:
                start, size = 0, j
            else:
                try:
                    start, size = i
                except TypeError:
                    start, size = i, 1
                start = ccode(start)
            sections.append('[%s:%s]' % (start, size))
        return ''.join(sections)

    @classmethod
    def _map_data(cls, f):
        if f.is_Array:
            return f.symbolic_shape
        else:
            return tuple(f._C_get_field(FULL, d).size for d in f.dimensions)

    @classmethod
    def _map_to(cls, f, imask=None, queueid=None):
        sections = cls._make_sections_from_imask(f, imask)
        return cls.lang['map-enter-to'](f.name, sections)

    _map_to_wait = _map_to

    @classmethod
    def _map_alloc(cls, f, imask=None):
        sections = cls._make_sections_from_imask(f, imask)
        return cls.lang['map-enter-alloc'](f.name, sections)

    @classmethod
    def _map_present(cls, f, imask=None):
        return

    @classmethod
    def _map_update(cls, f):
        return cls.lang['map-update'](f.name, ''.join('[0:%s]' % i
                                                      for i in cls._map_data(f)))

    @classmethod
    def _map_update_host(cls, f, imask=None, queueid=None):
        sections = cls._make_sections_from_imask(f, imask)
        return cls.lang['map-update-host'](f.name, sections)

    _map_update_wait_host = _map_update_host

    @classmethod
    def _map_update_device(cls, f, imask=None, queueid=None):
        sections = cls._make_sections_from_imask(f, imask)
        return cls.lang['map-update-device'](f.name, sections)

    _map_update_wait_device = _map_update_device

    @classmethod
    def _map_release(cls, f, devicerm=None):
        return cls.lang['map-release'](f.name,
                                       ''.join('[0:%s]' % i for i in cls._map_data(f)),
                                       (' if(%s)' % devicerm.name) if devicerm else '')

    @classmethod
    def _map_delete(cls, f, imask=None, devicerm=None):
        sections = cls._make_sections_from_imask(f, imask)
        # This ugly condition is to avoid a copy-back when, due to
        # domain decomposition, the local size of a Function is 0, which
        # would cause a crash
        items = []
        if devicerm is not None:
            items.append(devicerm.name)
        items.extend(['(%s != 0)' % i for i in cls._map_data(f)])
        cond = ' if(%s)' % ' && '.join(items)
        return cls.lang['map-exit-delete'](f.name, sections, cond)

    def _make_threaded_prodders(self, partree):
        if isinstance(partree.root, self._Iteration):
            # no-op for now
            return partree
        else:
            return super()._make_threaded_prodders(partree)

    def _make_partree(self, candidates, nthreads=None):
        """
        Parallelize the `candidates` Iterations attaching suitable OpenMP pragmas
        for parallelism. In particular:

            * All parallel Iterations not *writing* to a host Function, that
              is a Function `f` such that ``is_on_device(f) == False`, are offloaded
              to the device.
            * The remaining ones, that is those writing to a host Function,
              are parallelized on the host.
        """
        assert candidates
        root = candidates[0]

        if is_on_device(root, self.gpu_fit, only_writes=True):
            # The typical case: all written Functions are device Functions, that is
            # they're mapped in the device memory. Then we offload `root` to the device

            # Get the collapsable Iterations
            collapsable = self._find_collapsable(root, candidates)
            ncollapse = 1 + len(collapsable)

            body = self._Iteration(gpu_fit=self.gpu_fit, ncollapse=ncollapse, **root.args)
            partree = ParallelTree([], body, nthreads=nthreads)
            collapsed = [partree] + collapsable

            return root, partree, collapsed
        elif not self.par_disabled:
            # Resort to host parallelism
            return super()._make_partree(candidates, nthreads)
        else:
            return root, None, None

    def _make_parregion(self, partree, *args):
        if isinstance(partree.root, self._Iteration):
            # no-op for now
            return partree
        else:
            return super()._make_parregion(partree, *args)

    def _make_guard(self, parregion, *args):
        partrees = FindNodes(ParallelTree).visit(parregion)
        if any(isinstance(i.root, self._Iteration) for i in partrees):
            # no-op for now
            return parregion
        else:
            return super()._make_guard(parregion, *args)

    def _make_nested_partree(self, partree):
        if isinstance(partree.root, self._Iteration):
            # no-op for now
            return partree
        else:
            return super()._make_nested_partree(partree)

    @iet_pass
    def make_gpudirect(self, iet):
        """
        Modify MPI Callables to enable multiple GPUs performing GPU-Direct communication.
        """
        mapper = {}
        for node in FindNodes((IsendCall, IrecvCall)).visit(iet):
            header = c.Pragma('omp target data use_device_ptr(%s)' %
                              node.arguments[0].name)
            mapper[node] = Block(header=header, body=node)

        iet = Transformer(mapper).visit(iet)

        return iet, {}


# Utils

def is_on_device(maybe_symbol, gpu_fit, only_writes=False):  #TODO: MAKE IT CLASSMETHOD OF DEVICEPARALLELIZER ??
    """
    True if all given Functions are allocated in the device memory, False otherwise.

    Parameters
    ----------
    maybe_symbol : Indexed or Function or Node
        The inspected object. May be a single Indexed or Function, or even an
        entire piece of IET.
    gpu_fit : list of Function
        The Function's which are known to definitely fit in the device memory. This
        information is given directly by the user through the compiler option
        `gpu-fit` and is propagated down here through the various stages of lowering.
    only_writes : bool, optional
        Only makes sense if `maybe_symbol` is an IET. If True, ignore all Function's
        that do not appear on the LHS of at least one Expression. Defaults to False.
    """
    try:
        functions = (maybe_symbol.function,)
    except AttributeError:
        assert maybe_symbol.is_Node
        iet = maybe_symbol
        functions = set(FindSymbols().visit(iet))
        if only_writes:
            expressions = FindNodes(Expression).visit(iet)
            functions &= {i.write for i in expressions}

    return all(not (f.is_TimeFunction and f.save is not None and f not in gpu_fit)
               for f in functions)
