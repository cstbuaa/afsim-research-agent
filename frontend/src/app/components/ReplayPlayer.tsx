"use client";

// Plays a replay/1 (2D run or AFSIM .aer recording) on a canvas: vehicles with heading,
// trails, routes, interface points, sensor range, detections / shots; state timeline and
// verdicts below. Units: metres east (x) / north (y) of the own start.

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Pause, Play, SkipBack, SkipForward, Maximize2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Replay,
  sampleAt,
  sideColor,
  stateAt,
  stateColor,
} from "@/lib/workbench";

interface View {
  cx: number; // world point at the canvas centre
  cy: number;
  scale: number; // pixels per metre
}

const SPEEDS = [0.5, 1, 2, 4, 8, 16, 32];
const FLASH_S = 4;

function bounds(replay: Replay): [number, number, number, number] {
  let x0 = Infinity,
    y0 = Infinity,
    x1 = -Infinity,
    y1 = -Infinity;
  const add = (x: number, y: number) => {
    x0 = Math.min(x0, x);
    y0 = Math.min(y0, y);
    x1 = Math.max(x1, x);
    y1 = Math.max(y1, y);
  };
  Object.values(replay.samples).forEach((tr) => tr.forEach((s) => add(s[1], s[2])));
  replay.routes.forEach((r) => r.points.forEach((p) => add(p[0], p[1])));
  replay.points.forEach((p) => add(p.x, p.y));
  if (!isFinite(x0)) return [-500, -500, 500, 500];
  return [x0, y0, x1, y1];
}

function niceStep(metres: number): number {
  const p = Math.pow(10, Math.floor(Math.log10(metres)));
  for (const m of [1, 2, 5, 10]) if (m * p >= metres) return m * p;
  return 10 * p;
}

