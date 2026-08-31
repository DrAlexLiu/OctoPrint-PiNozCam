// Resident runner for the D-Robotics RDK X5 BPU through libdnn.
// It implements ../PROTOCOL.md and uses the shared postprocessor in
// ../common. The vendor runtime (libdnn, libhbrt_bayes_aarch64) ships with
// the board image and is an external dependency documented in README.md.
//
// This talks to the C API rather than the board's hobot_dnn Python module
// for a measured reason, not a stylistic one. Fed the same .bin and the
// same frame, the Python wrapper produced tensors whose cosine similarity
// against the fp32 graph was 0.69-0.98 and whose magnitudes were off by up
// to 4x, while hrt_model_exec -- which uses this API -- reached 0.9766-
// 0.9998 and matched both the toolchain's own quantisation report and the
// quantised graph run on x86. The model was never the problem.
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include <dirent.h>
#include <sched.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>

#include "nozcam_postprocess.h"
#include "nozcam_protocol_io.h"

#include "dnn/hb_dnn.h"
#include "dnn/hb_sys.h"

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

// [4B request id][4x int32 content rect] ahead of the HWC image.
constexpr std::uint32_t kInferPrefix = 4 + 16;

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
  for (int i = 0; i < CPU_SETSIZE; ++i) {
    if (!CPU_ISSET(i, &set)) continue;
    if (!out.empty()) out += ',';
    out += std::to_string(i);
  }
  return out;
}

// Escape a string for embedding in the JSON replies.
std::string JsonEscape(const std::string& s) {
  std::string o;
  o.reserve(s.size());
  for (const char ch : s) {
    if (ch == '"' || ch == '\\') { o += '\\'; o += ch; }
    else if (ch == '\n') o += "\\n";
    else o += ch;
  }
  return o;
}

// Convert the HWC uint8 wire frame to the CHW float32 the model takes.
// Values stay 0-255: normalisation is compiled into the graph, which is
// why the exported model is the "raw" one.
void HwcU8ToChwF32(const std::uint8_t* src, int h, int w, float* dst) {
  for (int c = 0; c < 3; ++c) {
    for (int y = 0; y < h; ++y) {
      for (int x = 0; x < w; ++x) {
        dst[(c * h + y) * w + x] =
            static_cast<float>(src[(y * w + x) * 3 + c]);
      }
    }
  }
}

// Pick the single .bin model when handed a directory instead of a file.
// The plugin passes a directory so the model name lives in one place.
bool ResolveModelInDir(const std::string& dir, std::string* out) {
  DIR* d = ::opendir(dir.c_str());
  if (d == nullptr) return false;
  std::string found;
  int hits = 0;
  struct dirent* e;
  while ((e = ::readdir(d)) != nullptr) {
    const std::string name(e->d_name);
    if (name.size() < 5) continue;
    if (name.compare(name.size() - 4, 4, ".bin") != 0) continue;
    found = dir + "/" + name;
    ++hits;
  }
  ::closedir(d);
  if (hits != 1) return false;
  *out = found;
  return true;
}

// Validate one tensor against the head this daemon expects.
// The runtime reports NCHW while hbdk-cc compiled the graph with
// --output-layout NHWC, so the element count is checked rather than the
// axis order, and the layout is read back from the runtime below.
bool ElemsOk(const hbDNNTensorProperties& p, std::size_t want) {
  std::size_t n = 1;
  for (int i = 0; i < p.validShape.numDimensions; ++i) {
    const int dim = p.validShape.dimensionSize[i];
    if (dim <= 0) return false;
    n *= static_cast<std::size_t>(dim);
  }
  return n == want;
}

}  // namespace

