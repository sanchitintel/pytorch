import itertools
import logging
import copy
import numbers
import threading
from functools import lru_cache
from math import prod
from typing import Callable, Dict, List, Sequence

import torch
import torch._C._onednn as llga
from torch._dynamo.utils import detect_fake_mode
from torch._inductor.decomposition import select_decomp_table
from torch.fx import Graph, GraphModule, Node, subgraph_rewriter
from torch.fx.experimental.proxy_tensor import make_fx
from torch.fx.passes.utils.fuser_utils import fuse_by_partitions
from torch.fx.passes.utils.matcher_utils import InternalMatch
from torch.profiler import record_function
from torch.utils._pytree import tree_flatten, tree_map

aten = torch.ops.aten
prims = torch.ops.prims

log = logging.getLogger(__name__)


class OnednnGraph:
    def __init__(self):
        self.graph = llga.graph(llga.engine.cpu)
        self.engine = llga.engine(llga.engine.cpu, 0)
        self.stream = llga.stream(self.engine)
        self.desc_id_to_node_map: dict[int, Node] = {}
        self.desc_to_scalar_data: dict[int, numbers.Number] = {}
        self.dtype_map: dict[torch.dtype, llga.logical_tensor] = {
            torch.float16: llga.logical_tensor.f16,
            torch.bfloat16: llga.logical_tensor.bf16,
            torch.float32: llga.logical_tensor.f32,
            torch.int32: llga.logical_tensor.s32,
            torch.int8: llga.logical_tensor.s8,
            torch.uint8: llga.logical_tensor.u8,
            torch.bool: llga.logical_tensor.boolean,
            torch.int64: llga.logical_tensor.dt_undef,
            torch.float64: llga.logical_tensor.dt_undef,
            torch.complex64: llga.logical_tensor.dt_undef,
        }
        self.reverse_dtype_map: dict[llga.logical_tensor, torch.dtype] = {
            self.dtype_map[key]: key for key in self.dtype_map
        }
        self.current_id = 0
        self.desc_id_to_queried_desc = {}
        self.desc_ids_with_any_layout = set()
        self.partitions = None
        self.is_inference = False

    def get_partitions(self, policy=llga.partition.fusion):
        if self.partitions:
            return self.partitions
        self.partitions = self.graph.get_partitions(policy)
        self.set_any_layout()
        return self.partitions

    # based on
    # https://github.com/oneapi-src/oneDNN/blob/6dbeffbae1f23cbbeae17adb7b5b13f1f37c080e/tests/benchdnn/graph/helpers_any_layout.hpp#L29-L96
    # Set tensor layout to "any" when used only by LLGA partitions
    def set_any_layout(self, partitions=None):
        if partitions is None:
            partitions = self.partitions
        self.desc_ids_with_any_layout = set()
        # map from output id to all supported flags of supported partitions
        output_to_flag_map = {}

        # Initialize map of supported tensors
        for p in self.partitions:
            p_is_supported = p in partitions and p.is_supported()
            for out_desc in p.get_output_ports():
                id = out_desc.get_id()
                if p_is_supported and id not in output_to_flag_map:
                    output_to_flag_map[id] = []
            for in_desc in p.get_input_ports():
                id = in_desc.get_id()
                if id in output_to_flag_map:
                    output_to_flag_map[id].append(p_is_supported)
        for p in partitions:
            if not p.is_supported():
                continue
            for in_desc in p.get_input_ports():
                id = in_desc.get_id()
                if id not in output_to_flag_map:
                    continue
                flags = output_to_flag_map[id]
                # if all uses of in_desc are supported use "any"
                if all(flags):
                    self.desc_ids_with_any_layout.add(id)

    def update_input_descs(
        self,
        descs: List[llga.logical_tensor],
        aten_inputs: List[torch.Tensor],
        cache_weight: bool = False,
    ):
        assert len(descs) == len(aten_inputs)
        for i, (d, at_in) in enumerate(zip(descs, aten_inputs)):
            make_constant = cache_weight and isinstance(at_in, torch.nn.Parameter)
            property_type = (
                llga.logical_tensor.property_type.constant
                if make_constant
                else llga.logical_tensor.property_type.variable
            )
            descs[i] = llga.logical_tensor(
                d.get_id(),
                d.get_data_type(),
                at_in.size(),
                at_in.stride(),
                property_type,
            )
        return descs

    def get_compiled_output_descs(
        self, cp: llga.compiled_partition, out_descs: List[llga.logical_tensor]
    ):
        return [cp.query_logical_tensor(desc.get_id()) for desc in out_descs]

    def compile_partition(self, p: llga.partition, inputs: List[llga.logical_tensor]):
        outputs = p.get_output_ports()
        for i, input in enumerate(inputs):
            id = input.get_id()
            if id in self.desc_id_to_queried_desc:
                inputs[i] = self.desc_id_to_queried_desc[id]
        for i, output in enumerate(outputs):
            id = output.get_id()
            if id in self.desc_ids_with_any_layout:
                outputs[i] = llga.logical_tensor(
                    id,
                    output.get_data_type(),
                    output.get_dims(),
                    llga.logical_tensor.layout_type.any,
                    output.get_property_type(),
                )
        cpart = p.compile(inputs, outputs, self.engine)
        for i, output in enumerate(outputs):
            id = output.get_id()
            outputs[i] = cpart.query_logical_tensor(id)
            self.desc_id_to_queried_desc[id] = outputs[i]
        return cpart

    def add_op(self, op_kind, name, inputs, outputs=None, kwargs=None):
        if outputs is None:
            outputs = []
        if outputs:
            id = outputs[0].get_id()
        else:
            id = self.generate_id()
        op = llga.op(id, op_kind, name)
        if kwargs is not None:
            for attr_key in kwargs:
                if isinstance(attr_key, str):
                    if hasattr(llga.op, attr_key):
                        op.set_attributes(getattr(llga.op, attr_key), kwargs[attr_key])
                else:
                    op.set_attributes(attr_key, kwargs[attr_key])
        op.add_inputs(inputs)
        if outputs:
            op.add_outputs(outputs)
        self.graph.add_op(op, True)
        return id

    def generate_id(self):
        id = self.current_id
        self.current_id += 1
        return id

    def create_onednn_descs_from_node(
        self, node: Node, ptype=llga.logical_tensor.property_type.variable
    ) -> List[llga.logical_tensor]:
        assert node.op in ["placeholder", "call_function"]
        if isinstance(node.meta["val"], (list, tuple)):
            outputs = [
                self.create_onednn_desc_from_meta(val, ptype)
                for val in node.meta["val"]
            ]
        else:
            outputs = [self.create_onednn_desc_from_meta(node.meta["val"], ptype)]
        for desc in outputs:
            self.register_node_by_desc(node, desc)
        return outputs

    def create_onednn_desc_from_meta(
        self, val, ptype=llga.logical_tensor.property_type.variable
    ) -> llga.logical_tensor:
        if isinstance(val, torch.SymInt):
            dtype = llga.logical_tensor.dt_undef
            size = [1]
            stride = [1]
        else:
            dtype = self.dtype_map[val.dtype]
            size = list(val.size())
            stride = list(val.stride())
        # TODO: workaround to reset stride due to oneDNN bug
        # for cases stride is larger than number of elements in tensor
        # and in decreasing order (contiguous)
        if (
            len(size)
            and size[0] == 1
            and stride[0] > prod(size)
            and stride == sorted(stride, reverse=True)
        ):
            stride[0] = prod(size)
        onednn_desc = llga.logical_tensor(
            self.generate_id(), dtype, size, stride, ptype
        )
        return onednn_desc

    def create_onednn_desc_from_scalar(self, scalar, dtype=None) -> llga.logical_tensor:
        fake_tensor = torch.tensor([scalar], dtype=dtype)
        desc = self.create_onednn_desc_from_meta(
            fake_tensor, ptype=llga.logical_tensor.property_type.constant
        )
        self.register_scalar_data(scalar, desc, dtype)
        return desc

    def overwrite_scalar_args(self, args, cast_scalar=True):
        args = list(args)
        if cast_scalar:
            assert len(args) == 2
            assert any(isinstance(arg, llga.logical_tensor) for arg in args)
            for arg in args:
                if isinstance(arg, llga.logical_tensor):
                    torch_type = self.reverse_dtype_map[arg.get_data_type()]
                    break
        for arg_idx, arg in enumerate(args):
            # We only handle scalars, not lists of constant scalars
            if isinstance(arg, numbers.Number):
                # arg is a scalar value, we get a logical_tensor of shape=()
                cast_type = torch_type if cast_scalar else None
                args[arg_idx] = self.create_onednn_desc_from_scalar(arg, cast_type)
        return args

    def register_node_by_desc(self, node, desc):
        if isinstance(desc, llga.logical_tensor):
            desc = desc.get_id()
        self.desc_id_to_node_map[desc] = node

    def get_node_from_desc(self, desc):
        if isinstance(desc, llga.logical_tensor):
            desc = desc.get_id()
        return self.desc_id_to_node_map[desc]

    def register_scalar_data(self, data, desc, dtype=None):
        if isinstance(desc, llga.logical_tensor):
            desc = desc.get_id()
        self.desc_to_scalar_data[desc] = (data, dtype)

    def get_scalar_data_from_desc(self, desc):
        if isinstance(desc, llga.logical_tensor):
            desc = desc.get_id()
        return self.desc_to_scalar_data[desc]

    def get_args_to_onednn_partition_order(self, onednn_partition, node_args):
        onednn_input_names = [
            in_desc.get_id()
            if in_desc.get_id() in self.desc_to_scalar_data.keys()
            else self.get_node_from_desc(in_desc).name
            for in_desc in onednn_partition.get_input_ports()
        ]
        arg_names = [n.name for n in node_args]

        # Get the index in args if arg exists in args, otherwise get the relevant scalar data from graph
        args_to_onednn_order = []
        for name in onednn_input_names:
            if name in arg_names:
                args_to_onednn_order.append(arg_names.index(name))
            else:
                scalar, dtype = self.get_scalar_data_from_desc(name)
                scalar = torch.tensor([scalar], dtype=dtype)
                if hasattr(scalar, "constant"):
                    scalar = scalar.constant
                args_to_onednn_order.append(scalar)
        return args_to_onednn_order


