// Demo engine: point-mass kinematics, rectangular terrain, range sensors.
// C++ twin of afsim_research_agent.ir.cosim.kinematic.KinematicEngine.
// Replace it with an IEngine implementation that drives your simulation engine.
#pragma once

#include "tbe/engine.hpp"

namespace tbe {

class DemoEngine : public IEngine {
 public:
  EngineInfo info() const override;
  Observation load(const ScenarioSetup& setup) override;
  bool apply(const Command& command, std::string& reason) override;
  void send(const std::vector<OutgoingMessage>&) override {}
  bool event(const Json& event) override;
  Observation step(double dt) override;
  Observation state() override;
  void close() override {}

 private:
  struct Body {
    double x = 0, y = 0, heading = 0, speed = 0;
    double max_speed = 15, accel = 2, turn_rate = 45;
    double target_speed = 0, target_heading = 0;
    std::vector<std::pair<double, double>> points;
    size_t index = 0;
    std::string mode = "idle";
    std::string leader;
    double forward = 0, right = 0, station_max = 15;
    bool arrived = false;
    double sensor_range = 2000;
  };
  struct Region {
    std::string cls;
    double x_min, x_max, y_min, y_max;
    double distance(double x, double y) const;
  };

  void move(Body& b, double dt);
  Json terrain_at(double x, double y) const;

  double time_ = 0;
  std::map<std::string, Body> bodies_;
  std::map<std::string, std::pair<double, double>> hostiles_;
  std::vector<Region> regions_;
  std::string default_class_ = "open";
};

}  // namespace tbe
