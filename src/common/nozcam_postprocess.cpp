// Detector output decode, NMS and severity shared by every backend.
// Ties use a deterministic score-descending, index-ascending order.
// Precision matches the reference implementation:
//   - scores are float, and comparisons happen in float
//   - box coordinates are always double
//   - exponentiation is evaluated from a float32 delta and then promoted;
//     promoting before exponentiation differs in the last bits.

#include "nozcam_postprocess.h"

#include <algorithm>
#ifdef NOZCAM_PROFILE_POSTPROCESS
#include <chrono>
#endif
#include <cmath>
#include <cstdint>
#include <vector>

namespace nozcam {
namespace {

#ifdef NOZCAM_PROFILE_POSTPROCESS
using ProfileClock = std::chrono::steady_clock;
thread_local PostprocessStageTimings g_last_stage_timings = {0.0, 0.0, 0.0};

// Convert a steady-clock interval to milliseconds for optional profiling.
double ElapsedMs(ProfileClock::time_point begin,
                 ProfileClock::time_point end) {
  return std::chrono::duration<double, std::milli>(end - begin).count();
}
#endif

constexpr int kNumAnchorsPerLevel = 9;  // 3 ratios x 3 scales
const double kRatios[3] = {1.0, 2.0, 0.5};

// Generate the fixed box priors used by the packaged runtime output contract.
// Each scale is paired with all three ratios before advancing to the next.
void GenerateAnchors(int stride, double out[kNumAnchorsPerLevel][4]) {
  double scales[3];
  for (int i = 0; i < 3; ++i) {
    scales[i] = 4.0 * std::pow(2.0, static_cast<double>(i) / 3.0);
  }
  for (int a = 0; a < kNumAnchorsPerLevel; ++a) {
    const double ratio = kRatios[a % 3];
    const double scale = scales[a / 3];
    const double s = static_cast<double>(stride);
    const double ws = std::sqrt(s * s / ratio);
    const double dw = ws;
    const double dh = ws * ratio;
    out[a][0] = 0.5 * (s - dw * scale);
    out[a][1] = 0.5 * (s - dh * scale);
    out[a][2] = 0.5 * (s + dw * scale);
    out[a][3] = 0.5 * (s + dh * scale);
  }
}

// Convert four box deltas into image coordinates. `grid` already includes
// the fixed prior offset.
void Delta2Box(const float delta[4], const double grid[4], int width,
               int height, int stride, double out[4]) {
  const double awh0 = grid[2] - grid[0] + 1.0;
  const double awh1 = grid[3] - grid[1] + 1.0;
  const double ctr0 = grid[0] + 0.5 * awh0;
  const double ctr1 = grid[1] + 0.5 * awh1;
  const double pc0 = static_cast<double>(delta[0]) * awh0 + ctr0;
  const double pc1 = static_cast<double>(delta[1]) * awh1 + ctr1;
  // np.exp is evaluated in float32 and promoted afterwards -- see the
  // precision conventions in the file header.
  const double pw = static_cast<double>(std::exp(delta[2])) * awh0;
  const double ph = static_cast<double>(std::exp(delta[3])) * awh1;

  const double mx = static_cast<double>(width) * stride - 1.0;
  const double my = static_cast<double>(height) * stride - 1.0;
  auto clamp_x = [mx](double t) { return std::max(0.0, std::min(t, mx)); };
  auto clamp_y = [my](double t) { return std::max(0.0, std::min(t, my)); };

  out[0] = clamp_x(pc0 - 0.5 * pw);
  out[1] = clamp_y(pc1 - 0.5 * ph);
  out[2] = clamp_x(pc0 + 0.5 * pw - 1.0);  // preserve the inclusive edge
  out[3] = clamp_y(pc1 + 0.5 * ph - 1.0);
}

struct Candidate {
  float score;
  double box[4];
  int cls;
};

// Decode one output level.
void DecodeLevel(const HeadLevel& lv, int stride, const Params& p,
                 int num_classes, std::vector<Candidate>* out) {
  const int width = lv.width;
  const int height = lv.height;
  const std::size_t n_cls =
      static_cast<std::size_t>(kNumAnchorsPerLevel) * num_classes *
      static_cast<std::size_t>(height) * static_cast<std::size_t>(width);

  // :159 keep = np.where(cls_head >= threshold)
  std::vector<std::uint32_t> keep;
  for (std::size_t i = 0; i < n_cls; ++i) {
    if (lv.cls[i] >= p.decode_threshold) {
      keep.push_back(static_cast<std::uint32_t>(i));
    }
  }
  if (keep.empty()) {
    return;
  }

  // Sort, then take top_n. Ties break by ascending index.
  const std::size_t take =
      std::min(keep.size(), static_cast<std::size_t>(p.decode_top_n));
  std::partial_sort(
      keep.begin(), keep.begin() + take, keep.end(),
      [&lv](std::uint32_t a, std::uint32_t b) {
        if (lv.cls[a] != lv.cls[b]) return lv.cls[a] > lv.cls[b];
        return a < b;
      });

  double anchors[kNumAnchorsPerLevel][4];
  GenerateAnchors(stride, anchors);

  const int hw = height * width;
  for (std::size_t k = 0; k < take; ++k) {
    const std::uint32_t idx = keep[k];

    // The two divisions are intentionally asymmetric because the two output
    // buffers use different logical layouts. Keep the contract unchanged.
    const int cls_id = static_cast<int>(idx / (static_cast<std::uint32_t>(
                           width) * height * kNumAnchorsPerLevel)) + 1;
    const int x = static_cast<int>(idx % static_cast<std::uint32_t>(width));
    const int y = static_cast<int>((idx / static_cast<std::uint32_t>(width)) %
                                   static_cast<std::uint32_t>(height));
    const int a = static_cast<int>(idx / static_cast<std::uint32_t>(
                      num_classes) / height / width);

    // Shift the selected prior to this grid position.
    const double grid[4] = {
        static_cast<double>(x) * stride + anchors[a][0],
        static_cast<double>(y) * stride + anchors[a][1],
        static_cast<double>(x) * stride + anchors[a][2],
        static_cast<double>(y) * stride + anchors[a][3],
    };

    // Each box row is four consecutive values in the flat output buffer.
    const float* delta = lv.box + static_cast<std::size_t>(idx) * 4;

    Candidate c;
    c.score = lv.cls[idx];
    c.cls = cls_id;
    Delta2Box(delta, grid, width, height, stride, c.box);
    out->push_back(c);
    (void)hw;
  }
}

// Apply non-maximum suppression to decoded candidates.
std::vector<Candidate> Nms(std::vector<Candidate> v, const Params& p) {
  // :54 keep = (all_scores > 0)
  std::vector<Candidate> s;
  s.reserve(v.size());
  for (std::size_t k = 0; k < v.size(); ++k) {
    if (v[k].score > 0.0f) s.push_back(v[k]);
  }
  if (s.empty()) return {};  // :59-60

  // :63 sort. Ties break by ascending pre-filter position -- the same
  // convention _decode uses.
  std::vector<std::uint32_t> order(s.size());
  for (std::uint32_t k = 0; k < order.size(); ++k) order[k] = k;
  std::sort(order.begin(), order.end(),
            [&s](std::uint32_t a, std::uint32_t b) {
              if (s[a].score != s[b].score) return s[a].score > s[b].score;
              return a < b;
            });
  {
    std::vector<Candidate> tmp(s.size());
    for (std::size_t k = 0; k < order.size(); ++k) tmp[k] = s[order[k]];
    s.swap(tmp);
  }

  // :68 areas -- note the +1
  std::vector<double> areas(s.size());
  for (std::size_t k = 0; k < s.size(); ++k) {
    areas[k] = (s[k].box[2] - s[k].box[0] + 1.0) *
               (s[k].box[3] - s[k].box[1] + 1.0);
  }

  // :70-73 is `for i in range(nd): if ...: i -= 1; break`, and :92 uses
  // `out[:i+1]`. Python's loop variable still exists after the loop and
  // equals nd-1 on a normal completion; C++'s `for(int i=...)` scopes i to
  // the loop and would leave it at nd. So i is declared outside and assigned
  // on the loop body's first line, reproducing the original exactly.
  // Measured semantics: with 0/3/10 candidates, i is -1 / 2 / 5.
  // :71's condition is `i >= keep.sum() or i >= scores.size`. Verified that
  // keep is only ever filtered, never set False, so
  // keep.sum() == keep.size == scores.size and the two halves are the same
  // test; they are merged into one here.
  int i = -1;
  for (int t = 0; t < p.nms_ndetections; ++t) {
    i = t;
    if (i >= static_cast<int>(s.size())) {
      --i;
      break;
    }

    // :76-78 overlap against box i
    const double ax1 = s[i].box[0], ay1 = s[i].box[1];
    const double ax2 = s[i].box[2], ay2 = s[i].box[3];
    const double area_i = areas[i];
    const float score_i = s[i].score;
    const int cls_i = s[i].cls;

    std::vector<Candidate> ks;
    std::vector<double> ka;
    ks.reserve(s.size());
    ka.reserve(s.size());
    for (std::size_t k = 0; k < s.size(); ++k) {
      bool crit;
      if (static_cast<int>(k) == i) {
        crit = true;  // :83 criterion[i] = True -- self-protection; omit
                      // it and the box suppresses itself
      } else {
        const double ix1 = std::max(s[k].box[0], ax1);
        const double iy1 = std::max(s[k].box[1], ay1);
        const double ix2 = std::min(s[k].box[2], ax2);
        const double iy2 = std::min(s[k].box[3], ay2);
        // :78 np.prod(np.maximum(0, xy2 - xy1 + 1)) -- the +1 again
        const double inter = std::max(0.0, ix2 - ix1 + 1.0) *
                             std::max(0.0, iy2 - iy1 + 1.0);
        const double iou = inter / (areas[k] + area_i - inter);
        // :80-82 the disjunction of three conditions
        crit = (s[k].score > score_i) || (iou <= p.nms_iou) ||
               (s[k].cls != cls_i);
      }
      if (crit) {
        ks.push_back(s[k]);
        ka.push_back(areas[k]);
      }
    }
    s.swap(ks);
    areas.swap(ka);
  }

  // :92-96 out[:i+1]
  const int n = i + 1;
  if (n <= 0) return {};
  s.resize(std::min(static_cast<std::size_t>(n), s.size()));
  return s;
}

}  // namespace

// Validate content rectangle values without overflow.
bool ValidContentRect(std::int64_t x, std::int64_t y,
                      std::int64_t width, std::int64_t height,
                      int proc_width, int proc_height) {
  // Zero area is rejected as well as negative: it would divide by zero.
  return x >= 0 && y >= 0 && width > 0 && height > 0 &&
         x + width <= proc_width && y + height <= proc_height;
}

// Decode, NMS-filter, and combine detections into severity metrics.
Result Postprocess(const std::vector<HeadLevel>& levels, const Params& p) {
#ifdef NOZCAM_PROFILE_POSTPROCESS
  g_last_stage_timings = {0.0, 0.0, 0.0};
  const auto decode_begin = ProfileClock::now();
#endif
  Result r;
  r.severity = 0.0;
  r.percentage_area = 0.0;
  r.total_area = 0;
  if (levels.empty()) {
#ifdef NOZCAM_PROFILE_POSTPROCESS
    g_last_stage_timings.decode_ms =
        ElapsedMs(decode_begin, ProfileClock::now());
#endif
    return r;
  }

  // Derive the class count from the score-buffer channel contract.
  // A*C is the same on every level, so the first level determines it; for
  // this model that is 9/9 = 1.
  const int num_classes = 1;

  std::vector<Candidate> all;
  for (const HeadLevel& lv : levels) {
    const int stride = p.proc_width / lv.width;  // :204
    DecodeLevel(lv, stride, p, num_classes, &all);
  }
#ifdef NOZCAM_PROFILE_POSTPROCESS
  const auto decode_end = ProfileClock::now();
  g_last_stage_timings.decode_ms = ElapsedMs(decode_begin, decode_end);
#endif
  if (all.empty()) return r;  // :216-217

#ifdef NOZCAM_PROFILE_POSTPROCESS
  const auto nms_begin = ProfileClock::now();
#endif
  const std::vector<Candidate> kept = Nms(all, p);
#ifdef NOZCAM_PROFILE_POSTPROCESS
  const auto nms_end = ProfileClock::now();
  g_last_stage_timings.nms_ms = ElapsedMs(nms_begin, nms_end);
  const auto severity_begin = ProfileClock::now();
#endif

  // The severity bitmap covers the half-open range
  // [y1,y2) x [x1,x2), and the filter is a STRICT greater-than against
  // scores_threshold.
  std::vector<unsigned char> bitmap(
      static_cast<std::size_t>(p.proc_width) * p.proc_height, 0);
  for (const Candidate& c : kept) {
    Detection d;
    d.score = c.score;
    d.x1 = c.box[0];
    d.y1 = c.box[1];
    d.x2 = c.box[2];
    d.y2 = c.box[3];
    d.label = c.cls;
    r.detections.push_back(d);

    if (c.score <= p.scores_threshold) continue;
    // map(int, box) truncates toward zero, and the coordinates are
    // non-negative, so a plain (int) cast matches
    int x1 = static_cast<int>(c.box[0]);
    int y1 = static_cast<int>(c.box[1]);
    int x2 = static_cast<int>(c.box[2]);
    int y2 = static_cast<int>(c.box[3]);
    // Clamped to the CONTENT rectangle, not the whole canvas. Anything a
    // box claims inside a letterbox bar is not camera, and counting it
    // would inflate the area fraction with padding.
    const int cx0 = p.content_x;
    const int cy0 = p.content_y;
    const int cx1 = p.content_x + p.content_width;
    const int cy1 = p.content_y + p.content_height;
    x1 = std::max(cx0, std::min(x1, cx1 - 1));
    y1 = std::max(cy0, std::min(y1, cy1 - 1));
    x2 = std::max(cx0, std::min(x2, cx1 - 1));
    y2 = std::max(cy0, std::min(y2, cy1 - 1));
    for (int yy = y1; yy < y2; ++yy) {
      unsigned char* row = &bitmap[static_cast<std::size_t>(yy) * p.proc_width];
      for (int xx = x1; xx < x2; ++xx) row[xx] = 1;
    }
  }

  int total = 0;
  for (unsigned char b : bitmap) total += b;
  r.total_area = total;
  const double denom = static_cast<double>(p.content_width) *
                       static_cast<double>(p.content_height);
  r.percentage_area = denom > 0.0 ? static_cast<double>(total) / denom : 0.0;
  r.severity = std::max(
      0.0, std::min(r.percentage_area / p.img_sensitivity, 1.0));
#ifdef NOZCAM_PROFILE_POSTPROCESS
  g_last_stage_timings.severity_ms =
      ElapsedMs(severity_begin, ProfileClock::now());
#endif
  return r;
}

#ifdef NOZCAM_PROFILE_POSTPROCESS
// Return the latest phase timings captured by this compilation unit.
const PostprocessStageTimings& LastPostprocessStageTimings() {
  return g_last_stage_timings;
}
#endif

}  // namespace nozcam
