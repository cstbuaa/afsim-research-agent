#include "tbe/protocol.hpp"

#include <algorithm>
#include <cmath>

namespace tbe {

namespace {

const char* kRequestTypes[] = {"Hello",   "LoadScenario", "Reset",    "ApplyActions", "SendMessages",
                               "ApplyEvent", "Step",       "RunUntil", "GetState",     "Terminate"};

bool known_type(const std::string& type) {
  return std::any_of(std::begin(kRequestTypes), std::end(kRequestTypes),
                     [&](const char* t) { return type == t; });
}

Json strings(const std::vector<std::string>& values) {
  Json out = Json::array();
  for (const auto& v : values) out.push_back(v);
  return out;
}

}  // namespace

ScenarioSetup decode_setup(const Json& j) {
  ScenarioSetup s;
  s.scenario = j.contains("scenario") ? j.at("scenario") : Json::object();
  for (const auto& e : j.at("entities").as_array()) {
    EntitySetup es;
    es.engine_id = e.at("engine_id").as_string();
    es.role = e.at("role").as_string();
    es.instance = static_cast<int>(e.at("instance").as_int());
    es.capability_class = e.at("capability_class").as_string();
    es.components = e.contains("components") ? e.at("components") : Json::object();
    es.x = e.at("position").as_array().at(0).as_number();
    es.y = e.at("position").as_array().at(1).as_number();
    es.heading_deg = e.at("heading_deg").as_number();
    es.speed_mps = e.at("speed_mps").as_number();
    s.entities.push_back(es);
  }
  if (j.contains("routes") && j.at("routes").is_object()) {
    for (const auto& [name, pts] : j.at("routes").as_object()) {
      auto& route = s.routes[name];
      for (const auto& p : pts.as_array())
        route.emplace_back(p.as_array().at(0).as_number(), p.as_array().at(1).as_number());
    }
  }
  if (j.contains("waypoints") && j.at("waypoints").is_object()) {
    for (const auto& [name, p] : j.at("waypoints").as_object())
      s.waypoints[name] = {p.as_array().at(0).as_number(), p.as_array().at(1).as_number()};
  }
  s.terrain = j.contains("terrain") ? j.at("terrain") : Json();
  s.time_step_s = j.at("time_step_s").as_number();
  s.duration_s = j.at("duration_s").as_number();
  s.random_seed = static_cast<int>(j.number_or("random_seed", 0));
  return s;
}

Command decode_command(const Json& j) {
  Command c;
  c.entity = j.at("entity").as_string();
  c.command = j.at("command").as_string();
  c.args = j.contains("args") ? j.at("args") : Json::object();
  c.role = j.string_or("role", "");
  c.instance = static_cast<int>(j.number_or("instance", 0));
  c.behavior = j.string_or("behavior", "");
  return c;
}

OutgoingMessage decode_message(const Json& j) {
  OutgoingMessage m;
  m.sender = j.at("sender").as_string();
  for (const auto& r : j.at("recipients").as_array()) m.recipients.push_back(r.as_string());
  m.message = j.at("message").as_string();
  m.fields = j.contains("fields") ? j.at("fields") : Json::object();
  m.latency_s = j.number_or("latency_s", 0.0);
  m.reliability = j.number_or("reliability", 1.0);
  return m;
}

Json encode_observation(const Observation& obs) {
  Json out = Json::object();
  out["time"] = obs.time;
  Json ents = Json::object();
  for (const auto& [id, e] : obs.entities) {
    Json je = Json::object();
    je["x"] = e.x;
    je["y"] = e.y;
    je["heading_deg"] = e.heading_deg;
    je["speed_mps"] = e.speed_mps;
    je["alive"] = e.alive;
    je["damaged"] = e.damaged;
    je["altitude_m"] = e.altitude_m;
    je["fuel_remaining"] = e.fuel_remaining;
    je["mover"] = e.mover;
    je["components"] = e.components;
    Json tracks = Json::array();
    for (const auto& t : e.tracks) {
      Json jt = Json::object();
      jt["id"] = t.id;
      jt["x"] = t.x;
      jt["y"] = t.y;
      jt["hostile"] = t.hostile;
      jt["range_m"] = t.range_m ? Json(*t.range_m) : Json();
      tracks.push_back(jt);
    }
    je["tracks"] = tracks;
    je["terrain"] = e.terrain;
    je["inbox"] = e.inbox;
    ents[id] = je;
  }
  out["entities"] = ents;
  return out;
}

Json Session::handle(const Json& request) {
  Json reply = Json::object();
  reply["protocol"] = kProtocol;
  reply["run_id"] = request.is_object() ? Json(request.string_or("run_id", "")) : Json("");
  reply["sequence_id"] = request.is_object() ? Json(request.number_or("sequence_id", -1)) : Json(-1);
  try {
    if (!request.is_object() || request.string_or("protocol", "") != kProtocol)
      throw ProtocolError("VERSION", std::string("expected protocol ") + kProtocol);
    // Every request consumes its sequence_id, including rejected ones.
    if (!request.contains("sequence_id") || !request.at("sequence_id").is_number() ||
        request.at("sequence_id").as_int() <= last_sequence_)
      throw ProtocolError("BAD_REQUEST", "sequence_id must increase strictly");
    std::string run_id = request.string_or("run_id", "");
    if (run_id_.empty()) run_id_ = run_id;
    else if (run_id != run_id_) throw ProtocolError("BAD_REQUEST", "run_id changed within a connection");
    last_sequence_ = request.at("sequence_id").as_int();
    std::string type = request.string_or("type", "");
    if (!known_type(type)) throw ProtocolError("BAD_REQUEST", "unknown request type '" + type + "'");
    if (type != "Hello" && !greeted_) throw ProtocolError("BAD_STATE", "send Hello first");
    if (terminated_) throw ProtocolError("BAD_STATE", "session terminated");
    Json body = request.contains("body") ? request.at("body") : Json::object();
    Json result = dispatch(type, body);
    reply["type"] = type + "Result";
    reply["ok"] = true;
    reply["body"] = result;
    reply["error"] = Json();
  } catch (const ProtocolError& e) {
    reply["type"] = "Error";
    reply["ok"] = false;
    reply["body"] = Json::object();
    Json err = Json::object();
    err["code"] = e.code();
    err["message"] = e.what();
    reply["error"] = err;
  } catch (const JsonError& e) {
    reply["type"] = "Error";
    reply["ok"] = false;
    reply["body"] = Json::object();
    Json err = Json::object();
    err["code"] = "BAD_REQUEST";
    err["message"] = e.what();
    reply["error"] = err;
  } catch (const std::exception& e) {
    reply["type"] = "Error";
    reply["ok"] = false;
    reply["body"] = Json::object();
    Json err = Json::object();
    err["code"] = "ENGINE_ERROR";
    err["message"] = e.what();
    reply["error"] = err;
  }
  return reply;
}

Json Session::step_to(double requested) {
  if (!loaded_) throw ProtocolError("BAD_STATE", "no scenario loaded");
  Observation obs = engine_->state();
  while (obs.time + 1e-9 < requested) {
    double dt = std::min(requested - obs.time, setup_->time_step_s);
    obs = engine_->step(std::max(dt, 1e-9));
  }
  time_ = obs.time;
  bool done = obs.time >= setup_->duration_s - 1e-9;
  Json out = Json::object();
  out["requested_time"] = requested;
  out["actual_sim_time"] = obs.time;
  out["observation"] = encode_observation(obs);
  out["events"] = Json::array();
  out["warnings"] = Json::array();
  out["done"] = done;
  out["end_reason"] = done ? Json("duration") : Json();
  return out;
}

Json Session::dispatch(const std::string& type, const Json& body) {
  Json out = Json::object();
  if (type == "Hello") {
    bool speaks = !body.contains("versions");
    if (body.contains("versions"))
      for (const auto& v : body.at("versions").as_array())
        if (v.is_string() && v.as_string() == kProtocol) speaks = true;
    if (!speaks) throw ProtocolError("VERSION", std::string("server speaks ") + kProtocol);
    greeted_ = true;
    EngineInfo info = engine_->info();
    out["engine"] = info.name;
    out["engine_version"] = info.version;
    out["protocol"] = kProtocol;
    out["messaging"] = info.messaging;
    out["commands"] = strings(info.commands);
    out["events"] = strings(info.events);
    Json step = Json::object();
    step["modes"] = strings({"dt", "until"});
    out["step"] = step;
  } else if (type == "LoadScenario" || type == "Reset") {
    if (type == "LoadScenario") {
      setup_ = std::make_unique<ScenarioSetup>(decode_setup(body.at("setup")));
    } else if (!setup_) {
      throw ProtocolError("BAD_STATE", "no scenario loaded");
    } else if (body.contains("seed")) {
      setup_->random_seed = static_cast<int>(body.at("seed").as_int());
    }
    Observation obs = engine_->load(*setup_);
    loaded_ = true;
    time_ = obs.time;
    if (type == "LoadScenario") {
      Json ids = Json::array();
      for (const auto& [id, e] : obs.entities) ids.push_back(id);
      out["entities"] = ids;
    }
    out["observation"] = encode_observation(obs);
  } else if (type == "ApplyActions") {
    if (!loaded_) throw ProtocolError("BAD_STATE", "no scenario loaded");
    Json rejected = Json::array();
    int accepted = 0;
    const JsonArray empty;
    const JsonArray& cmds = body.contains("commands") ? body.at("commands").as_array() : empty;
    for (size_t i = 0; i < cmds.size(); ++i) {
      std::string reason;
      if (engine_->apply(decode_command(cmds[i]), reason)) {
        ++accepted;
      } else {
        Json r = Json::object();
        r["index"] = static_cast<double>(i);
        r["reason"] = reason;
        rejected.push_back(r);
      }
    }
    out["accepted"] = accepted;
    out["rejected"] = rejected;
  } else if (type == "SendMessages") {
    if (!loaded_) throw ProtocolError("BAD_STATE", "no scenario loaded");
    if (engine_->info().messaging != "engine")
      throw ProtocolError("UNSUPPORTED", "engine does not model messaging");
    std::vector<OutgoingMessage> msgs;
    for (const auto& m : body.at("messages").as_array()) msgs.push_back(decode_message(m));
    engine_->send(msgs);
    out["accepted"] = static_cast<double>(msgs.size());
  } else if (type == "ApplyEvent") {
    if (!loaded_) throw ProtocolError("BAD_STATE", "no scenario loaded");
    out["supported"] = engine_->event(body.at("event"));
  } else if (type == "Step") {
    double dt = body.at("dt").as_number();
    if (dt <= 0) throw ProtocolError("BAD_REQUEST", "dt must be positive");
    double requested = body.number_or("requested_time", time_ + dt);
    if (requested + 1e-9 < time_) throw ProtocolError("BAD_REQUEST", "requested_time is in the past");
    return step_to(requested);
  } else if (type == "RunUntil") {
    double target = body.at("time").as_number();
    if (target + 1e-9 < time_) throw ProtocolError("BAD_REQUEST", "time is in the past");
    return step_to(target);
  } else if (type == "GetState") {
    if (!loaded_) throw ProtocolError("BAD_STATE", "no scenario loaded");
    out["observation"] = encode_observation(engine_->state());
  } else if (type == "Terminate") {
    engine_->close();
    loaded_ = false;
    terminated_ = true;
  }
  return out;
}

}  // namespace tbe
