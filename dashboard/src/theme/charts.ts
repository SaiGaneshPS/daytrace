// DT-52: the chart kit. Every chart uses one ECharts theme built from the CSS tokens (theme/tokens.css), so charts
// and cards always agree, in light and dark:
//
// - the category palette, the app's font, rounded bars, soft dashed grid lines, a card-like tooltip;
// - animated entrances and updates, switched off under reduced motion;
// - never color alone: each category has its own pattern (an ECharts decal), and ECharts' aria module describes
//   the chart to screen readers. Pages still label their marks.
//
// useEChart(option, label) gives a ref for a <div>: it makes the chart, follows the color scheme, resizes with its
// box and cleans up. Only the chart types and components below are bundled.
import { BarChart, CustomChart, LineChart, PieChart, ScatterChart } from "echarts/charts";
import {
  AriaComponent,
  DatasetComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
} from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef } from "react";
import { useMediaQuery, useReducedMotionPreference } from "./motion";

echarts.use([
  BarChart, CustomChart, LineChart, PieChart, ScatterChart,
  AriaComponent, DatasetComponent, GridComponent, LegendComponent, MarkLineComponent, TooltipComponent,
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

/** itemStyle for a mark of this category: its color and its pattern. */
export function categoryStyle(category: string | null | undefined): Record<string, unknown> {
  const name = asCategory(category);
  return { color: categoryColor(name), decal: CATEGORY_DECALS[name] };
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

/** A ref for a chart's <div>. `label` names the chart for screen readers; `option` may be null while loading. */
export function useEChart(option: ChartOption | null, label: string) {
  const element = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const scheme = useScheme();
  const reduced = useReducedMotionPreference();

  useEffect(() => {
    const box = element.current;
    if (!box) return;
    const name = `daytrace-${scheme}`;
    echarts.registerTheme(name, chartTheme()); // read again: the tokens differ per scheme
    const instance = echarts.init(box, name, { renderer: "canvas" });
    chart.current = instance;
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(box);
    return () => {
      observer.disconnect();
      instance.dispose();
      chart.current = null;
    };
  }, [scheme]);

  useEffect(() => {
    if (!chart.current || !option) return;
    chart.current.setOption(
      {
        animation: !reduced,
        // The label names the chart; ECharts then describes its series and values after it.
        aria: { enabled: true, label: { general: { withoutTitle: `${label}. ` } }, decal: { show: true } },
        ...option,
      },
      { notMerge: true },
    );
  }, [option, label, reduced, scheme]);

  return element;
}
