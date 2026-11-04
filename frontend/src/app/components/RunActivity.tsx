"use client";

// What the agent is doing right now: the running tool with its latest progress line and
// elapsed time (expandable log), "thinking" between tools, and run errors.

import React, { useEffect, useRef, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronUp, Loader2 } from "lucide-react";
import { useChatContext } from "@/providers/ChatProvider";
import { PROGRESS_EVENT, ProgressEvent, toolLabel } from "@/lib/workbench";

interface Line {
  at: number; // wall clock (ms)
  tool: string;
  kind: ProgressEvent["kind"];
  text: string;
}

function errorText(error: unknown): string {
  if (!error) return "";
  const raw =
    typeof error === "string"
      ? error
      : (error as any)?.message || (error as any)?.error || JSON.stringify(error);
  const text = String(raw);
  if (/model_not_found|Model not exist/i.test(text)) {
    return `模型服务里没有配置的模型（${text.slice(0, 160)}）。检查启动脚本里的 ORCHESTRATOR_MODEL。`;
  }
  return text.slice(0, 400);
}

export function RunActivity() {
  const { isLoading, error } = useChatContext();
  const [lines, setLines] = useState<Line[]>([]);
  const [tool, setTool] = useState<{ name: string; since: number } | null>(null);
  const [runSince, setRunSince] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  const [open, setOpen] = useState(false);
  const wasLoading = useRef(false);

  useEffect(() => {
    const handler = (ev: Event) => {
      const e = (ev as CustomEvent).detail as ProgressEvent;
      if (!e) return;
      const at = Date.now();
      if (e.kind === "start") setTool({ name: e.tool, since: at - e.t * 1000 });
      if (e.kind === "end") setTool((cur) => (cur && cur.name === e.tool ? null : cur));
      setLines((prev) => [...prev, { at, tool: e.tool, kind: e.kind, text: e.text }].slice(-200));
    };
    window.addEventListener(PROGRESS_EVENT, handler);
    return () => window.removeEventListener(PROGRESS_EVENT, handler);
  }, []);

  useEffect(() => {
    if (isLoading && !wasLoading.current) {
      setRunSince(Date.now());
      setLines([]);
    }
    if (!isLoading) {
      setTool(null);
      setRunSince(null);
    }
    wasLoading.current = isLoading;
  }, [isLoading]);

  useEffect(() => {
    if (!isLoading) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [isLoading]);

  const last = [...lines].reverse().find((l) => l.kind !== "heartbeat" && l.kind !== "end");
  const err = errorText(error);
  if (!isLoading && !err && lines.length === 0) return null;

  return (
    <div className="border-b border-border bg-muted/40 px-4 py-2 text-xs">
      {err && !isLoading && (
        <div className="mb-1 flex items-start gap-2 rounded bg-red-50 px-2 py-1 text-red-700 dark:bg-red-950 dark:text-red-200">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>运行出错：{err}</span>
        </div>
      )}
      {isLoading && (
        <div className="flex items-center gap-2">
          <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
          <span className="font-medium">
            {tool ? `正在运行：${toolLabel(tool.name)}` : "模型思考中，决定下一步"}
          </span>
          <span className="min-w-0 flex-1 truncate text-muted-foreground" title={last?.text}>
            {tool && last && last.tool === tool.name ? last.text : ""}
          </span>
          <span className="shrink-0 tabular-nums text-muted-foreground">
            {tool
              ? `本步 ${Math.round((now - tool.since) / 1000)} s`
              : runSince
                ? `已 ${Math.round((now - runSince) / 1000)} s`
                : ""}
          </span>
        </div>
      )}
      {lines.length > 0 && (
        <button
          type="button"
          className="mt-1 flex items-center gap-1 text-muted-foreground hover:text-foreground"
          onClick={() => setOpen(!open)}
        >
          {open ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
          运行记录（{lines.filter((l) => l.kind !== "heartbeat").length} 条）
        </button>
      )}
      {open && (
        <div className="mt-1 max-h-48 overflow-auto rounded border border-border bg-background p-2 font-mono text-[11px] leading-5">
          {lines
            .filter((l) => l.kind !== "heartbeat")
            .map((l, i) => (
              <div key={i}>
                <span className="text-muted-foreground">
                  {new Date(l.at).toLocaleTimeString("zh-CN", { hour12: false })}
                </span>{" "}
                <span className="text-sky-700 dark:text-sky-300">{l.tool}</span> {l.text}
              </div>
            ))}
        </div>
      )}
    </div>
  );
}
