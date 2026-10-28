# tbe-step/1 引擎宿主（C++ 骨架）

与具体仿真引擎无关的桥接程序：TCP 服务 + 协议处理 + 抽象引擎接口 `tbe::IEngine` + 演示引擎。协议说明见 `docs/design/engine-step-protocol-v1.md`。

## 构建与运行

```bash
cmake -S integrations/engine_bridge_cpp -B build/bridge -DCMAKE_BUILD_TYPE=Release
cmake --build build/bridge --config Release
build/bridge/tbe_bridge --host 0.0.0.0 --port 47100      # Windows: build\bridge\Release\tbe_bridge.exe
```

C++17，无第三方依赖（自带最小 JSON 实现 `include/tbe/json.hpp`）；Windows 自动链接 `ws2_32`。

在决策端验证：

```bash
python -m afsim_research_agent.ir protocol-check --host <引擎主机> --port 47100      # 17/17
python -m afsim_research_agent.ir cosim examples/armor_platoon_terrain_formation --engine tcp://<引擎主机>:47100
```

演示引擎下，示例包 3 个想定的 L3 测量值与内置仿真一致。

## 接入自己的引擎

1. 新建一个类实现 `tbe::IEngine`（`include/tbe/engine.hpp`）：
   - `info()`：引擎名称、消息模式（`adapter`/`engine`）、支持的动作和事件；
   - `load(setup)`：按 `ScenarioSetup` 创建实体，返回 t=0 观测；
   - `apply(command, reason)`：把中立动作转为引擎命令；不支持时返回 false 并给出原因；
   - `event(event)`、`send(messages)`（仅 `messaging = engine`）；
   - `step(dt)`：推进不超过一个 `dt`（事件驱动引擎可以越过），返回新观测；
   - `state()`：不推进时间返回当前观测；`close()`。
2. 在 `src/main.cpp` 的 `make_engine()` 中返回你的实现，把源文件加入 `CMakeLists.txt`。
3. 坐标、航向、速度在接口处一律是规范单位（东-北米制、正北顺时针度、m/s），换算放在实现内部。
4. 所有 `IEngine` 方法只在引擎线程（进程主线程）调用；需要主线程运行的引擎可以直接使用。

`tbe::DemoEngine`（`src/demo_engine.cpp`）是完整的参考实现，可以对照着写。
