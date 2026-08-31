// Post-inference validation for Allwinner's AWNN paths.
// Helps avoid reusing stale buffers when the vendor invocation has no status.

#ifndef PINOZCAM_ALLWINNER_RUN_GUARD_H_
#define PINOZCAM_ALLWINNER_RUN_GUARD_H_

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <limits>
#include <type_traits>
#include <utility>

namespace nozcam {
namespace allwinner {

// The guard is strict on nulls and NaN/Inf so no stale vendor output can pass.
// Ensure output tensor pointers are valid and finite before use.
inline bool OutputsAreFinite(const float* const* outputs,
                            const std::size_t* expected_sizes,
                            int n_outputs) {
  if (outputs == nullptr || expected_sizes == nullptr || n_outputs < 0) {
    return false;
  }
  for (int i = 0; i < n_outputs; ++i) {
    if (outputs[i] == nullptr) return false;
    const std::size_t n = expected_sizes[i];
    for (std::size_t j = 0; j < n; ++j) {
      const float v = outputs[i][j];
      if (!std::isfinite(v)) return false;
    }
  }
  return true;
}

// Clear output buffers with a NaN sentinel before each run.
inline bool ClearOutputsToNaN(float** outputs,
                             const std::size_t* expected_sizes,
                             int n_outputs) {
  if (outputs == nullptr || expected_sizes == nullptr || n_outputs < 0) {
    return false;
  }
  const float kSentinel = std::numeric_limits<float>::quiet_NaN();
  for (int i = 0; i < n_outputs; ++i) {
    if (outputs[i] == nullptr) return false;
    std::fill_n(outputs[i], expected_sizes[i], kSentinel);
  }
  return true;
}

// Run awnn::run() when it returns void and validate output buffers afterward.
template <typename RunFn>
typename std::enable_if<
    std::is_void<decltype(std::declval<RunFn>()())>::value, bool>::type
RunAwnnWithObservedStatus(RunFn run_fn,
                          float** outputs,
                          const std::size_t* expected_sizes,
                          int n_outputs) {
  if (!ClearOutputsToNaN(outputs, expected_sizes, n_outputs)) return false;
  run_fn();
  return OutputsAreFinite(outputs, expected_sizes, n_outputs);
}

// Run awnn::run() when it returns int and require 0 success.
template <typename RunFn>
typename std::enable_if<
    std::is_integral<decltype(std::declval<RunFn>()())>::value, bool>::type
RunAwnnWithObservedStatus(RunFn run_fn,
                          float** outputs,
                          const std::size_t* expected_sizes,
                          int n_outputs) {
  (void)outputs;
  (void)expected_sizes;
  (void)n_outputs;
  return run_fn() == 0;
}

// Reject unexpected return types from the AWNN run callable.
template <typename RunFn>
typename std::enable_if<
    !std::is_void<decltype(std::declval<RunFn>()())>::value &&
    !std::is_integral<decltype(std::declval<RunFn>()())>::value, bool>::type
RunAwnnWithObservedStatus(RunFn,
                          float** outputs,
                          const std::size_t* expected_sizes,
                          int n_outputs) {
  (void)outputs;
  (void)expected_sizes;
  (void)n_outputs;
  return false;
}

}  // namespace allwinner
}  // namespace nozcam

#endif  // PINOZCAM_ALLWINNER_RUN_GUARD_H_
