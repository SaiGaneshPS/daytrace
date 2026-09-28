// DT-31: the cross-device timeline: one lane per device, in the hub's order (Windows, Mac, Android, iPhone, browser
// sites), then the calendar, sleep and meals, over the local day. Built on ECharts (a custom series) with the DT-52
// theme.
//
// - Blocks are colored by category and carry the category's pattern, and wide blocks show the app's name, so color is
//   never the only clue. Hover or tap shows the app, its start and end, and the exact minutes from the hub.
// - Zoom with Ctrl and the wheel, drag to pan, or use the slider (on touch screens, the slider only, so the page still
//   scrolls). The first view frames the part of the day with activity, and the view you choose is kept, even when
//   the chart is rebuilt for a theme change.
// - Live: the page refreshes the data every few seconds; the chart updates in place, so new blocks slide in. A dashed
//   line marks now, and devices that synced in the last minute get a pulsing dot.
// Every number is the hub's (GET /timeline); nothing is added up here.
import type { CustomSeriesRenderItemAPI, CustomSeriesRenderItemParams } from "echarts";
import * as echarts from "echarts/core";
import { useCallback, useEffect, useMemo, useRef } from "react";
import type { components } from "../api/schema";
import { CATEGORY_DECALS, CATEGORY_LABELS, type Category, type ChartOption, token, useEChart } from "../theme/charts";
import { useMediaQuery } from "../theme/motion";
import { formatMinutes } from "./StatCard";

type TimelineData = components["schemas"]["Timeline"];

const LIVE_MS = 60_000;
const WHEEL_EVENTS = ["wheel", "mousewheel"] as const;
const ROW_HEIGHT = 44;
const SLEEP_NAMES: Record<string, string> = {
  in_bed: "In bed", awake: "Awake", asleep: "Asleep", light: "Light sleep", core: "Core sleep", deep: "Deep sleep", rem: "REM sleep",
};

type Row = { label: string };
type Block = {
  row: number;
  start: number;
  end: number;
  name: string;
  detail: string;
  minutes: number | null;
  estimated: boolean;
  category: Category | null;
  color: string;
  opacity: number;
};
type Meal = { row: number; time: number; name: string };
type View = { date: string; start: number; end: number };

const escape = (text: string) => echarts.format.encodeHTML(text);
const clock = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

/** Dark or white text, whichever reads better on a block's color. */
function readableOn(color: unknown): string {
  const hex = typeof color === "string" && /^#[0-9a-f]{6}$/i.test(color) ? color : "#64748b";
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const linear = (c: number) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  const luminance = 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
  return luminance > 0.36 ? "#111827" : "#ffffff";
}

function asCategory(category: string | null | undefined): Category {
  return category && category in CATEGORY_LABELS ? (category as Category) : "other";
}

type Props = {
  data: TimelineData;
  /** The current time (ms) when the day is today, for the "now" line and the live dots. */
  now?: number;
};

