// Minimal JSON value with parser and serializer (header-only, C++17).
// Enough for the tbe-step/1 protocol; replace with your preferred library if you like.
#pragma once

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>
#include <variant>
#include <vector>

namespace tbe {

class Json;
using JsonArray = std::vector<Json>;
using JsonObject = std::map<std::string, Json>;

class JsonError : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

class Json {
 public:
  using Value = std::variant<std::nullptr_t, bool, double, std::string, std::shared_ptr<JsonArray>,
                             std::shared_ptr<JsonObject>>;

  Json() : v_(nullptr) {}
  Json(std::nullptr_t) : v_(nullptr) {}
  Json(bool b) : v_(b) {}
  Json(int n) : v_(static_cast<double>(n)) {}
  Json(long n) : v_(static_cast<double>(n)) {}
  Json(long long n) : v_(static_cast<double>(n)) {}
  Json(unsigned n) : v_(static_cast<double>(n)) {}
  Json(size_t n) : v_(static_cast<double>(n)) {}
  Json(double d) : v_(d) {}
  Json(const char* s) : v_(std::string(s)) {}
  Json(std::string s) : v_(std::move(s)) {}
  Json(JsonArray a) : v_(std::make_shared<JsonArray>(std::move(a))) {}
  Json(JsonObject o) : v_(std::make_shared<JsonObject>(std::move(o))) {}

  static Json array() { return Json(JsonArray{}); }
  static Json object() { return Json(JsonObject{}); }

  bool is_null() const { return std::holds_alternative<std::nullptr_t>(v_); }
  bool is_bool() const { return std::holds_alternative<bool>(v_); }
  bool is_number() const { return std::holds_alternative<double>(v_); }
  bool is_string() const { return std::holds_alternative<std::string>(v_); }
  bool is_array() const { return std::holds_alternative<std::shared_ptr<JsonArray>>(v_); }
  bool is_object() const { return std::holds_alternative<std::shared_ptr<JsonObject>>(v_); }

  bool as_bool() const { return get<bool>("bool"); }
  double as_number() const { return get<double>("number"); }
  long long as_int() const { return static_cast<long long>(std::llround(as_number())); }
  const std::string& as_string() const { return get<std::string>("string"); }
  const JsonArray& as_array() const { return *get<std::shared_ptr<JsonArray>>("array"); }
  JsonArray& as_array() { return *get<std::shared_ptr<JsonArray>>("array"); }
  const JsonObject& as_object() const { return *get<std::shared_ptr<JsonObject>>("object"); }
  JsonObject& as_object() { return *get<std::shared_ptr<JsonObject>>("object"); }

  // Object access. operator[] creates members (turning null into an object).
  Json& operator[](const std::string& key) {
    if (is_null()) v_ = std::make_shared<JsonObject>();
    return as_object()[key];
  }
  bool contains(const std::string& key) const {
    return is_object() && as_object().count(key) > 0;
  }
  const Json& at(const std::string& key) const {
    if (!is_object()) throw JsonError("not an object when reading '" + key + "'");
    auto it = as_object().find(key);
    if (it == as_object().end()) throw JsonError("missing field '" + key + "'");
    return it->second;
  }
  // Value or default for optional fields.
  double number_or(const std::string& key, double fallback) const {
    return contains(key) && at(key).is_number() ? at(key).as_number() : fallback;
  }
  std::string string_or(const std::string& key, const std::string& fallback) const {
    return contains(key) && at(key).is_string() ? at(key).as_string() : fallback;
  }
  void push_back(Json value) {
    if (is_null()) v_ = std::make_shared<JsonArray>();
    as_array().push_back(std::move(value));
  }

  std::string dump() const {
    std::string out;
    write(out);
    return out;
  }
  static Json parse(const std::string& text) {
    size_t pos = 0;
    Json value = parse_value(text, pos);
    skip_ws(text, pos);
    if (pos != text.size()) throw JsonError("trailing characters in JSON");
    return value;
  }

 private:
  Value v_;

  template <typename T>
  const T& get(const char* what) const {
    if (auto p = std::get_if<T>(&v_)) return *p;
    throw JsonError(std::string("JSON value is not a ") + what);
  }
  template <typename T>
  T& get(const char* what) {
    if (auto p = std::get_if<T>(&v_)) return *p;
    throw JsonError(std::string("JSON value is not a ") + what);
  }

  static void write_string(std::string& out, const std::string& s) {
    out += '"';
    for (unsigned char c : s) {
      switch (c) {
        case '"': out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        default:
          if (c < 0x20) {
            char buf[8];
            std::snprintf(buf, sizeof buf, "\\u%04x", c);
            out += buf;
          } else {
            out += static_cast<char>(c);
          }
      }
    }
    out += '"';
  }

  void write(std::string& out) const {
    if (is_null()) {
      out += "null";
    } else if (is_bool()) {
      out += as_bool() ? "true" : "false";
    } else if (is_number()) {
      double d = as_number();
      if (!std::isfinite(d)) {
        out += "null";  // JSON has no infinity; the protocol treats null as "no value"
      } else if (d == std::floor(d) && std::fabs(d) < 1e15) {
        out += std::to_string(static_cast<long long>(d));
      } else {
        char buf[32];
        std::snprintf(buf, sizeof buf, "%.17g", d);
        out += buf;
      }
    } else if (is_string()) {
      write_string(out, as_string());
    } else if (is_array()) {
      out += '[';
      bool first = true;
      for (const auto& item : as_array()) {
        if (!first) out += ',';
        first = false;
        item.write(out);
      }
      out += ']';
    } else {
      out += '{';
      bool first = true;
      for (const auto& [key, item] : as_object()) {
        if (!first) out += ',';
        first = false;
        write_string(out, key);
        out += ':';
        item.write(out);
      }
      out += '}';
    }
  }