def onednn_graph_fuse_fx(gm: GraphModule, is_inference: bool):
    log.info("Compiling graph with oneDNN backend")

    rewrite_graph(gm)
    log.debug("Build oneDNN graph")
    onednn_graph = build_onednn_graph(gm)
    onednn_graph.is_inference = is_inference
    log.debug("Fuse fx graph based on oneDNN graph partitions")
    fuse_graph(gm, onednn_graph)
    log.debug("Re-apply Inductor Decomps after fusion for any un-lowered ops")
    reapply_decomps(gm)

    log.info("Finished compiling graph with oneDNN backend")
    log.debug("====== Fx Graph after oneDNN compile ======")
    log.debug(gm.print_readable(print_output=False))
    return gm


onednn_graph_intensive_ops = [
    aten.addmm.default,
    aten.avg_pool2d.default,
    aten.bmm.default,
    aten.batch_norm.default,
    aten.baddbmm.default,
    aten.convolution.default,
    aten.layer_norm.default,
    aten.max_pool2d.default,
    aten.max_pool3d.default,
    aten.mm.default,
]


def get_filtered_partitions(
    onednn_graph: OnednnGraph, allowed_ops=onednn_graph_intensive_ops
):
    """
    Get onednn Graph partitions which include at least one op which is in allowed_ops.
    By default this function will return partitions which are intensive,
    i.e. defined in onednn_graph_intensive_ops and known to have performant onednn code.

    Args:
        ``onednn_graph``: The OnednnGraph containing lowered ops to partition
        ``allowed_ops``: The list of aten.ops which will allow a partition to be lowered

    Returns:
        ``filtered_partitions``: List[llga.partition]: A list of partitions
        which contain at least one intensive op
        ``filtered_node_lists``: List[List[Node]]: A list of Nodes in the
        fx Graph which correspond to each partition
    """

    onednn_graph_partitions = onednn_graph.get_partitions()
    supported_partitions = [lp for lp in onednn_graph_partitions if lp.is_supported()]
    node_lists = [
        [onednn_graph.get_node_from_desc(id) for id in lp.get_ops()]
        for lp in supported_partitions
    ]

    if not allowed_ops:
        return supported_partitions, node_lists

    filtered_partitions = []
    filtered_node_lists = []
    for partition, nodes in zip(supported_partitions, node_lists):
        if len(nodes) >= 2 or any(node.target in allowed_ops for node in nodes):
            filtered_partitions.append(partition)
            filtered_node_lists.append(nodes)
    # Need to reset descs with any_layout since set of supported partitions is different
    onednn_graph.set_any_layout(filtered_partitions)
    return filtered_partitions, filtered_node_lists


