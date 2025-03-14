from __future__ import annotations  # type: ignore[attr-defined]
from dataclasses import dataclass
from typing import (
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Union
)
import copy
import weakref

import threading
import torch
import torch.distributed as dist
from torch.distributed import rpc
from torch.distributed import distributed_c10d
import torch.distributed._shard.sharding_spec as shard_spec
from torch.distributed._shard.sharding_spec._internals import (
    check_tensor,
    validate_non_overlapping_shards_metadata,
)
from .metadata import TensorProperties, ShardedTensorMetadata
from .shard import Shard
from .reshard import reshuffle_local_shard, reshard_local_shard
from .utils import (
    get_current_process_group,
    _flatten_tensor_size,
    _parse_and_validate_remote_device,
    _validate_output_tensor_for_gather,
    build_metadata_from_local_shards,
    build_global_metadata
)

# Tracking for sharded tensor objects.
_sharded_tensor_lock = threading.Lock()
_sharded_tensor_current_id = 0
_sharded_tensor_map: Dict[int, 'weakref.ReferenceType[ShardedTensor]'] = {}

# Custom sharded ops
_SHARDED_OPS: Dict[str, Callable] = {}
def _register_sharded_op(op, func):
    from inspect import signature
    if len(signature(func).parameters) != 4:
        raise TypeError(
            f'Custom sharded op function expects signature: '
            f'(types, args, kwargs, process_group), but received '
            f'signature: {signature(func)}')

    global _SHARDED_OPS
    _SHARDED_OPS[op] = func

def _register_remote_shards(sharded_tensor_id: int, rrefs: List[rpc.RRef[Shard]], rpc_rank: int):
    with _sharded_tensor_lock:
        if sharded_tensor_id not in _sharded_tensor_map:
            raise RuntimeError(
                f'Could not find sharded_tensor_id: {sharded_tensor_id} in map: {_sharded_tensor_map.keys()}')

        sharded_tensor = _sharded_tensor_map[sharded_tensor_id]()
        if sharded_tensor is None:
            raise RuntimeError('ShardedTensor weakref has been deallocated')
        else:
            sharded_tensor._register_remote_shards(rrefs, rpc_rank)

