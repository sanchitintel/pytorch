#include <torch/csrc/jit/codegen/onednn/graph_helper.h>
#include <torch/csrc/jit/codegen/onednn/kernel.h>

#include <ATen/core/functional.h>
#include <torch/csrc/jit/jit_log.h>

namespace torch {
namespace jit {
namespace fuser {
namespace onednn {

using namespace dnnl::graph;


LlgaKernel::LlgaKernel(const Node* fusionNode)
    : fusionNode_(fusionNode),
      graph_(fusionNode->g(attr::Subgraph)),
      nGraphInputs_(graph_->inputs().size()),
      nOutputs_(graph_->outputs().size()),
      debugName_(genDebugName()) {
  // TODO: This is a workaround to recreate the partitions here.
  // The ideal way is to use the partition serialization API (not available from
  // LLGA now) to carry a serialized string representation from graph rewrite
  // and deserialize it here.
  auto llgaGraphHelper = LlgaGraphHelper(graph_);
  auto partitions = llgaGraphHelper.getPartitions();
  tensorIdToValue_ = llgaGraphHelper.getTensorIdToValue();
  TORCH_CHECK(
      partitions.size() == 1,
      "LLGA subgraph should contain only one partition");
  partition_ = partitions[0];
  nPartitionInputs_ = partition_.get_in_ports().size();
  GRAPH_DEBUG("Initialized ", debugName(), "\n", graph_->toString());
}

bool LlgaKernel::useOpaqueLayout(size_t offset) const {
  return LlgaNodeWrapper(fusionNode_).useOpaqueLayout(offset);
}

void LlgaKernel::initializeConstantInputs() {
  for (auto& lt : partition_.get_in_ports()) {
    auto inputId = lt.get_id();
    if (initializedInputIds_.find(inputId) == initializedInputIds_.end()) {
      TORCH_CHECK(
          tensorIdToValue_.count(inputId) > 0,
          "inputs with inputId ",
          inputId,
          " is missing");
      auto* value = tensorIdToValue_[inputId];

      TORCH_CHECK(
          value->node()->kind() == prim::Constant &&
              value->type()->cast<TensorType>(),
          "inputs with inputId ",
          inputId,
          " should be a Constant tensor");
      constantValues_.emplace_back(value);

      auto const_tensor = toIValue(value)->toTensor();
      constantInputs_.emplace_back(const_tensor);
    }
  }
}

ArgSpecs LlgaKernel::initializeInputSpecs(const TensorArgs& inputs) {
  ArgSpecs inputSpecs;
  inputSpecs.reserve(nPartitionInputs_);
  GRAPH_DEBUG("Initializing graph input logical tensors");
  for (size_t i = 0; i < nGraphInputs_; i++) {
    auto spec = ArgSpec(graph_->inputs()[i]).supplementTensorInfo(inputs[i]);
    initializedInputIds_.insert(spec.tid());
    inputSpecs.emplace_back(spec);
  }

  GRAPH_DEBUG("Initializing constant input tensors");
  initializeConstantInputs();

  TORCH_CHECK(
      inputSpecs.size() + constantValues_.size() == nPartitionInputs_,
      "Partition inputs are missing");

  GRAPH_DEBUG(
      "Concatenating constant input logical tensors to graph input "
      "logical tensors");
  for (Value* constant_value : constantValues_) {
    inputSpecs.emplace_back(ArgSpec(constant_value));
  }
  return inputSpecs;
}

ArgSpecs LlgaKernel::initializeOutputSpecs(
    const partition& partition,
    const ArgSpecs& inputSpecs) const {
  auto inputs = fmap(inputSpecs, toLogicalTensor);
  auto outputs = fmap(graph_->outputs(), toLogicalTensor);
  partition.infer_shape(inputs, outputs);

  ArgSpecs outputSpecs;
  outputSpecs.reserve(nOutputs_);
  for (size_t i = 0; i < nOutputs_; i++) {
    auto spec = ArgSpec(outputs[i]).dtype(inputSpecs[0].dtype());
    if (useOpaqueLayout(i))
      spec = spec.any();
    outputSpecs.emplace_back(spec);
  }
  return outputSpecs;
}

std::tuple<RunArgs, RunArgs>
LlgaKernel::prepareRunArgs(const TensorArgs &inputs,
                           TensorArgs &outputs) const {
  RunArgs runInputs, runOutputs;
  for (size_t i = 0; i < nGraphInputs_; i++) {
    auto spec = inputSpecs_[i];
    runInputs.push_back(
        {spec.logical_tensor(), Engine::getEngine(), inputs[i].data_ptr()});
  }
  for (size_t i = 0; i < constantInputs_.size(); i++) {
    // constantInputSpecs are placed after graphInputSpecs
    auto constantInputSpecIdx = nGraphInputs_ + i;
    auto constantInputSpec = inputSpecs_[constantInputSpecIdx];
    runInputs.push_back({constantInputSpec.logical_tensor(),
                         Engine::getEngine(),
                         constantInputs_[i].data_ptr()});
  }
  for (size_t i = 0; i < constantInputs_.size(); i++) {
    // constantInputSpecs are placed after graphInputSpecs
    auto constantInputSpecIdx = nGraphInputs_ + i;
    auto constantInputSpec = inputSpecs_[constantInputSpecIdx];
    runInputs.push_back(
        {constantInputSpec.logical_tensor(), constantInputs_[i].data_ptr()});
  }

  for (size_t i = 0; i < nOutputs_; i++) {
    auto spec = outputSpecs_[i];
    auto opt = c10::TensorOptions(spec.aten_scalar_type()).device(device_);

    auto outputId = spec.tid();
    auto iter = inplacePairs_.find(outputId);
    if (iter != inplacePairs_.end()) {
      // output reuses one of input tensors
      auto inputOffset = iter->second;
      auto inputTensor = inputs[inputOffset];
      outputs.push_back(inputTensor);
      runOutputs.push_back(
          {spec.logical_tensor(), Engine::getEngine(), inputTensor.data_ptr()});
    } else if (spec.is_opaque()) {
      auto tensor = empty_llga(spec, opt);
      outputs.push_back(tensor);
      runOutputs.push_back(llga_from_aten_tensor(tensor));
    } else {
      auto tensor = at::empty_strided(spec.sizes(), spec.strides(), opt);
      outputs.push_back(tensor);
      runOutputs.push_back(
          {spec.logical_tensor(), Engine::getEngine(), tensor.data_ptr()});
    }
  }

  return std::make_tuple(runInputs, runOutputs);
}

compiled_partition LlgaKernel::compile(const partition& partition) {
  auto inputs = fmap(inputSpecs_, toLogicalTensor);
  auto outputs = fmap(outputSpecs_, toLogicalTensor);
  auto compilation = partition.compile(inputs, outputs, Engine::getEngine());

  // Since layouts of opaque outputs would be known after compilation,
  // we need to query them out from compilation and update outputSpecs
  for (size_t i = 0; i < nOutputs_; i++) {
    auto tid = outputSpecs_[i].tid();
    outputSpecs_[i] = compilation.query_logical_tensor(tid);
  }

  // Build static mapping from output id to input offset
  // in accordance with available inplace options
  for (auto&& option : compilation.get_inplace_ports()) {
    size_t inputId = option.first;
    size_t outputId = option.second;
    auto inputSpecIter =
        std::find_if(inputSpecs_.begin(), inputSpecs_.end(), [&](auto& spec) {
          return spec.tid() == inputId;
        });
    TORCH_CHECK(inputSpecIter != inputSpecs_.end(), "In-place input not found");
    auto inputOffset = inputSpecIter - inputSpecs_.begin();
    inplacePairs_[outputId] = inputOffset;
  }

  return compilation;
}

void LlgaKernel::run(Stack& stack) {
  GRAPH_DEBUG("In ", debugName(), "\n");

  // Grab input values from stack
  auto stackInputs = last(stack, nGraphInputs_);
  auto inputs = fmap(stackInputs, [&](const IValue& v) {
    TORCH_CHECK(
        v.isTensor(), "Stack values for LLGA partition must be Tensor type");
    return v.toTensor();
  });

  lock_read();
  if (is_initialized_) {
    unlock_read();
  } else {
    unlock_read();

    lock_write();
    if (!is_initialized_) {
      GRAPH_DEBUG("Initializing input logical tensors");
      inputSpecs_ = initializeInputSpecs(inputs);
      GRAPH_DEBUG("Initializing output logical tensors");
      outputSpecs_ = initializeOutputSpecs(partition_, inputSpecs_);
      GRAPH_DEBUG("Compiling partition");
      compilation_ = compile(partition_);
      is_initialized_ = true;
    }
    unlock_write();
  }

  GRAPH_DEBUG("Preparing runtime tensors");
  TensorArgs outputs;
  RunArgs runInputs, runOutputs;
  std::tie(runInputs, runOutputs) = prepareRunArgs(inputs, outputs);

  GRAPH_DEBUG("Executing partition");
  compilation_.execute(Stream::getStream(), runInputs, runOutputs);
  GRAPH_DEBUG("Partition executed");

  // Update the stack.
  drop(stack, nGraphInputs_);
  for (auto& o : outputs)
    push_one(stack, std::move(o));
  GRAPH_DEBUG("Stack updated");
}

engine& Engine::getEngine() {
  static engine cpu_engine(dnnl::graph::engine::kind::cpu, 0);
  return cpu_engine;
}

stream& Stream::getStream() {
  static stream cpu_stream{Engine::getEngine(), nullptr};
  return cpu_stream;
}

} // namespace onednn
} // namespace fuser
} // namespace jit
} // namespace torch