@lru_cache(maxsize=None)
def allocate_empty_aten_from_desc(desc: llga.logical_tensor) -> torch.Tensor:
    return torch.empty_strided(desc.get_dims(), desc.get_strides())


class OnednnGraphPartitionModule:
    def __init__(
        self,
        onednn_graph: OnednnGraph,
        partition: llga.partition,
        input_order_data: List = None,
        name="",
    ):
        super().__init__()
        self.is_opaque = True
        self.__name__ = name
        self.partition = partition
        self.onednn_graph = onednn_graph
        self.input_order_data = [] if input_order_data is None else input_order_data
        self.input_descs = partition.get_input_ports()
        self.kernel = None
        self.output_descs = None

        # assume static shape
        # cache the onednn graph tensors
        self.input_onednn_tensors = []
        self.output_onednn_tensors = []
        # cache the output tensors to avoid reallocation
        self.output_tensors = []

        self.lock = threading.Lock()

    def name(self):
        return self.__name__

    @record_function("OnednnGraphPartitionModule__call__")
    def __call__(self, *args):
        # If val is an int, then it gives the index of args, otherwise it is a scalar tensor so we use as-is
        input_tensors = [
            args[val] if isinstance(val, int) else val for val in self.input_order_data
        ]

        # TODO: remove detect_fake_mode with an meta impl
        #fake_mode = detect_fake_mode(args)
        fake_mode = isinstance(args[0], torch._subclasses.fake_tensor.FakeTensor)
        if fake_mode:
            input_descs = self.onednn_graph.update_input_descs(
                self.input_descs, input_tensors
            )
            compiled_partition = self.onednn_graph.compile_partition(
                self.partition, input_descs
            )
            output_descs = self.onednn_graph.get_compiled_output_descs(
                compiled_partition, self.partition.get_output_ports()
            )
            output_tensors = [
                torch.empty_strided(out_desc.get_dims(), out_desc.get_strides())
                for out_desc in output_descs
            ]
            with self.lock:
                if not self.kernel:
                    cache_parameter = self.onednn_graph.is_inference
                    '''self.input_descs = self.onednn_graph.update_input_descs(
                        self.input_descs, input_tensors, cache_parameter
                    )'''
                    self.kernel = self.onednn_graph.compile_partition(
                        self.partition, self.input_descs
                    )
                    self.output_descs = self.onednn_graph.get_compiled_output_descs(
                        self.kernel,
                        self.partition.get_output_ports()
                    )
            return output_tensors[0] if len(output_tensors) == 1 else output_tensors

        if not self.input_onednn_tensors:
            self.input_onednn_tensors = [
                llga.tensor(
                    input_desc, self.onednn_graph.engine, input_tensor.data_ptr()
                )
                for input_desc, input_tensor in zip(self.input_descs, input_tensors)
            ]
        else:
            for onednn_t, aten_t in zip(self.input_onednn_tensors, input_tensors):
                onednn_t.from_aten(aten_t.data_ptr())


        if not self.output_tensors:
            self.output_tensors = [
                allocate_empty_aten_from_desc(out_desc) for out_desc in self.output_descs
            ]

        if not self.output_onednn_tensors:
            self.output_onednn_tensors = [
                llga.tensor(
                    output_desc, self.onednn_graph.engine, self.output_tensor.data_ptr()
                )
                for output_desc, self.output_tensor in zip(
                    self.output_descs, self.output_tensors
                )
            ]

        assert not any(
            isinstance(out, torch._subclasses.FakeTensor) for out in self.output_tensors
        ), "unexpected faketensor in output_tensors"

        with record_function(f"onednn_fuse_{self.__name__}"):
            self.kernel.execute(
                self.onednn_graph.stream,
                self.input_onednn_tensors,
                self.output_onednn_tensors,
            )
        # TODO: It seems like this fix and also the return statements of def call_function should be handled differently.
        return (
            self.output_tensors[0]
            if len(self.output_tensors) == 1
            else self.output_tensors
        )