class ShardedTensor(object):
    """
    ShardedTensor is an abstraction to represent Tensors that are sharded
    across multiple devices and multiple processes.

    ShardedTensor is initialized in an SPMD like fashion where each rank
    initializes the ShardedTensor. The ShardedTensor object on each rank
    then only stores the local shard for the Tensor and provides global
    metadata for all the shards.

    ShardedTensor doesn't provide any Tensor like operations but is a wrapper
    providing the Tensor representing the local shard and the global metadata.
    Using these, users can build their custom distributed._sharded computations
    on top of this primitive. The local shards are all initialized using the
    create_op specified by tensor_init_params.create_op, e.g., torch.ones, or
    torch.empty

    Args:
        sharding_spec (:class:`torch.distributed._shard.sharding_spec.ShardingSpec`): The specification
            describing how to shard the Tensor.
        size (int...): a sequence of integers defining the shape of the output
            tensor. Can be a variable number of arguments or a collection like a list or tuple.

    Keyword args:
        dtype (:class:`torch.dtype`, optional): the desired data type of returned tensor.
                Default: if ``None``, uses a global default (see :func:`torch.set_default_tensor_type`).
        layout (:class:`torch.layout`, optional): the desired layout of returned Tensor.
            Default: ``torch.strided``.
        requires_grad (bool, optional): If autograd should record operations on the
            returned tensor. Default: ``False``.
        pin_memory (bool, optional): If set, returned tensor would be allocated in
            the pinned memory. Works only for CPU tensors. Default: ``False``.
        memory_format (:class:`torch.memory_format`, optional): the desired memory format of
            returned Tensor. Default: ``torch.contiguous_format``.
        init_rrefs (bool, optional): Whether or not to initialize
            :class:`torch.distributed.rpc.RRef`s pointing to remote shards.
            Need to initialize the RPC Framework if specified as ``True``.
            Default: ``False``.

    .. note:: ShardedTensor uses collectives to do various operations, i.e. it
        uses all_gather to do cross rank validations. For NCCL-based processed
        groups, internal tensor representations of objects must be moved to the
        GPU device before communication takes place. In this case, the device
        used is given by ``torch.cuda.current_device()`` and it is the user's
        responsiblity to ensure that this is set so that each rank has an
        individual GPU, via ``torch.cuda.set_device()``

    """

    def __new__(cls, *args, **kwargs):
        # Use __new__ for logging purposes.
        torch._C._log_api_usage_once("torch.distributed._shard.sharded_tensor")
        return super(ShardedTensor, cls).__new__(cls)

    def __init__(
        self,
        sharding_spec: shard_spec.ShardingSpec,
        *size,
        dtype=None,
        layout=torch.strided,
        requires_grad=False,
        pin_memory=False,
        memory_format=torch.contiguous_format,
        process_group=None,
        init_rrefs=False,
    ):
        # prepare initialization, initialize fields like
        # _process_group, _local_shards, etc.
        self._prepare_init(process_group=process_group, init_rrefs=init_rrefs)

        tensor_properties = TensorProperties(dtype, layout, requires_grad, memory_format, pin_memory)

        if tensor_properties is None:
            raise ValueError('tensor_properties must not be None.')

        if tensor_properties.dtype is None:
            tensor_properties.dtype = torch.get_default_dtype()

        if tensor_properties.layout != torch.strided:
            raise ValueError('Only torch.strided layout is currently supported')

        if tensor_properties.memory_format != torch.contiguous_format:
            raise ValueError('Only torch.contiguous_format memory_format is currently supported')

        dims = _flatten_tensor_size(size)

        if not isinstance(sharding_spec, shard_spec.ShardingSpec):
            raise ValueError(f'Expecting ShardingSpec but got: {type(sharding_spec)}')

        self._sharding_spec = sharding_spec

        sharded_tensor_metadata = sharding_spec.build_metadata(
            dims, tensor_properties=tensor_properties)

        current_rank = dist.get_rank(self._process_group)

        for shard_metadata in sharded_tensor_metadata.shards_metadata:
            rank, device = _parse_and_validate_remote_device(self._process_group, shard_metadata.placement)
            if rank == current_rank:
                local_tensor = _create_tensor_from_params(
                    shard_metadata.shard_sizes,
                    local_device=device,
                    tensor_properties=sharded_tensor_metadata.tensor_properties
                )
                self._local_shards.append(Shard(local_tensor, shard_metadata))
        self._metadata = sharded_tensor_metadata

        # do post initialization (i.e. register sharded_tensor_id, initialize_rpc)
        self._post_init()

    def _prepare_init(self, process_group=None, init_rrefs=False):
        self._init_rrefs = init_rrefs
        self._sharded_tensor_id = None

        self._process_group = (
            process_group
            if process_group is not None
            else distributed_c10d._get_default_group()
        )

        self._local_shards: List[Shard] = []
        self._remote_shards: Dict[int, List[rpc.RRef[Shard]]] = {}

    def _post_init(self):
        # Initialize RPC if available.
        if self._init_rrefs:
            with _sharded_tensor_lock:
                global _sharded_tensor_current_id, _sharded_tensor_map
                self._sharded_tensor_id = _sharded_tensor_current_id
                _sharded_tensor_map[self._sharded_tensor_id] = weakref.ref(self)
                _sharded_tensor_current_id += 1

            if not rpc._is_current_rpc_agent_set():
                raise RuntimeError(
                    'RPC Framework needs to be initialized using'
                    ' torch.distributed.rpc.init_rpc if init_rrefs is set to True')
            self._init_rpc()

    def __del__(self):
        # Clean up the global map.
        with _sharded_tensor_lock:
            global _sharded_tensor_current_id, _sharded_tensor_map
            if self._sharded_tensor_id in _sharded_tensor_map:
                _sharded_tensor_map.pop(self._sharded_tensor_id)  # type: ignore[call-overload]

    def _init_rpc(self):
        # Validate PG and RPC ranks match.
        pg_rank = dist.get_rank()
        rpc_rank = rpc.get_worker_info().id
        if pg_rank != rpc_rank:
            raise ValueError(
                f'Default ProcessGroup and RPC ranks must be '
                f'the same for ShardedTensor, found process group rank: '
                f'{pg_rank} and RPC rank: {rpc_rank}'
            )

        self._remote_shards = {}

        # Gather all the sharded tensor ids.
        worker_infos = rpc._get_current_rpc_agent().get_worker_infos()
        rank_to_name = {}
        name_to_rank = {}

        for worker_info in worker_infos:
            rank_to_name[worker_info.id] = worker_info.name
            name_to_rank[worker_info.name] = worker_info.id

        all_tensor_ids = rpc.api._all_gather(self._sharded_tensor_id)

        # Share the local shards to the entire world.
        futs = []
        rpc_rank = rpc.get_worker_info().id
        for rank in range(dist.get_world_size()):
            # Skip self.
            if rank == dist.get_rank():
                continue

            if len(self.local_shards()) != 0:
                rrefs: List[rpc.RRef[Shard]] = [rpc.RRef(shard) for shard in self.local_shards()]
                fut = rpc.rpc_async(
                    rank,
                    _register_remote_shards,
                    args=(all_tensor_ids[rank_to_name[rank]], rrefs, rpc_rank))
                futs.append(fut)

        torch.futures.wait_all(futs)

        # Barrier for all RPCs to finish on all ranks.
        rpc.api._all_gather(None)

    def gather(
        self,
        dst: int = 0,
        out: Optional[torch.Tensor] = None,
    ) -> None:
        """
        Creates a full :class:`Tensor` on rank ``dst`` by gathering all shards of the
        sharded tensor.

        The API needs to be called on all ranks in SPMD fashion. All ranks should have
        the same ``dst``. ``out`` should be a tensor of the same size as the overall
        size of the sharded tensor on ``dst`` and ``None`` on all other ranks.

        Args:
            dst(int): The rank where full tensor is constructed.
                Default: 0
            out (:class `torch.Tensor`, optional): The output full tensor.
                Must to be provided ONLY on ``dst`` rank.
                Default: ``None``
        """
        rank = dist.get_rank(self._process_group)
        full_size = self.metadata().size
        _validate_output_tensor_for_gather(rank, dst, full_size, out)

        local_shards = self.local_shards()

        world_size = dist.get_world_size(self._process_group)

        gathered_shards: List[Optional[List[Shard]]] = [None] * world_size if rank == dst else []
        # TODO: see how we could use dist.gather() instead of dist.gather_object
        # as the latter one involves pickling on CPU, see more context
        # https://github.com/pytorch/pytorch/issues/73935
        dist.gather_object(
            obj=local_shards,
            object_gather_list=gathered_shards,
            dst=dst,
            group=self._process_group,
        )
        if rank == dst:
            if out is None:
                raise ValueError("`out` Tensor must be provided on dst rank!")
            dims = len(full_size)
            for shards in gathered_shards:
                if shards is None:
                    raise RuntimeError(
                        'Gathered shards cannot be None on dst rank {dst}'
                    )
                for shard in shards:
                    metadata = shard.metadata
                    tensor = shard.tensor

                    out_narrow_view = out
                    for dim in range(dims):
                        out_narrow_view = out_narrow_view.narrow(
                            dim,
                            metadata.shard_offsets[dim],
                            metadata.shard_sizes[dim],
                        )

                    out_narrow_view.copy_(tensor)

    def cpu(
        self,
        memory_format=torch.preserve_format,
        process_group=None
    ) -> ShardedTensor:
        """
        Returns a copy of this object in CPU memory.

        If this ShardedTensor is already on CPU memory, then no copy is
        performed and original object is returned.

        .. note:: When moving a ShardedTensor from GPU to CPU, the ShardedTensor might
            need to be managed by a different type of ProcessGroup(i.e. ProcessGroupGloo),
            it is the user's responsiblity to explicitly pass in a new process_group that
            is compatible with CPU.
        """
        # TODO: make this a __torch_function__ op once ShardedTensor becomes a
        # torch.Tensor subclass, see https://github.com/pytorch/pytorch/issues/75402
        if memory_format != torch.preserve_format and \
                memory_format != torch.contiguous_format:
            raise RuntimeError("Only `torch.contiguous_format` or "
                               "`torch.preserve_format` is supported!")
        all_on_cpu = True
        for meta in self.metadata().shards_metadata:
            all_on_cpu &= (meta.placement.device().type == "cpu")  # type: ignore[union-attr]

        # if every shard is already on CPU, return the original object
        if all_on_cpu:
            return self

        # if not, returns a copy of this object on CPU
        list_shards: List[Shard] = []
        # move all local shards to cpu, and change metadata
        for shard in self._local_shards:
            cpu_tensor = shard.tensor.cpu(memory_format=memory_format)  # type: ignore[call-arg]
            metadata = copy.deepcopy(shard.metadata)
            metadata.placement._device = torch.device("cpu")  # type: ignore[union-attr]
            list_shards.append(
                Shard(cpu_tensor, metadata)
            )

        st_meta = copy.deepcopy(self.metadata())
        for meta in st_meta.shards_metadata:
            if meta.placement.device().type != "cpu":  # type: ignore[union-attr]
                meta.placement._device = torch.device("cpu")  # type: ignore[union-attr]

        pg = self._process_group if process_group is None else process_group
        st_cpu = ShardedTensor._init_from_local_shards_and_global_metadata(
            list_shards,
            sharded_tensor_metadata=st_meta,
            process_group=pg,
            init_rrefs=self._init_rrefs
        )
        return st_cpu

    @classmethod
    def _init_from_local_shards(
        cls,
        local_shards: List[Shard],
        *global_size,
        process_group=None,
        init_rrefs=False,
    ):
        # STEP 1: Validate the Shardmetadatas locally
        process_group = (
            process_group
            if process_group is not None
            else distributed_c10d._get_default_group()
        )
        current_rank = dist.get_rank(process_group)
        world_size = dist.get_world_size(process_group)

        local_sharded_tensor_metadata: Optional[ShardedTensorMetadata] = None
        global_tensor_size = _flatten_tensor_size(global_size)

        if len(local_shards) > 0:
            local_sharded_tensor_metadata = \
                build_metadata_from_local_shards(local_shards, global_tensor_size, current_rank, process_group)

        # STEP 2. Validate metadata across ranks, and build a global sharded tensor
        # metadata by gathering local ShardedTensorMetadata
        gathered_metadatas: List[Optional[ShardedTensorMetadata]] = []
        if world_size > 1:
            gathered_metadatas = [None for _ in range(world_size)]

            dist.all_gather_object(
                gathered_metadatas,
                local_sharded_tensor_metadata,
                group=process_group
            )
        else:
            gathered_metadatas = [local_sharded_tensor_metadata]

        global_sharded_tensor_metadata = build_global_metadata(gathered_metadatas)

        # STEP 3: Validation done, create the actual ShardedTensor and populate fields
        # prepare initialization
        sharded_tensor = cls.__new__(cls)
        sharded_tensor._prepare_init(process_group=process_group, init_rrefs=init_rrefs)

        # add to metadata and local_shards
        sharded_tensor._metadata = global_sharded_tensor_metadata
        sharded_tensor._local_shards = local_shards
        sharded_tensor._sharding_spec = shard_spec._infer_sharding_spec_from_shards_metadata(
            global_sharded_tensor_metadata.shards_metadata
        )

        # run post initialization, i.e. map registration, rpc initialization
        sharded_tensor._post_init()
        return sharded_tensor

    @classmethod
    def _init_from_local_tensor(
        cls,
        local_tensor: torch.Tensor,
        sharding_spec: shard_spec.ShardingSpec,
        *global_size: Sequence[int],
        process_group: dist.ProcessGroup = None,
        init_rrefs=False,
    ) -> "ShardedTensor":
        """
        Initialize a ShardedTensor given only one local tensor, global sharded tensor
        size and sharding spec on each rank.

        Args:
            local_tensor (Tensor): Single tensor of local shard stored in each rank.
            sharding_spec (:class:`torch.distributed._shard.sharding_spec.ShardingSpec`):
                The specification describing how to shard the Tensor.
            global_size (Sequence[int]): Size of the sharded tensor.
            process_group (ProcessGroup, optional): The process group to aggregate on.
                Default: None
            init_rrefs (bool, optional): Whether or not to initialize
                :class:`torch.distributed.rpc.RRef`s pointing to remote shards.
                Need to initialize the RPC Framework if specified as ``True``.
                Default: ``False``.

        Returns:
            A :class:`ShardedTensor` sharded based on the given sharding_spec with local
                tensor stored in the current rank.

        Examples:
            >>> # All tensors below are of torch.int64 type.
            >>> # We have 2 process groups, 2 ranks.
            >>> tensor = torch.arange(2, dtype=torch.int64) + 1 + 2 * rank
            >>> local_tensor = torch.unsqueeze(torch.cat([tensor, tensor + 2]))
            >>> local_tensor
            tensor([[1, 2, 3, 4]]) # Rank 0
            tensor([[3, 4, 5, 6]]) # Rank 1
            >>> sharding_dim = 0
            >>> sharding_spec = ChunkShardingSpec(
                    dim=sharding_dim,
                    placements=[
                        "rank:0/cuda:0",
                        "rank:1/cuda:1",
                    ],
                )
            >>> st = ShardedTensor._init_from_local_tensor(local_tensor, sharding_spec, [2, 4])
            >>> st
            ShardedTensor(
                ShardedTensorMetadata(
                    shards_metadata=[
                        ShardMetadata(shard_offsets=[0, 0], shard_sizes=[1, 4], placement=rank:0/cuda:0),
                        ShardMetadata(shard_offsets=[1, 0], shard_sizes=[1, 4], placement=rank:1/cuda:1),
                    ],
                    size=torch.Size([2, 4])
            )
            >>> st.local_tensor()
            tensor([1, 2, 3, 4]) # Rank 0
            tensor([3, 4, 5, 6]) # Rank 1

        Warning: This API is experimental and subject to change. It lacks of a fully across
                 rank validations, and we only validate the local shard on the current rank.
                 We fully rely on the user to ensure local tensor is sharded based on the
                 sharding spec.
        """
        if not local_tensor.is_contiguous():
            raise ValueError('local_tensor is not a contiguous Tensor.')

        global_tensor_size = _flatten_tensor_size(global_size)
        tensor_properties = TensorProperties(
            dtype=local_tensor.dtype,
            layout=local_tensor.layout,
            requires_grad=local_tensor.requires_grad,
            memory_format=torch.contiguous_format,
            pin_memory=local_tensor.is_pinned())
        sharded_tensor_metadata = sharding_spec.build_metadata(
            global_tensor_size,
            tensor_properties
        )

        process_group = (
            process_group
            if process_group is not None
            else distributed_c10d._get_default_group()
        )
        current_rank = dist.get_rank(process_group)

        local_shards: List[Shard] = []
        for shard_metadata in sharded_tensor_metadata.shards_metadata:
            rank, device = _parse_and_validate_remote_device(process_group, shard_metadata.placement)
            if rank == current_rank:
                local_shards.append(Shard(local_tensor, shard_metadata))

        # TODO: figure out what the API should behave when some rank have no shard
        # see https://github.com/pytorch/pytorch/issues/7313
        return ShardedTensor._init_from_local_shards_and_global_metadata(
            local_shards,
            sharded_tensor_metadata,
            process_group=process_group,
            init_rrefs=init_rrefs,
        )

    @classmethod
    def _init_from_local_shards_and_global_metadata(
        cls,
        local_shards: List[Shard],
        sharded_tensor_metadata: ShardedTensorMetadata,
        process_group=None,
        init_rrefs=False,
    ) -> "ShardedTensor":
        """
        Initialize a ShardedTensor with local shards and a global
        ShardedTensorMetadata built on each rank.

        Warning: This API is experimental and subject to change. It does
                 not do cross rank validations, and fully rely on the user
                 for the correctness of sharded_tensor_metadata on each rank
        """
        process_group = (
            process_group
            if process_group is not None
            else distributed_c10d._get_default_group()
        )
        current_rank = dist.get_rank(process_group)

        shards_metadata = sharded_tensor_metadata.shards_metadata
        tensor_properties = sharded_tensor_metadata.tensor_properties

        if len(shards_metadata) == 0:
            raise ValueError("shards_metadata must not be empty!")

        if tensor_properties.layout != torch.strided:
            raise ValueError('Only torch.strided layout is currently supported')

        sharded_tensor = cls.__new__(cls)
        sharded_tensor._prepare_init(process_group=process_group, init_rrefs=init_rrefs)

        sharded_tensor._metadata = sharded_tensor_metadata

        local_shard_metadatas = []

        def _raise_if_mismatch(expected, actual, prop_name, rank, is_property=False):
            tensor_property_or_metadata = "tensor property" if is_property else "local ShardMetadata"
            if expected != actual:
                raise ValueError(f"Local shards' tensor {prop_name} property is incompatible with "
                                 f"{tensor_property_or_metadata} on rank {rank}: "
                                 f"{tensor_property_or_metadata} {prop_name}={expected}, "
                                 f"local shard tensor {prop_name}={actual}.")

        # collect local shard metadatas from the global sharded_tensor_metadata
        for shard_metadata in shards_metadata:  # type: ignore[attr-defined]
            rank, local_device = _parse_and_validate_remote_device(sharded_tensor._process_group, shard_metadata.placement)

            if current_rank == rank:
                local_shard_metadatas.append(shard_metadata)

        if len(local_shards) != len(local_shard_metadatas):
            raise RuntimeError(
                f'Number of local shards ({len(local_shards)}) does not match number of local '
                f'shards metadata in sharded_tensor_metadata ({len(local_shard_metadatas)}) '
                f'on rank ({current_rank}) '
            )

        for shard in local_shards:
            shard_meta = shard.metadata
            local_shard_tensor = shard.tensor
            rank, local_device = _parse_and_validate_remote_device(sharded_tensor._process_group, shard_meta.placement)

            # validate if shard_meta in the metadatas collected from sharded_tensor_metadata
            assert shard_meta in local_shard_metadatas, \
                "local shard metadata not in sharded_tensor_metadata!"

            _raise_if_mismatch(tensor_properties.layout, local_shard_tensor.layout, "layout", current_rank, True)
            if not local_shard_tensor.is_contiguous():
                raise ValueError('Only torch.contiguous_format memory_format is currently supported')

            _raise_if_mismatch(shard_meta.shard_sizes, list(local_shard_tensor.size()), "size", current_rank)
            _raise_if_mismatch(tensor_properties.pin_memory, local_shard_tensor.is_pinned(), "pin_memory", current_rank, True)
            _raise_if_mismatch(local_device, local_shard_tensor.device, "device", current_rank)
            _raise_if_mismatch(tensor_properties.dtype, local_shard_tensor.dtype, "dtype", current_rank, True)
            _raise_if_mismatch(
                tensor_properties.requires_grad, local_shard_tensor.requires_grad, "requires_grad", current_rank, True)

        # check if shards_metadata have overlap shards
        validate_non_overlapping_shards_metadata(shards_metadata)

        # check if the shards_metadata is compatible with overall size of the sharded tensor.
        check_tensor(shards_metadata, list(sharded_tensor_metadata.size))

        # done validation, add local_shards
        sharded_tensor._local_shards = local_shards
        sharded_tensor._sharding_spec = shard_spec._infer_sharding_spec_from_shards_metadata(shards_metadata)

        # run post initialization, i.e. map registration, rpc initialization
        sharded_tensor._post_init()
        return sharded_tensor

    def sharding_spec(self) -> shard_spec.ShardingSpec:
        """
        Returns the ShardingSpec for the tensor.
        """
        return self._sharding_spec

    def reshard(self, resharding_spec: shard_spec.ShardingSpec) -> ShardedTensor:
        """
        Reshard a sharded tensor given the ``resharding_spec``. For now, we only support
        single local shard.

        If ``resharding_spec`` is same as the original one, this becomes a no-op.
        If only ``resharding_spec`` shares the same sharding dim with the original one,
        we swap local shards directly.
        For more generic cases, we merge different shards across different ranks and split
        the local shards based on the ``resharding_spec`` via `all_to_all` collective API.

        Args:
            resharding_spec (:class:`torch.distributed._shard.sharding_spec.ShardingSpec`): The
                specification describing how the tensor is sharded.

        Returns:
            A :class:`ShardedTensor` object whose local shards are resharded.

        Examples:
            >>> # We have 2 process groups, 2 ranks.
            >>> tensor = torch.arange(4, dtype=torch.int64) + 1 + 2 * rank
            >>> tensor = torch.stack([tensor, tensor])
            >>> tensor
            tensor([[1, 2, 3, 4], [1, 2, 3, 4]]) # Rank 0
            tensor([[3, 4, 5, 6], [3, 4, 5, 6]]) # Rank 1
            tensor([[5, 6, 7, 8], [5, 6, 7, 8]]) # Rank 2
            tensor([[7, 8, 9, 10], [7, 8, 9, 10]]) # Rank 3
            >>> sharding_dim = 0
            >>> spec = ChunkShardingSpec(
                    dim=sharding_dim,
                    placements=[
                        "rank:0/cuda:0",
                        "rank:1/cuda:1",
                        "rank:2/cuda:2",
                        "rank:3/cuda:3",
                    ],
                )
            >>> current_offsets = [0] * 2
            >>> current_offsets[0] = rank * 2
            >>> shard_metadata = ShardMetadata(
                    shard_offsets=copy.deepcopy(current_offsets),
                    shard_sizes=tensor.size(),
                    placement=spec.placements[rank],
                )
            >>> local_shards = [
                    Shard(
                        tensor=tensor,
                        metadata=shard_metadata,
                    )
                ]
            >>> st = ShardedTensor._init_from_local_shards(local_shards, tensor.size())
            >>> sharding_dim = 1
            >>> resharding_spec = ChunkShardingSpec(
                    dim=sharding_dim,
                    placements=[
                        "rank:0/cuda:0",
                        "rank:1/cuda:1",
                        "rank:2/cuda:2",
                        "rank:3/cuda:3",
                    ],
                )
            >>> st.reshard(resharding_spec)
            >>> tensor = st.local_shards()[0].tensor
            >>> tensor
            tensor([[1], [1], [3], [3], [5], [5], [7], [7]]) # Rank 0
            tensor([[2], [2], [4], [4], [6], [6], [8], [8]]) # Rank 1
            tensor([[3], [3], [5], [5], [7], [7], [9], [9]]) # Rank 2
            tensor([[4], [4], [6], [6], [8], [8], [10], [10]]) # Rank 3
        """
        if (
            not isinstance(resharding_spec, shard_spec.ChunkShardingSpec) or
            not isinstance(self._sharding_spec, shard_spec.ChunkShardingSpec)
        ):
            raise NotImplementedError("Only ChunkShardingSpec supported for reshard.")
        if (len(self.local_shards()) != 1):
            raise NotImplementedError("Only single local shard supported for reshard.")

        if self._sharding_spec.dim == resharding_spec.dim:  # type: ignore[attr-defined]
            if self._sharding_spec.placements == resharding_spec.placements:  # type: ignore[attr-defined]
                return self
            else:
                local_shards, shards_metadata = reshuffle_local_shard(
                    self.local_tensor(),
                    self.size(),  # type: ignore[arg-type]
                    self._sharding_spec,
                    resharding_spec,
                    self._process_group,
                )
        else:
            local_shards, shards_metadata = reshard_local_shard(
                self.local_tensor(),
                self.size(),  # type: ignore[arg-type]
                self._sharding_spec,
                resharding_spec,
                self._process_group,
            )
        self._local_shards = local_shards
        self._metadata.shards_metadata = shards_metadata
        self._sharding_spec = resharding_spec
        return self

    def local_tensor(self) -> torch.Tensor:
        """
        Return local tensor for a sharded_tensor. For now we only support single local shard.

        Returns:
            A :class:`torch.Tensor` of the local shard.
        """
        if len(self.local_shards()) != 1:
            raise NotImplementedError("Only single local shard is supported.")
        return self.local_shards()[0].tensor

    @classmethod
    def __torch_function__(cls, func, types, args=(), kwargs=None):
        if func in _SHARDED_OPS:
            # Find ShardedTensor instance to get process_group.
            for arg in args:
                if isinstance(arg, ShardedTensor):
                    return _SHARDED_OPS[func](types, args, kwargs, arg._process_group)

            for kwarg in kwargs.values():
                if isinstance(kwarg, ShardedTensor):
                    return _SHARDED_OPS[func](types, args, kwargs, kwarg._process_group)

        raise RuntimeError(
            f"torch function '{func.__name__}', with args: {args} and "
            f"kwargs: {kwargs} not supported for ShardedTensor!")

    def metadata(self) -> ShardedTensorMetadata:
        """
        Returns a :class:`ShardedTensorMetadata` object corresponding to the
        metadata for the entire tensor.
        """
        return self._metadata

    def local_shards(self) -> List[Shard]:
        """
        Returns a list of :class:`Shard' corresponding to the
        local shards for this rank. Returns an empty list if the current rank
        does not host any shards for this Tensor.
        """
        return self._local_shards

    def size(self, dim: int = None) -> Union[torch.Size, int]:
        """
        Returns a :Union:`[torch.Size, int]` which represents the size of the tensor.
            The dimension can be specified.

        Args:
            dim (int, optional): the dimension over which the size represents.
                If specified, it returns the size of the given dimension.
                If not, it returns a subclass of tuple.
                Default: ``None``

        Returns:
            A :Union:`[torch.Size, int]` represents the size of the tensor.
        """
        size = self._metadata.size
        if dim is None:
            return size
        if dim < 0 or dim >= len(size):
            raise ValueError(
                f"Argument ``dim`` must be within the range of tensor dimensions [0, {len(size)})"
            )
        return size[dim]


    def is_pinned(self) -> bool:
        """
        Returns True if the sharded tensor (each local shard) resides in pinned memory.
        """
        return self._metadata.tensor_properties.pin_memory

    def is_contiguous(self) -> bool:
        """
        Returns True if the sharded tensor (each local shard) is contiguous in memory
        in the order specified by memory format.
        """
        return self._metadata.tensor_properties.memory_format == torch.contiguous_format

    @property
    def shape(self):
        return self._metadata.size

    @property
    def requires_grad(self):
        return self._metadata.tensor_properties.requires_grad

    @property
    def dtype(self):
        return self._metadata.tensor_properties.dtype

    @property
    def layout(self):
        return self._metadata.tensor_properties.layout

    def _register_remote_shards(self, remote_shards: List[rpc.RRef[Shard]], rpc_rank: int):
        self._remote_shards[rpc_rank] = remote_shards

    def remote_shards(self) -> Dict[int, List[rpc.RRef[Shard]]]:
        """
        Returns a Dict[int, RRef] with keys being the RPC rank and values
        being RRefs to shards on that rank. Need to initialize the
        RPC framework for this functionality.

        Raises an exception if ShardedTensor was created with ``init_rrefs=False``
        """
        if not self._init_rrefs:
            raise RuntimeError(
                'ShardedTensor created with init_rrefs=False, no RRefs to remote shards available'
            )
        return self._remote_shards

    def __hash__(self):
        return id(self)

    def __repr__(self):
        return f'ShardedTensor({self._metadata})'

    @dataclass
    class ProcessGroupState:
        """
        State for ser-de of process group
        """
        local_rank: int
        global_rank: int
        local_world_size: int
        global_world_size: int

    def __getstate__(self):
        pg_state = ShardedTensor.ProcessGroupState(
            distributed_c10d.get_rank(self._process_group),
            distributed_c10d.get_rank(),
            distributed_c10d.get_world_size(self._process_group),
            distributed_c10d.get_world_size(),
        )

        return self._local_shards, self._metadata, pg_state, self._sharding_spec, self._init_rrefs

    def __setstate__(self, state):
        self._sharded_tensor_id = None
        if not distributed_c10d.is_initialized():
            raise RuntimeError(
                'Need to initialize default process group using '
                '"init_process_group" before loading ShardedTensor')

        self._local_shards, self._metadata, pg_state, self._sharding_spec, self._init_rrefs = state

        # Setup process group
        self._process_group = get_current_process_group()

        # Validate process group.
        local_rank = distributed_c10d.get_rank(self._process_group)
        if pg_state.local_rank != local_rank:
            raise RuntimeError(
                f'Local rank at save time was {pg_state.local_rank}, but at '
                f'load time was {local_rank}')

        global_rank = distributed_c10d.get_rank()
        if pg_state.global_rank != global_rank:
            raise RuntimeError(
                f'Global rank at save time was {pg_state.global_rank}, but at '
                f'load time was {global_rank}')

        local_world_size = distributed_c10d.get_world_size(self._process_group)
        if pg_state.local_world_size != local_world_size:
            raise RuntimeError(
                f'Local world size at save time was {pg_state.local_world_size}, '
                f'but at load time was {local_world_size}')

        global_world_size = distributed_c10d.get_world_size()
        if pg_state.global_world_size != global_world_size:
            raise RuntimeError(
                f'Global world size at save time was {pg_state.global_world_size}, '
                f'but at load time was {global_world_size}')

        self._post_init()


def _create_tensor_from_params(*size, local_device, tensor_properties: TensorProperties):
    """ Helper to construct tensor from size, device and common params. """
    dtype = tensor_properties.dtype
    layout = tensor_properties.layout
    requires_grad = tensor_properties.requires_grad
    memory_format = tensor_properties.memory_format
    pin_memory = tensor_properties.pin_memory

    return torch.empty(
        *size, dtype=dtype, layout=layout,
        device=local_device, requires_grad=requires_grad,
        memory_format=memory_format, pin_memory=pin_memory
    )
