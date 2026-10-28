// Minimal blocking TCP with length-prefixed frames (POSIX and Winsock).
#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
using tbe_socket_t = SOCKET;
#define TBE_INVALID_SOCKET INVALID_SOCKET
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>
using tbe_socket_t = int;
#define TBE_INVALID_SOCKET (-1)
#endif

namespace tbe {

inline constexpr uint32_t kMaxFrameBytes = 64u * 1024u * 1024u;

class NetError : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

struct NetInit {
  NetInit() {
#ifdef _WIN32
    WSADATA data;
    if (WSAStartup(MAKEWORD(2, 2), &data) != 0) throw NetError("WSAStartup failed");
#endif
  }
  ~NetInit() {
#ifdef _WIN32
    WSACleanup();
#endif
  }
};

inline void close_socket(tbe_socket_t s) {
#ifdef _WIN32
  closesocket(s);
#else
  ::close(s);
#endif
}

inline tbe_socket_t listen_on(const std::string& host, uint16_t port) {
  tbe_socket_t s = ::socket(AF_INET, SOCK_STREAM, 0);
  if (s == TBE_INVALID_SOCKET) throw NetError("socket() failed");
  int yes = 1;
  setsockopt(s, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char*>(&yes), sizeof yes);
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(port);
  if (inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1) throw NetError("bad host address " + host);
  if (::bind(s, reinterpret_cast<sockaddr*>(&addr), sizeof addr) != 0) throw NetError("bind() failed");
  if (::listen(s, 1) != 0) throw NetError("listen() failed");
  return s;
}

inline void send_all(tbe_socket_t s, const char* data, size_t size) {
  while (size > 0) {
    auto n = ::send(s, data, static_cast<int>(size), 0);
    if (n <= 0) throw NetError("send() failed");
    data += n;
    size -= static_cast<size_t>(n);
  }
}

inline bool recv_all(tbe_socket_t s, char* data, size_t size) {
  while (size > 0) {
    auto n = ::recv(s, data, static_cast<int>(size), 0);
    if (n <= 0) return false;
    data += n;
    size -= static_cast<size_t>(n);
  }
  return true;
}

// Disable Nagle's algorithm: request/response traffic must not wait for delayed ACKs.
inline void set_no_delay(tbe_socket_t s) {
  int yes = 1;
  setsockopt(s, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char*>(&yes), sizeof yes);
}

inline void send_frame(tbe_socket_t s, const std::string& payload) {
  if (payload.size() > kMaxFrameBytes) throw NetError("frame too large");
  uint32_t n = static_cast<uint32_t>(payload.size());
  std::string frame;
  frame.reserve(payload.size() + 4);
  frame.push_back(static_cast<char>((n >> 24) & 0xFF));
  frame.push_back(static_cast<char>((n >> 16) & 0xFF));
  frame.push_back(static_cast<char>((n >> 8) & 0xFF));
  frame.push_back(static_cast<char>(n & 0xFF));
  frame += payload;
  send_all(s, frame.data(), frame.size());  // one write per frame
}

// Returns false when the peer closed the connection.
inline bool recv_frame(tbe_socket_t s, std::string& payload) {
  unsigned char header[4];
  if (!recv_all(s, reinterpret_cast<char*>(header), 4)) return false;
  uint32_t n = (uint32_t(header[0]) << 24) | (uint32_t(header[1]) << 16) | (uint32_t(header[2]) << 8) |
               uint32_t(header[3]);
  if (n > kMaxFrameBytes) throw NetError("frame too large");
  payload.assign(n, '\0');
  return n == 0 || recv_all(s, payload.data(), n);
}

}  // namespace tbe