def build_onednn_graph(gm: GraphModule) -> OnednnGraph:
    onednn_graph = OnednnGraph()

    graph_input_nodes = list(filter(lambda n: n.op == "placeholder", gm.graph.nodes))

    class FusionInterpreter(torch.fx.Interpreter):
        def run_node(self, node):
            self.current_node = node
            return super().run_node(node)

        def placeholder(self, target, args, kwargs):
            # Add an input to graph
            return onednn_graph.create_onednn_descs_from_node(self.current_node)[0]

        def call_function(self, target, args, kwargs):
            # With placeholder defined, args is always a tuple of logical tensors except for the case of scalars
            if not isinstance(target, torch._ops.OpOverload) and "_mkl_" not in self.current_node.name:
                res = target(*args)
                if "getitem" in self.current_node.name and isinstance(
                    res, llga.logical_tensor
                ):
                    out_descs = onednn_graph.create_onednn_descs_from_node(
                        self.current_node
                    )
                    onednn_graph.add_op(
                        llga.op.Wildcard, self.current_node.name, [res], out_descs
                    )
                    return out_descs[0]
                return res

            '''modified_args = list(copy.copy(args))

            # Create & replace logical tensors for constant tensor inputs
            if target.name() in ["aten::convolution", "aten::batch_norm", "aten::addmm", "aten::bmm", "aten::mm"]:
                # index of weight as per op schema in native_functions.yaml
                weight_index = 1
                if target.name == "aten::addmm":
                    weight_index = 2

                weight_node = onednn_graph.get_node_from_desc(args[weight_index])

                # we are in inference mode
                args_to_replace = [weight_index]
                # we might need to replace more logical tensors
                args_to_check = []
                if target.name() == "aten::convolution":
                    args_to_check = [2]
                elif target.name() == "aten::batch_norm":
                    args_to_check = [2, 3]
                elif target.name() == "aten::addmm":
                    args_to_check.append(0)

                if args_to_check is not None:
                    for index in args_to_check:
                            if not args[index] is None:
                                args_to_replace.append(index)

                    for index in args_to_replace:
                        lt_to_replace = args[index]
                        node = onednn_graph.get_node_from_desc(lt_to_replace)
                        modified_args[index] = llga.logical_tensor(
                            onednn_graph.generate_id(),
                            lt_to_replace.get_data_type(),
                            lt_to_replace.shape,
                            lt_to_replace.get_strides(),
                            llga.logical_tensor.property_type.constant
                        )
                        onednn_graph.register_node_by_desc(node, modified_args[index])

            modified_args = tuple(modified_args)'''

            out_descs = onednn_graph.create_onednn_descs_from_node(self.current_node)

            if target not in lowerings or not self._is_valid_lowering(
                target, args, out_descs
            ):
                onednn_graph.add_op(
                    llga.op.Wildcard,
                    self.current_node.name,
                    [
                        arg
                        for arg in tree_flatten(args)[0]
                        if isinstance(arg, llga.logical_tensor)
                    ],
                    out_descs,
                )
            else:
                # target in lowerings, add node to onednn_graph
                lowerings[target](
                    onednn_graph, self.current_node.name, args, out_descs, dict(kwargs)
                )
            if isinstance(self.current_node.meta["val"], Sequence):
                return out_descs
            return out_descs[0]

        def output(self, target, args, kwargs):
            flat_out, _ = tree_flatten(args[0])
            for i, output_logten in enumerate(flat_out):
                if isinstance(
                    output_logten, llga.logical_tensor
                ) and output_logten.get_data_type() in [
                    llga.logical_tensor.f32,
                    llga.logical_tensor.f16,
                    llga.logical_tensor.bf16,
                ]:
                    endop_id = onednn_graph.add_op(
                        llga.op.End,
                        self.current_node.name + str(output_logten.get_id()),
                        [output_logten],
                        [],
                    )
                    onednn_graph.register_node_by_desc(self.current_node, endop_id)

        def _is_valid_lowering(self, target, args, out_descs):
            valid_types = [
                llga.logical_tensor.f32,
                llga.logical_tensor.f16,
                llga.logical_tensor.bf16,
            ]

            if (
                (target == aten.layer_norm and len(args[1]) > 1)
                or (target == aten.batch_norm and None in args[:5])
                or any(
                    arg.get_data_type() not in valid_types
                    for arg in args
                    if isinstance(arg, llga.logical_tensor)
                )
                or out_descs[0].get_data_type() not in valid_types
            ):
                return False
            return True

    onednn_graph_args = tuple(graph_input_nodes)
    FusionInterpreter(gm).run(*onednn_graph_args)
    onednn_graph.graph.finalize()
    return onednn_graph


