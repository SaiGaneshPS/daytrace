// DT-52: the chart kit. Every chart uses one ECharts theme built from the CSS tokens (theme/tokens.css), so charts
// and cards always agree, in light and dark:
//
// - the category palette, the app's font, rounded bars, soft dashed grid lines, a card-like tooltip;
// - animated entrances and updates, switched off under reduced motion;
// - never color alone: each category has its own pattern (an ECharts decal), and ECharts' aria module describes
//   the chart to screen readers (the label names it, even with no data). Pages still label their marks.
//
// useEChart(option, label) gives a ref for a <div>: it makes the chart whenever the div mounts (also after a
// loading skeleton), follows the color scheme, resizes with its box and cleans up. Colors in an option may be
// written as "var(--token)" (categoryStyle does): they are read again whenever the scheme changes, so a chart never
// keeps the other theme's colors. Only the chart types and components below are bundled.
import { BarChart, CustomChart, HeatmapChart, LineChart, PieChart, SankeyChart, ScatterChart, TreemapChart } from "echarts/charts";
import {
  AriaComponent,
  DatasetComponent,
  DataZoomInsideComponent,
  DataZoomSliderComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  VisualMapComponent,
} from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useCallback, useEffect, useRef, useState } from "react";
import { useMediaQuery, useReducedMotionPreference } from "./motion";

echarts.use([
  BarChart, CustomChart, HeatmapChart, LineChart, PieChart, SankeyChart, ScatterChart, TreemapChart,
  AriaComponent, DatasetComponent, DataZoomInsideComponent, DataZoomSliderComponent, GridComponent, LegendComponent,
  MarkLineComponent, TooltipComponent, VisualMapComponent,
  CanvasRenderer,
]);

export type ChartOption = echarts.EChartsCoreOption;

export const CATEGORIES = ["social", "video", "work", "study", "comms", "games", "health", "other"] as const;
export type Category = (typeof CATEGORIES)[number];
export const CATEGORY_LABELS: Record<Category, string> = {
  social: "Social",
  video: "Video",
  work: "Work",
  study: "Study",
  comms: "Chat and calls",
  games: "Games",
  health: "Health",
  other: "Other",
};

const stripes = (rotation: number) => ({
  symbol: "rect", symbolSize: 1, dashArrayX: [1, 0], dashArrayY: [2, 5], rotation, color: "rgba(255,255,255,0.5)",
});
const shapes = (symbol: string, size: number) => ({
  symbol, symbolSize: size, dashArrayX: [[8, 8], [0, 8, 8, 0]], dashArrayY: [6, 0], color: "rgba(255,255,255,0.55)",
});
/** One pattern per category, so marks can be told apart without color. */
export const CATEGORY_DECALS: Record<Category, Record<string, unknown>> = {
  social: shapes("circle", 0.8),
  video: stripes(Math.PI / 4),
  work: stripes(0),
  study: stripes(Math.PI / 2),
  comms: stripes(-Math.PI / 4),
  games: shapes("triangle", 0.9),
  health: shapes("diamond", 0.9),
  other: { symbol: "none" },
};

function asCategory(category: string | null | undefined): Category {
  return (CATEGORIES as readonly string[]).includes(category ?? "") ? (category as Category) : "other";
}

/** A token's current value (it changes with the color scheme). */
export function token(name: string, fallback = ""): string {
  if (typeof document === "undefined") return fallback;
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

/** The fill color for chart marks of a category (unknown categories are "other"). */
export function categoryColor(category: string | null | undefined): string {
  return token(`--cat-${asCategory(category)}`, "#64748b");
}

/** The text color for a category: readable on cards, unlike the fill. */
export function categoryInk(category: string | null | undefined): string {
  return token(`--cat-${asCategory(category)}-ink`, "#475569");
}

/** itemStyle for a mark of this category: its color (a token, resolved by useEChart) and its pattern. */
export function categoryStyle(category: string | null | undefined): Record<string, unknown> {
  const name = asCategory(category);
  return { color: `var(--cat-${name})`, decal: CATEGORY_DECALS[name] };
}

const TOKEN_REFERENCE = /^var\((--[\w-]+)\)$/;

/** The option with every "var(--token)" string replaced by the token's current value. */
export function resolveTokens<T>(value: T): T {
  if (typeof value === "string") {
    const match = TOKEN_REFERENCE.exec(value);
    return (match ? token(match[1], value) : value) as T;
  }
  if (Array.isArray(value)) return value.map((item) => resolveTokens(item)) as T;
  if (value && typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype) {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, resolveTokens(item)])) as T;
  }
  return value; // functions (formatters), dates, numbers
}

