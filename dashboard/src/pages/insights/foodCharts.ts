// DT-57: the Food and Calendar tab's chart options, as plain functions of the hub's series (the e2e tests check them
// directly: what a legend shows, what a tooltip says, which bars are unknown rather than 0).
import type { components } from "../../api/schema";
import { hourOfDay } from "./clock";

type Series = components["schemas"]["Series"];
type Option = Record<string, unknown>;
/** How days, minutes and text are written (the page's own helpers). */
export type Format = { day: (iso: string) => string; minutes: (value: number) => string; escape: (text: string) => string };

/** Each meal type's color and mark: never color alone, in the chart and in its legend. */
export const MEALS: Record<string, { label: string; color: string; symbol: string }> = {
  breakfast: { label: "Breakfast", color: "var(--cat-games)", symbol: "circle" },
  lunch: { label: "Lunch", color: "var(--cat-comms)", symbol: "rect" },
  dinner: { label: "Dinner", color: "var(--cat-video)", symbol: "triangle" },
  snack: { label: "Snack", color: "var(--cat-social)", symbol: "diamond" },
  other: { label: "Other", color: "var(--cat-other)", symbol: "pin" },
};

/** What an event's time went to. "No screen" and "unknown" are light fills, so each has an outline (3:1 against
 * the card) and a pattern. */
export const PARTS: Record<string, { color: string; decal: Record<string, unknown>; borderColor?: string }> = {
  on_plan: { color: "var(--cat-study)", decal: { symbol: "none" } },
  off_plan: { color: "var(--cat-social)", decal: { symbol: "rect", symbolSize: 1, dashArrayX: [1, 0], dashArrayY: [3, 4], rotation: Math.PI / 4, color: "rgba(255,255,255,0.55)" } },
  other: { color: "var(--cat-comms)", decal: { symbol: "circle", symbolSize: 0.6, dashArrayX: [[6, 6], [0, 6, 6, 0]], dashArrayY: [5, 0], color: "rgba(255,255,255,0.55)" } },
  idle: { color: "var(--surface-2)", borderColor: "var(--muted)", decal: { symbol: "none" } },
  unknown: { color: "var(--surface)", borderColor: "var(--muted)", decal: { symbol: "rect", symbolSize: 1, dashArrayX: [1, 0], dashArrayY: [2, 5], rotation: -Math.PI / 4, color: "rgba(100,116,139,0.6)" } },
};
export const UNKNOWN = "No screen data that day";
const DEFAULT_PARTS = [
  { key: "on_plan", name: "On plan" },
  { key: "off_plan", name: "Off plan" },
  { key: "other", name: "Other screen time" },
  { key: "idle", name: "No screen" },
];

/** The hub's late-night window for meals (hours of the day), from the chart's series. */
export function lateWindow(series: Series | undefined): { from: number; until: number } | null {
  const from = series?.stats?.late_from;
  const until = series?.stats?.late_until;
  return typeof from === "number" && typeof until === "number" ? { from, until } : null;
}

const partStyle = (key: string) => {
  const part = PARTS[key] ?? PARTS.unknown;
  return { color: part.color, decal: part.decal, ...(part.borderColor ? { borderColor: part.borderColor, borderWidth: 1 } : {}) };
};

/** An axis tooltip listing only the parts a bar has (an unknown event says so once, never "0m on plan"). */
function partsTooltip(format: Format) {
  return (params: { seriesName: string; value: number | null; axisValueLabel: string; marker: string }[]) => {
    const shown = params.filter((part) => part.value !== null && part.value !== undefined && !Number.isNaN(part.value));
    const lines = shown.map((part) => `${part.marker}${format.escape(part.seriesName)}: ${format.minutes(Number(part.value))}`);
    return [format.escape(params[0]?.axisValueLabel ?? ""), ...lines].join("<br>");
  };
}