def fuse_graph(gm: GraphModule, onednn_graph: OnednnGraph) -> GraphModule:
    supported_partitions, node_lists = get_filtered_partitions(onednn_graph)

    if len(node_lists) == 0:
        return

    gm = fuse_by_partitions(gm, node_lists)

    for node in gm.graph.nodes:
        if node.op == "call_module" and "fused_" in node.name:
            partition_idx = int(node.name.split("fused_")[1])
            current_partition = supported_partitions[partition_idx]
            current_partition_nodes = node_lists[partition_idx]
            current_partition_ext_name = "_".join(
                [n.name for n in current_partition_nodes]
            )

            out_descs = current_partition.get_output_ports()
            if len(out_descs) == 1:
                onednn_graph.register_node_by_desc(node, out_descs[0])
            else:
                getitem_users = [
                    node for node in node.users.keys() if "getitem" in node.name
                ]
                for getitem in getitem_users:
                    ind = getitem.args[1]
                    onednn_graph.register_node_by_desc(getitem, out_descs[ind])

            args_to_onednn_order = onednn_graph.get_args_to_onednn_partition_order(
                current_partition, node.args
            )

            gm.delete_submodule(node.target)
            node.name = f"onednn_{node.name}"
            node.op = "call_function"
            node.target = OnednnGraphPartitionModule(
                onednn_graph,
                current_partition,
                input_order_data=args_to_onednn_order,
                name=current_partition_ext_name,
            )
            log.info("Using oneDNN fusion: %s", current_partition_ext_name)

    gm.recompile()
    return gm


def replace_pattern_with_replacement(
    gm: GraphModule,
    pattern: Callable,
    replacement: Callable,
    filters: List[Callable[[InternalMatch, Graph, Graph], bool]] = None,
):
    matches_and_replacements = subgraph_rewriter.replace_pattern_with_filters(
        gm, pattern, replacement, filters
    )
    # We need to reassign the meta (FakeTensor) from old nodes to the new
    for match in matches_and_replacements:
        if match.replacements:
            old_meta = match.nodes_map[match.anchor].meta
            nodes = [
                node
                for node in match.replacements
                if isinstance(node.target, torch._ops.OpOverload)
            ]
            nodes[0].meta = old_meta
    return matches_and_replacements


def replace_t_matmul_to_matmul(gm: GraphModule):
    def matmul_patterns_generator(func, transpose_a, transpose_b):
        # Need to check single/double transpose cases separately since number of args differs
        def single_addmm_pattern(bias, mat1, mat2, inds1):
            return func(
                bias,
                aten.permute.default(mat1, inds1) if transpose_a else mat1,
                aten.permute.default(mat2, inds1) if transpose_b else mat2,
            )

        def single_addmm_replacement(bias, mat1, mat2, inds1):
            return func(
                bias, mat1, mat2, transpose_a=transpose_a, transpose_b=transpose_b
            )

        def double_addmm_pattern(bias, mat1, mat2, inds1, inds2):
            return func(
                bias,
                aten.permute.default(mat1, inds1) if transpose_a else mat1,
                aten.permute.default(mat2, inds2) if transpose_b else mat2,
            )

        def double_addmm_replacement(bias, mat1, mat2, inds1, inds2):
            return func(
                bias, mat1, mat2, transpose_a=transpose_a, transpose_b=transpose_b
            )

        def single_mm_pattern(mat1, mat2, inds1):
            return func(
                aten.permute.default(mat1, inds1) if transpose_a else mat1,
                aten.permute.default(mat2, inds1) if transpose_b else mat2,
            )

        def single_mm_replacement(mat1, mat2, inds1):
            return func(mat1, mat2, transpose_a=transpose_a, transpose_b=transpose_b)

        def double_mm_pattern(mat1, mat2, inds1, inds2):
            return func(
                aten.permute.default(mat1, inds1) if transpose_a else mat1,
                aten.permute.default(mat2, inds2) if transpose_b else mat2,
            )

        def double_mm_replacement(mat1, mat2, inds1, inds2):
            return func(mat1, mat2, transpose_a=transpose_a, transpose_b=transpose_b)

        if func == aten.addmm.default:
            if transpose_a and transpose_b:
                return double_addmm_pattern, double_addmm_replacement
            else:
                return single_addmm_pattern, single_addmm_replacement
        else:
            if transpose_a and transpose_b:
                return double_mm_pattern, double_mm_replacement
            else:
                return single_mm_pattern, single_mm_replacement

    def ind_filter(match: InternalMatch, graph1: Graph, graph2: Graph):
        # If transpose inds are the last two dims, replacement is valid
        transpose_nodes = [
            match.nodes_map[n]
            for n in match.nodes_map
            if hasattr(n, "target") and n.target == aten.permute.default
        ]
        for node in transpose_nodes:
            ndims = len(node.meta["val"].shape)
            inds = node.args[1]
            for i, ind in enumerate(inds[:-2]):
                if i != ind:
                    return False
            # Handle negative inds using %
            min_ind = min(node.args[1][-2:]) % ndims
            max_ind = max(node.args[1][-2:]) % ndims
            if min_ind != ndims - 2 or max_ind != min_ind + 1:
                return False
        return True

    matches = []
    for func, t1, t2 in itertools.product(
        [
            aten.addmm.default,
            aten.mm.default,
            aten.bmm.default,
        ],
        [True, False],
        [True, False],  # transpose a, b
    ):
        if not t1 and not t2:
            continue
        pat, rep = matmul_patterns_generator(func, t1, t2)
        matches += replace_pattern_with_replacement(gm, pat, rep, [ind_filter])

    return matches