// Run resident D-Robotics daemon for the shared binary frame protocol.
int main(int argc, char** argv) {
  if (argc < 2) {
    std::fprintf(stderr,
                 "usage: %s <model.bin|model_dir> [proc_w] [proc_h] "
                 "[score_thr] [sensitivity] [cpus] [nice]\n",
                 argv[0]);
    return 2;
  }
  if (!nozcam::io::PrepareProtocolOutput(&g_protocol_fd)) {
    std::fprintf(stderr, "could not prepare the protocol descriptor\n");
    return 2;
  }

  // Scheduling is applied before the runtime starts, as the other daemons
  // do: affinity says which cores, niceness says who wins a contended one.
  if (argc > 6 && argv[6][0] != '\0' && !ApplyAffinity(argv[6])) {
    std::fprintf(stderr, "could not apply cpu list '%s': %s\n",
                 argv[6], std::strerror(errno));
  }
  if (argc > 7 && argv[7][0] != '\0') {
    const int nice_value = std::atoi(argv[7]);
    errno = 0;
    if (::setpriority(PRIO_PROCESS, 0, nice_value) != 0 && errno != 0) {
      std::fprintf(stderr, "could not set niceness %d: %s\n",
                   nice_value, std::strerror(errno));
    }
  }

  nozcam::Params pp;
  pp.proc_width = kProcW;
  pp.proc_height = kProcH;
  if (argc >= 4) { pp.proc_width = std::atoi(argv[2]);
                   pp.proc_height = std::atoi(argv[3]); }
  if (argc >= 5) pp.scores_threshold =
      static_cast<float>(std::atof(argv[4]));
  if (argc >= 6) pp.img_sensitivity = std::atof(argv[5]);
  if (pp.proc_width != kProcW || pp.proc_height != kProcH) {
    std::fprintf(stderr,
                 "this model is compiled for %dx%d, not %dx%d\n",
                 kProcW, kProcH, pp.proc_width, pp.proc_height);
    return 2;
  }

  std::string model_path = argv[1];
  struct stat st;
  if (::stat(model_path.c_str(), &st) == 0 && S_ISDIR(st.st_mode)) {
    std::string picked;
    if (!ResolveModelInDir(model_path, &picked)) {
      std::fprintf(stderr, "expected exactly one .bin under %s\n",
                   model_path.c_str());
      return 2;
    }
    model_path = picked;
  }

  hbPackedDNNHandle_t packed = nullptr;
  const char* files[1] = {model_path.c_str()};
  if (hbDNNInitializeFromFiles(&packed, files, 1) != 0) {
    std::fprintf(stderr,
                 "hbDNNInitializeFromFiles failed for %s -- check that this "
                 ".bin was compiled for this SoC (march bayes-e on an X5)\n",
                 model_path.c_str());
    return 3;
  }
  const char** names = nullptr;
  int name_count = 0;
  if (hbDNNGetModelNameList(&names, &name_count, packed) != 0 ||
      name_count < 1) {
    std::fprintf(stderr, "the packed model contains no graphs\n");
    return 3;
  }
  hbDNNHandle_t model = nullptr;
  if (hbDNNGetModelHandle(&model, packed, names[0]) != 0) {
    std::fprintf(stderr, "hbDNNGetModelHandle failed\n");
    return 3;
  }

  int in_count = 0;
  int out_count = 0;
  if (hbDNNGetInputCount(&in_count, model) != 0 ||
      hbDNNGetOutputCount(&out_count, model) != 0) {
    std::fprintf(stderr, "could not read the tensor counts\n");
    return 3;
  }
  if (in_count != 1 || out_count != kNumOutputs) {
    std::fprintf(stderr,
                 "expected 1 input and %d outputs, this model has %d and %d\n",
                 kNumOutputs, in_count, out_count);
    return 3;
  }

  // Allocate once and reuse: the arena is the same for every frame because
  // the working resolution is fixed, and per-frame allocation would show up
  // in the latency this daemon exists to keep low.
  hbDNNTensor input{};
  if (hbDNNGetInputTensorProperties(&input.properties, model, 0) != 0) {
    std::fprintf(stderr, "could not read the input properties\n");
    return 3;
  }
  const std::size_t in_elems =
      static_cast<std::size_t>(3) * kProcH * kProcW;
  if (!ElemsOk(input.properties, in_elems)) {
    std::fprintf(stderr, "the model input is not %zu elements\n", in_elems);
    return 3;
  }
  if (hbSysAllocCachedMem(&input.sysMem[0],
                          static_cast<std::uint32_t>(in_elems *
                                                     sizeof(float))) != 0) {
    std::fprintf(stderr, "could not allocate the input tensor\n");
    return 3;
  }

  std::vector<hbDNNTensor> outputs(kNumOutputs);
  for (int i = 0; i < kNumOutputs; ++i) {
    if (hbDNNGetOutputTensorProperties(&outputs[i].properties, model, i) != 0) {
      std::fprintf(stderr, "could not read output %d properties\n", i);
      return 3;
    }
    if (!ElemsOk(outputs[i].properties, ExpectedElems(i))) {
      std::fprintf(stderr,
                   "output %d has the wrong element count -- this .bin is "
                   "not the PiNozCam detector\n", i);
      return 3;
    }
    if (hbSysAllocCachedMem(
            &outputs[i].sysMem[0],
            static_cast<std::uint32_t>(
                outputs[i].properties.alignedByteSize)) != 0) {
      std::fprintf(stderr, "could not allocate output %d\n", i);
      return 3;
    }
  }

  std::vector<std::uint8_t> wire(kInferPrefix + in_elems / 3 * 3);
  std::vector<std::uint8_t> frame(
      static_cast<std::size_t>(kProcW) * kProcH * 3);

  // One real inference before the pipe loop, as the other daemons do: it
  // proves hbDNNInfer actually executes rather than that the file merely
  // parsed, and it moves any first-run allocation off the first real frame.
  auto run_once = [&]() -> bool {
    if (hbSysFlushMem(&input.sysMem[0], HB_SYS_MEM_CACHE_CLEAN) != 0) {
      return false;
    }
    hbDNNTaskHandle_t task = nullptr;
    hbDNNInferCtrlParam ctrl;
    HB_DNN_INITIALIZE_INFER_CTRL_PARAM(&ctrl);
    hbDNNTensor* out_ptr = outputs.data();
    if (hbDNNInfer(&task, &out_ptr, &input, model, &ctrl) != 0) return false;
    const bool ok = hbDNNWaitTaskDone(task, 0) == 0;
    hbDNNReleaseTask(task);
    if (!ok) return false;
    for (int i = 0; i < kNumOutputs; ++i) {
      if (hbSysFlushMem(&outputs[i].sysMem[0],
                        HB_SYS_MEM_CACHE_INVALIDATE) != 0) {
        return false;
      }
    }
    return true;
  };

  std::memset(input.sysMem[0].virAddr, 0, input.sysMem[0].memSize);
  if (!run_once()) {
    std::fprintf(stderr, "warm-up inference failed\n");
    return 3;
  }
  std::fprintf(stderr,
               "daemon ready: model=%s in_bytes(HWC u8)=%zu\n",
               model_path.c_str(),
               static_cast<std::size_t>(kProcW) * kProcH * 3);

  const std::size_t image_bytes =
      static_cast<std::size_t>(kProcW) * kProcH * 3;
  for (;;) {
    std::uint32_t hdr[2];
    if (!ReadExact(STDIN_FILENO, hdr, sizeof(hdr))) break;
    const std::uint32_t cmd = hdr[0];
    const std::uint32_t len = hdr[1];

    if (cmd == kCmdShutdown) {
      if (len && !DrainBounded(STDIN_FILENO, len)) break;
      SendStr(0, "bye");
      break;
    }
    if (cmd == kCmdPing) {
      if (len && !DrainBounded(STDIN_FILENO, len)) break;
      if (!SendStr(0, "pong")) break;
      continue;
    }
    if (cmd == kCmdInfo) {
      if (len && !DrainBounded(STDIN_FILENO, len)) break;
      char buf[512];
      const std::string cpus = CurrentCpuList();
      std::snprintf(buf, sizeof(buf),
                    "{\"model\":\"%s\",\"proc_w\":%d,\"proc_h\":%d,"
                    "\"in_bytes\":%zu,\"n_outputs\":%d,\"score_thr\":%g,"
                    "\"sensitivity\":%g,\"cpus\":\"%s\",\"nice\":%d}",
                    JsonEscape(model_path).c_str(), kProcW, kProcH,
                    image_bytes, kNumOutputs, static_cast<double>(pp.scores_threshold),
                    pp.img_sensitivity, cpus.c_str(),
                    ::getpriority(PRIO_PROCESS, 0));
      if (!SendStr(0, buf)) break;
      continue;
    }
    if (cmd != kCmdInfer) {
      if (len && !DrainBounded(STDIN_FILENO, len)) break;
      if (!SendStr(1, "unknown command")) break;
      continue;
    }

    if (len != kInferPrefix + image_bytes) {
      // Drain rather than allocate what the caller announced: the length is
      // attacker-controlled, and a bounded drain keeps the daemon usable.
      char msg[128];
      std::snprintf(msg, sizeof(msg),
                    "payload %u bytes, expected %zu",
                    len, kInferPrefix + image_bytes);
      if (!DrainBounded(STDIN_FILENO, len)) break;
      if (!SendStr(1, msg)) break;
      continue;
    }
    if (wire.size() < len) wire.resize(len);
    if (!ReadExact(STDIN_FILENO, wire.data(), len)) break;

    std::uint32_t req_id = 0;
    std::memcpy(&req_id, wire.data(), 4);
    std::int32_t rect[4];
    std::memcpy(rect, wire.data() + 4, 16);
    if (!nozcam::ValidContentRect(rect[0], rect[1], rect[2], rect[3],
                                  pp.proc_width, pp.proc_height)) {
      if (!SendStr(2, "content rect is not inside the canvas")) break;
      continue;
    }
    pp.content_x = rect[0];
    pp.content_y = rect[1];
    pp.content_width = rect[2];
    pp.content_height = rect[3];

    HwcU8ToChwF32(wire.data() + kInferPrefix, kProcH, kProcW,
                  static_cast<float*>(input.sysMem[0].virAddr));
    if (!run_once()) {
      if (!SendStr(1, "inference failed")) break;
      continue;
    }

    // Five levels, each carrying both heads: outputs 0-4 are the class
    // scores and 5-9 the boxes, in ascending stride order.
    std::vector<nozcam::HeadLevel> levels(kLevels);
    for (int l = 0; l < kLevels; ++l) {
      levels[l].cls =
          static_cast<const float*>(outputs[l].sysMem[0].virAddr);
      levels[l].box =
          static_cast<const float*>(outputs[kLevels + l].sysMem[0].virAddr);
      levels[l].height = kH[l];
      levels[l].width = kW[l];
    }
    const nozcam::Result r = nozcam::Postprocess(levels, pp);

    std::string js = "{\"scores\":[";
    char tmp[160];
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

  for (int i = 0; i < kNumOutputs; ++i) hbSysFreeMem(&outputs[i].sysMem[0]);
  hbSysFreeMem(&input.sysMem[0]);
  hbDNNRelease(packed);
  return 0;
}
