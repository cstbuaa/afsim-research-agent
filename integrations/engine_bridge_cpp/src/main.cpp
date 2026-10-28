// tbe-step/1 engine host.
//
// Threads:
//   * network thread: accepts one client at a time, reads frames, pushes requests
//     into a queue, waits for the reply and writes it back. It never touches the engine.
//   * engine thread (this process's main thread): pops requests, runs the Session
//     (and therefore the engine), pushes replies. Engines that must live on the main
//     thread are therefore supported.
//
// Usage: tbe_bridge [--host 127.0.0.1] [--port 47100]
// To use your engine, replace `std::make_unique<tbe::DemoEngine>()` in make_engine().

#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <mutex>
#include <optional>
#include <queue>
#include <string>
#include <thread>

#include "tbe/demo_engine.hpp"
#include "tbe/net.hpp"
#include "tbe/protocol.hpp"

namespace {

template <typename T>
class BlockingQueue {
 public:
  void push(T value) {
    {
      std::lock_guard<std::mutex> lock(mutex_);
      items_.push(std::move(value));
    }
    cv_.notify_one();
  }
  T pop() {
    std::unique_lock<std::mutex> lock(mutex_);
    cv_.wait(lock, [&] { return !items_.empty(); });
    T value = std::move(items_.front());
    items_.pop();
    return value;
  }

 private:
  std::mutex mutex_;
  std::condition_variable cv_;
  std::queue<T> items_;
};

struct Work {
  enum Kind { kRequest, kDisconnected, kShutdown } kind = kRequest;
  std::string payload;
};

std::unique_ptr<tbe::IEngine> make_engine() {
  // TODO: return your engine adapter here, e.g. std::make_unique<MyEngine>(config)
  return std::make_unique<tbe::DemoEngine>();
}

}  // namespace

int main(int argc, char** argv) {
  std::string host = "127.0.0.1";
  int port = 47100;
  int max_connections = -1;
  for (int i = 1; i + 1 < argc; i += 2) {
    if (!std::strcmp(argv[i], "--host")) host = argv[i + 1];
    else if (!std::strcmp(argv[i], "--port")) port = std::atoi(argv[i + 1]);
    else if (!std::strcmp(argv[i], "--max-connections")) max_connections = std::atoi(argv[i + 1]);
  }

  tbe::NetInit net;
  tbe_socket_t listener = tbe::listen_on(host, static_cast<uint16_t>(port));
  std::fprintf(stderr, "tbe-step/1 bridge listening on %s:%d\n", host.c_str(), port);

  BlockingQueue<Work> to_engine;
  BlockingQueue<std::string> to_network;

  std::thread network([&] {
    int served = 0;
    while (max_connections < 0 || served < max_connections) {
      tbe_socket_t client = ::accept(listener, nullptr, nullptr);
      if (client == TBE_INVALID_SOCKET) break;
      ++served;
      tbe::set_no_delay(client);
      try {
        std::string payload;
        while (tbe::recv_frame(client, payload)) {
          to_engine.push({Work::kRequest, payload});
          std::string reply = to_network.pop();
          tbe::send_frame(client, reply);
          if (reply.empty()) break;  // engine signalled end of session
        }
      } catch (const std::exception& e) {
        std::fprintf(stderr, "connection error: %s\n", e.what());
      }
      tbe::close_socket(client);
      to_engine.push({Work::kDisconnected, {}});
    }
    to_engine.push({Work::kShutdown, {}});
  });

  // Engine thread: owns the session and the engine.
  std::unique_ptr<tbe::Session> session = std::make_unique<tbe::Session>(make_engine());
  while (true) {
    Work work = to_engine.pop();
    if (work.kind == Work::kShutdown) break;
    if (work.kind == Work::kDisconnected) {
      session = std::make_unique<tbe::Session>(make_engine());
      continue;
    }
    tbe::Json reply;
    try {
      reply = session->handle(tbe::Json::parse(work.payload));
    } catch (const tbe::JsonError& e) {
      reply = tbe::Json::object();
      reply["protocol"] = tbe::kProtocol;
      reply["type"] = "Error";
      reply["ok"] = false;
      reply["run_id"] = "";
      reply["sequence_id"] = -1;
      reply["body"] = tbe::Json::object();
      tbe::Json err = tbe::Json::object();
      err["code"] = "BAD_REQUEST";
      err["message"] = e.what();
      reply["error"] = err;
    }
    to_network.push(reply.dump());
  }
  network.join();
  tbe::close_socket(listener);
  return 0;
}