def allow_manydim_bmm(gm: GraphModule):
    # Pytorch bmm only allows 3 dims, but MatMul in oneDNN Graph allows
    # an arbitrary number of dims, so we can simplify the graph to match
    # existing semi-compiler patterns
    def pattern_generator(view_func1, view_func2, view_func3):
        def bmm_pattern(input1, shape1, input2, shape2, shape3):
            x1 = view_func1(input1, shape1)
            x2 = view_func2(input2, shape2)
            x3 = aten.bmm.default(x1, x2)
            return view_func3(x3, shape3)

        return bmm_pattern

    def bmm_replacement(input1, shape1, input2, shape2, shape3):
        return aten.bmm.default(input1, input2)

    def view_filter(match: InternalMatch, graph1: Graph, graph2: Graph):
        # Replacement is valid if view ops are all not changing last two dims, so only batch changes.
        after_view_node = match.nodes_map[match.anchors[0]]
        before_view_node0 = after_view_node.args[0].args[0]
        before_view_node1 = after_view_node.args[0].args[1]
        valid_match = True
        for node in [after_view_node, before_view_node0, before_view_node1]:
            valid_match &= node.args[0].meta["val"].shape[-2:] == torch.Size(
                node.args[1][-2:]
            )
        return valid_match

    matches = []
    view_ops = [
        aten.view.default,
        aten.reshape.default,
    ]
    for func1 in view_ops:
        for func2 in view_ops:
            for func3 in view_ops:
                matches += replace_pattern_with_replacement(
                    gm,
                    pattern_generator(func1, func2, func3),
                    bmm_replacement,
                    [view_filter],
                )
    return matches


def _redundant_filter(match: InternalMatch, graph1: Graph, graph2: Graph):
    # Return true if match is "redundant", ie output has same shape and stride as input
    node = match.nodes_map[match.anchors[0]]
    return (
        node.args[0].meta["val"].shape == node.meta["val"].shape
        and node.args[0].meta["val"].stride() == node.meta["val"].stride()
    )


def remove_redundant_expand(gm: GraphModule):
    def expand_pattern(input, shape):
        return aten.expand.default(input, shape)

    def expand_replacement(input, shape):
        return input

    return replace_pattern_with_replacement(
        gm, expand_pattern, expand_replacement, [_redundant_filter]
    )


def replace_max_pool_with_indices(
    gm: torch.fx.GraphModule, num_dims=2
) -> torch.fx.GraphModule:
    assert num_dims in [2, 3]
    default_args = [None, None, None, [0] * num_dims, [1] * num_dims, False]
    pattern_func = (
        aten.max_pool2d_with_indices.default
        if num_dims == 2
        else aten.max_pool3d_with_indices.default
    )
    replace_func = aten.max_pool2d.default if num_dims == 2 else aten.max_pool3d.default

    def pattern3(self, kernel_size, stride):
        return pattern_func(self, kernel_size, stride)[0]

    def pattern4(self, kernel_size, stride, padding):
        return pattern_func(self, kernel_size, stride, padding)[0]

    def pattern5(self, kernel_size, stride, padding, dilation):
        return pattern_func(self, kernel_size, stride, padding, dilation)[0]

    def pattern6(self, kernel_size, stride, padding, dilation, ceil_mode):
        return pattern_func(self, kernel_size, stride, padding, dilation, ceil_mode)[0]

    def replacement3(self, kernel_size, stride):
        return replace_func(
            self, kernel_size, stride, default_args[3], default_args[4], default_args[5]
        )

    def replacement4(self, kernel_size, stride, padding):
        return replace_func(
            self, kernel_size, stride, padding, default_args[4], default_args[5]
        )

    def replacement5(self, kernel_size, stride, padding, dilation):
        return replace_func(
            self, kernel_size, stride, padding, dilation, default_args[5]
        )

    def replacement6(self, kernel_size, stride, padding, dilation, ceil_mode):
        return replace_func(self, kernel_size, stride, padding, dilation, ceil_mode)

    matches = []
    matches += replace_pattern_with_replacement(gm, pattern3, replacement3)
    matches += replace_pattern_with_replacement(gm, pattern4, replacement4)
    matches += replace_pattern_with_replacement(gm, pattern5, replacement5)
    matches += replace_pattern_with_replacement(gm, pattern6, replacement6)
    return matches


def rewrite_graph(gm):
    # Remove expand nodes that have identical input and output shape
    remove_redundant_expand(gm)
    # Replace view(a) + view(b) + bmm(a,b) + view() with a N-dim call to MatMul
    allow_manydim_bmm(gm)
    # Replace (transpose + (add/b)mm) with single call to addmm with kwarg "transpose_=True" to integrate with oneDNN API
    replace_t_matmul_to_matmul(gm)
    # Replace aten::max_pool{2,3}d_with_indices to aten::max_pool{2,3}d because
    # MaxPool currently doesn't support with indices
    replace_max_pool_with_indices(gm, num_dims=2)
    replace_max_pool_with_indices(gm, num_dims=3)