export default function Timeline({ data, now }: Props) {
  const touch = useMediaQuery("(pointer: coarse)");
  // On a phone the lane names get a narrow column (cut short: the chips above give them in full), so the day has room.
  const narrow = useMediaQuery("(max-width: 599px)");
  const view = useRef<View | null>(null);
  const wrapper = useRef<HTMLDivElement>(null);

  // ECharts cancels every wheel event over a chart with an inside zoom (it checks "ctrl" only after that), which would
  // trap the page's scroll here. A wheel without Ctrl stops before it reaches the chart, so it scrolls the page.
  useEffect(() => {
    const element = wrapper.current;
    if (!element) return;
    const pass = (event: Event) => {
      if (!(event as WheelEvent).ctrlKey) event.stopPropagation();
    };
    for (const name of WHEEL_EVENTS) element.addEventListener(name, pass, { capture: true });
    return () => {
      for (const name of WHEEL_EVENTS) element.removeEventListener(name, pass, { capture: true });
    };
  }, []);
  const lanes = data.lanes; // already in the hub's order

  const { rows, blocks, meals } = useMemo(() => {
    const rows: Row[] = lanes.map((lane) => ({ label: lane.counted ? lane.name : `${lane.name} (sites)` }));
    const blocks: Block[] = [];
    lanes.forEach((lane, row) => {
      for (const session of lane.sessions) {
        const category = asCategory(session.category);
        blocks.push({
          row, start: Date.parse(session.start), end: Date.parse(session.end), name: session.app ?? session.app_id ?? "Unknown app",
          detail: CATEGORY_LABELS[category], minutes: session.minutes, estimated: session.estimated, category,
          color: `var(--cat-${category})`, opacity: lane.counted ? 1 : 0.55,
        });
      }
    });
    const timed = data.calendar.filter((entry) => !entry.all_day);
    if (timed.length) {
      const row = rows.push({ label: "Calendar" }) - 1;
      for (const entry of timed) {
        const start = Date.parse(entry.start);
        const end = Date.parse(entry.end);
        blocks.push({
          row, start, end, name: entry.title || "Calendar event", detail: "Calendar", minutes: null, estimated: false,
          category: null, color: "var(--accent)", opacity: 0.35,
        });
      }
    }
    if (data.sleep.length) {
      const row = rows.push({ label: "Sleep" }) - 1;
      // Time in bed goes underneath, the stages on top; the hub counts only the stages as sleep.
      const stages = [...data.sleep].sort((a, b) => Number(b.stage === "in_bed") - Number(a.stage === "in_bed"));
      for (const entry of stages) {
        const background = entry.stage === "in_bed" || entry.stage === "awake";
        blocks.push({
          row, start: Date.parse(entry.start), end: Date.parse(entry.end), name: SLEEP_NAMES[entry.stage ?? "asleep"] ?? "Sleep",
          detail: entry.stage === "in_bed" ? "In bed (not counted as sleep)" : entry.stage === "awake" ? "Awake (not counted as sleep)" : "Sleep",
          minutes: entry.minutes, estimated: entry.estimated, category: null, color: "var(--cat-study)", opacity: background ? 0.22 : 0.55,
        });
      }
    }
    let meals: Meal[] = [];
    if (data.meals.length) {
      const row = rows.push({ label: "Meals" }) - 1;
      meals = data.meals.map((meal) => ({
        row,
        time: Date.parse(meal.time),
        name: [meal.meal_type, meal.items?.length ? meal.items.join(", ") : meal.text].filter(Boolean).join(": ") || "Meal",
      }));
    }
    return { rows, blocks, meals };
  }, [lanes, data.calendar, data.sleep, data.meals]);

  const dayStart = Date.parse(data.meta.range.start);
  const dayEnd = Date.parse(data.meta.range.end);

  // The view to show: the one the user chose for this day, else a frame around the day's activity.
  const currentView = useCallback((): View => {
    if (view.current?.date === data.date) return view.current;
    const times = blocks.flatMap((block) => [block.start, block.end]).concat(meals.map((meal) => meal.time));
    const framed: View = times.length
      ? { date: data.date, start: Math.max(dayStart, Math.min(...times) - 30 * 60_000), end: Math.min(dayEnd, Math.max(...times, now ?? 0) + 30 * 60_000) }
      : { date: data.date, start: dayStart, end: dayEnd };
    view.current = framed;
    return framed;
  }, [blocks, meals, data.date, dayStart, dayEnd, now]);

  const onZoom = useCallback(
    (params: unknown) => {
      const event = params as { start?: number; end?: number; batch?: { start?: number; end?: number }[] };
      const zoom = event.batch?.[0] ?? event;
      if (zoom.start === undefined || zoom.end === undefined) return;
      const span = dayEnd - dayStart;
      view.current = { date: data.date, start: dayStart + (span * zoom.start) / 100, end: dayStart + (span * zoom.end) / 100 };
    },
    [data.date, dayStart, dayEnd],
  );

  const option = useCallback((): ChartOption => {
    const renderBlock = (params: CustomSeriesRenderItemParams, api: CustomSeriesRenderItemAPI) => {
      const block = blocks[params.dataIndex];
      const row = api.value(0) as number;
      const from = api.coord([api.value(1), row]);
      const to = api.coord([api.value(2), row]);
      const height = (api.size?.([0, 1]) as number[])[1] * 0.64;
      const system = params.coordSys as unknown as { x: number; y: number; width: number; height: number };
      const shape = echarts.graphic.clipRectByRect(
        { x: from[0], y: from[1] - height / 2, width: Math.max(to[0] - from[0], 1.5), height },
        system,
      );
      if (!shape) return null;
      const style = api.style();
      const label = block && shape.width > 60 ? block.name : "";
      // On a see-through block (calendar, sleep, browser sites) the text sits on the card: use the text color.
      const ink = block && block.opacity < 1 ? token("--text", "#111827") : readableOn((style as { fill?: unknown }).fill);
      return {
        type: "rect" as const,
        transition: ["shape"],
        shape: { ...shape, r: 5 },
        style,
        textContent: label
          ? { style: { text: label, fill: ink, fontSize: 11, fontWeight: 600, overflow: "truncate", width: shape.width - 10 } }
          : undefined,
        textConfig: label ? { position: "inside" as const } : undefined,
      };
    };
    const shown = currentView();
    return {
      grid: { left: 8, right: 16, top: 26, bottom: 44, containLabel: true }, // top: room for the "now" label
      tooltip: {
        trigger: "item",
        confine: true,
        formatter: (params: unknown) => {
          const item = params as { seriesId?: string; dataIndex: number };
          if (item.seriesId === "meals") {
            const meal = meals[item.dataIndex];
            return meal ? `<strong>${escape(meal.name)}</strong><br/>${clock(meal.time)}` : "";
          }
          const block = blocks[item.dataIndex];
          if (!block) return "";
          const minutes = block.minutes === null ? "" : `<br/>${block.minutes.toLocaleString()} min (${formatMinutes(block.minutes)})`;
          return `<strong>${escape(block.name)}</strong><br/>${escape(block.detail)}<br/>${clock(block.start)} to ${clock(block.end)}${minutes}${block.estimated ? "<br/><em>Estimated</em>" : ""}`;
        },
      },
      xAxis: {
        type: "time",
        min: dayStart,
        max: dayEnd,
        axisLabel: { formatter: (value: number) => clock(value), hideOverlap: true },
        splitLine: { show: true, lineStyle: { color: "var(--border)", type: "dashed" } },
      },
      yAxis: {
        type: "category",
        data: rows.map((row) => row.label),
        inverse: true,
        axisLabel: narrow ? { fontWeight: 600, fontSize: 11, width: 84, overflow: "truncate" } : { fontWeight: 600 },
      },
      dataZoom: [
        {
          id: "zoom", type: "inside", xAxisIndex: 0, filterMode: "weakFilter", minValueSpan: 10 * 60_000,
          disabled: touch, // on touch screens a swipe must scroll the page; the slider zooms there
          zoomOnMouseWheel: "ctrl", moveOnMouseWheel: false, moveOnMouseMove: true, // plain wheels never get here (above)
          startValue: shown.start, endValue: shown.end,
        },
        {
          id: "slider", type: "slider", xAxisIndex: 0, filterMode: "weakFilter", height: 18, bottom: 8,
          labelFormatter: (value: number) => clock(value), borderColor: "var(--border)", fillerColor: "rgba(99,102,241,0.15)",
          startValue: shown.start, endValue: shown.end,
        },
      ],
      series: [
        {
          id: "blocks",
          type: "custom",
          renderItem: renderBlock,
          encode: { x: [1, 2], y: 0 },
          data: blocks.map((block) => ({
            value: [block.row, block.start, block.end],
            itemStyle: { color: block.color, opacity: block.opacity, ...(block.category ? { decal: CATEGORY_DECALS[block.category] } : {}) },
          })),
          markLine:
            now !== undefined
              ? { silent: true, symbol: "none", label: { formatter: "now", position: "start", color: "var(--accent-ink)" },
                  lineStyle: { color: "var(--accent)", type: "dashed", width: 2 }, data: [{ xAxis: now }] }
              : undefined,
        },
        {
          id: "meals",
          type: "scatter",
          symbolSize: 14,
          itemStyle: { color: "var(--cat-video)", borderColor: "var(--surface)", borderWidth: 2 },
          data: meals.map((meal) => ({ value: [meal.time, meal.row] })),
        },
      ],
    };
  }, [blocks, meals, rows, dayStart, dayEnd, now, touch, narrow, currentView]);

  const chart = useEChart(option, `Timeline of ${data.date}, one lane per device`, { merge: true, events: { datazoom: onZoom } });
  const categories = [...new Set(blocks.map((block) => block.category).filter((c): c is Category => c !== null))];

  return (
    <div ref={wrapper} className="timeline">
      <ul className="device-chips" aria-label="Devices">
        {lanes.map((lane) => {
          const live = now !== undefined && lane.last_seen ? now - Date.parse(lane.last_seen) < LIVE_MS : false;
          return (
            <li key={lane.device_id} className="device-chip">
              <span className={`live-dot${live ? " live" : ""}`} aria-hidden="true" />
              <span className="device-chip-name">{lane.name}</span>
              <span className="muted">{lane.counted ? formatMinutes(lane.minutes) : "sites"}</span>
              {live && <span className="visually-hidden">, synced in the last minute</span>}
            </li>
          );
        })}
      </ul>
      <div ref={chart} className="chart timeline-chart" style={{ height: rows.length * ROW_HEIGHT + 70 }} />
      {categories.length > 0 && (
        <ul className="palette timeline-legend" aria-label="Categories">
          {categories.map((category) => (
            <li key={category} className={`chip cat-${category}`}>
              <span className="swatch" aria-hidden="true" />
              {CATEGORY_LABELS[category]}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
