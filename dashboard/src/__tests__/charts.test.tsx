// DT-59: the Insights tabs, rendered from the hub's own answers (the fixtures the hub's tests check against its code),
// draw and say exactly the hub's numbers: each chart's data is the fixture's, its tooltips and labels say the same
// numbers, "Estimated" shows where the hub says so, and the hero numbers equal the charts' totals. useApi is mocked
// with the fixtures, and useEChart keeps each chart's option instead of drawing it.
import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import appsDevices from "../../e2e/fixtures/insights-apps-devices.json";
import foodCalendar from "../../e2e/fixtures/insights-food-calendar.json";
import focusSleep from "../../e2e/fixtures/insights-focus-sleep.json";
import overview from "../../e2e/fixtures/insights-overview.json";
import AppsDevicesTab from "../pages/insights/AppsDevicesTab";
import FocusSleepTab from "../pages/insights/FocusSleepTab";
import FoodCalendarTab from "../pages/insights/FoodCalendarTab";
import OverviewTab from "../pages/insights/OverviewTab";

type Option = Record<string, any>;
const charts = new Map<string, Option>();
let answers: Record<string, unknown> = {};

vi.mock("../api/client", async (importOriginal) => {
  const real = await importOriginal<typeof import("../api/client")>();
  return {
    ...real,
    useApi: (path: string, options: { path?: { tab?: string }; enabled?: boolean }) => {
      const key = path.endsWith("/detail") ? "detail" : (options.path?.tab ?? path);
      const data = options.enabled === false ? undefined : answers[key];
      return { data, error: undefined, current: data !== undefined, loading: false, reload: () => undefined };
    },
    usePolling: () => undefined,
  };
});
vi.mock("../theme/charts", async (importOriginal) => {
  const real = await importOriginal<typeof import("../theme/charts")>();
  return {
    ...real,
    useEChart: (option: Option | (() => Option) | null, label: string) => {
      if (option) charts.set(label, typeof option === "function" ? option() : option);
      return () => undefined;
    },
  };
});

const PROPS = { range: "7d", tz: "America/Toronto", today: "2026-09-25" };
/** Minutes as the page should write them ("2h 35m", "45m"), worked out here rather than with the page's own
 * formatter, so a bug in that formatter can't hide on both sides of a check. */
function formatMinutes(value: number): string {
  const whole = Math.round(value);
  const hours = Math.floor(whole / 60);
  const rest = whole % 60;
  return !hours ? `${rest}m` : rest ? `${hours}h ${rest}m` : `${hours}h`;
}
type Metrics = { metrics: { id: string; value: unknown; estimated?: boolean }[] };
const metric = (data: Metrics, id: string) => data.metrics.find((item) => item.id === id)?.value as number;
/** The chart whose label starts so (labels name what a chart shows). */
function chart(start: string): Option {
  const found = [...charts.entries()].find(([label]) => label.startsWith(start));
  if (!found) throw new Error(`no chart "${start}" among: ${[...charts.keys()].join(" | ")}`);
  return found[1];
}
const sum = (values: (number | null | undefined)[]) => values.reduce<number>((total, value) => total + (value ?? 0), 0);
/** Parts rounded to hundredths on their own add up to a total (rounded too) within half a hundredth each. */
const addsUp = (total: number, parts: (number | null | undefined)[]) => expect(Math.abs(total - sum(parts))).toBeLessThanOrEqual(0.005 * (parts.length + 1) + 1e-9);
const hero = (name: string) => within(screen.getByRole("region", { name })).getByText((_, element) => !!element?.classList.contains("visually-hidden") && !!element.closest(".stat-value")).textContent;

beforeEach(() => {
  charts.clear();
  answers = {};
});

describe("the Overview tab", () => {
  const data = overview["7d"];

  it("draws the hub's minutes, and the hero total is every chart's total", () => {
    answers = { overview: data };
    render(<OverviewTab {...PROPS} />);
    const total = metric(data, "screen_time");
    expect(hero("Screen time")).toBe(formatMinutes(total));
    expect(hero("Screen time")).toBe("63h 38m"); // 3818.32 minutes, written out by hand
    const donut = chart("By category");
    expect(donut.series[0].data.map((item: Option) => [item.name, item.value])).toEqual(data.series.categories.items.map((item) => [item.name, item.value]));
    addsUp(total, donut.series[0].data.map((item: Option) => item.value));
    const [first] = data.series.categories.items;
    expect(donut.tooltip.formatter({ name: first.name, value: first.value, percent: 45.5 })).toBe(`${first.name}: ${formatMinutes(first.value)} (45.5%)`);
    const stacked = chart("Screen time by device");
    expect(stacked.series.map((series: Option) => series.data)).toEqual(data.series.screen_by_device.lines?.map((line) => line.values));
    addsUp(total, stacked.series.flatMap((series: Option) => series.data));
    const hours = chart("When screens were on");
    expect(hours.series[0].data).toEqual(data.series.hours.cells?.map((cell) => [cell.x, cell.y, cell.value]));
    addsUp(total, hours.series[0].data.map((cell: number[]) => cell[2]));
    expect(chart("Phone and computer").series.map((series: Option) => series.data)).toEqual(data.series.phone_vs_computer.lines?.map((line) => line.values));
    expect(screen.getByText(`${formatMinutes(total)} in all, each device counted.`)).toBeTruthy();
  });

  it("shows Estimated where the hub says a number was inferred", () => {
    const estimated = {
      ...data,
      metrics: data.metrics.map((item) => (item.id === "sleep" ? { ...item, estimated: true } : item)),
      series: { ...data.series, categories: { ...data.series.categories, estimated: true } },
    };
    answers = { overview: estimated };
    render(<OverviewTab {...PROPS} />);
    expect(within(screen.getByRole("region", { name: "By category" })).getByText("Estimated", { selector: ".badge" })).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "Sleep a night" })).getByText("Estimated")).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "Screen time by device" })).queryByText("Estimated", { selector: ".badge" })).toBeNull();
  });
});

