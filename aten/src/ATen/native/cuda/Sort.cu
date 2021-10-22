#include <limits>

#include <ATen/ATen.h>
#include <ATen/MemoryOverlap.h>
#include <ATen/WrapDimUtils.h>
#include <ATen/core/Array.h>
#include <ATen/cuda/cub.cuh>
#include <ATen/cuda/CUDAContext.h>
#include <ATen/cuda/detail/KernelUtils.h>
#include <ATen/cuda/detail/OffsetCalculator.cuh>
#include <ATen/native/cuda/SortUtils.cuh>
#include <ATen/native/cuda/SortingCommon.cuh>
#include <ATen/native/Sorting.h>

namespace at { namespace native {

bool should_use_small_sort(const Tensor &self, int64_t dim) {
  int64_t ndim = self.dim();
  int64_t nsort = self.sizes()[dim];
  int64_t threshold;
  if (self.scalar_type() == kLong || self.scalar_type() == kDouble) {
    threshold = 1024;
  } else {
    threshold = 2048;
  }
  return nsort <= threshold;
}

std::vector<int64_t> infer_dense_strides_dim_last(const Tensor & self, int64_t dim);

void fillSliceWithIndex(const Tensor& t,int dim) {
  if (t.numel()) {
    auto sizes = DimVector(t.dim(), 1);
    sizes[dim] = t.sizes()[dim];
    auto range = at::arange(t.sizes()[dim], t.options());
    auto rangeview = range.view(sizes);
    t.copy_(rangeview);
  }
}

// In alignment with default sort on a c++ map, this function
// will permute key and value tensors identically, and
// in such a way that the 'key' tensor is ordered numerically
void sortKeyValueInplace(const Tensor& key,
                         const Tensor& value,
                         int dim, bool dir) {
  TORCH_CHECK(key.sizes() == value.sizes(),
              "Key tensor must have same size as value tensor");
  int dims = value.dim();
  TORCH_CHECK(dims <= MAX_DIMS, "value tensor has too many dimensions");
  // if key and value tensors have the same size, we do not need to check both

  ptrdiff_t inElements = key.numel();

  if (inElements == 0) {
    return;
  }

  int64_t keySliceSize = key.size(dim);
  ptrdiff_t keySlices = inElements / keySliceSize;

  // The amount of shared memory and block size is based on
  // 2^ceil(lg(n)); we choose that sorting implementation for a given
  // size.
  int64_t ceilPowerOf2 = nextHighestPowerOf2(keySliceSize);

  // FIXME: We'd have to find some other trick with Thrust to perform a
  // vectorized (key, value) sort by slice segment
  TORCH_INTERNAL_ASSERT(ceilPowerOf2 <= 2048, "sortKeyValueInplace only works for sizes <= 2048 at present");

  // The grid is based on the number of independent slices that we
  // have to sort; one block per slice
  dim3 grid;
  TORCH_INTERNAL_ASSERT(getGridFromTiles(keySlices, grid), "Too many slices to sort");

#define HANDLE_CASE(TYPE, A, SIZE)                                      \
  do {                                                                  \
    int blockSize = SIZE / 2;                                           \
    if (blockSize < 1) {                                                \
      blockSize = 1;                                                    \
    }                                                                   \
                                                                        \
    dim3 block(blockSize);                                              \
                                                                        \
    if (dir) {                                                          \
      bitonicSortKVInPlace<scalar_t, int64_t, A, -1,                    \
          GTOp<scalar_t, true>, TYPE, SIZE>                           \
        <<<grid, block, 0, c10::cuda::getCurrentCUDAStream()>>>(        \
          keyInfo,                                                      \
          keySlices,                                                    \
          (TYPE) keySliceSize,                                          \
          (TYPE) keyInfo.strides[collapseKeyDim],                       \
          valueInfo,                                                    \
          (TYPE) valueInfo.strides[collapseValueDim],                   \
          GTOp<scalar_t, true>());                                    \
      C10_CUDA_KERNEL_LAUNCH_CHECK();                                   \
    } else {                                                            \
      bitonicSortKVInPlace<scalar_t, int64_t, A, -1,                    \
      LTOp<scalar_t, true>, TYPE, SIZE>                               \
        <<<grid, block, 0, c10::cuda::getCurrentCUDAStream()>>>(        \
          keyInfo,                                                      \
          keySlices,                                                    \
          (TYPE) keySliceSize,                                          \
          (TYPE) keyInfo.strides[collapseKeyDim],                       \
          valueInfo,                                                    \
          (TYPE) valueInfo.strides[collapseValueDim],                   \
          LTOp<scalar_t, true>());                                    \
      C10_CUDA_KERNEL_LAUNCH_CHECK();                                   \
    }                                                                   \
  } while (0)

#define HANDLE_SORT_CASE(TYPE, A)                       \
  {                                                     \
    switch (ceilPowerOf2) {                             \
      case 2048:                                        \
      HANDLE_CASE(TYPE, A, 2048);                       \
      break;                                            \
      case 1024:                                        \
      case 512:                                         \
      case 256:                                         \
      HANDLE_CASE(TYPE, A, 1024);                       \
      break;                                            \
      case 128:                                         \
      case 64:                                          \
      HANDLE_CASE(TYPE, A, 128);                        \
      break;                                            \
      case 32:                                          \
      case 16:                                          \
      case 8:                                           \
      case 4:                                           \
      case 2:                                           \
      HANDLE_CASE(TYPE, A, 32);                         \
      break;                                            \
      case 1:                                           \
      /* Nothing to do, data already sorted */          \
      break;                                            \
      default:                                          \
      TORCH_INTERNAL_ASSERT(false);                     \
    }                                                   \
  }

  // The constructed key/value tensor info is used to select the slice
  // we are sorting on a per-block basis
  // The constructed key/value tensor info is used to select the slice
  // we are sorting on a per-block basis
  AT_DISPATCH_ALL_TYPES_AND3(at::ScalarType::Half, at::ScalarType::BFloat16, at::ScalarType::Bool, key.scalar_type(), "sortKeyValueInplace", [&]  {
    if (at::cuda::detail::canUse32BitIndexMath(key)) {
      at::cuda::detail::TensorInfo<scalar_t, unsigned int> keyInfo =
        at::cuda::detail::getTensorInfo<scalar_t, unsigned int>(key);
      at::cuda::detail::TensorInfo<int64_t, unsigned int> valueInfo =
        at::cuda::detail::getTensorInfo<int64_t, unsigned int>(value);

      auto strideKey = keyInfo.strides[dim];
      keyInfo.sizes[dim] = 1;
      int collapseKeyDim = keyInfo.collapseDims(dim);
      keyInfo.strides[collapseKeyDim] = strideKey;
      auto strideValue = valueInfo.strides[dim];
      valueInfo.sizes[dim]=1;
      int collapseValueDim = valueInfo.collapseDims(dim);
      valueInfo.strides[collapseValueDim] = strideValue;

      if (keyInfo.isContiguous()) {
        HANDLE_SORT_CASE(unsigned int, -2);
      } else {
        switch (keyInfo.dims) {
          case 2:
            HANDLE_SORT_CASE(unsigned int, 2);
            break;
          default:
            HANDLE_SORT_CASE(unsigned int, -1);
            break;
        }
      }

    } else {
      at::cuda::detail::TensorInfo<scalar_t, uint64_t> keyInfo =
        at::cuda::detail::getTensorInfo<scalar_t, uint64_t>(key);
      at::cuda::detail::TensorInfo<int64_t, uint64_t> valueInfo =
        at::cuda::detail::getTensorInfo<int64_t, uint64_t>(value);

      auto strideKey = keyInfo.strides[dim];
      keyInfo.sizes[dim] = 1;
      int collapseKeyDim = keyInfo.collapseDims(dim);
      keyInfo.strides[collapseKeyDim] = strideKey;
      auto strideValue = valueInfo.strides[dim];
      valueInfo.sizes[dim]=1;
      int collapseValueDim = valueInfo.collapseDims(dim);
      valueInfo.strides[collapseValueDim] = strideValue;

      // int64_t case is rare, just instantiate the generic version
      HANDLE_SORT_CASE(uint64_t, -1);
    }
  });
#undef HANDLE_CASE
#undef HANDLE_SORT_CASE
#undef HANDLE_A_CASE
}

namespace {

struct offset_t {
  int stride;
  int begin;
  __device__ int operator[](int i) {
    return stride * (begin + i);
  }
};

}

namespace {

// Segmented sort by full sort algorithm:.
// Say we are sorting a (2, 3) tensor. We have in flattened form:
// values       0.4 1.2 5.3 6.2 1.3 2.3
// indices        0   1   2   0   1   2
// segment_id     0   0   0   1   1   1

// First we sort by values, globally:
// values       6.2 5.3 2.3 1.2 1.3 0.4
// indices        0   2   2   1   1   0
// segment_id     1   0   1   0   1   0

// Then we stable sort by segment id:
// values       5.3 1.2 0.4 6.2 2.3 1.3
// indices        2   1   0   0   2   1
// segment_id     0   0   0   1   1   1

// This method can only work if the slice we are sorting (`dim`) is
// innermost, and both values and indices are contiguous. We do this
// by re-arranging the input into this form as needed, which will
// unfortunately allocate memory if the request is not in this form.
// Vectorized sort is slower than iterated sort if the number of
// slices is small (since we're sorting twice, instead of invoking a
// smaller sort `numSlices` times), but the cub sort
// implementation here is a catch-all, so we're not looking for
// efficiency, but instead correctness.

template<typename scalar_t>
__global__ void sort_postprocess_kernel(const scalar_t *in, scalar_t *out, int64_t *index, const int2 *i_s_ptr, int nsegments, int nsort) {
  CUDA_KERNEL_LOOP(i, nsegments * nsort) {
    int segment = i / nsort;
    int j = i % nsort;

    int offset = segment * nsort;
    const scalar_t *in_ = in + offset;
    scalar_t *out_ = out + offset;
    int64_t *index_ = index + offset;
    const int2 *i_s_ptr_ = i_s_ptr + offset;

    int idx = i_s_ptr_[j].y;
    index_[j] = idx;
    out_[j] = in_[idx];
  }
}


__global__ void fill_index_and_segment_kernel(
    int2 *data, int numel, at::cuda::detail::IntDivider<uint32_t> nsort_divider) {
  CUDA_KERNEL_LOOP(idx, numel) {
    auto div_mod = nsort_divider.divmod(idx);
    auto segment = static_cast<int>(div_mod.div);
    auto sort = static_cast<int>(div_mod.mod);
    data[idx] = int2{segment, sort};
  }
}

__global__ void fill_reverse_indices_kernel(
    int64_t *data, int numel, at::cuda::detail::IntDivider<uint32_t> nsort_divider) {
  CUDA_KERNEL_LOOP(idx, numel) {
    data[idx] = nsort_divider.mod(idx);
  }
}


template<typename scalar_t>
inline void segmented_sort_pairs_by_full_sort(
  int64_t nsegments, int64_t nsort, int64_t n, bool descending, const Tensor &indices,
  const scalar_t *self_ptr, scalar_t *values_ptr, int64_t *indices_ptr
) {
  int64_t segment_bits = std::max<int64_t>(1L, static_cast<int64_t>(std::ceil(std::log2(nsegments))));

  const auto numel = nsort * nsegments;
  auto cuda_allocator = at::cuda::getCUDADeviceAllocator();
  auto indices_and_segment = cuda_allocator->allocate(numel * sizeof(int2));
  auto i_s_ptr = static_cast<int2 *>(indices_and_segment.get());

  using namespace at::cuda::detail;
  dim3 block = CUDA_NUM_THREADS;
  dim3 grid = GET_BLOCKS(numel);
  auto stream = c10::cuda::getCurrentCUDAStream();
  at::cuda::detail::IntDivider<uint32_t> nsort_divider(nsort);
  fill_index_and_segment_kernel<<<grid, block, 0, stream>>>(
      i_s_ptr, numel, nsort_divider);

  auto indices_and_segment2 = cuda_allocator->allocate(nsegments * nsort * sizeof(int2));
  auto i_s_ptr2 = static_cast<int2 *>(indices_and_segment2.get());

  at::cuda::cub::radix_sort_pairs<scalar_t, int2>(
    self_ptr, nullptr, i_s_ptr, i_s_ptr2,
    n, descending);

  TORCH_INTERNAL_ASSERT(segment_bits <= 32);

  // sort on lower 32bits, i.e. segment index
  at::cuda::cub::radix_sort_keys<int64_t>(
    reinterpret_cast<int64_t *>(i_s_ptr2), reinterpret_cast<int64_t *>(i_s_ptr),
    n, false, 0, segment_bits);

  sort_postprocess_kernel<<<(n + 511) / 512, 512, 0, at::cuda::getCurrentCUDAStream()>>>(
    self_ptr, values_ptr, indices_ptr, i_s_ptr, nsegments, nsort);
}

template<typename scalar_t>
void segmented_sort_pairs(
    int64_t nsegments, int64_t nsort, int64_t n, bool descending,
    const scalar_t *self_ptr, scalar_t *values_ptr, int64_t *indices_ptr) {
  const auto numel = nsort * nsegments;
  auto cuda_allocator = at::cuda::getCUDADeviceAllocator();
  auto reverse_indices = cuda_allocator->allocate(numel * sizeof(int64_t));
  int64_t *reverse_indices_ptr = static_cast<int64_t *>(reverse_indices.get());

  using namespace at::cuda::detail;
  dim3 block = CUDA_NUM_THREADS;
  dim3 grid = GET_BLOCKS(numel);
  auto stream = c10::cuda::getCurrentCUDAStream();
  at::cuda::detail::IntDivider<uint32_t> nsort_divider(nsort);
  fill_reverse_indices_kernel<<<grid, block, 0, stream>>>(
      reverse_indices_ptr, numel, nsort_divider);

  at::cuda::cub::segmented_sort_pairs(self_ptr, values_ptr,
                                      reverse_indices_ptr, indices_ptr, n, nsegments,
                                      offset_t{(int)nsort, 0}, offset_t{(int)nsort, 1}, descending);
}

}  // namespace

// We perform a segmented sort in cub with inputs that have
// more than 1024/2048 elements along the selected dimension.
// Otherwise, we do an inplace bitonic sort (see sortKeyValueInplace).
void sort_cuda_kernel(
    const Tensor& self,
    int64_t dim,
    bool descending,
    bool stable,
    const Tensor& values,
    const Tensor& indices) {
  // this algorithm is always stable
  bool is_non_overlapping_and_dense = self.is_non_overlapping_and_dense();
  int64_t numel = self.numel();
  int64_t ndim = self.dim();
  dim = maybe_wrap_dim(dim, ndim);
  int64_t nsort = self.sizes()[dim];

  TORCH_CHECK(nsort <= std::numeric_limits<int>::max(),
    "The dimension being sorted can not have more than INT_MAX elements.");

  const auto self_dtype = self.dtype();
  // FIXME: remove this check once cub sort supports bool
  TORCH_CHECK(self_dtype != ScalarType::Bool,
    "Sort currently does not support bool dtype on CUDA.");
  TORCH_CHECK(self_dtype != ScalarType::ComplexFloat && self_dtype != ScalarType::ComplexDouble,
    "Sort currently does not support complex dtypes on CUDA.");

  // use inplace algorithm for smaller input sizes without stable=True
  if (should_use_small_sort(self, dim) && !stable) {
    // from thc: sorted->values, indices->indices, input->self
    // Make sure sufficient output space is allocated
    fillSliceWithIndex(indices, dim);

    // We sort k/v pairs in-place; copy unsorted input to output
    values.copy_(self);

    // Sort using our in-place k/v kernel that supports arbitrary
    // layout
    sortKeyValueInplace(values, indices, dim, descending);
    return;
  }

  Tensor self_;
  bool newself = false;
  if (is_non_overlapping_and_dense && self.stride(dim) == 1) {
    self_ = self;
  } else {
    auto new_strides_unsort = infer_dense_strides_dim_last(self, dim);
    self_ = at::empty_strided(self.sizes(), new_strides_unsort, self.options());
    self_.copy_(self);
    newself = true;
  }

  Tensor values_tmp, indices_tmp;
  void *values_ptr_;
  int64_t *indices_ptr;
  if (values.strides() == self_.strides() && (newself || get_overlap_status(self, values) == MemOverlapStatus::NO)) {
    values_ptr_ = values.data_ptr();
  } else {
    values_tmp = at::empty_strided(self_.sizes(), self_.strides(), self_.options());
    values_ptr_ = values_tmp.data_ptr();
  }

  if (indices.strides() != self_.strides()) {
    indices_tmp = at::empty_strided(self_.sizes(), self_.strides(), self_.options().dtype(kLong));
    indices_ptr = indices_tmp.data_ptr<int64_t>();
  } else {
    indices_ptr = indices.data_ptr<int64_t>();
  }

  if (numel == 0) {
    return;
  }

  int64_t numel_or_intmax = std::min(numel, static_cast<int64_t>(std::numeric_limits<int>::max()));
  int64_t nbatch = (numel_or_intmax / nsort) * nsort;
  TORCH_CHECK(nbatch > 0, "Cannot sort dimension of length ", nsort);

#if defined(USE_ROCM)
  constexpr bool is_rocm = true;
#else
  constexpr bool is_rocm = false;
#endif

  AT_DISPATCH_ALL_TYPES_AND3(kBool, kHalf, kBFloat16, self_.scalar_type(), "sort", [&]{
    c10::guts::if_constexpr<!(is_rocm && std::is_same<scalar_t, c10::BFloat16>::value)>([&](auto _){
      const scalar_t *self_ptr = self_.data_ptr<scalar_t>();
      auto values_ptr = reinterpret_cast<scalar_t *>(values_ptr_);
      int64_t remaining = _(numel);
      while (remaining > 0) {
        int64_t n = std::min(remaining, nbatch);
        int64_t nsegments = n / nsort;

        if (nsegments < 128) {
          segmented_sort_pairs_by_full_sort(nsegments, nsort, n, descending,
            indices, self_ptr, values_ptr, indices_ptr);
        } else {
          segmented_sort_pairs(nsegments, nsort, n, descending,
                               self_ptr, values_ptr, indices_ptr);
        }

        remaining -= n;
        self_ptr += n;
        values_ptr += n;
        indices_ptr += n;
      }
    }, [&](auto _){ TORCH_CHECK(_(false), "BFloat16 is not supported on ROCm"); });
  });

  if (values_tmp.defined()) {
    values.copy_(values_tmp);
  }
  if (indices_tmp.defined()) {
    indices.copy_(indices_tmp);
  }
}

// NOLINTNEXTLINE(cppcoreguidelines-avoid-non-const-global-variables)
REGISTER_DISPATCH(sort_stub, &sort_cuda_kernel);

}}  // namespace at::native
