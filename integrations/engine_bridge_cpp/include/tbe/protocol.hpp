// tbe-step/1: JSON <-> struct conversions and the request handler (session).
#pragma once

#include <memory>
#include <stdexcept>
#include <string>

#include "tbe/engine.hpp"
#include "tbe/json.hpp"

namespace tbe {

inline constexpr const char* kProtocol = "tbe-step/1";

class ProtocolError : public std::runtime_error {
 public:
  ProtocolError(std::string code, const std::string& message)
      : std::runtime_error(message), code_(std::move(code)) {}
  const std::string& code() const { return code_; }

 private:
  std::string code_;
};

ScenarioSetup decode_setup(const Json& j);
Command decode_command(const Json& j);
OutgoingMessage decode_message(const Json& j);
Json encode_observation(const Observation& obs);

// Handles one connection's requests in order. Not thread-safe by design: the
// engine thread owns the session and the engine.
class Session {
 public:
  explicit Session(std::unique_ptr<IEngine> engine) : engine_(std::move(engine)) {}
  Json handle(const Json& request);
  bool terminated() const { return terminated_; }

 private:
  Json dispatch(const std::string& type, const Json& body);
  Json step_to(double requested);

  std::unique_ptr<IEngine> engine_;
  std::unique_ptr<ScenarioSetup> setup_;
  bool greeted_ = false;
  bool loaded_ = false;
  bool terminated_ = false;
  long long last_sequence_ = 0;
  std::string run_id_;
  double time_ = 0;
};

}  // namespace tbe