export function ReplayPlayer({ replay }: { replay: Replay }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ w: 600, h: 400 });
  const [t, setT] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(4);
  const [view, setView] = useState<View | null>(null);
  const [layers, setLayers] = useState({ trails: true, sensors: true, routes: true, labels: true });
  const [selected, setSelected] = useState<string | null>(null);
  const drag = useRef<{ x: number; y: number; v: View } | null>(null);

  const duration = Math.max(replay.duration_s || 0, 1);
  const platforms = replay.platforms;
  const side = useMemo(
    () => Object.fromEntries(platforms.map((p) => [p.id, p.side])),
    [platforms]
  );

  const fit = useCallback(() => {
    const [x0, y0, x1, y1] = bounds(replay);
    const w = Math.max(x1 - x0, 200);
    const h = Math.max(y1 - y0, 200);
    const scale = 0.85 * Math.min(size.w / w, size.h / h);
    setView({ cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, scale });
  }, [replay, size]);

  useEffect(() => {
    setT(0);
    setPlaying(false);
    setSelected(null);
  }, [replay]);

  useEffect(() => {
    fit();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [replay, size.w > 0]);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const r = el.getBoundingClientRect();
      setSize({ w: Math.max(200, Math.floor(r.width)), h: Math.max(200, Math.floor(r.height)) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // playback clock
  useEffect(() => {
    if (!playing) return;
    let last = performance.now();
    let raf = 0;
    const tick = (now: number) => {
      const dt = ((now - last) / 1000) * speed;
      last = now;
      setT((prev) => {
        const next = prev + dt;
        if (next >= duration) {
          setPlaying(false);
          return duration;
        }
        return next;
      });
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing, speed, duration]);

  const lastAction = useCallback(
    (id: string, time: number): string => {
      let out = "";
      for (const a of replay.actions) {
        if (a[1] === id && (a[0] as number) <= time + 1e-6 && a[2] === "start") out = String(a[3]);
      }
      return out;
    },
    [replay]
  );

  // draw
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !view) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = size.w * dpr;
    canvas.height = size.h * dpr;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const css = getComputedStyle(document.documentElement);
    const fg = css.getPropertyValue("--foreground").trim();
    const ink = fg ? `hsl(${fg})` : "#111827";
    const bgVar = css.getPropertyValue("--background").trim();
    ctx.fillStyle = bgVar ? `hsl(${bgVar})` : "#f8fafc";
    ctx.fillRect(0, 0, size.w, size.h);
    const X = (x: number) => size.w / 2 + (x - view.cx) * view.scale;
    const Y = (y: number) => size.h / 2 - (y - view.cy) * view.scale;

    // grid
    const step = niceStep(80 / view.scale);
    ctx.strokeStyle = "#94a3b833";
    ctx.lineWidth = 1;
    ctx.font = "10px var(--font-family-mono, monospace)";
    ctx.fillStyle = "#9ca3af";
    const wx0 = view.cx - size.w / 2 / view.scale;
    const wx1 = view.cx + size.w / 2 / view.scale;
    const wy0 = view.cy - size.h / 2 / view.scale;
    const wy1 = view.cy + size.h / 2 / view.scale;
    for (let gx = Math.ceil(wx0 / step) * step; gx <= wx1; gx += step) {
      ctx.beginPath();
      ctx.moveTo(X(gx), 0);
      ctx.lineTo(X(gx), size.h);
      ctx.stroke();
      ctx.fillText(`${Math.round(gx)}`, X(gx) + 2, size.h - 4);
    }
    for (let gy = Math.ceil(wy0 / step) * step; gy <= wy1; gy += step) {
      ctx.beginPath();
      ctx.moveTo(0, Y(gy));
      ctx.lineTo(size.w, Y(gy));
      ctx.stroke();
      ctx.fillText(`${Math.round(gy)}`, 2, Y(gy) - 2);
    }

    // routes and points
    if (layers.routes) {
      ctx.setLineDash([6, 4]);
      ctx.lineWidth = 1.5;
      for (const r of replay.routes) {
        if (r.points.length < 2) continue;
        ctx.strokeStyle = r.platform ? sideColor(side[r.platform] || "") + "88" : "#64748b";
        ctx.beginPath();
        r.points.forEach((p, i) => (i ? ctx.lineTo(X(p[0]), Y(p[1])) : ctx.moveTo(X(p[0]), Y(p[1]))));
        ctx.stroke();
      }
      ctx.setLineDash([]);
      for (const p of replay.points) {
        ctx.fillStyle = "#0f766e";
        ctx.beginPath();
        ctx.moveTo(X(p.x), Y(p.y) - 6);
        ctx.lineTo(X(p.x) + 6, Y(p.y));
        ctx.lineTo(X(p.x), Y(p.y) + 6);
        ctx.lineTo(X(p.x) - 6, Y(p.y));
        ctx.closePath();
        ctx.fill();
        ctx.fillText(p.id, X(p.x) + 8, Y(p.y) - 6);
      }
    }

    // sensors
    if (layers.sensors) {
      for (const [id, s] of Object.entries(replay.sensors)) {
        const now = sampleAt(replay.samples[id], t);
        if (!now || !s.range_m) continue;
        ctx.strokeStyle = "#2563eb55";
        ctx.fillStyle = "#2563eb0d";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(X(now[1]), Y(now[2]), s.range_m * view.scale, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
      }
    }

    // trails
    if (layers.trails) {
      for (const p of platforms) {
        const tr = replay.samples[p.id];
        if (!tr) continue;
        ctx.strokeStyle = sideColor(p.side) + "99";
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        let started = false;
        for (const s of tr) {
          if (s[0] > t) break;
          if (!started) {
            ctx.moveTo(X(s[1]), Y(s[2]));
            started = true;
          } else ctx.lineTo(X(s[1]), Y(s[2]));
        }
        ctx.stroke();
      }
    }

    // detections and shots (recent)
    for (const e of replay.events) {
      const et = e[0] as number;
      if (et > t || t - et > FLASH_S) continue;
      const a = sampleAt(replay.samples[String(e[2])], t);
      const b = e[3] !== undefined ? sampleAt(replay.samples[String(e[3])], t) : null;
      if (!a) continue;
      const kind = String(e[1]);
      const color = kind === "fire" || kind === "hit" ? "#dc2626" : kind === "lost" ? "#9ca3af" : "#f59e0b";
      if (b) {
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.setLineDash(kind === "lost" ? [4, 4] : []);
        ctx.beginPath();
        ctx.moveTo(X(a[1]), Y(a[2]));
        ctx.lineTo(X(b[1]), Y(b[2]));
        ctx.stroke();
        ctx.setLineDash([]);
      }
    }

    // vehicles
    for (const p of platforms) {
      const s = sampleAt(replay.samples[p.id], t);
      if (!s) continue;
      const x = X(s[1]);
      const y = Y(s[2]);
      const hd = ((s[4] || 0) * Math.PI) / 180;
      ctx.save();
      ctx.translate(x, y);
      ctx.rotate(hd); // compass heading: 0 = up (north), clockwise
      ctx.fillStyle = sideColor(p.side);
      ctx.strokeStyle = selected === p.id ? "#111827" : "#ffffff";
      ctx.lineWidth = selected === p.id ? 2.5 : 1.5;
      ctx.beginPath();
      ctx.moveTo(0, -10);
      ctx.lineTo(7, 8);
      ctx.lineTo(0, 4);
      ctx.lineTo(-7, 8);
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
      ctx.restore();
      if (layers.labels) {
        const st = stateAt(replay.states[p.id], t);
        const act = lastAction(p.id, t);
        ctx.fillStyle = ink;
        ctx.font = "12px var(--font-family-base, sans-serif)";
        ctx.fillText(p.label, x + 12, y - 4);
        ctx.font = "11px var(--font-family-mono, monospace)";
        if (st) {
          ctx.fillStyle = stateColor(st);
          ctx.fillText(st, x + 12, y + 10);
        }
        if (act && selected === p.id) {
          ctx.fillStyle = "#475569";
          ctx.fillText(act, x + 12, y + 23);
        }
      }
    }

    // scale bar
    const bar = niceStep(120 / view.scale);
    ctx.fillStyle = ink;
    ctx.fillRect(size.w - 20 - bar * view.scale, 16, bar * view.scale, 3);
    ctx.font = "11px var(--font-family-base, sans-serif)";
    ctx.fillText(bar >= 1000 ? `${bar / 1000} km` : `${bar} m`, size.w - 20 - bar * view.scale, 32);
  }, [replay, view, size, t, layers, selected, platforms, side, lastAction]);

  // interaction
  const onWheel = (e: React.WheelEvent) => {
    if (!view) return;
    const k = e.deltaY < 0 ? 1.2 : 1 / 1.2;
    setView({ ...view, scale: Math.min(50, Math.max(0.005, view.scale * k)) });
  };
  const onDown = (e: React.MouseEvent) => {
    if (view) drag.current = { x: e.clientX, y: e.clientY, v: view };
  };
  const onMove = (e: React.MouseEvent) => {
    const d = drag.current;
    if (!d) return;
    setView({
      ...d.v,
      cx: d.v.cx - (e.clientX - d.x) / d.v.scale,
      cy: d.v.cy + (e.clientY - d.y) / d.v.scale,
    });
  };
  const onUp = (e: React.MouseEvent) => {
    const d = drag.current;
    drag.current = null;
    if (!d || !view || Math.hypot(e.clientX - d.x, e.clientY - d.y) > 3) return;
    // a click: select the nearest vehicle
    const rect = canvasRef.current!.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;
    let best: string | null = null;
    let bestD = 18;
    for (const p of platforms) {
      const s = sampleAt(replay.samples[p.id], t);
      if (!s) continue;
      const dx = size.w / 2 + (s[1] - view.cx) * view.scale - px;
      const dy = size.h / 2 - (s[2] - view.cy) * view.scale - py;
      const dist = Math.hypot(dx, dy);
      if (dist < bestD) {
        bestD = dist;
        best = p.id;
      }
    }
    setSelected(best);
  };

  const now = (id: string) => sampleAt(replay.samples[id], t);
  const nearestEnemy = (id: string): number | null => {
    const a = now(id);
    if (!a) return null;
    let best: number | null = null;
    for (const p of platforms) {
      if (p.side === side[id]) continue;
      const b = now(p.id);
      if (!b) continue;
      const d = Math.hypot(a[1] - b[1], a[2] - b[2]);
      best = best === null ? d : Math.min(best, d);
    }
    return best;
  };

  const own = platforms.filter((p) => p.side === "blue");
  const timelineRows = platforms.filter((p) => replay.states[p.id]?.length);

  return (
    <div className="flex h-full min-h-0 flex-col gap-2">
      <div
        ref={wrapRef}
        className="relative min-h-[240px] flex-1 overflow-hidden rounded-md border border-border"
      >
        <canvas
          ref={canvasRef}
          style={{ width: size.w, height: size.h, cursor: drag.current ? "grabbing" : "grab" }}
          onWheel={onWheel}
          onMouseDown={onDown}
          onMouseMove={onMove}
          onMouseUp={onUp}
          onMouseLeave={() => (drag.current = null)}
        />
        <div className="absolute left-2 top-2 rounded bg-background/85 px-2 py-1 text-xs text-foreground shadow-sm">
          {replay.source === "afsim" ? "AFSIM 回放" : "二维仿真"} · t = {t.toFixed(1)} s / {duration.toFixed(0)} s
          {own.map((p) => {
            const d = nearestEnemy(p.id);
            return d !== null ? (
              <div key={p.id}>
                {p.label} 距最近敌方 {(d / 1000).toFixed(2)} km
              </div>
            ) : null;
          })}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Button size="sm" variant="outline" onClick={() => setT(Math.max(0, t - 1))} title="后退 1 s">
          <SkipBack className="h-4 w-4" />
        </Button>
        <Button
          size="sm"
          onClick={() => {
            if (t >= duration) setT(0);
            setPlaying(!playing);
          }}
        >
          {playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
        </Button>
        <Button size="sm" variant="outline" onClick={() => setT(Math.min(duration, t + 1))} title="前进 1 s">
          <SkipForward className="h-4 w-4" />
        </Button>
        <select
          className="rounded border border-border bg-background px-1 py-1"
          value={speed}
          onChange={(e) => setSpeed(Number(e.target.value))}
          aria-label="倍速"
        >
          {SPEEDS.map((s) => (
            <option key={s} value={s}>
              {s}×
            </option>
          ))}
        </select>
        <Button size="sm" variant="outline" onClick={fit} title="适应窗口">
          <Maximize2 className="h-4 w-4" />
        </Button>
        {(
          [
            ["trails", "轨迹"],
            ["routes", "路线/点"],
            ["sensors", "雷达范围"],
            ["labels", "标注"],
          ] as const
        ).map(([k, label]) => (
          <label key={k} className="flex items-center gap-1">
            <input
              type="checkbox"
              checked={layers[k]}
              onChange={(e) => setLayers({ ...layers, [k]: e.target.checked })}
            />
            {label}
          </label>
        ))}
      </div>

      <input
        type="range"
        min={0}
        max={duration}
        step={0.1}
        value={t}
        onChange={(e) => setT(Number(e.target.value))}
        aria-label="时间"
      />

      {/* state timeline */}
      <div className="space-y-1 text-[11px]">
        {timelineRows.map((p) => {
          const seq = replay.states[p.id];
          return (
            <div key={p.id} className="flex items-center gap-2">
              <span className="w-24 shrink-0 truncate" title={p.label}>
                {p.label}
              </span>
              <div className="relative h-4 flex-1 overflow-hidden rounded bg-muted">
                {seq.map(([ts, s], i) => {
                  const te = i + 1 < seq.length ? seq[i + 1][0] : duration;
                  return (
                    <div
                      key={i}
                      title={`${s} ${ts}–${te} s`}
                      className="absolute top-0 h-full"
                      style={{
                        left: `${(ts / duration) * 100}%`,
                        width: `${Math.max(((te - ts) / duration) * 100, 0.4)}%`,
                        background: stateColor(s),
                        opacity: 0.8,
                      }}
                    />
                  );
                })}
                {replay.events
                  .filter((e) => e[2] === p.id)
                  .map((e, i) => (
                    <div
                      key={`e${i}`}
                      title={`${e[1]} ${e[3] ?? ""} @ ${e[0]} s`}
                      className="absolute top-0 h-full w-[2px] bg-black"
                      style={{ left: `${((e[0] as number) / duration) * 100}%` }}
                    />
                  ))}
                <div
                  className="absolute top-0 h-full w-[2px] bg-red-600"
                  style={{ left: `${(t / duration) * 100}%` }}
                />
              </div>
            </div>
          );
        })}
      </div>

      {selected && (
        <div className="rounded border border-border p-2 text-[11px]">
          <div className="font-medium">{platforms.find((p) => p.id === selected)?.label}</div>
          <div>
            状态：
            {(replay.states[selected] || []).map(([ts, s]) => `${s}@${ts}`).join(" → ") || "—"}
          </div>
          <div>当前动作：{lastAction(selected, t) || "—"}</div>
        </div>
      )}

      {replay.verdicts.length > 0 && (
        <div className="max-h-40 overflow-auto rounded border border-border p-2 text-[11px]">
          {replay.verdicts.map((v, i) => (
            <div key={i} className="flex gap-2">
              <span
                className={
                  v.judged === false ? "text-slate-400" : v.passed ? "text-green-700" : "text-red-700"
                }
              >
                {v.judged === false ? "未判定" : v.passed ? "通过" : "未通过"}
              </span>
              <span className="font-mono">{v.id}</span>
              <span className="text-muted-foreground">{v.detail}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
