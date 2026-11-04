// Replays served by the local LangGraph server (orchestrator/workbench.py), proxied by
// Next (next.config.ts rewrites /workbench/* → http://127.0.0.1:2024/workbench/*).

export interface ReplayEntry {
  id: string;
  source: "afsim" | "2d";
  title: string;
  size: number;
  mtime: number;
}

export interface ReplayPlatform {
  id: string;
  label: string;
  side: string;
  icon?: string;
}

export interface Verdict {
  id: string;
  passed: boolean;
  judged?: boolean;
  detail?: string;
}

// format "replay/1" (src/afsim_research_agent/replay/build.py)
export interface Replay {
  format: string;
  source: "afsim" | "2d";
  title: string;
  duration_s: number;
  step_s: number;
  platforms: ReplayPlatform[];
  samples: Record<string, number[][]>; // [t, x east, y north, speed, heading deg]
  states: Record<string, [number, string][]>;
  actions: (string | number)[][]; // [t, id, start|done|fail, action, code?]
  events: (string | number)[][]; // [t, kind, id, other?]
  routes: { id: string; platform?: string; points: number[][] }[];
  points: { id: string; x: number; y: number }[];
  sensors: Record<string, { range_m: number | null }>;
  verdicts: Verdict[];
}

export const OPEN_REPLAY_EVENT = "workbench:open-replay";
export const PROGRESS_EVENT = "workbench:progress";

// one progress line of a running tool (orchestrator/tools.py _progress)
export interface ProgressEvent {
  type: "workbench_progress";
  tool: string;
  kind: "start" | "progress" | "heartbeat" | "end";
  t: number; // seconds since the tool started
  text: string;
}

export const TOOL_LABELS: Record<string, string> = {
  overview: "了解流程",
  crow_research: "检索资料（Crow）",
  crow_propose: "提出候选战术（Crow）",
  select_tactic: "记录选定的战术",
  falcon_draft3: "起草行为模型（Falcon v3）",
  scenario_show: "查看想定",
  scenario_set: "修改想定",
  simulate2d: "二维仿真",
  afsim_run: "AFSIM 运行",
  ask_human: "向你提问",
  write_todos: "制定计划",
  read_file: "读取文件",
  ls: "查看目录",
};

export function toolLabel(name: string): string {
  return TOOL_LABELS[name] ? `${TOOL_LABELS[name]} · ${name}` : name;
}

export async function listReplays(): Promise<ReplayEntry[]> {
  const res = await fetch("/workbench/replays", { cache: "no-store" });
  if (!res.ok) throw new Error(`回放列表读取失败（${res.status}）`);
  return res.json();
}

export async function loadReplay(id: string): Promise<Replay> {
  const res = await fetch(`/workbench/replay?id=${encodeURIComponent(id)}`, {
    cache: "no-store",
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data?.error || `回放读取失败（${res.status}）`);
  return data;
}

// workspace paths of replays named in a tool result (/runs/....aer, /runs/....replay.json)
export function replayPaths(text: string | undefined): string[] {
  if (!text) return [];
  const found = text.match(/\/runs\/[^"'\s,}]+?\.(?:aer|replay\.json)/g) || [];
  return Array.from(new Set(found));
}

// a workspace path → the replay id (root 0 is <data>/runs)
export function idForPath(path: string, entries: ReplayEntry[]): string | null {
  const rel = path.replace(/^\/runs\//, "");
  const hit = entries.find((e) => e.id === `0:${rel}`);
  return hit ? hit.id : null;
}

export function openReplay(path: string) {
  window.dispatchEvent(new CustomEvent(OPEN_REPLAY_EVENT, { detail: { path } }));
}

// the sample of a track at time t (last sample at or before t)
export function sampleAt(track: number[][] | undefined, t: number): number[] | null {
  if (!track || track.length === 0) return null;
  if (t < track[0][0]) return null;
  let lo = 0;
  let hi = track.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (track[mid][0] <= t + 1e-6) lo = mid;
    else hi = mid - 1;
  }
  return track[lo];
}

export function stateAt(seq: [number, string][] | undefined, t: number): string {
  if (!seq) return "";
  let cur = "";
  for (const [ts, s] of seq) {
    if (ts <= t + 1e-6) cur = s;
    else break;
  }
  return cur;
}

const PALETTE = [
  "#2563eb",
  "#16a34a",
  "#d97706",
  "#9333ea",
  "#0891b2",
  "#db2777",
  "#65a30d",
  "#ea580c",
  "#4f46e5",
  "#0d9488",
];

export function stateColor(state: string): string {
  let h = 0;
  for (let i = 0; i < state.length; i++) h = (h * 31 + state.charCodeAt(i)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

export function sideColor(side: string): string {
  const s = side.toLowerCase();
  if (s === "blue") return "#2563eb";
  if (s === "red") return "#dc2626";
  return "#6b7280";
}
