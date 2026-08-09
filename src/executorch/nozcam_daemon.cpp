// Resident ExecuTorch runner using a little-endian stdin/stdout protocol:
//   request   [4B cmd][4B len][payload]
//   response  [4B status][4B len][payload]
//   cmd 1 PING      empty payload                     -> "pong"
//   cmd 2 INFER     payload = [4B req_id][16B rect][HWC uint8] -> JSON
//                   rect = 4 x int32 x, y, w, h content rectangle
//   cmd 3 INFO      empty payload                     -> JSON metadata
//   cmd 4 SHUTDOWN  empty payload                     -> "bye", then exits
//
// The fixed eight-byte header remains backward-compatible; request IDs and
// content rectangles live in the INFER payload. Python sends HWC uint8 and
// performs the production Pillow fit; this process converts to CHW float.

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include <sched.h>
#include <sys/resource.h>
#include <unistd.h>

#include <executorch/extension/data_loader/file_data_loader.h>
#include <executorch/extension/runner_util/inputs.h>
#include <executorch/runtime/executor/program.h>
#include <executorch/runtime/platform/log.h>
#include <executorch/runtime/platform/runtime.h>

#include "nozcam_postprocess.h"

using executorch::extension::FileDataLoader;
using executorch::runtime::Error;
using executorch::runtime::EValue;
using executorch::runtime::HierarchicalAllocator;
using executorch::runtime::MemoryAllocator;
using executorch::runtime::MemoryManager;
using executorch::runtime::Method;
using executorch::runtime::Program;
using executorch::runtime::Span;
using executorch::runtime::Result;
using executorch::runtime::Tag;
using executorch::aten::TensorImpl;

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

// Validate every dimension before postprocess indexes raw output buffers.
//   score head l: (anchors*classes, H, W)
//   box   head l: (anchors*4,       H, W)
int ExpectedChannels(int i) {
  return (i < kLevels) ? kAnchors * kClasses : kAnchors * 4;
}

// Compute the expected output tensor element count for head i.
std::size_t ExpectedElems(int i) {
  const int l = i % kLevels;
  return static_cast<std::size_t>(ExpectedChannels(i)) * kH[l] * kW[l];
}

// Read exactly n bytes, handling partial reads and EINTR.
bool ReadExact(int fd, void* buf, std::size_t n) {
  auto* p = static_cast<std::uint8_t*>(buf);
  while (n > 0) {
    const ssize_t r = ::read(fd, p, n);
    if (r == 0) return false;              // EOF: the peer closed
    if (r < 0) {
      if (errno == EINTR) continue;
      return false;
    }
    p += r;
    n -= static_cast<std::size_t>(r);
  }
  return true;
}

// Discard an untrusted payload length using bounded memory.
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

// Write exactly n bytes to fd, retrying on EINTR and rejecting short writes.
bool WriteExact(int fd, const void* buf, std::size_t n) {
  const auto* p = static_cast<const std::uint8_t*>(buf);
  while (n > 0) {
    const ssize_t w = ::write(fd, p, n);
    if (w == 0) return false;  // no progress: do not spin forever
    if (w < 0) {
      if (errno == EINTR) continue;
      return false;
    }
    p += w;
    n -= static_cast<std::size_t>(w);
  }
  return true;
}

// Send one protocol response header followed by its optional binary payload.
bool SendFrame(std::uint32_t status, const void* payload, std::uint32_t len) {
  std::uint32_t hdr[2] = {status, len};
  if (!WriteExact(STDOUT_FILENO, hdr, sizeof(hdr))) return false;
  if (len && !WriteExact(STDOUT_FILENO, payload, len)) return false;
  return true;
}

// Convenience wrapper for the JSON and diagnostic string responses.
bool SendStr(std::uint32_t status, const std::string& s) {
  return SendFrame(status, s.data(), static_cast<std::uint32_t>(s.size()));
}