  static void skip_ws(const std::string& s, size_t& pos) {
    while (pos < s.size() && (s[pos] == ' ' || s[pos] == '\n' || s[pos] == '\r' || s[pos] == '\t')) ++pos;
  }

  static void append_utf8(std::string& out, uint32_t cp) {
    if (cp < 0x80) {
      out += static_cast<char>(cp);
    } else if (cp < 0x800) {
      out += static_cast<char>(0xC0 | (cp >> 6));
      out += static_cast<char>(0x80 | (cp & 0x3F));
    } else if (cp < 0x10000) {
      out += static_cast<char>(0xE0 | (cp >> 12));
      out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
      out += static_cast<char>(0x80 | (cp & 0x3F));
    } else {
      out += static_cast<char>(0xF0 | (cp >> 18));
      out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
      out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
      out += static_cast<char>(0x80 | (cp & 0x3F));
    }
  }

  static uint32_t parse_hex4(const std::string& s, size_t& pos) {
    if (pos + 4 > s.size()) throw JsonError("bad \\u escape");
    uint32_t cp = 0;
    for (int i = 0; i < 4; ++i) {
      char c = s[pos++];
      cp <<= 4;
      if (c >= '0' && c <= '9') cp |= c - '0';
      else if (c >= 'a' && c <= 'f') cp |= c - 'a' + 10;
      else if (c >= 'A' && c <= 'F') cp |= c - 'A' + 10;
      else throw JsonError("bad hex digit");
    }
    return cp;
  }

  static std::string parse_string(const std::string& s, size_t& pos) {
    if (s[pos] != '"') throw JsonError("expected string");
    ++pos;
    std::string out;
    while (pos < s.size()) {
      char c = s[pos++];
      if (c == '"') return out;
      if (c != '\\') {
        out += c;
        continue;
      }
      if (pos >= s.size()) break;
      char e = s[pos++];
      switch (e) {
        case '"': out += '"'; break;
        case '\\': out += '\\'; break;
        case '/': out += '/'; break;
        case 'b': out += '\b'; break;
        case 'f': out += '\f'; break;
        case 'n': out += '\n'; break;
        case 'r': out += '\r'; break;
        case 't': out += '\t'; break;
        case 'u': {
          uint32_t cp = parse_hex4(s, pos);
          if (cp >= 0xD800 && cp <= 0xDBFF && pos + 6 <= s.size() && s[pos] == '\\' && s[pos + 1] == 'u') {
            pos += 2;
            uint32_t low = parse_hex4(s, pos);
            cp = 0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00);
          }
          append_utf8(out, cp);
          break;
        }
        default: throw JsonError("bad escape");
      }
    }
    throw JsonError("unterminated string");
  }

  static Json parse_value(const std::string& s, size_t& pos) {
    skip_ws(s, pos);
    if (pos >= s.size()) throw JsonError("unexpected end of JSON");
    char c = s[pos];
    if (c == '{') {
      ++pos;
      JsonObject obj;
      skip_ws(s, pos);
      if (pos < s.size() && s[pos] == '}') {
        ++pos;
        return Json(std::move(obj));
      }
      while (true) {
        skip_ws(s, pos);
        std::string key = parse_string(s, pos);
        skip_ws(s, pos);
        if (pos >= s.size() || s[pos] != ':') throw JsonError("expected ':'");
        ++pos;
        obj[key] = parse_value(s, pos);
        skip_ws(s, pos);
        if (pos < s.size() && s[pos] == ',') { ++pos; continue; }
        if (pos < s.size() && s[pos] == '}') { ++pos; return Json(std::move(obj)); }
        throw JsonError("expected ',' or '}'");
      }
    }
    if (c == '[') {
      ++pos;
      JsonArray arr;
      skip_ws(s, pos);
      if (pos < s.size() && s[pos] == ']') {
        ++pos;
        return Json(std::move(arr));
      }
      while (true) {
        arr.push_back(parse_value(s, pos));
        skip_ws(s, pos);
        if (pos < s.size() && s[pos] == ',') { ++pos; continue; }
        if (pos < s.size() && s[pos] == ']') { ++pos; return Json(std::move(arr)); }
        throw JsonError("expected ',' or ']'");
      }
    }
    if (c == '"') return Json(parse_string(s, pos));
    if (s.compare(pos, 4, "true") == 0) { pos += 4; return Json(true); }
    if (s.compare(pos, 5, "false") == 0) { pos += 5; return Json(false); }
    if (s.compare(pos, 4, "null") == 0) { pos += 4; return Json(nullptr); }
    size_t start = pos;
    if (s[pos] == '-') ++pos;
    while (pos < s.size() && (std::isdigit(static_cast<unsigned char>(s[pos])) || s[pos] == '.' ||
                              s[pos] == 'e' || s[pos] == 'E' || s[pos] == '+' || s[pos] == '-'))
      ++pos;
    if (start == pos) throw JsonError("unexpected character in JSON");
    try {
      return Json(std::stod(s.substr(start, pos - start)));
    } catch (const std::exception&) {
      throw JsonError("bad number");
    }
  }
};

}  // namespace tbe
