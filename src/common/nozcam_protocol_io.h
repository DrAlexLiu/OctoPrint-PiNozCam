// Utilities shared by native daemons for the binary protocol transport.
// The protocol reads from stdin and writes frames to one dedicated fd.

#ifndef PINOZCAM_PROTOCOL_IO_H_
#define PINOZCAM_PROTOCOL_IO_H_

#include <cerrno>
#include <cstdint>
#include <cstddef>
#include <unistd.h>

namespace nozcam {
namespace io {

using WriteFn = ssize_t (*)(int, const void*, std::size_t);

// Read exactly n bytes, handling partial reads and EINTR.
inline bool ReadExact(int fd, void* buf, std::size_t n) {
  auto* p = static_cast<std::uint8_t*>(buf);
  while (n > 0) {
    const ssize_t r = ::read(fd, p, n);
    if (r == 0) return false;  // EOF: peer closed.
    if (r < 0) {
      if (errno == EINTR) continue;
      return false;
    }
    p += r;
    n -= static_cast<std::size_t>(r);
  }
  return true;
}

// Discard an untrusted payload in fixed-size chunks.
inline bool DrainBounded(int fd, std::uint32_t n) {
  std::uint8_t buf[65536];
  while (n > 0) {
    const std::uint32_t chunk =
        n < sizeof(buf) ? n : static_cast<std::uint32_t>(sizeof(buf));
    if (!ReadExact(fd, buf, chunk)) return false;
    n -= chunk;
  }
  return true;
}

// Write exactly n bytes, handling partial writes and EINTR.
// `write_fn` exists for tests that need to inject transport behavior.
inline bool WriteExact(int fd, const void* buf, std::size_t n,
                      WriteFn write_fn = ::write) {
  auto* p = static_cast<const std::uint8_t*>(buf);
  while (n > 0) {
    const ssize_t w = write_fn(fd, p, n);
    if (w == 0) return false;
    if (w < 0) {
      if (errno == EINTR) continue;
      return false;
    }
    p += w;
    n -= static_cast<std::size_t>(w);
  }
  return true;
}

// Save the current stdout fd for protocol output and redirect future vendor
// stdout traffic to stderr so W/I/D/T lines cannot corrupt the protocol.
inline bool PrepareProtocolOutput(int* protocol_fd) {
  if (protocol_fd == nullptr) {
    errno = EINVAL;
    return false;
  }
  const int saved = ::dup(STDOUT_FILENO);
  if (saved < 0) return false;
  if (::dup2(STDERR_FILENO, STDOUT_FILENO) < 0) {
    const int err = errno;
    ::close(saved);
    errno = err;
    return false;
  }
  *protocol_fd = saved;
  return true;
}

}  // namespace io
}  // namespace nozcam

#endif  // PINOZCAM_PROTOCOL_IO_H_
