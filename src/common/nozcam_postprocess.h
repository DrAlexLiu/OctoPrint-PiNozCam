// Backend-independent detector output decode, NMS and severity.

#ifndef NOZCAM_POSTPROCESS_H_
#define NOZCAM_POSTPROCESS_H_

#include <cstddef>
#include <cstdint>
#include <vector>

namespace nozcam {

// One output level's score and box buffers. The caller owns the buffers.
struct HeadLevel {
  const float* cls;  // (num_anchors * num_classes, height, width)
  const float* box;  // declared as (num_anchors * 4, height, width) but
                     // indexed as (num_anchors * height * width, 4) in row
                     // major order -- see pitfall 9 in the .cpp. That is
                     // the original implementation's behaviour, not a typo.
  int height;
  int width;
};

struct Detection {
  float score;
  double x1, y1, x2, y2;  // decode uses the production float64 convention
  int label;
};

struct Result {
  std::vector<Detection> detections;
  double severity;
  double percentage_area;
  int total_area;
};

struct Params {
  int proc_width = 640;
  int proc_height = 384;
  float decode_threshold = 0.05f;
  int decode_top_n = 1000;
  double nms_iou = 0.5;
  int nms_ndetections = 6;
  float scores_threshold = 0.3f;   // used by severity; passed in by the plugin
  double img_sensitivity = 0.04;   // used by severity; passed in by the plugin

  // The letterboxed CONTENT rectangle inside proc_width x proc_height: the
  // part of the frame that is camera, not black bar. Defaults to the whole
  // frame, which is what an exactly-proportioned camera gives.
  //
  // Severity is an area FRACTION, so the denominator has to be the picture
  // and not the canvas. With 16:9 letterboxed into 15:9 the bars are 6.25%
  // of the canvas and 4:3 makes them 20%: reporting union/canvas would
  // quietly scale every severity down by that much, and the alarm
  // threshold with it. Boxes are also clamped to this rectangle, so a
  // detection bleeding into a bar cannot add area that is not camera.
  int content_x = 0;
  int content_y = 0;
  int content_width = 640;
  int content_height = 384;
};

// `levels` must be given in ascending stride order.
// Decode shared model heads and compute severity metrics.
Result Postprocess(const std::vector<HeadLevel>& levels, const Params& p);

// Benchmark-only stage timings. Production builds do not define
// NOZCAM_PROFILE_POSTPROCESS, so neither this API nor any clock reads are
// present in the shipped daemon. Keeping the instrumentation behind a
// compile-time gate matters here: the whole postprocess is sub-millisecond on
// A76, and unconditional steady_clock calls would perturb the quantity being
// measured.
#ifdef NOZCAM_PROFILE_POSTPROCESS
struct PostprocessStageTimings {
  double decode_ms;
  double nms_ms;
  double severity_ms;
};

// Return per-stage latencies from the most recent profiling run.
const PostprocessStageTimings& LastPostprocessStageTimings();
#endif

// Validate an untrusted content rectangle using overflow-safe arithmetic.
// Return false for negative, empty, or out-of-bounds content rectangles.
bool ValidContentRect(std::int64_t x, std::int64_t y,
                      std::int64_t width, std::int64_t height,
                      int proc_width, int proc_height);

}  // namespace nozcam

#endif  // NOZCAM_POSTPROCESS_H_