def replace_node_with_subgraph(node: Node, subgraph: Graph):
    val_map: Dict[Node, Node] = {}
    node_inputs = [arg for arg in node.args if isinstance(arg, Node)]
    replacement_placeholders = [n for n in subgraph.nodes if n.op == "placeholder"]
    for ni, rp in zip(node_inputs, replacement_placeholders):
        val_map[rp] = ni
    [
        subgraph.erase_node(n)
        for n in reversed(subgraph.nodes)
        if len(n.users) == 0 and n.op != "output"
    ]
    with node.graph.inserting_before(node):
        copied_returning_node = node.graph.graph_copy(subgraph, val_map)
    if not isinstance(copied_returning_node, Node):
        copied_returning_node = copied_returning_node[0]
    node.replace_all_uses_with(copied_returning_node)
    node.graph.erase_node(node)


def reapply_decomps(gm: GraphModule):
    all_decomps = select_decomp_table()
    search_decomps = {
        key: all_decomps[key] for key in (lowerings.keys() & all_decomps.keys())
    }
    for node in gm.graph.nodes:
        if node.op == "call_function" and node.target in search_decomps:
            args = [
                arg.meta["val"] if hasattr(arg, "meta") and "val" in arg.meta else arg
                for arg in node.args
            ]
            replacement = make_fx(node.target, all_decomps)(*args)
            replace_node_with_subgraph(node, replacement.graph)
    gm.graph.eliminate_dead_code()
    gm.recompile()


# define lowerings from aten to oneDNN
lowerings = {}


def register_lowering(aten_op_packet):
    def decorator(fn):
        def _register_lowering(aten_op):
            if isinstance(aten_op, torch._ops.OpOverloadPacket):
                overload_funcs = [
                    getattr(aten_op, name) for name in aten_op.overloads()
                ]
            elif isinstance(aten_op, torch._ops.OpOverload):
                overload_funcs = [aten_op]
            else:
                raise RuntimeError(f"Attempting to lower unsupported op {aten_op}")
            for overload_fn in overload_funcs:
                if overload_fn in lowerings:
                    raise RuntimeError(
                        f"Already registered for {overload_fn.name()} in lowerings"
                    )
                lowerings[overload_fn] = fn

        tree_map(_register_lowering, aten_op_packet)
        return fn

    return decorator


class LoweringConfig:
    def __init__(
        self,
        llga_op,
        in_descs_filtering=None,
        attribute_inputs=None,
        scalar_in_descs=False,
    ):
        """
        Map aten op to llga op for lowering.

        Args:
            ``llga_op``: The llga Op for lowering
            ``in_descs_filtering``: A lambda function which is used to reorder or filter
            the input logical tensors `in_descs`. For example,
            `lambda in_descs: [in_descs[1], in_descs[2], in_descs[0]]` could be used to
            reorder the inputs. For convenience, the string "first" can be used in place of
            `lambda in_descs: in_descs[:1]`
            ``attribute_inputs``: A dictionary with llga op attributes as keys, and
            lambda functions to retrieve the value from the input logical tensors `in_descs`
            ``scalar_in_descs``: bool, True if scalar inputs should be cast to match other input types
        """
        self.llga_op = llga_op
        self.in_descs_filtering_func = in_descs_filtering
        if in_descs_filtering == "first":
            self.in_descs_filtering_func = lambda in_descs: in_descs[:1]
        self.attribute_inputs = {} if attribute_inputs is None else attribute_inputs
        self.scalar_in_descs = scalar_in_descs