export function mealTimesOption(series: Series, format: Format): Option {
  const days = (series.x ?? []).map(format.day);
  const points = series.points ?? [];
  const groups = Object.keys(MEALS).filter((group) => points.some((point) => (point.group ?? "other") === group));
  const late = lateWindow(series);
  return {
    tooltip: {
      trigger: "item",
      formatter: (params: { seriesName: string; value: [number, number]; name: string }) =>
        format.escape(`${params.seriesName}, ${days[params.value[0]]} at ${hourOfDay(params.value[1])}: ${params.name}`),
    },
    // Each type with its own mark in the legend too (the theme's rounded squares would leave only color), scrolling
    // rather than wrapping over the axis on a narrow screen.
    legend: { bottom: 0, type: "scroll", data: groups.map((group) => ({ name: MEALS[group].label, icon: MEALS[group].symbol })) },
    grid: { left: 8, right: 16, top: 16, bottom: 40, containLabel: true },
    xAxis: { type: "category", data: days },
    yAxis: { type: "value", min: 0, max: 24, interval: 4, inverse: true, axisLabel: { formatter: (value: number) => hourOfDay(value) } },
    series: [
      ...groups.map((group) => ({
        id: group,
        name: MEALS[group].label,
        type: "scatter",
        symbol: MEALS[group].symbol,
        symbolSize: 12,
        itemStyle: { color: MEALS[group].color },
        data: points.filter((point) => (point.group ?? "other") === group).map((point) => ({ name: point.label, value: [point.x, point.y] })),
      })),
      // The late-night band on a series of its own, outside the legend: hiding a meal type can't hide it.
      ...(late
        ? [{
            id: "late",
            name: "Late at night",
            type: "scatter",
            data: [],
            silent: true,
            markArea: {
              silent: true,
              itemStyle: { color: "var(--surface-2)", opacity: 0.7 },
              label: { show: true, position: "insideTopLeft", color: "var(--muted)", formatter: "Late" },
              data: [
                [{ yAxis: late.from }, { yAxis: 24 }],
                [{ yAxis: 0 }, { yAxis: late.until }],
              ],
            },
          }]
        : []),
    ],
  };
}

export function mealsByDayOption(series: Series, format: Format): Option {
  return {
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: (value: unknown) => (value === null || value === undefined || value === "-" ? "no data" : String(value)) },
    legend: { bottom: 0, type: "scroll", data: (series.lines ?? []).map((line) => ({ name: (MEALS[line.key ?? "other"] ?? MEALS.other).label, icon: (MEALS[line.key ?? "other"] ?? MEALS.other).symbol })) },
    grid: { left: 8, right: 16, top: 16, bottom: 40, containLabel: true },
    xAxis: { type: "category", data: (series.x ?? []).map(format.day) },
    yAxis: { type: "value", minInterval: 1 },
    series: (series.lines ?? []).map((line) => {
      const meal = MEALS[line.key ?? "other"] ?? MEALS.other;
      return { id: line.key ?? line.name, name: meal.label, type: "bar", stack: "meals", itemStyle: { color: meal.color }, data: line.values };
    }),
  };
}

/** Each event as a bar split into the hub's parts (its `children`, in the hub's order and names); an event on a day
 * nobody saw is one "unknown" part, and its other parts are no data, not 0. */
export function eventSplitOption(series: Series, format: Format, wide: boolean): Option {
  const events = [...(series.items ?? [])].reverse(); // the longest at the top
  const names = events.map((event) => `${event.name} (${event.key ? format.day(event.key) : ""})`);
  const parts = events.find((event) => event.children)?.children?.map((child) => ({ key: child.key ?? child.name, name: child.name })) ?? DEFAULT_PARTS;
  const partOf = (children: (typeof events)[number]["children"], key: string) => children?.find((child) => (child.key ?? child.name) === key)?.value ?? 0;
  const bars = [
    ...parts.map((part) => ({
      id: part.key,
      name: part.name,
      type: "bar",
      stack: "event",
      barMaxWidth: 26,
      itemStyle: partStyle(part.key),
      data: events.map((event) => (event.children ? partOf(event.children, part.key) : null)),
    })),
    ...(events.some((event) => !event.children)
      ? [{ id: "unknown", name: UNKNOWN, type: "bar", stack: "event", barMaxWidth: 26, itemStyle: partStyle("unknown"),
           data: events.map((event) => (event.children ? null : event.value)) }]
      : []),
  ];
  return {
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, formatter: partsTooltip(format) },
    legend: { bottom: 0, type: "scroll" },
    grid: { left: 8, right: 16, top: 8, bottom: 40, containLabel: true },
    xAxis: { type: "value", splitNumber: wide ? 5 : 2, axisLabel: { hideOverlap: true, formatter: (value: number) => format.minutes(value) } },
    yAxis: { type: "category", data: names, axisLabel: { width: wide ? 150 : 90, overflow: "truncate" } },
    series: bars,
  };
}

export function planByDayOption(series: Series, format: Format): Option {
  return {
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, formatter: partsTooltip(format) },
    legend: { bottom: 0, type: "scroll" },
    grid: { left: 8, right: 16, top: 16, bottom: 40, containLabel: true },
    xAxis: { type: "category", data: (series.x ?? []).map(format.day) },
    yAxis: { type: "value", axisLabel: { formatter: (value: number) => format.minutes(value) } },
    series: (series.lines ?? []).map((line) => ({
      id: line.key ?? line.name,
      name: line.name,
      type: "bar",
      stack: "plan",
      itemStyle: partStyle(line.key ?? "unknown"),
      // On an unknown day the four parts are null and "No screen data that day" has the planned time.
      data: line.values.map((value) => (line.key === "unknown" && value === 0 ? null : value)),
    })),
  };
}
