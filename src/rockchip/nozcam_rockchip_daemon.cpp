// Resident runner for Rockchip RK3566, RK3576 and RK3588 NPUs.
// It implements ../PROTOCOL.md and uses the shared postprocessor in
// ../common. The vendor RKNN SDK and runtime are external dependencies;
// build and deployment requirements are documented in README.md.

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <string>
#include <sys/stat.h>
#include <vector>

#include <sched.h>
#include <sys/resource.h>
#include <unistd.h>

#include "nozcam_postprocess.h"
#include "nozcam_protocol_io.h"
#include "rknn_api.h"

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

// Return expected channels for head i.
int ExpectedChannels(int i) {
  return (i < kLevels) ? kAnchors * kClasses : kAnchors * 4;
}

// Return expected elements for head i after layout conversion.
std::size_t ExpectedElems(int i) {
  const int l = i % kLevels;
  return static_cast<std::size_t>(ExpectedChannels(i)) * kH[l] * kW[l];
}

// Read exactly n bytes with partial-read handling and EINTR retry.
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

// Discard exactly n bytes in bounded memory chunks.
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

int g_protocol_fd = -1;

// Send one protocol frame (status + length + optional payload).
bool SendFrame(std::uint32_t status, const void* payload, std::uint32_t len) {
  std::uint32_t hdr[2] = {status, len};
  if (!nozcam::io::WriteExact(g_protocol_fd, hdr, sizeof(hdr))) return false;
  if (len && !nozcam::io::WriteExact(g_protocol_fd, payload, len)) return false;
  return true;
}

// Send a string payload as a protocol frame.
bool SendStr(std::uint32_t status, const std::string& s) {
  return SendFrame(status, s.data(), static_cast<std::uint32_t>(s.size()));
}

// Parse cpus argument and pin process affinity before startup.
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

// Build current affinity mask as a comma-separated CPU list.
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

// Read current nice value with errno-safe POSIX query.
int CurrentNice() {
  errno = 0;
  const int v = ::getpriority(PRIO_PROCESS, 0);
  return errno == 0 ? v : 0;
}

// Clamp snprintf output length to keep room for string terminator.
std::size_t ClampSnprintf(int n, std::size_t cap) {
  if (n < 0) return 0;
  const std::size_t un = static_cast<std::size_t>(n);
  return un < cap ? un : cap - 1;
}

// Escape double-quote and control characters for JSON messages.
std::string JsonEscape(const std::string& s) {
  std::string o;
  for (char ch : s) {
    if (ch == '"' || ch == '\\') { o += '\\'; o += ch; }
    else if (ch == '\n') o += "\\n";
    else o += ch;
  }
  return o;
}

// Batch-one NHWC has the same order as the HWC wire frame, so only the
// element type changes here. Normalisation is part of the compiled graph.
void HwcU8ToF32(const std::uint8_t* src, float* dst, std::size_t n) {
  for (std::size_t i = 0; i < n; ++i) dst[i] = static_cast<float>(src[i]);
}

// Detect supported Rockchip SoC family from device-tree compatibility list.
// A board name is not a reliable SoC identifier; the device tree is.
bool DetectChip(std::string* out) {
  FILE* f = std::fopen("/proc/device-tree/compatible", "rb");
  if (!f) return false;
  char buf[512];
  const std::size_t n = std::fread(buf, 1, sizeof(buf) - 1, f);
  std::fclose(f);
  if (n == 0) return false;
  buf[n] = '\0';
  for (std::size_t i = 0; i < n;) {
    const char* entry = buf + i;
    const char* comma = std::strchr(entry, ',');
    if (comma && std::strncmp(entry, "rockchip,", 9) == 0) {
      const char* chip = comma + 1;
      if (std::strchr(chip, '-') == nullptr) {   // skip board entries
        *out = chip;
        return true;
      }
    }
    i += std::strlen(entry) + 1;
  }
  return false;
}

// Map SoC name to core count for runtime configuration.
// A single-core device must not receive an explicit RKNN core mask.
int CoreCountFor(const std::string& chip) {
  if (chip == "rk3588") return 3;
  if (chip == "rk3576") return 2;
  return 1;
}

// Resolve model path inside a directory by matching '*<chip>*.rknn'.
// Select <directory>/*<chip>*.rknn when the caller supplies a directory.
bool FindModelInDir(const std::string& dir, const std::string& chip,
                    std::string* out) {
  DIR* d = ::opendir(dir.c_str());
  if (!d) return false;
  struct dirent* e;
  bool found = false;
  while ((e = ::readdir(d)) != nullptr) {
    const std::string name = e->d_name;
    if (name.size() < 6 || name.substr(name.size() - 5) != ".rknn") continue;
    if (name.find(chip) == std::string::npos) continue;
    *out = dir + "/" + name;
    found = true;
    break;
  }
  ::closedir(d);
  return found;
}

