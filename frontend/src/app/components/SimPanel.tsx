"use client";

// The simulation panel: the replays under the workspace (2D runs and AFSIM recordings)
// and the player. A tool card's "查看回放" button opens a replay here.

import React, { useCallback, useEffect, useState } from "react";
import { RefreshCw, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ReplayPlayer } from "@/app/components/ReplayPlayer";
import {
  OPEN_REPLAY_EVENT,
  Replay,
  ReplayEntry,
  idForPath,
  listReplays,
  loadReplay,
} from "@/lib/workbench";

export function SimPanel({ onClose }: { onClose?: () => void }) {
  const [entries, setEntries] = useState<ReplayEntry[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [replay, setReplay] = useState<Replay | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<"all" | "afsim" | "2d">("all");

  const refresh = useCallback(async () => {
    try {
      const list = await listReplays();
      setEntries(list);
      setError(null);
      return list;
    } catch (e) {
      setError(String((e as Error).message || e));
      return [];
    }
  }, []);

  const open = useCallback(async (id: string) => {
    setCurrent(id);
    setLoading(true);
    setError(null);
    try {
      setReplay(await loadReplay(id));
    } catch (e) {
      setReplay(null);
      setError(String((e as Error).message || e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 20000);
    return () => clearInterval(timer);
  }, [refresh]);

  // "查看回放" from a tool card: a workspace path
  useEffect(() => {
    const handler = async (ev: Event) => {
      const path = (ev as CustomEvent).detail?.path as string | undefined;
      if (!path) return;
      let id = idForPath(path, entries);
      if (!id) id = idForPath(path, await refresh());
      if (id) open(id);
      else setError(`没有找到回放 ${path}`);
    };
    window.addEventListener(OPEN_REPLAY_EVENT, handler);
    return () => window.removeEventListener(OPEN_REPLAY_EVENT, handler);
  }, [entries, refresh, open]);

  const shown = entries.filter((e) => filter === "all" || e.source === filter);

  return (
    <div className="flex h-full min-h-0 flex-col gap-2 p-3">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold">仿真回放</h2>
        <select
          className="min-w-0 flex-1 rounded border border-border bg-background px-2 py-1 text-xs"
          value={current ?? ""}
          onChange={(e) => e.target.value && open(e.target.value)}
          aria-label="选择回放"
        >
          <option value="">{shown.length ? "选择一个回放…" : "暂无回放"}</option>
          {shown.map((e) => (
            <option key={e.id} value={e.id}>
              {e.title} · {new Date(e.mtime * 1000).toLocaleString("zh-CN", { hour12: false })}
            </option>
          ))}
        </select>
        <select
          className="rounded border border-border bg-background px-1 py-1 text-xs"
          value={filter}
          onChange={(e) => setFilter(e.target.value as typeof filter)}
          aria-label="来源"
        >
          <option value="all">全部</option>
          <option value="afsim">AFSIM</option>
          <option value="2d">二维</option>
        </select>
        <Button size="sm" variant="outline" onClick={refresh} title="刷新列表">
          <RefreshCw className="h-4 w-4" />
        </Button>
        {onClose && (
          <Button size="sm" variant="ghost" onClick={onClose} title="关闭面板">
            <X className="h-4 w-4" />
          </Button>
        )}
      </div>
      {error && <div className="rounded bg-red-50 px-2 py-1 text-xs text-red-700">{error}</div>}
      {loading && <div className="text-xs text-muted-foreground">正在读取回放（AFSIM 记录较大时需要几秒）…</div>}
      {replay ? (
        <div className="min-h-0 flex-1">
          <div className="mb-1 truncate text-xs text-muted-foreground" title={replay.title}>
            {replay.title}
          </div>
          <ReplayPlayer replay={replay} />
        </div>
      ) : (
        !loading && (
          <div className="flex flex-1 items-center justify-center text-center text-xs text-muted-foreground">
            二维仿真（simulate2d）和 AFSIM 运行（afsim_run）结束后，回放会出现在上面的列表里；
            <br />
            也可以点对话中工具卡片上的“查看回放”。
          </div>
        )
      )}
    </div>
  );
}