describe("the Apps and Devices tab", () => {
  it("lists the leaderboard and draws the treemap and devices as the hub has them", () => {
    const apps = appsDevices.apps["7d"];
    const devices = appsDevices.devices["7d"];
    answers = { apps, devices };
    render(<AppsDevicesTab {...PROPS} app={null} onApp={() => undefined} />);
    const rows = within(screen.getByRole("region", { name: "Top apps and sites" })).getAllByRole("listitem");
    const leaders = apps.series.leaderboard.items ?? [];
    expect(rows.map((row) => within(row).getByText((_, element) => !!element?.classList.contains("leader-app")).textContent)).toEqual(leaders.map((item) => item.name));
    expect(rows.map((row) => row.querySelector(".leader-time")?.textContent)).toEqual(leaders.map((item) => formatMinutes(item.value)));
    expect(hero("Most used")).toBe(formatMinutes(leaders[0].value)); // the hero is the leaderboard's first
    const treemap = chart("Categories and their apps");
    expect(treemap.series[0].data.map((group: Option) => [group.name, group.value, group.children.map((child: Option) => child.value)])).toEqual(
      (apps.series.treemap.items ?? []).map((group) => [group.name, group.value, (group.children ?? []).map((child) => child.value)]),
    );
    expect(chart("Each device by day").series.map((series: Option) => series.data)).toEqual(devices.series.by_day.lines?.map((line) => line.values));
    expect(hero("Device switches")).toBe(String(metric(devices, "handoffs")));
    expect(sum((devices.series.handoffs.links ?? []).map((link) => link.value))).toBeLessThanOrEqual(metric(devices, "handoffs")); // the 12 most common
  });
});

describe("the Focus and Sleep tab", () => {
  it("draws the score, the nights and the late-night pattern with its line", () => {
    const focus = focusSleep.focus["7d"];
    const sleep = focusSleep.sleep["7d"];
    answers = { focus, sleep };
    render(<FocusSleepTab {...PROPS} />);
    expect(chart("Focus score").series[0].data[0].value).toBe(focus.series.score.value);
    expect(hero("Focus score, on average")).toBe(String(metric(focus, "focus_score"))); // the hero is the gauge's number
    const scatter = focus.series.late_vs_focus;
    const late = chart("Late nights and the next day's focus");
    expect(late.series[0].data.map((point: Option) => point.value)).toEqual(scatter.points?.map((point) => [point.x, point.y]));
    const xs = (scatter.points ?? []).map((point) => point.x);
    const line = late.series[1].data as number[][];
    expect(line[0][0]).toBe(Math.min(...xs));
    expect(line[0][1]).toBeCloseTo(Number(scatter.stats?.intercept) + Number(scatter.stats?.slope) * Math.min(...xs), 1);
    expect(screen.getByText(`${scatter.stats?.n} nights`)).toBeTruthy();
    const bars = chart("Sleep each night");
    expect(bars.series.map((series: Option) => series.data)).toEqual(sleep.series.sleep_by_night.lines?.map((line) => line.values));
    const focused = focus.series.focus_by_day.lines?.find((line) => line.key === "focused")?.values ?? [];
    expect(document.querySelectorAll(".cal-grid .cal-day")).toHaveLength(focused.length);
  });
});

describe("the Food and Calendar tab", () => {
  it("draws each meal as logged and each event split as the hub splits it", () => {
    const food = foodCalendar.food["7d"];
    const calendar = foodCalendar.calendar["7d"];
    answers = { food, calendar };
    render(<FoodCalendarTab {...PROPS} />);
    const meals = chart("When you ate");
    const drawn = meals.series.filter((series: Option) => series.id !== "late").flatMap((series: Option) => series.data.map((point: Option) => [point.name, ...point.value]));
    expect(drawn.sort()).toEqual((food.series.meal_times.points ?? []).map((point) => [point.label, point.x, point.y]).sort());
    expect(hero("Meals logged")).toBe(String(metric(food, "meals")));
    expect(drawn).toHaveLength(metric(food, "meals")); // one mark per meal logged
    const events = chart("Longest events");
    const items = [...(calendar.series.blocks.items ?? [])].reverse();
    for (const series of events.series.filter((series: Option) => series.id !== "unknown")) {
      expect(series.data).toEqual(items.map((item) => item.children?.find((child) => child.key === series.id)?.value ?? null));
    }
    for (const [index, item] of items.entries()) addsUp(item.value, events.series.map((series: Option) => series.data[index]));
    expect(chart("Spent as planned").series[0].data[0].value).toBe(metric(calendar, "on_plan"));
    const foods = within(screen.getByRole("region", { name: "Most logged foods" })).getAllByRole("listitem");
    expect(foods.map((row) => row.textContent)).toEqual((food.series.top_items.items ?? []).map((item) => `${item.name}${item.value === 1 ? "once" : `${item.value} times`}`));
  });
});
