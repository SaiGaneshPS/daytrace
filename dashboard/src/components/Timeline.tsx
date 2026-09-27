// DT-31: the cross-device timeline: one lane per device (Windows, Mac, Android, iPhone, browser sites), then the
// calendar, sleep and meals, over the local day. Built on ECharts (a custom series) with the DT-52 theme.
//
// - Blocks are colored by category (with the category's pattern), and wide blocks carry the app's name, so color
//   is never the only clue. Hover or tap shows the app, its start and end, and the exact minutes from the hub.
// - Zoom and pan with the wheel, a pinch or the slider; the first view frames the part of the day with activity.
// - Live: the page refreshes it every few seconds; the chart updates in place, so new blocks slide in and your
//   zoom stays. A dashed line marks now, and devices that synced in the last minute get a pulsing dot.
// Every number is the hub's (GET /timeline); nothing is added up here.
import type { CustomSeriesRenderItemAPI, CustomSeriesRenderItemParams } from "echarts";
import * as echarts from "echarts/core";
import { useMemo, useRef } from "react";
import type { components } from "../api/schema";
import { CATEGORY_LABELS, type Category, type ChartOption, token, useEChart } from "../theme/charts";
import { formatMinutes } from "./StatCard";

type TimelineData = components["schemas"]["Timeline"];

const TYPE_ORDER = ["windows", "macos", "android", "ios", "browser"];
const LIVE_MS = 60_000;
const ROW_HEIGHT = 44;

type Row = { label: string };
type Block = {
  row: number;
  start: number;
  end: number;
  name: string;
  detail: string;
  minutes: number | null;
  category: string | null;
  estimated: boolean;
  color: string;
  opacity: number;
};

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

function categoryName(category: string | null): string {
  return category && category in CATEGORY_LABELS ? CATEGORY_LABELS[category as Category] : "Other";
}

type Props = {
  data: TimelineData;
  /** When each device last synced (ISO), for the live dots. */
  lastSeen?: Record<string, string | null>;
  /** The current time (ms) when the day is today, for the "now" line and live dots. */
  now?: number;
};

export default function Timeline({ data, lastSeen = {}, now }: Props) {
  const framedFor = useRef<string | null>(null);

  const lanes = useMemo(
    () =>
      [...data.lanes].sort(
        (a, b) => TYPE_ORDER.indexOf(a.device_type) - TYPE_ORDER.indexOf(b.device_type) || a.name.localeCompare(b.name),
      ),
    [data.lanes],
  );

  const { rows, blocks, meals } = useMemo(() => {
    const rows: Row[] = lanes.map((lane) => ({ label: lane.counted ? lane.name : `${lane.name} (sites)` }));
    const blocks: Block[] = [];
    lanes.forEach((lane, row) => {
      for (const session of lane.sessions) {
        blocks.push({
          row,
          start: Date.parse(session.start),
          end: Date.parse(session.end),
          name: session.app ?? session.app_id ?? "Unknown app",
          detail: categoryName(session.category ?? null),
          minutes: session.minutes,
          category: session.category ?? null,
          estimated: session.estimated,
          color: `var(--cat-${session.category && session.category in CATEGORY_LABELS ? session.category : "other"})`,
          opacity: lane.counted ? 1 : 0.55,
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
          row, start, end, name: entry.title || "Calendar event", detail: "Calendar", minutes: Math.round((end - start) / 600) / 100,
          category: null, estimated: false, color: "var(--accent)", opacity: 0.35,
        });
      }
    }
    if (data.sleep.length) {
      const row = rows.push({ label: "Sleep" }) - 1;
      for (const entry of data.sleep) {
        blocks.push({
          row, start: Date.parse(entry.start), end: Date.parse(entry.end), name: entry.stage === "awake" ? "Awake" : "Asleep",
          detail: "Sleep", minutes: entry.minutes, category: null, estimated: entry.estimated,
          color: "var(--cat-study)", opacity: entry.stage === "awake" ? 0.25 : 0.5,
        });
      }
    }
    let meals: { row: number; time: number; name: string }[] = [];
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

  const option = useMemo<ChartOption>(() => {
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

    // Frame the part of the day with activity, once per day shown; after that the user's zoom stays.
    const times = blocks.flatMap((block) => [block.start, block.end]).concat(meals.map((meal) => meal.time));
    const frame = framedFor.current !== data.date && times.length > 0;
    if (frame) framedFor.current = data.date;
    const first = Math.max(dayStart, Math.min(...times) - 30 * 60_000);
    const last = Math.min(dayEnd, Math.max(...times, now ?? 0) + 30 * 60_000);

    return {
      grid: { left: 8, right: 16, top: 26, bottom: 44, containLabel: true }, // top: room for the "now" label
      tooltip: {
        trigger: "item",
        confine: true,
        formatter: (params: unknown) => {
          const item = (params as { data?: { block?: Block; meal?: { time: number; name: string } } }).data;
          if (item?.meal) return `<strong>${escape(item.meal.name)}</strong><br/>${clock(item.meal.time)}`;
          const block = item?.block;
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
      yAxis: { type: "category", data: rows.map((row) => row.label), inverse: true, axisLabel: { fontWeight: 600 } },
      dataZoom: [
        { id: "zoom", type: "inside", xAxisIndex: 0, filterMode: "weakFilter", minValueSpan: 10 * 60_000,
          ...(frame ? { startValue: first, endValue: last } : {}) },
        { id: "slider", type: "slider", xAxisIndex: 0, filterMode: "weakFilter", height: 18, bottom: 8,
          labelFormatter: (value: number) => clock(value), borderColor: "var(--border)", fillerColor: "rgba(99,102,241,0.15)" },
      ],
      series: [
        {
          id: "blocks",
          type: "custom",
          renderItem: renderBlock,
          encode: { x: [1, 2], y: 0 },
          data: blocks.map((block) => ({
            value: [block.row, block.start, block.end],
            itemStyle: { color: block.color, opacity: block.opacity },
            block,
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
          data: meals.map((meal) => ({ value: [meal.time, meal.row], meal })),
        },
      ],
    };
  }, [blocks, meals, rows, dayStart, dayEnd, now, data.date]);

  const chart = useEChart(option, `Timeline of ${data.date}, one lane per device`, { merge: true });
  const categories = [...new Set(blocks.map((block) => block.category).filter((c): c is string => Boolean(c)))];

  return (
    <div className="timeline">
      <ul className="device-chips" aria-label="Devices">
        {lanes.map((lane) => {
          const seen = lastSeen[lane.device_id];
          const live = now !== undefined && seen ? now - Date.parse(seen) < LIVE_MS : false;
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
            <li key={category} className={`chip cat-${category in CATEGORY_LABELS ? category : "other"}`}>
              <span className="swatch" aria-hidden="true" />
              {categoryName(category)}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
