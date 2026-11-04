# Upstream

This directory starts from [langchain-ai/deep-agents-ui](https://github.com/langchain-ai/deep-agents-ui)
(MIT, see `LICENSE`), commit `f6a4f34565b42688be06498031fc9351c152614e` (2026-05-14).

Local changes are made in place and listed here:

- `package.json`: `dev` / `start` listen on 127.0.0.1:3000 only.
- `.github/` removed (upstream CI).
- `src/app/layout.tsx`: no Google Fonts (`next/font/google` fetches at build time; builds must work offline); system font stack from `globals.css`; `lang="zh-CN"`.
- Package caches on `D:\tools` through `YARN_CACHE_FOLDER` / `npm_config_cache` in the start script (everything installed on drive D).
- `src/lib/config.ts`: defaults to the local server `http://127.0.0.1:2024` and graph `afsim_research` (no configuration dialog on first start).
- Simulation panel (`src/app/components/SimPanel.tsx`, `ReplayPlayer.tsx`, `src/lib/workbench.ts`): replays of 2D runs and AFSIM recordings (`.aer`) from the local server; `page.tsx` adds it as a third panel (`?sim=1`) and renames the header to Chinese; `ToolCallBox.tsx` shows a "查看回放" button for replay paths in a tool result.
- `next.config.ts`: `/workbench/*` is proxied to the local LangGraph server (127.0.0.1:2024).
- Live progress: `useChat.ts` forwards custom stream events (`workbench_progress`) to the page; `RunActivity.tsx` (running tool, latest line, elapsed time, run log, run errors) above the chat; `ToolCallBox.tsx` shows a running tool's latest lines and Chinese tool names.