// Load whole model file into memory once before initialization.
void* ReadWholeFile(const std::string& path, std::size_t* size_out) {
  FILE* f = std::fopen(path.c_str(), "rb");
  if (!f) return nullptr;
  std::fseek(f, 0, SEEK_END);
  const long size = std::ftell(f);
  std::fseek(f, 0, SEEK_SET);
  if (size <= 0) { std::fclose(f); return nullptr; }
  void* buf = std::malloc(static_cast<std::size_t>(size));
  if (buf && std::fread(buf, 1, static_cast<std::size_t>(size), f) !=
                 static_cast<std::size_t>(size)) {
    std::free(buf);
    buf = nullptr;
  }
  std::fclose(f);
  if (buf) *size_out = static_cast<std::size_t>(size);
  return buf;
}

}  // namespace

// Run resident RKNN daemon and serve fixed-size binary protocol requests.
int main(int argc, char** argv) {
  if (argc < 2) {
    std::fprintf(stderr,
                 "usage: %s <model.rknn-or-dir> [proc_w proc_h score_thr sens]"
                 " [cpus nice]\n"
                 "  <model.rknn-or-dir>: an exact .rknn path, or a directory\n"
                 "    containing nozcam-<chip>.rknn (chip auto-detected)\n"
                 "  cpus  like \"0,1,2\"; empty = leave affinity alone\n"
                 "  nice  scheduling niceness; positive = yield to others\n"
                 "  binary protocol on stdin/stdout, see ../PROTOCOL.md\n",
                 argv[0]);
    return 2;
  }

  if (!nozcam::io::PrepareProtocolOutput(&g_protocol_fd)) {
    std::fprintf(stderr, "cannot prepare protocol output fd: %s\n",
                 std::strerror(errno));
    return 3;
  }

  // Apply scheduling before the vendor runtime creates worker threads.
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

  // ---- Chip identification, once, before touching the model ----
  std::string chip;
  if (!DetectChip(&chip)) {
    std::fprintf(stderr, "cannot read the SoC name from "
                          "/proc/device-tree/compatible\n");
    return 3;
  }
  const int cores = CoreCountFor(chip);

  // ---- Resolve the model path: exact file, or pick from a directory ----
  std::string model_path = argv[1];
  struct stat st;
  if (::stat(argv[1], &st) == 0 && S_ISDIR(st.st_mode)) {
    if (!FindModelInDir(argv[1], chip, &model_path)) {
      std::fprintf(stderr,
                   "no *%s*.rknn found in %s -- this chip needs its own "
                   "build; a model for another chip is refused by "
                   "rknn_init, it cannot be reused\n",
                   chip.c_str(), argv[1]);
      return 3;
    }
  }

  // ---- One-time load: read the file, init the NPU context ----
  std::size_t model_size = 0;
  void* model_buf = ReadWholeFile(model_path, &model_size);
  if (!model_buf) {
    std::fprintf(stderr, "cannot read %s\n", model_path.c_str());
    return 3;
  }
  rknn_context ctx = 0;
  const int init_ret =
      rknn_init(&ctx, model_buf, static_cast<uint32_t>(model_size), 0, nullptr);
  std::free(model_buf);   // rknn_init copies what it needs
  if (init_ret < 0) {
    std::fprintf(stderr,
                 "rknn_init failed (%d) for %s -- if the runtime log above "
                 "says the model is for another platform, this .rknn was "
                 "built for a different chip and must be rebuilt with "
                 "target_platform='%s'\n",
                 init_ret, model_path.c_str(), chip.c_str());
    return 3;
  }

  // Core mask ONLY on chips that support it. Calling this on a single-core
  // chip is not merely a no-op -- the validated behaviour (measured via
  // rknnlite, the C API shares the same underlying check) is that it makes
  // initialisation FAIL, so it must not be attempted at all rather than
  // attempted-and-ignored-on-error.
  if (cores > 1) {
    const rknn_core_mask mask =
        (cores == 3) ? RKNN_NPU_CORE_0_1_2 : RKNN_NPU_CORE_0_1;
    if (rknn_set_core_mask(ctx, mask) != 0) {
      std::fprintf(stderr, "WARNING: rknn_set_core_mask failed for %s "
                            "(%d cores) -- continuing on default core\n",
                   chip.c_str(), cores);
    }
  }

  // ---- Output shape validation, ONCE (see file header for why once is
  // enough here, unlike the CPU daemon's per-frame check) ----
  rknn_input_output_num io;
  if (rknn_query(ctx, RKNN_QUERY_IN_OUT_NUM, &io, sizeof(io)) != 0) {
    std::fprintf(stderr, "rknn_query IN_OUT_NUM failed\n");
    return 3;
  }
  if (io.n_input != 1 || io.n_output != kNumOutputs) {
    std::fprintf(stderr,
                 "model has %u input(s) / %u output(s), expected 1 / %d\n",
                 io.n_input, io.n_output, kNumOutputs);
    return 3;
  }
  for (int i = 0; i < kNumOutputs; ++i) {
    rknn_tensor_attr attr;
    std::memset(&attr, 0, sizeof(attr));
    attr.index = static_cast<uint32_t>(i);
    if (rknn_query(ctx, RKNN_QUERY_OUTPUT_ATTR, &attr, sizeof(attr)) != 0) {
      std::fprintf(stderr, "rknn_query OUTPUT_ATTR failed for output %d\n", i);
      return 3;
    }
    // Same per-dimension check as the CPU daemon, and for the same reason:
    // numel alone cannot catch a channel/spatial swap. RKNN reports NCHW
    // dims for the *declared* shape regardless of the runtime data_format,
    // so dims are [1, C, H, W] or [C, H, W].
    const int l = i % kLevels;
    const int want_c = ExpectedChannels(i);
    const bool rank_ok = (attr.n_dims == 3 || attr.n_dims == 4);
    const int off = (attr.n_dims == 4) ? 1 : 0;
    const int got_c = rank_ok ? static_cast<int>(attr.dims[off]) : -1;
    const int got_h = rank_ok ? static_cast<int>(attr.dims[off + 1]) : -1;
    const int got_w = rank_ok ? static_cast<int>(attr.dims[off + 2]) : -1;
    if (!rank_ok || got_c != want_c || got_h != kH[l] || got_w != kW[l]) {
      std::fprintf(stderr,
                   "output %d is shape [%d,%d,%d] (n_dims=%u), expected "
                   "[%d,%d,%d] -- wrong model for this graph?\n",
                   i, got_c, got_h, got_w, attr.n_dims, want_c, kH[l], kW[l]);
      return 3;
    }
    if (attr.n_elems != ExpectedElems(i)) {
      std::fprintf(stderr,
                   "output %d reports %u elements, expected %zu\n",
                   i, attr.n_elems, ExpectedElems(i));
      return 3;
    }
  }

  const std::size_t in_elems =
      static_cast<std::size_t>(pp.proc_width) * pp.proc_height * 3;
  std::vector<float> in_f32(in_elems);
  std::vector<std::uint8_t> in_u8(in_elems);

  rknn_input rk_in;
  std::memset(&rk_in, 0, sizeof(rk_in));
  rk_in.index = 0;
  rk_in.buf = in_f32.data();
  rk_in.size = static_cast<uint32_t>(in_elems * sizeof(float));
  rk_in.type = RKNN_TENSOR_FLOAT32;
  rk_in.fmt = RKNN_TENSOR_NHWC;   // see file header: no transpose needed

  rknn_output rk_out[kNumOutputs];
  std::memset(rk_out, 0, sizeof(rk_out));
  for (int i = 0; i < kNumOutputs; ++i) {
    rk_out[i].index = static_cast<uint32_t>(i);
    rk_out[i].want_float = 1;
  }

  // Warm-up: one real inference before the pipe loop starts, same rationale
  // as the CPU daemon (moves allocator growth off the first real frame, and
  // proves execute() actually runs, not just that the model file loaded).
  std::fill(in_f32.begin(), in_f32.end(), 0.0f);
  if (rknn_inputs_set(ctx, 1, &rk_in) == 0 && rknn_run(ctx, nullptr) == 0 &&
      rknn_outputs_get(ctx, kNumOutputs, rk_out, nullptr) == 0) {
    rknn_outputs_release(ctx, kNumOutputs, rk_out);
  }

  std::fprintf(stderr,
               "daemon ready: chip=%s cores=%d model=%s in_bytes(HWC "
               "u8)=%zu\n",
               chip.c_str(), cores, model_path.c_str(), in_elems);

  // ---- The resident loop: identical shape to the CPU daemon ----
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
    HwcU8ToF32(in_u8.data(), in_f32.data(), in_elems);

    if (rknn_inputs_set(ctx, 1, &rk_in) != 0) {
      if (!SendStr(3, "rknn_inputs_set failed")) break;
      continue;
    }
    if (rknn_run(ctx, nullptr) != 0) {
      if (!SendStr(3, "rknn_run failed")) break;
      continue;
    }
    if (rknn_outputs_get(ctx, kNumOutputs, rk_out, nullptr) != 0) {
      if (!SendStr(3, "rknn_outputs_get failed")) break;
      continue;
    }

    std::vector<nozcam::HeadLevel> levels(kLevels);
    for (int l = 0; l < kLevels; ++l) {
      levels[l].cls = static_cast<const float*>(rk_out[l].buf);
      levels[l].box = static_cast<const float*>(rk_out[kLevels + l].buf);
      levels[l].height = kH[l];
      levels[l].width = kW[l];
    }
    const nozcam::Result r = nozcam::Postprocess(levels, pp);
    rknn_outputs_release(ctx, kNumOutputs, rk_out);

    std::string js = "{\"scores\":[";
    char tmp[128];
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

  if (g_protocol_fd >= 0) ::close(g_protocol_fd);
  rknn_destroy(ctx);
  return 0;
}
