// Engine interface of the tbe-step/1 bridge.
//
// Implement IEngine for your simulation engine. Everything here is engine
// neutral: metres in a local east-north frame (+x east, +y north), seconds,
// m/s, heading in degrees clockwise from north. Convert to and from the
// engine's conventions inside your implementation.
//
// Threading: all IEngine methods are called from one thread only (the engine
// thread in main.cpp), never concurrently and never from the network thread.
#pragma once

#include <map>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "tbe/json.hpp"

namespace tbe {

struct EntitySetup {
  std::string engine_id;
  std::string role;
  int instance = 0;
  std::string capability_class;
  Json components;  // logical component id -> {"component": engine name, "settings": {...}}
  double x = 0, y = 0, heading_deg = 0, speed_mps = 0;
};

struct ScenarioSetup {
  Json scenario;  // the scenario as defined in the behavior model (for reference)
  std::vector<EntitySetup> entities;
  std::map<std::string, std::vector<std::pair<double, double>>> routes;
  std::map<std::string, std::pair<double, double>> waypoints;
  Json terrain;  // {"default_class": ..., "regions": [{"class", "x_min", "x_max", "y_min", "y_max"}]}
  double time_step_s = 1.0;
  double duration_s = 0.0;
  int random_seed = 0;
};

struct Command {
  std::string entity;   // engine entity id
  std::string command;  // neutral action name, e.g. "follow_route", "keep_station", "halt"
  Json args;            // canonical units; runner-added fields such as points, leader_entity,
                        // forward_m, right_m
  std::string role;
  int instance = 0;
  std::string behavior;
};

struct OutgoingMessage {
  std::string sender;
  std::vector<std::string> recipients;
  std::string message;
  Json fields;
  double latency_s = 0;
  double reliability = 1;
};

struct Track {
  std::string id;
  double x = 0, y = 0;
  bool hostile = true;
  std::optional<double> range_m;
};

struct EntityState {
  double x = 0, y = 0, heading_deg = 0, speed_mps = 0;
  bool alive = true, damaged = false;
  double altitude_m = 0, fuel_remaining = 1;
  Json mover = Json::object();       // available, at_destination, route_complete, moving
  Json components = Json::object();  // logical id -> {"available":..., "on":..., ...}
  std::vector<Track> tracks;
  Json terrain = Json::object();     // {"class": "...", "distance_to": {"urban": 120.0}}
  Json inbox = Json::array();        // only when the engine delivers messages
};

struct Observation {
  double time = 0;
  std::map<std::string, EntityState> entities;
};

struct EngineInfo {
  std::string name;
  std::string version;
  std::string messaging = "adapter";  // "adapter" or "engine"
  std::vector<std::string> commands;  // neutral actions this engine executes
  std::vector<std::string> events;    // scenario event types this engine supports
};

class IEngine {
 public:
  virtual ~IEngine() = default;
  virtual EngineInfo info() const = 0;
  // Create all entities for the scenario and return the t=0 observation.
  virtual Observation load(const ScenarioSetup& setup) = 0;
  // Return false (with a reason) for a command this engine cannot execute.
  virtual bool apply(const Command& command, std::string& reason) = 0;
  // Only called when info().messaging == "engine".
  virtual void send(const std::vector<OutgoingMessage>& messages) = 0;
  // Scenario events such as spawn_hostile; return false if unsupported.
  virtual bool event(const Json& event) = 0;
  // Advance by exactly one engine step of at most `dt` seconds; may overshoot if the
  // engine is event-stepped. Return the observation after the step.
  virtual Observation step(double dt) = 0;
  // Current observation without advancing time.
  virtual Observation state() = 0;
  virtual void close() = 0;
};

}  // namespace tbe
