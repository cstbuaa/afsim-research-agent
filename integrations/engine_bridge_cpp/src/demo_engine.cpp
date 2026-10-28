#include "tbe/demo_engine.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <set>

namespace tbe {

namespace {
constexpr double kPi = 3.14159265358979323846;
constexpr double kArrival = 5.0;
double rad(double deg) { return deg * kPi / 180.0; }
double deg(double r) { return r * 180.0 / kPi; }
double wrap(double a) { return std::fmod(std::fmod(a + 180.0, 360.0) + 360.0, 360.0) - 180.0; }

size_t resume_index(const std::vector<std::pair<double, double>>& pts, double x, double y) {
  if (pts.size() < 2) return 0;
  double best = std::numeric_limits<double>::infinity();
  size_t best_index = 1;
  for (size_t i = 0; i + 1 < pts.size(); ++i) {
    double ax = pts[i].first, ay = pts[i].second, vx = pts[i + 1].first - ax, vy = pts[i + 1].second - ay;
    double len2 = vx * vx + vy * vy;
    double t = len2 > 0 ? std::clamp(((x - ax) * vx + (y - ay) * vy) / len2, 0.0, 1.0) : 0.0;
    double d = std::hypot(ax + t * vx - x, ay + t * vy - y);
    if (d < best - 1e-9) {
      best = d;
      best_index = i + 1;
    }
  }
  return best_index;
}
}  // namespace

double DemoEngine::Region::distance(double x, double y) const {
  double dx = std::max({x_min - x, 0.0, x - x_max});
  double dy = std::max({y_min - y, 0.0, y - y_max});
  return std::hypot(dx, dy);
}

EngineInfo DemoEngine::info() const {
  return {"tbe-demo-engine", "1.0", "adapter",
          {"follow_route", "follow_route_with_offset", "move_to_position", "bound_to_position", "halt",
           "hold_overwatch_position", "set_speed", "set_heading", "keep_station", "assume_formation",
           "send_message", "report_contact"},
          {"spawn_hostile", "remove_hostile"}};
}

Observation DemoEngine::load(const ScenarioSetup& setup) {
  time_ = 0;
  bodies_.clear();
  hostiles_.clear();
  regions_.clear();
  for (const auto& e : setup.entities) {
    Body b;
    b.x = e.x;
    b.y = e.y;
    b.heading = b.target_heading = e.heading_deg;
    b.speed = b.target_speed = e.speed_mps;
    if (e.components.is_object()) {
      for (const auto& [id, comp] : e.components.as_object()) {
        if (!comp.is_object() || !comp.contains("settings")) continue;
        const Json& st = comp.at("settings");
        if (id == "mover") b.max_speed = st.number_or("max_speed_mps", b.max_speed);
        if (st.contains("range_m")) b.sensor_range = st.at("range_m").as_number();
      }
    }
    bodies_[e.engine_id] = b;
  }
  if (setup.terrain.is_object()) {
    default_class_ = setup.terrain.string_or("default_class", "open");
    if (setup.terrain.contains("regions"))
      for (const auto& r : setup.terrain.at("regions").as_array())
        regions_.push_back({r.at("class").as_string(), r.at("x_min").as_number(), r.at("x_max").as_number(),
                            r.at("y_min").as_number(), r.at("y_max").as_number()});
  }
  return state();
}

bool DemoEngine::apply(const Command& c, std::string& reason) {
  auto it = bodies_.find(c.entity);
  if (it == bodies_.end()) {
    reason = "unknown entity " + c.entity;
    return false;
  }
  Body& b = it->second;
  const Json& a = c.args;
  double speed = a.contains("speed") && a.at("speed").is_number() ? a.at("speed").as_number() : -1;
  auto set_speed = [&](double fallback) { b.target_speed = speed >= 0 ? std::min(speed, b.max_speed) : fallback; };
  if (c.command == "follow_route" || c.command == "follow_route_with_offset") {
    b.points.clear();
    if (a.contains("points"))
      for (const auto& p : a.at("points").as_array())
        b.points.emplace_back(p.as_array().at(0).as_number(), p.as_array().at(1).as_number());
    b.index = resume_index(b.points, b.x, b.y);
    b.mode = "route";
    b.arrived = false;
    set_speed(b.max_speed);
  } else if (c.command == "move_to_position") {
    b.points.clear();
    if (a.contains("point") && a.at("point").is_array())
      b.points.emplace_back(a.at("point").as_array().at(0).as_number(), a.at("point").as_array().at(1).as_number());
    b.index = 0;
    b.mode = "route";
    b.arrived = false;
    set_speed(b.max_speed);
  } else if (c.command == "bound_to_position") {
    // distance along the heading, or toward a point without ending closer than stop_short
    double d = a.at("distance").as_number();
    double stop_short = a.number_or("stop_short", 0.0);
    std::string toward = a.string_or("toward", "heading");
    bool has_point = false;
    double px = 0, py = 0;
    if (a.contains("toward_point") && a.at("toward_point").is_array()) {
      px = a.at("toward_point").as_array().at(0).as_number();
      py = a.at("toward_point").as_array().at(1).as_number();
      has_point = true;
    } else if (toward == "nearest_hostile") {
      double best = std::numeric_limits<double>::infinity();
      for (const auto& [hid, pos] : hostiles_) {
        double r = std::hypot(pos.first - b.x, pos.second - b.y);
        if (r <= b.sensor_range && r < best) {
          best = r;
          px = pos.first;
          py = pos.second;
          has_point = true;
        }
      }
    }
    if (!has_point) {
      b.points = {{b.x + d * std::sin(rad(b.heading)), b.y + d * std::cos(rad(b.heading))}};
    } else {
      double dx = px - b.x, dy = py - b.y, gap = std::hypot(dx, dy);
      double step = std::min(d, gap - stop_short);
      if (gap < 1e-9 || step <= 0) b.points = {{b.x, b.y}};
      else b.points = {{b.x + dx / gap * step, b.y + dy / gap * step}};
    }
    b.index = 0;
    b.mode = "route";
    b.arrived = false;
    set_speed(0.6 * b.max_speed);
  } else if (c.command == "halt" || c.command == "hold_overwatch_position") {
    b.mode = "halt";
    b.target_speed = 0;
  } else if (c.command == "set_speed") {
    b.target_speed = std::min(a.at("speed").as_number(), b.max_speed);
  } else if (c.command == "set_heading") {
    b.mode = "heading";
    b.target_heading = a.at("heading").as_number();
  } else if (c.command == "keep_station") {
    b.mode = "station";
    b.leader = a.string_or("leader_entity", "");
    b.forward = a.number_or("forward_m", 0);
    b.right = a.number_or("right_m", 0);
    b.station_max = a.number_or("max_speed", b.max_speed);
  } else if (c.command == "assume_formation" || c.command == "send_message" || c.command == "report_contact") {
    // bookkeeping / messaging handled outside the physics
  } else {
    reason = "unsupported command '" + c.command + "'";
    return false;
  }
  return true;
}

bool DemoEngine::event(const Json& e) {
  std::string type = e.string_or("type", "");
  std::string id = e.string_or("target_id", e.string_or("id", ""));
  if (type == "spawn_hostile") {
    hostiles_[id] = {e.at("position").as_array().at(0).as_number(), e.at("position").as_array().at(1).as_number()};
    return true;
  }
  if (type == "remove_hostile") {
    hostiles_.erase(id);
    return true;
  }
  return false;
}

void DemoEngine::move(Body& b, double dt) {
  double target_speed = b.target_speed;
  if (b.mode == "route") {
    while (b.index < b.points.size() &&
           std::hypot(b.points[b.index].first - b.x, b.points[b.index].second - b.y) <= kArrival)
      ++b.index;
    if (b.index >= b.points.size()) {
      b.arrived = true;
      target_speed = 0;
    } else {
      double px = b.points[b.index].first, py = b.points[b.index].second;
      b.target_heading = deg(std::atan2(px - b.x, py - b.y));
      if (b.index + 1 == b.points.size())
        target_speed = std::min(target_speed, std::max(1.0, std::sqrt(2 * b.accel * std::hypot(px - b.x, py - b.y))));
    }
  } else if (b.mode == "station") {
    auto it = bodies_.find(b.leader);
    if (it == bodies_.end()) {
      target_speed = 0;
    } else {
      const Body& l = it->second;
      double h = rad(l.heading);
      double sx = l.x + b.forward * std::sin(h) + b.right * std::cos(h);
      double sy = l.y + b.forward * std::cos(h) - b.right * std::sin(h);
      double dx = sx - b.x, dy = sy - b.y;
      double along = dx * std::sin(h) + dy * std::cos(h);
      double cross = dx * std::cos(h) - dy * std::sin(h);
      b.target_heading = l.heading + deg(std::atan2(cross, 30.0));
      target_speed = std::max(0.0, std::min({b.station_max, b.max_speed, l.speed + 0.3 * along}));
    }
  } else if (b.mode == "halt") {
    target_speed = 0;
  }
  double diff = wrap(b.target_heading - b.heading);
  double turn = b.turn_rate * dt;
  b.heading = wrap(b.heading + std::clamp(diff, -turn, turn));
  double dv = target_speed - b.speed;
  b.speed = std::max(0.0, b.speed + std::clamp(dv, -b.accel * dt, b.accel * dt));
  b.x += b.speed * dt * std::sin(rad(b.heading));
  b.y += b.speed * dt * std::cos(rad(b.heading));
}

Observation DemoEngine::step(double dt) {
  for (auto& [id, b] : bodies_) move(b, dt);
  time_ += dt;
  return state();
}

Json DemoEngine::terrain_at(double x, double y) const {
  Json t = Json::object();
  std::string cls = default_class_;
  for (const auto& r : regions_)
    if (r.distance(x, y) == 0.0) {
      cls = r.cls;
      break;
    }
  t["class"] = cls;
  Json dist = Json::object();
  std::set<std::string> classes;
  for (const auto& r : regions_) classes.insert(r.cls);
  for (const auto& c : classes) {
    double best = std::numeric_limits<double>::infinity();
    for (const auto& r : regions_)
      if (r.cls == c) best = std::min(best, r.distance(x, y));
    dist[c] = best;
  }
  t["distance_to"] = dist;
  return t;
}

Observation DemoEngine::state() {
  Observation obs;
  obs.time = time_;
  for (const auto& [id, b] : bodies_) {
    EntityState s;
    s.x = b.x;
    s.y = b.y;
    s.heading_deg = std::fmod(b.heading + 360.0, 360.0);
    s.speed_mps = b.speed;
    s.mover["available"] = true;
    s.mover["at_destination"] = b.arrived;
    s.mover["route_complete"] = b.arrived;
    s.mover["moving"] = b.speed > 0.1;
    for (const auto& [hid, pos] : hostiles_) {
      double r = std::hypot(pos.first - b.x, pos.second - b.y);
      if (r <= b.sensor_range) s.tracks.push_back({hid, pos.first, pos.second, true, r});
    }
    s.terrain = terrain_at(b.x, b.y);
    obs.entities[id] = s;
  }
  return obs;
}

}  // namespace tbe
