// Resident runner for the Allwinner A733 NPU through AWNN/VIPLite.
// It implements ../PROTOCOL.md and uses the shared postprocessor in
// ../common. Vendor SDK sources, headers and libraries are external
// dependencies documented in README.md.
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <string>
#include <vector>

#include <sched.h>
#include <sys/resource.h>
#include <unistd.h>

#include "nozcam_postprocess.h"
#include "nozcam_protocol_io.h"
#include "nozcam_allwinner_run_guard.h"

#include "awnn_internal.h"  // full Awnn_Context_t -- needed to validate
                            // input/output tensor shapes before trusting
                            // them, same bar as the other two daemons.
#include "awnn_lib.h"
extern "C" {
#include "awnn_quantize.h"  // fp32_to_fp16
}

namespace {

constexpr std::uint32_t kCmdPing = 1;
constexpr std::uint32_t kCmdInfer = 2;
constexpr std::uint32_t kCmdInfo = 3;
constexpr std::uint32_t kCmdShutdown = 4;

constexpr int kProcW = 640;
constexpr int kProcH = 384;
constexpr int kLevels = 5;
const int kH[kLevels] = {48, 24, 12, 6, 3};
const int kW[kLevels] = {80, 40, 20, 10, 5};
constexpr int kAnchors = 9;
constexpr int kClasses = 1;
constexpr int kNumOutputs = 10;
int g_protocol_fd = -1;

// Return expected channels for a single model head.
int ExpectedChannels(int i) {
  return (i < kLevels) ? kAnchors * kClasses : kAnchors * 4;
}

// Return expected head element count from output grid shape.
std::size_t ExpectedElems(int i) {
  const int l = i % kLevels;
  return static_cast<std::size_t>(ExpectedChannels(i)) * kH[l] * kW[l];
}

// Read exactly n bytes from a descriptor, with EINTR-safe loop.
bool ReadExact(int fd, void* buf, std::size_t n) {
  auto* p = static_cast<std::uint8_t*>(buf);
  while (n > 0) {
    const ssize_t r = ::read(fd, p, n);
    if (r == 0) return false;
    if (r < 0) { if (errno == EINTR) continue; return false; }
    p += r;
    n -= static_cast<std::size_t>(r);
  }
  return true;
}

// Drop n bytes from input by bounded chunk reads.
bool DrainBounded(int fd, std::uint32_t n) {
  std::uint8_t buf[65536];
  while (n > 0) {
    const std::uint32_t chunk =
        n < sizeof(buf) ? n : static_cast<std::uint32_t>(sizeof(buf));
    if (!ReadExact(fd, buf, chunk)) return false;
    n -= chunk;
  }
  return true;
}

// Write one protocol frame to the shared protocol descriptor.
bool SendFrame(std::uint32_t status, const void* payload, std::uint32_t len) {
  std::uint32_t hdr[2] = {status, len};
  if (!nozcam::io::WriteExact(g_protocol_fd, hdr, sizeof(hdr))) return false;
  if (len && !nozcam::io::WriteExact(g_protocol_fd, payload, len)) return false;
  return true;
}

// Encode a response payload as a protocol frame.
bool SendStr(std::uint32_t status, const std::string& s) {
  return SendFrame(status, s.data(), static_cast<std::uint32_t>(s.size()));
}

// Parse and apply CPU affinity list before inference work starts.
bool ApplyAffinity(const char* list) {
  cpu_set_t set;
  CPU_ZERO(&set);
  int n = 0;
  const char* p = list;
  while (*p) {
    char* end = nullptr;
    const long cpu = std::strtol(p, &end, 10);
    if (end == p) { errno = EINVAL; return false; }
    if (cpu < 0 || cpu >= CPU_SETSIZE) { errno = ERANGE; return false; }
    CPU_SET(static_cast<int>(cpu), &set);
    ++n;
    p = end;
    while (*p == ',' || *p == ' ') ++p;
  }
  if (n == 0) { errno = EINVAL; return false; }
  return ::sched_setaffinity(0, sizeof(set), &set) == 0;
}

// Report current CPU mask in comma-separated list form.
std::string CurrentCpuList() {
  cpu_set_t set;
  CPU_ZERO(&set);
  if (::sched_getaffinity(0, sizeof(set), &set) != 0) return "";
  std::string out;
  for (int c = 0; c < CPU_SETSIZE; ++c) {
    if (!CPU_ISSET(c, &set)) continue;
    if (!out.empty()) out += ',';
    char t[12];
    std::snprintf(t, sizeof(t), "%d", c);
    out += t;
  }
  return out;
}

// Return current scheduling priority or zero if query fails.
int CurrentNice() {
  errno = 0;
  const int v = ::getpriority(PRIO_PROCESS, 0);
  return errno == 0 ? v : 0;
}

// Clamp printf length to destination size minus NUL.
std::size_t ClampSnprintf(int n, std::size_t cap) {
  if (n < 0) return 0;
  const std::size_t un = static_cast<std::size_t>(n);
  return un < cap ? un : cap - 1;
}

// Escape characters that must be JSON-safe in INFO responses.
std::string JsonEscape(const std::string& s) {
  std::string o;
  for (char ch : s) {
    if (ch == '"' || ch == '\\') { o += '\\'; o += ch; }
    else if (ch == '\n') o += "\\n";
    else o += ch;
  }
  return o;
}

// Convert the HWC wire frame to the CHW FP16 layout required by VIPLite.
// Normalisation is part of the compiled graph.
void HwcU8ToChwFp16(const std::uint8_t* src, int h, int w,
                    unsigned short* dst) {
  for (int c = 0; c < 3; ++c) {
    for (int y = 0; y < h; ++y) {
      for (int x = 0; x < w; ++x) {
        const std::uint8_t px = src[(y * w + x) * 3 + c];
        dst[(c * h + y) * w + x] = fp32_to_fp16(static_cast<float>(px));
      }
    }
  }
}

// Validate AWNN tensor layout against expected width, height, and channel.
// VIPLite reports dimensions as [W, H, C, N]. Reject the wrong model early.
bool ShapeOk(const Awnn_params_t& p, int want_w, int want_h, int want_c) {
  if (p.vip_param.num_of_dims != 4) return false;
  if (static_cast<int>(p.vip_param.sizes[0]) != want_w) return false;
  if (static_cast<int>(p.vip_param.sizes[1]) != want_h) return false;
  if (static_cast<int>(p.vip_param.sizes[2]) != want_c) return false;
  if (static_cast<int>(p.vip_param.sizes[3]) != 1) return false;
  return true;
}

}  // namespace