_lowerings_map = {
    aten.add: LoweringConfig(llga.op.Add, scalar_in_descs=True),
    aten.addmm: LoweringConfig(
        llga.op.MatMul, lambda in_descs: [in_descs[1], in_descs[2], in_descs[0]]
    ),
    aten.avg_pool2d: LoweringConfig(
        llga.op.AvgPool,
        "first",
        attribute_inputs={
            llga.op.strides: lambda in_descs: in_descs[2]
            if len(in_descs) >= 3
            else in_descs[1],
            llga.op.pads_begin: lambda in_descs: in_descs[3]
            if len(in_descs) >= 4
            else [0, 0],
            llga.op.pads_end: lambda in_descs: in_descs[3]
            if len(in_descs) >= 4
            else [0, 0],
            llga.op.rounding_type: lambda in_descs: (
                "ceil" if len(in_descs) == 6 and in_descs[5] else "floor"
            ),
            llga.op.exclude_pad: False,
            llga.op.kernel: lambda in_descs: in_descs[1],
            llga.op.data_format: "NCX",
        },
    ),
    aten.bmm: LoweringConfig(llga.op.MatMul),
    aten.clone: LoweringConfig(llga.op.Reorder),
    aten.div: LoweringConfig(llga.op.Divide, scalar_in_descs=True),
    aten.elu: LoweringConfig(
        llga.op.Elu,
        "first",
        attribute_inputs={llga.op.alpha: lambda in_descs: in_descs[1]},
    ),
    aten.gelu: LoweringConfig(llga.op.GELU),
    aten.hardsigmoid: LoweringConfig(
        llga.op.HardSigmoid, attribute_inputs={llga.op.alpha: 1 / 6, llga.op.beta: 0.5}
    ),
    aten.hardswish: LoweringConfig(llga.op.HardSwish),
    aten.hardtanh: LoweringConfig(
        llga.op.Clamp,
        "first",
        attribute_inputs={
            llga.op.min: lambda in_descs: float(in_descs[1]),
            llga.op.max: lambda in_descs: float(in_descs[2]),
        },
    ),
    aten.leaky_relu: LoweringConfig(
        llga.op.LeakyReLU,
        "first",
        attribute_inputs={
            llga.op.alpha: lambda in_descs: 0.01 if len(in_descs) == 1 else in_descs[1]
        },
    ),
    aten.mish: LoweringConfig(llga.op.Mish),
    aten.max_pool2d: LoweringConfig(
        llga.op.MaxPool,
        "first",
        attribute_inputs={
            llga.op.strides: lambda in_descs: in_descs[2],
            llga.op.pads_begin: lambda in_descs: in_descs[3]
            if len(in_descs) >= 4
            else [0, 0],
            llga.op.pads_end: lambda in_descs: in_descs[3]
            if len(in_descs) >= 4
            else [0, 0],
            llga.op.kernel: lambda in_descs: in_descs[1],
            llga.op.rounding_type: lambda in_descs: (
                "ceil" if len(in_descs) == 6 and in_descs[5] else "floor"
            ),
            llga.op.dilations: lambda in_descs: in_descs[4]
            if len(in_descs) >= 5
            else [1, 1],
            llga.op.data_format: "NCX",
        },
    ),
    aten.mm: LoweringConfig(llga.op.MatMul),
    aten.mul: LoweringConfig(llga.op.Multiply, scalar_in_descs=True),
    aten.permute: LoweringConfig(
        llga.op.StaticTranspose,
        "first",
        attribute_inputs={llga.op.order: lambda in_descs: in_descs[1]},
    ),
    aten.relu: LoweringConfig(llga.op.ReLU),
    aten.sigmoid: LoweringConfig(llga.op.Sigmoid),
    aten.sub: LoweringConfig(llga.op.Subtract, scalar_in_descs=True),
    aten.tanh: LoweringConfig(llga.op.Tanh),
    aten._softmax: LoweringConfig(
        llga.op.SoftMax,
        "first",
        attribute_inputs={llga.op.axis: lambda in_descs: in_descs[1]},
    ),
    #aten.cat: LoweringConfig(
    #    llga.op.Concat,
    #    lambda x: x[0],
    #    {llga.op.axis: lambda x: x[1] if len(x) == 2 else 0},
    #),
}

lowering_functions = []
for op in _lowerings_map:

    @register_lowering(op)
    def _lowering(
        onednn_graph,
        node_name,
        in_descs,
        out_descs,
        kwargs=None,
        lowering_config=_lowerings_map[op],
    ):
        if kwargs is None:
            kwargs = {}
        for attr_kind in lowering_config.attribute_inputs:
            attr_value = lowering_config.attribute_inputs[attr_kind]
            if callable(attr_value):
                # Lambda functions used to get attribute value from in_descs
                kwargs[attr_kind] = attr_value(in_descs)
            else:
                kwargs[attr_kind] = attr_value
        if lowering_config.in_descs_filtering_func:
            # Lambda function used to filter or reorder in_descs
            in_descs = lowering_config.in_descs_filtering_func(in_descs)
        if lowering_config.scalar_in_descs:
            # If scalar inputs should be cast to match other input type
            in_descs = onednn_graph.overwrite_scalar_args(in_descs)
        return onednn_graph.add_op(
            lowering_config.llga_op, node_name, in_descs, out_descs, kwargs
        )

    lowering_functions.append(_lowering)


@register_lowering(aten.convolution)
def _onednn_convolution(onednn_graph, node_name, in_descs, out_descs, kwargs):
    # in_descs[2] is None when optional bias is False
    input_len = 2 if len(in_descs) > 2 and in_descs[2] is None else 3
    if in_descs[6]:  # if ConvTranspose
        return onednn_graph.add_op(
            llga.op.ConvTranspose,
            node_name,
            in_descs[:input_len],
            out_descs,
            {
                llga.op.strides: in_descs[3],
                llga.op.pads_begin: in_descs[4],
                llga.op.pads_end: in_descs[4],
                llga.op.dilations: in_descs[5],
                llga.op.output_padding: in_descs[7],
                llga.op.groups: in_descs[8],
                llga.op.data_format: "NCX",
                llga.op.weights_format: "IOX",
            },
        )
    else:
        return onednn_graph.add_op(
            llga.op.Convolution,
            node_name,
            in_descs[:input_len],
            out_descs,
            {
                llga.op.strides: in_descs[3],
                llga.op.pads_begin: in_descs[4],
                llga.op.pads_end: in_descs[4],
                llga.op.dilations: in_descs[5],
                llga.op.groups: in_descs[8],
                llga.op.data_format: "NCX",
                llga.op.weights_format: "OIX",
            },
        )


@register_lowering([aten.view, aten.reshape, aten.squeeze, aten.unsqueeze])
def _onednn_view(onednn_graph, node_name, in_descs, out_descs, kwargs):
    return onednn_graph.add_op(
        llga.op.StaticReshape,
        node_name,
        in_descs[:1],
        out_descs,
        {llga.op.shape: out_descs[0].shape, llga.op.special_zero: True},
    )

@register_lowering(aten.batch_norm)
def _onednn_batch_norm(
    onednn_graph,
    node_name: str,
    in_descs,
    out_descs: List[llga.logical_tensor],
    kwargs,
):
    return onednn_graph.add_op(
        llga.op.BatchNormInference,
        node_name,
        in_descs[:5],
        out_descs,
        epsilon=in_descs[7],
        data_format="NCX"
    )
