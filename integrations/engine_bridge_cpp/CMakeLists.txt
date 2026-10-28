cmake_minimum_required(VERSION 3.16)
project(tbe_bridge LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Threads REQUIRED)

add_library(tbe_protocol STATIC src/protocol.cpp src/demo_engine.cpp)
target_include_directories(tbe_protocol PUBLIC include)

add_executable(tbe_bridge src/main.cpp)
target_link_libraries(tbe_bridge PRIVATE tbe_protocol Threads::Threads)
if(WIN32)
  target_link_libraries(tbe_bridge PRIVATE ws2_32)
endif()

if(MSVC)
  target_compile_options(tbe_protocol PRIVATE /W4)
  target_compile_options(tbe_bridge PRIVATE /W4)
else()
  target_compile_options(tbe_protocol PRIVATE -Wall -Wextra)
  target_compile_options(tbe_bridge PRIVATE -Wall -Wextra)
endif()

# To build your engine host: add your IEngine implementation sources here (or in a
# separate target linking tbe_protocol) and return it from make_engine() in main.cpp.