// Run resident Allwinner daemon for the shared binary frame protocol.
int main(int argc, char** argv) {
  if (argc < 2) {
    std::fprintf(stderr,
                 "usage: %s <model.nb> [proc_w proc_h score_thr sens]"
                 " [cpus nice]\n"
                 "  binary protocol on stdin/stdout, see ../PROTOCOL.md\n",
                 argv[0]);
    return 2;
  }

  if (!nozcam::io::PrepareProtocolOutput(&g_protocol_fd)) {
    std::fprintf(stderr, "cannot prepare protocol output fd: %s\n",
                 std::strerror(errno));
    return 3;
  }

  if (argc >= 7 && argv[6][0] != '\0') {
    if (!ApplyAffinity(argv[6])) {
      std::fprintf(stderr, "WARNING: cannot pin to CPUs '%s': %s\n",
                   argv[6], std::strerror(errno));
    }
  }
  if (argc >= 8 && argv[7][0] != '\0') {
    const int want = std::atoi(argv[7]);
    if (::setpriority(PRIO_PROCESS, 0, want) != 0) {
      std::fprintf(stderr, "WARNING: cannot set niceness to %d: %s\n",
                   want, std::strerror(errno));
    }
  }

  static constexpr std::size_t kInferPrefix = 4 + 16;

  nozcam::Params pp;
  pp.proc_width = kProcW;
  pp.proc_height = kProcH;
  if (argc >= 4) { pp.proc_width = std::atoi(argv[2]);
                   pp.proc_height = std::atoi(argv[3]); }
  if (argc >= 5) pp.scores_threshold = static_cast<float>(std::atof(argv[4]));
  if (argc >= 6) pp.img_sensitivity = std::atof(argv[5]);

  const std::string model_path = argv[1];

  awnn_init();
  Awnn_Context_t* ctx = awnn_create(model_path.c_str());
  if (!ctx) {
    std::fprintf(stderr, "awnn_create failed for %s -- check dmesg / "
                          "/dev/vipcore, and that this .nb was built for "
                          "this exact silicon\n", model_path.c_str());
    return 3;
  }

  if (ctx->input_count != 1 || ctx->output_count != kNumOutputs) {
    std::fprintf(stderr,
                 "model has %u input(s) / %u output(s), expected 1 / %d\n",
                 ctx->input_count, ctx->output_count, kNumOutputs);
    return 3;
  }
  if (ctx->input_params[0].vip_param.data_format != VIP_BUFFER_FORMAT_FP16) {
    std::fprintf(stderr,
                 "input tensor data_format=%d, expected %d (FP16) -- this "
                 ".nb was not built the way this daemon assumes; probe it "
                          "with the vendor inspection tool before deploying\n",
                 static_cast<int>(ctx->input_params[0].vip_param.data_format),
                 static_cast<int>(VIP_BUFFER_FORMAT_FP16));
    return 3;
  }
  if (!ShapeOk(ctx->input_params[0], pp.proc_width, pp.proc_height, 3)) {
    std::fprintf(stderr, "input tensor shape does not match %dx%dx3\n",
                 pp.proc_width, pp.proc_height);
    return 3;
  }
  for (int i = 0; i < kNumOutputs; ++i) {
    const int l = i % kLevels;
    if (!ShapeOk(ctx->output_params[i], kW[l], kH[l], ExpectedChannels(i))) {
      std::fprintf(stderr,
                   "output %d shape mismatch (want %dx%dx%d) -- wrong model "
                   "for this graph?\n",
                   i, kW[l], kH[l], ExpectedChannels(i));
      return 3;
    }
    if (ctx->output_params[i].elements != ExpectedElems(i)) {
      std::fprintf(stderr, "output %d reports %u elements, expected %zu\n",
                   i, ctx->output_params[i].elements, ExpectedElems(i));
      return 3;
    }
  }
  std::size_t output_sizes[kNumOutputs];
  for (int i = 0; i < kNumOutputs; ++i) output_sizes[i] = ExpectedElems(i);

  const std::size_t in_elems =
      static_cast<std::size_t>(pp.proc_width) * pp.proc_height * 3;
  std::vector<std::uint8_t> in_u8(in_elems);
  std::vector<unsigned short> in_fp16(in_elems);
  void* input_buffers[1] = {in_fp16.data()};

  // Warm-up: one real inference before the pipe loop starts -- proves
  // awnn_run() actually executes, not just that awnn_create() parsed the
  // file, and moves any first-run allocator growth off the first real
  // frame. Same rationale as the other two daemons.
  std::fill(in_fp16.begin(), in_fp16.end(), fp32_to_fp16(0.0f));
  awnn_set_input_buffers(ctx, input_buffers);
  float** outs = awnn_get_output_buffers(ctx);
  if (!outs ||
      !nozcam::allwinner::RunAwnnWithObservedStatus(
          [&ctx]() { return awnn_run(ctx); }, outs, output_sizes, kNumOutputs)) {
    std::fprintf(stderr, "awnn_run warm-up failed\n");
    return 3;
  }

  std::fprintf(stderr, "daemon ready: model=%s in_bytes(HWC u8)=%zu\n",
               model_path.c_str(), in_elems);

  // ---- The resident loop: identical shape to the other two daemons ----
  for (;;) {
    std::uint32_t hdr[2];
    if (!ReadExact(STDIN_FILENO, hdr, sizeof(hdr))) break;
    const std::uint32_t cmd = hdr[0], len = hdr[1];

    if (cmd == kCmdPing) {
      if (len && !DrainBounded(STDIN_FILENO, len)) break;
      if (!SendStr(0, "pong")) break;
      continue;
    }
    if (cmd == kCmdShutdown) { SendStr(0, "bye"); break; }
    if (cmd == kCmdInfo) {
      char buf[1024];
      const int n = std::snprintf(buf, sizeof(buf),
          "{\"model\":\"%s\",\"proc_w\":%d,\"proc_h\":%d,"
          "\"in_bytes\":%zu,\"n_outputs\":%d,"
          "\"score_thr\":%.6g,\"sensitivity\":%.6g,"
          "\"cpus\":\"%s\",\"nice\":%d}",
          JsonEscape(model_path).c_str(), pp.proc_width, pp.proc_height,
          in_elems, kNumOutputs,
          static_cast<double>(pp.scores_threshold), pp.img_sensitivity,
          CurrentCpuList().c_str(), CurrentNice());
      if (!SendStr(0, std::string(buf, ClampSnprintf(n, sizeof(buf))))) break;
      continue;
    }
    if (cmd != kCmdInfer) {
      if (!SendStr(1, "unknown command")) break;
      continue;
    }

    if (len != kInferPrefix + in_elems) {
      char msg[192];
      const int n = std::snprintf(msg, sizeof(msg),
          "payload %u bytes, expected %zu "
          "(4B request_id + 16B content rect + HWC uint8 %dx%dx3)",
          len, kInferPrefix + in_elems, pp.proc_height, pp.proc_width);
      if (!SendStr(2, std::string(msg, ClampSnprintf(n, sizeof(msg))))) break;
      if (!DrainBounded(STDIN_FILENO, len)) break;
      continue;
    }
    std::uint32_t req_id = 0;
    if (!ReadExact(STDIN_FILENO, &req_id, sizeof(req_id))) break;
    std::int32_t rect[4] = {0, 0, pp.proc_width, pp.proc_height};
    if (!ReadExact(STDIN_FILENO, rect, sizeof(rect))) break;
    if (!ReadExact(STDIN_FILENO, in_u8.data(), in_elems)) break;

    const std::int64_t rx = rect[0], ry = rect[1];
    const std::int64_t rw = rect[2], rh = rect[3];
    if (!nozcam::ValidContentRect(rx, ry, rw, rh,
                                  pp.proc_width, pp.proc_height)) {
      char msg[160];
      const int n = std::snprintf(
          msg, sizeof(msg),
          "content rect (%lld,%lld,%lld,%lld) is not inside the %dx%d canvas",
          static_cast<long long>(rx), static_cast<long long>(ry),
          static_cast<long long>(rw), static_cast<long long>(rh),
          pp.proc_width, pp.proc_height);
      if (!SendStr(2, std::string(msg, ClampSnprintf(n, sizeof(msg))))) break;
      continue;
    }
    pp.content_x = static_cast<int>(rx);
    pp.content_y = static_cast<int>(ry);
    pp.content_width = static_cast<int>(rw);
    pp.content_height = static_cast<int>(rh);
    HwcU8ToChwFp16(in_u8.data(), pp.proc_height, pp.proc_width,
                   in_fp16.data());

    awnn_set_input_buffers(ctx, input_buffers);
    outs = awnn_get_output_buffers(ctx);
    if (!nozcam::allwinner::RunAwnnWithObservedStatus(
            [&ctx]() { return awnn_run(ctx); }, outs, output_sizes,
            kNumOutputs)) {
      if (!SendStr(3, "awnn_run failed")) break;
      continue;
    }

    std::vector<nozcam::HeadLevel> levels(kLevels);
    for (int l = 0; l < kLevels; ++l) {
      levels[l].cls = outs[l];
      levels[l].box = outs[kLevels + l];
      levels[l].height = kH[l];
      levels[l].width = kW[l];
    }
    const nozcam::Result r = nozcam::Postprocess(levels, pp);

    std::string js = "{\"scores\":[";
    char tmp[96];
    for (std::size_t i = 0; i < r.detections.size(); ++i) {
      std::snprintf(tmp, sizeof(tmp), "%s%.17g", i ? "," : "",
                    static_cast<double>(r.detections[i].score));
      js += tmp;
    }
    js += "],\"boxes\":[";
    for (std::size_t i = 0; i < r.detections.size(); ++i) {
      const nozcam::Detection& d = r.detections[i];
      std::snprintf(tmp, sizeof(tmp), "%s[%.17g,%.17g,%.17g,%.17g]",
                    i ? "," : "", d.x1, d.y1, d.x2, d.y2);
      js += tmp;
    }
    js += "],\"labels\":[";
    for (std::size_t i = 0; i < r.detections.size(); ++i) {
      std::snprintf(tmp, sizeof(tmp), "%s%d", i ? "," : "",
                    r.detections[i].label);
      js += tmp;
    }
    std::snprintf(tmp, sizeof(tmp),
                  "],\"severity\":%.17g,\"percentage_area\":%.17g,",
                  r.severity, r.percentage_area);
    js += tmp;
    std::snprintf(tmp, sizeof(tmp), "\"total_area\":%d,\"req_id\":%u}",
                  r.total_area, req_id);
    js += tmp;

    if (!SendStr(0, js)) break;
  }

  awnn_destroy(ctx);
  awnn_uninit();
  if (g_protocol_fd >= 0) ::close(g_protocol_fd);
  return 0;
}