// HWC uint8 -> CHW float32, values 0-255 and NOT normalised -- the
// normalisation lives inside the graph. Equivalent to
// np.asarray(im, dtype=np.float32).transpose(2,0,1); verified bit-identical.
void HwcU8ToChwF32(const std::uint8_t* src, float* dst, int h, int w) {
  const std::size_t plane = static_cast<std::size_t>(h) * w;
  for (int c = 0; c < 3; ++c) {
    float* out = dst + static_cast<std::size_t>(c) * plane;
    const std::uint8_t* in = src + c;
    for (std::size_t i = 0; i < plane; ++i) {
      out[i] = static_cast<float>(in[i * 3]);
    }
  }
}

// "0,1,2" -> cpu_set_t, then sched_setaffinity(0, ...) applied to OURSELVES.
// An empty string or empty list means leave affinity alone: the parent asked
// for nothing.
bool ApplyAffinity(const char* list) {
  cpu_set_t set;
  CPU_ZERO(&set);
  int n = 0;
  const char* p = list;
  while (*p) {
    char* end = nullptr;
    const long cpu = std::strtol(p, &end, 10);
    if (end == p) { errno = EINVAL; return false; }      // not a number
    if (cpu < 0 || cpu >= CPU_SETSIZE) { errno = ERANGE; return false; }
    CPU_SET(static_cast<int>(cpu), &set);
    ++n;
    p = end;
    while (*p == ',' || *p == ' ') ++p;
  }
  if (n == 0) { errno = EINVAL; return false; }
  return ::sched_setaffinity(0, sizeof(set), &set) == 0;
}

// The affinity actually in force, in the same format the argument uses. INFO
// reports the value READ BACK from the kernel rather than the one we asked
// for: the client needs to see what took effect, not what we intended.
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

// getpriority returning -1 is either a legitimate nice value or an error,
// and only errno distinguishes them. That is POSIX's interface design, not a
// mistake here.
int CurrentNice() {
  errno = 0;
  const int v = ::getpriority(PRIO_PROCESS, 0);
  return errno == 0 ? v : 0;
}

// snprintf returns the length it WOULD have written, which can exceed the
// buffer. Constructing a std::string from it directly reads out of bounds --
// reachable with a long enough model path -- so it has to be clamped.
std::size_t ClampSnprintf(int n, std::size_t cap) {
  if (n < 0) return 0;
  const std::size_t un = static_cast<std::size_t>(n);
  return un < cap ? un : cap - 1;
}

// Escape the subset needed by metadata strings emitted in INFO JSON.
std::string JsonEscape(const std::string& s) {
  std::string o;
  for (char ch : s) {
    if (ch == '"' || ch == '\\') { o += '\\'; o += ch; }
    else if (ch == '\n') o += "\\n";
    else o += ch;
  }
  return o;
}

}  // namespace