/** The ECharts theme for the current color scheme. */
export function chartTheme(): Record<string, unknown> {
  const text = token("--text", "#111827");
  const muted = token("--muted", "#4b5563");
  const border = token("--border", "#e3e6f0");
  const surface = token("--surface", "#ffffff");
  const font = token("--font", "system-ui, sans-serif");
  const axis = {
    axisLine: { lineStyle: { color: border } },
    axisTick: { show: false },
    axisLabel: { color: muted, fontFamily: font },
    splitLine: { show: false },
  };
  return {
    color: CATEGORIES.map((category) => categoryColor(category)),
    backgroundColor: "transparent",
    textStyle: { fontFamily: font, color: text },
    legend: { textStyle: { color: muted, fontFamily: font }, icon: "roundRect", itemWidth: 12, itemHeight: 12 },
    tooltip: {
      backgroundColor: surface,
      borderColor: border,
      borderWidth: 1,
      padding: [8, 12],
      textStyle: { color: text, fontFamily: font },
      extraCssText: "border-radius: 12px; box-shadow: 0 8px 24px -12px rgba(0,0,0,0.35);",
    },
    grid: { left: 8, right: 16, top: 24, bottom: 8, containLabel: true },
    categoryAxis: axis,
    timeAxis: axis,
    valueAxis: { ...axis, axisLine: { show: false }, splitLine: { show: true, lineStyle: { color: border, type: "dashed" } } },
    bar: { itemStyle: { borderRadius: [6, 6, 0, 0] }, barMaxWidth: 36 },
    line: { smooth: true, symbolSize: 7, lineStyle: { width: 3 } },
    pie: { itemStyle: { borderColor: surface, borderWidth: 2 } },
    animationDuration: 700,
    animationEasing: "cubicOut",
    animationDurationUpdate: 450,
    animationEasingUpdate: "cubicInOut",
  };
}

const DARK = "(prefers-color-scheme: dark)";

/** The scheme charts should use: a forced data-theme wins over the system. */
function useScheme(): "light" | "dark" {
  const systemDark = useMediaQuery(DARK);
  const forced = typeof document !== "undefined" ? document.documentElement.dataset.theme : undefined;
  return forced === "dark" || forced === "light" ? forced : systemDark ? "dark" : "light";
}

export type ChartSettings = {
  /** Update in place instead of replacing the chart: only what changed animates (new blocks slide in, bars
   * re-sort), and the user's zoom and pan stay. For charts that refresh live. Series need stable ids. */
  merge?: boolean;
  /** Chart events to listen to ("datazoom": the user zoomed or panned). The latest handlers are always used. */
  events?: Record<string, (params: unknown) => void>;
};

/** A ref for a chart's <div>. `label` names the chart for screen readers; `option` may be null while loading, or
 * a function (memoized) that builds it at the moment it is applied, so it can read state kept in refs (a zoom). */
export function useEChart(
  option: ChartOption | (() => ChartOption) | null,
  label: string,
  settings: ChartSettings = {},
): (box: HTMLDivElement | null) => void {
  const merge = settings.merge ?? false;
  const events = useRef(settings.events);
  events.current = settings.events;
  const eventNames = Object.keys(settings.events ?? {}).sort().join(",");
  // A callback ref: the chart is made when the div mounts, whenever that is, and remade if it is replaced.
  const [box, setBox] = useState<HTMLDivElement | null>(null);
  const [chart, setChart] = useState<echarts.ECharts | null>(null);
  const scheme = useScheme();
  const reduced = useReducedMotionPreference();

  useEffect(() => {
    if (!box) return;
    const name = `daytrace-${scheme}`;
    echarts.registerTheme(name, chartTheme()); // read again: the tokens differ per scheme
    const instance = echarts.init(box, name, { renderer: "canvas" });
    for (const event of eventNames ? eventNames.split(",") : []) {
      instance.on(event, (params: unknown) => events.current?.[event]?.(params));
    }
    setChart(instance);
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(box);
    return () => {
      observer.disconnect();
      instance.dispose();
      setChart(null);
    };
  }, [box, scheme, eventNames]);

  useEffect(() => {
    if (!box || !chart || !option) return;
    // A name even when ECharts adds no description (no series), replacing any stale one; with data, ECharts'
    // description (which starts with this label) takes its place.
    box.setAttribute("role", "img");
    box.setAttribute("aria-label", label);
    const built = typeof option === "function" ? option() : option;
    const aria = (built.aria as Record<string, unknown> | undefined) ?? {};
    chart.setOption(
      resolveTokens({
        ...built,
        // Set after the page's option, so no page can switch these off.
        animation: reduced ? false : (built.animation ?? true),
        aria: { ...aria, enabled: true, label: { general: { withoutTitle: `${label}. ` } }, decal: { show: true } },
      }),
      merge ? { replaceMerge: ["series"] } : { notMerge: true },
    );
  }, [box, chart, option, label, reduced, scheme, merge]);

  return useCallback((element: HTMLDivElement | null) => setBox(element), []);
}