// Load the fixed-shape model once, then serve the binary pipe protocol until
// the parent closes stdin or sends SHUTDOWN.
int main(int argc, char** argv) {
  if (argc < 2) {
    std::fprintf(stderr,
                 "usage: %s <model.pte> [proc_w proc_h score_thr sens]"
                 " [cpus nice]\n"
                 "  cpus  like \"0,1,2\"; empty = leave affinity alone\n"
                 "  nice  scheduling niceness; positive = yield to others\n"
                 "  binary protocol on stdin/stdout, described at the top of "
                 "the source\n", argv[0]);
    return 2;
  }

  // Apply scheduling constraints before the runtime creates worker threads.
  //
  // Failure warns and continues rather than exiting: affinity and niceness
  // are scheduling optimisations, not part of correctness. Without them
  // detection is slower or competes for CPU (issue #11) but is still right,
  // and refusing to start over that is the worse outcome -- a print monitor
  // that gave up is indistinguishable from one that saw nothing.
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

  executorch::runtime::runtime_init();

  // 4B request id + 4 x int32 content rectangle.
  static constexpr std::size_t kInferPrefix = 4 + 16;

  nozcam::Params pp;
  pp.proc_width = kProcW;
  pp.proc_height = kProcH;
  if (argc >= 4) { pp.proc_width = std::atoi(argv[2]);
                   pp.proc_height = std::atoi(argv[3]); }
  if (argc >= 5) pp.scores_threshold = static_cast<float>(std::atof(argv[4]));
  if (argc >= 6) pp.img_sensitivity = std::atof(argv[5]);

  // ---- One-time load: about 996 ms on a CM4, paid once because we stay
  // resident ----
  Result<FileDataLoader> loader_res = FileDataLoader::from(argv[1]);
  if (!loader_res.ok()) {
    std::fprintf(stderr, "cannot load %s\n", argv[1]);
    return 3;
  }
  FileDataLoader loader = std::move(loader_res.get());
  Result<Program> prog_res = Program::load(&loader);
  if (!prog_res.ok()) { std::fprintf(stderr, "bad program\n"); return 3; }
  Program program = std::move(prog_res.get());

  const char* method_name = nullptr;
  {
    Result<const char*> m = program.get_method_name(0);
    if (!m.ok()) { std::fprintf(stderr, "no method\n"); return 3; }
    method_name = *m;
  }
  Result<executorch::runtime::MethodMeta> meta_res =
      program.method_meta(method_name);
  if (!meta_res.ok()) { std::fprintf(stderr, "no method meta\n"); return 3; }
  auto meta = meta_res.get();

  // Memory planning: the planned buffers' sizes come from the .pte. The input
  // shape is a constant 640x384, so one allocation here serves every frame --
  // which is the structural reason VmHWM stays flat.
  std::vector<std::vector<std::uint8_t>> planned;
  std::vector<Span<std::uint8_t>> planned_spans;
  const size_t n_planned = meta.num_memory_planned_buffers();
  planned.reserve(n_planned);
  planned_spans.reserve(n_planned);
  for (size_t i = 0; i < n_planned; ++i) {
    const size_t sz = static_cast<size_t>(
        meta.memory_planned_buffer_size(i).get());
    planned.emplace_back(sz);
    planned_spans.emplace_back(planned.back().data(), sz);
  }
  std::vector<std::uint8_t> method_arena(4u * 1024u * 1024u);
  MemoryAllocator method_alloc(
      static_cast<uint32_t>(method_arena.size()), method_arena.data());
  HierarchicalAllocator planned_alloc(
      Span<Span<std::uint8_t>>(planned_spans.data(), planned_spans.size()));
  MemoryManager mm(&method_alloc, &planned_alloc);

  Result<Method> method_res = program.load_method(method_name, &mm);
  if (!method_res.ok()) { std::fprintf(stderr, "load_method failed\n"); return 3; }
  Method method = std::move(method_res.get());

  const std::size_t in_elems =
      static_cast<std::size_t>(pp.proc_width) * pp.proc_height * 3;
  std::vector<float> in_f32(in_elems);
  std::vector<std::uint8_t> in_u8(in_elems);

  std::int32_t sizes[4] = {1, 3, pp.proc_height, pp.proc_width};
  TensorImpl impl(executorch::aten::ScalarType::Float, 4, sizes,
                  in_f32.data());
  executorch::aten::Tensor in_tensor(&impl);
  // ExecuTorch's static memory planning COPIES the data into a planned buffer
  // at set_input time, so overwriting in_f32 in place afterwards is invisible
  // to it -- the model keeps evaluating the initial all-zero input. Inference
  // returns normally and takes its usual time, but produces ZERO detections
  // forever, because every score falls below the threshold. A silent failure:
  // no error, no crash, only wrong results.
  if (method.set_input(EValue(in_tensor), 0) != Error::Ok) {
    std::fprintf(stderr, "set_input failed\n");
    return 3;
  }

  ET_LOG(Info, "daemon ready: model=%s method=%s inputs=%zu outputs=%zu "
               "in_bytes(HWC u8)=%zu",
         argv[1], method_name, method.inputs_size(), method.outputs_size(),
         in_elems);

  // ---- The resident loop ----
  for (;;) {
    std::uint32_t hdr[2];
    if (!ReadExact(STDIN_FILENO, hdr, sizeof(hdr))) break;   // peer closed
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
          "\"in_bytes\":%zu,\"n_outputs\":%zu,"
          "\"score_thr\":%.6g,\"sensitivity\":%.6g,"
          "\"cpus\":\"%s\",\"nice\":%d}",
          JsonEscape(argv[1]).c_str(), pp.proc_width, pp.proc_height,
          in_elems, method.outputs_size(),
          static_cast<double>(pp.scores_threshold), pp.img_sensitivity,
          CurrentCpuList().c_str(), CurrentNice());
      if (!SendStr(0, std::string(buf, ClampSnprintf(n, sizeof(buf))))) break;
      continue;
    }
    if (cmd != kCmdInfer) {
      if (!SendStr(1, "unknown command")) break;
      continue;
    }

    // INFER: the payload must be exactly [4B req_id][16B content rect]
    // plus one HWC uint8 frame. A wrong length is reported, never guessed
    // at.
    //
    // client sends 20 extra bytes, so an older daemon stops here with status
    // 2 instead of treating the prefix as pixels; and an older client
    // sending 737,280 or 737,284 to this daemon stops here too. Both
    // directions fail loudly rather than quietly returning wrong
    // detections.
    if (len != kInferPrefix + in_elems) {
      char msg[192];
      const int n = std::snprintf(msg, sizeof(msg),
          "payload %u bytes, expected %zu "
          "(4B request_id + 16B content rect + HWC uint8 %dx%dx3)",
          len, kInferPrefix + in_elems, pp.proc_height, pp.proc_width);
      if (!SendStr(2, std::string(msg, ClampSnprintf(n, sizeof(msg))))) break;
      // The payload is still in the pipe and must be consumed or every
      // later frame is misaligned. Fixed buffer, read in chunks: len is
      // whatever the peer claimed and must not be used as an allocation
      // size (see DrainBounded).
      if (!DrainBounded(STDIN_FILENO, len)) break;
      continue;
    }
    std::uint32_t req_id = 0;
    if (!ReadExact(STDIN_FILENO, &req_id, sizeof(req_id))) break;
    // The letterboxed content rectangle for THIS frame. Per frame rather
    // than per process because the camera can change without a restart, and
    // a stale rectangle would silently mis-scale every severity.
    std::int32_t rect[4] = {0, 0, pp.proc_width, pp.proc_height};
    if (!ReadExact(STDIN_FILENO, rect, sizeof(rect))) break;
    if (!ReadExact(STDIN_FILENO, in_u8.data(), in_elems)) break;
    // would index the bitmap out of bounds and one of zero area would
    // divide by zero, so it cannot simply be used -- but falling back to
    // the whole canvas is worse than an error: severity is an area
    // FRACTION, so the wrong denominator makes every alarm on that frame
    // quietly wrong while the reply still looks like a result. The client
    // computes this rectangle from a resize it just did; getting it wrong
    // is a bug in the pair, and the pair should hear about it.
    //
    // 64-bit sums: rect comes off the wire, and int32 x + width overflows
    // into undefined behaviour on a corrupt or hostile frame.
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
    HwcU8ToChwF32(in_u8.data(), in_f32.data(), pp.proc_height, pp.proc_width);

    // set_input again for this frame -- see the comment above; skipping it
    // silently returns zero boxes
    if (method.set_input(EValue(in_tensor), 0) != Error::Ok) {
      if (!SendStr(3, "set_input failed")) break;
      continue;
    }
    const Error err = method.execute();
    if (err != Error::Ok) {
      if (!SendStr(3, "execute failed")) break;
      continue;
    }

    // Ten outputs: the first five are score heads, the last five box heads,
    // matching the ONNX output order.
    //
    // and dimensions have to be checked: the postprocess computes indices
    // from kH/kW/kAnchors into a raw float*, so one output smaller than
    // expected is an OUT-OF-BOUNDS READ. It raises nothing; it reads another
    // tensor or unrelated heap and emits that as scores and boxes. All it
    // takes is a user swapping in a different .pte that also has ten
    // outputs -- and "let me try another model" is a thing people do.
    if (method.outputs_size() != 10) {
      char msg[96];
      const int n = std::snprintf(msg, sizeof(msg),
          "model has %zu outputs, expected 10",
          static_cast<std::size_t>(method.outputs_size()));
      if (!SendStr(3, std::string(msg, ClampSnprintf(n, sizeof(msg))))) break;
      continue;
    }
    std::vector<const float*> outs(10, nullptr);
    bool shapes_ok = true;
    char shape_err[192] = {0};
    for (size_t i = 0; i < 10 && shapes_ok; ++i) {
      const EValue& v = method.get_output(i);
      if (v.tag != Tag::Tensor) {
        std::snprintf(shape_err, sizeof(shape_err),
                      "output %zu is not a tensor", i);
        shapes_ok = false;
        break;
      }
      const auto t = v.toTensor();
      if (t.scalar_type() != executorch::aten::ScalarType::Float) {
        std::snprintf(shape_err, sizeof(shape_err),
                      "output %zu is dtype %d, expected float32", i,
                      static_cast<int>(t.scalar_type()));
        shapes_ok = false;
        break;
      }
      // Per-dimension, not just the element total. Both (C,H,W) and
      // (1,C,H,W) are accepted: exporters have been seen to add the batch
      // dimension or not, and the memory layout is identical either way.
      const int l = static_cast<int>(i) % kLevels;
      const int want_c = ExpectedChannels(static_cast<int>(i));
      const auto rank = t.dim();
      const bool rank_ok = (rank == 3 || rank == 4);
      int got_c = -1, got_h = -1, got_w = -1;
      if (rank_ok) {
        const int off = (rank == 4) ? 1 : 0;
        if (rank == 4 && t.size(0) != 1) {
          std::snprintf(shape_err, sizeof(shape_err),
                        "output %zu has batch %d, expected 1", i,
                        static_cast<int>(t.size(0)));
          shapes_ok = false;
          break;
        }
        got_c = static_cast<int>(t.size(off));
        got_h = static_cast<int>(t.size(off + 1));
        got_w = static_cast<int>(t.size(off + 2));
      }
      if (!rank_ok || got_c != want_c || got_h != kH[l] || got_w != kW[l]) {
        std::snprintf(shape_err, sizeof(shape_err),
                      "output %zu is rank %d shape [%d,%d,%d], expected "
                      "[%d,%d,%d] -- wrong model?",
                      i, static_cast<int>(rank), got_c, got_h, got_w,
                      want_c, kH[l], kW[l]);
        shapes_ok = false;
        break;
      }
      // numel cross-checks the dimensions above -- they must multiply out
      // to the same count.
      //
      // claimed it was. numel() is just the product of the sizes, so a
      // strided or permuted tensor reports exactly the same number. The
      // postprocess indexes this buffer as contiguous row-major, so a
      // non-contiguous tensor would be read wrongly and nothing here would
      // notice. It is not reachable with the shipped model, which the oracle
      // covers frame by frame; it is a gap that only opens if someone swaps
      // in a different .pte. A future model change must validate dim_order
      // or strides before accepting that model.
      const std::size_t want = ExpectedElems(static_cast<int>(i));
      if (static_cast<std::size_t>(t.numel()) != want) {
        std::snprintf(shape_err, sizeof(shape_err),
                      "output %zu shape says %zu elements but numel is %zu "
                      "-- not contiguous?", i, want,
                      static_cast<std::size_t>(t.numel()));
        shapes_ok = false;
        break;
      }
      outs[i] = t.const_data_ptr<float>();
      if (outs[i] == nullptr) {
        std::snprintf(shape_err, sizeof(shape_err),
                      "output %zu has no data", i);
        shapes_ok = false;
        break;
      }
    }
    if (!shapes_ok) {
      if (!SendStr(3, std::string(shape_err))) break;
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
    // req_id is echoed verbatim: the client uses it to confirm that this is
    // the result for the frame it just sent.
    std::snprintf(tmp, sizeof(tmp), "\"total_area\":%d,\"req_id\":%u}",
                  r.total_area, req_id);
    js += tmp;

    if (!SendStr(0, js)) break;
  }
  return 0;
}
