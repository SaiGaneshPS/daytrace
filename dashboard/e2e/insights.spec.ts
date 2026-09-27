// DT-34: the Insights page with the hub mocked by the hub's own answers for 14 seeded days (e2e/fixtures, made by the
// hub's code): every Overview chart and how fast it shows, the totals agreeing, the change chips, the range picker
// and the address keeping the view, the best and toughest days, an empty range, a hub that can't answer, and
// accessibility. The clock is fixed at 21:00 on Friday 25 September 2026 in Toronto, as when the fixture was made
// (tests of what happens as time passes run their own clock).
import { readFileSync } from "node:fs";
import { join } from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";
import { DEVICE_COLORS, deviceColors } from "../src/theme/devices";

type Overview = {
  range: { first: string; last: string; days: number; label: string };
  in_progress: boolean;
  metrics: { id: string; value: number | string | null; explain: string }[];
  series: Record<string, { lines?: { values: (number | null)[] }[] | null; items?: { value: number }[] | null; cells?: unknown[] | null }>;
  changes: { id: string; label: string; direction: string; better: string; change_pct: number | null; delta: number; now: number; before: number; unit: string }[];
  meta: { source: string };
};
const FIXTURES = JSON.parse(readFileSync(join(process.cwd(), "e2e", "fixtures", "insights-overview.json"), "utf-8")) as Record<string, Overview>;
const TODAY = "2026-09-25";
const FOURTEEN = "2026-09-12..2026-09-25";

test.use({ timezoneId: "America/Toronto", locale: "en-US" });

const metricOf = (data: Overview, id: string) => data.metrics.find((item) => item.id === id)?.value;
function minutes(value: number): string {
  const whole = Math.round(value);
  const hours = Math.floor(whole / 60);
  const rest = whole % 60;
  return !hours ? `${rest}m` : rest ? `${hours}h ${rest}m` : `${hours}h`;
}
function shift(day: string, by: number): string {
  const [year, month, date] = day.split("-").map(Number);
  const moved = new Date(Date.UTC(year, month - 1, date + by));
  return moved.toISOString().slice(0, 10);
}

/** The 14-day answer, relabelled for another range (the charts don't change; the range the page checks does). */
function answerFor(range: string): Overview {
  if (range === "7d" || range === "today") return FIXTURES[range];
  if (range === FOURTEEN) return FIXTURES["14d"];
  const custom = /^(.+)\.\.(.+)$/.exec(range);
  const counted = /^(\d+)d$/.exec(range);
  const first = custom ? custom[1] : shift(TODAY, 1 - Number(counted?.[1] ?? 1));
  const last = custom ? custom[2] : TODAY;
  const days = Math.round((Date.parse(last) - Date.parse(first)) / 86_400_000) + 1;
  const label = custom ? `${first} to ${last}` : `Last ${days} days`;
  return { ...FIXTURES["14d"], in_progress: last === TODAY, range: { first, last, days, label } };
}

async function mockHub(page: Page, answer: (range: string) => object | "fail" = answerFor, { fixedClock = true } = {}) {
  const asked: string[] = [];
  if (fixedClock) await page.clock.setFixedTime(new Date("2026-09-25T21:00:00-04:00"));
  await page.route("**/api/**", (route) => route.fulfill({ status: 404, json: { error: { code: "not_found", message: "Not mocked" } } }));
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", profile: "demo", version: "0.1.0", local: true } }));
  await page.route("**/api/v1/insights/overview?**", (route) => {
    const range = new URL(route.request().url()).searchParams.get("range") ?? "";
    asked.push(range);
    const found = answer(range);
    return found === "fail"
      ? route.fulfill({ status: 500, json: { error: { code: "internal_error", message: "The hub had a problem", details: [] } } })
      : route.fulfill({ json: found });
  });
  return asked;
}

/** Whether all four Overview charts have drawn: each has a canvas with something painted on it (a chart may use
 * more than one canvas, and makes them before it draws). */
const chartsDrawn = () =>
  [...document.querySelectorAll(".chart")].filter((chart) =>
    [...chart.querySelectorAll("canvas")].some((canvas) => {
      if (!canvas.width || !canvas.height) return false;
      const pixels = canvas.getContext("2d")?.getImageData(0, 0, canvas.width, canvas.height).data ?? [];
      for (let alpha = 3; alpha < pixels.length; alpha += 4 * 37) if (pixels[alpha]) return true;
      return false;
    }),
  ).length >= 4;

async function settled(page: Page) {
  await page.evaluate(() =>
    Promise.all(document.getAnimations().filter((a) => a.effect?.getTiming().iterations !== Infinity).map((a) => a.finished.catch(() => undefined))),
  );
}

test("every Overview chart draws 14 seeded days within 1.5 s of the hub's answer", async ({ page }) => {
  await mockHub(page);
  // Timed from the answer arriving, not from the page starting to load (a slow machine's loading isn't the charts').
  let answeredAt: number | null = null;
  page.on("response", (response) => {
    if (response.url().includes("/api/v1/insights/overview")) answeredAt = performance.now();
  });
  await page.goto(`/insights?range=${FOURTEEN}`);
  await page.waitForFunction(chartsDrawn);
  expect(answeredAt).not.toBeNull();
  expect(performance.now() - (answeredAt ?? 0)).toBeLessThan(1500);
  for (const title of ["Screen time by device", "By category", "Phone and computer", "When screens were on"]) {
    await expect(page.getByRole("region", { name: title }).locator(".chart canvas").first()).toBeVisible();
  }
  await expect(page.getByRole("region", { name: "By category" }).locator(".chart-card-source")).toHaveText("From demo data");
  await expect(page.getByRole("region", { name: "Screen time by device" }).locator(".chart-card-meta")).toContainText("Last 14 days, so far");
});

test("the numbers are the hub's, and every total agrees", async ({ page }) => {
  await mockHub(page);
  await page.goto("/insights");
  const data = FIXTURES["7d"];
  const total = minutes(Number(metricOf(data, "screen_time")));
  await expect(page.getByRole("region", { name: "Screen time", exact: true }).locator(".stat-value")).toContainText(total);
  await expect(page.getByRole("region", { name: "By category" }).locator(".donut-total strong")).toHaveText(total);
  await expect(page.getByRole("region", { name: "Screen time by device" }).locator(".chart-total")).toHaveText(`${total} in all, each device counted.`);
  // The donut and the stacked days are cut from the same minutes: they add up to the same total (each part is
  // rounded to a hundredth on its own).
  const donut = (data.series.categories.items ?? []).reduce((sum, item) => sum + item.value, 0);
  const stacked = (data.series.screen_by_device.lines ?? []).flatMap((line) => line.values).reduce<number>((sum, value) => sum + (value ?? 0), 0);
  const parts = (data.series.categories.items ?? []).length + (data.series.screen_by_device.lines ?? []).flatMap((line) => line.values).length;
  expect(Math.abs(donut - stacked)).toBeLessThanOrEqual(0.005 * parts);
  expect(Math.abs(donut - Number(metricOf(data, "screen_time")))).toBeLessThanOrEqual(0.005 * parts);
  await expect(page.getByRole("region", { name: "Focus score", exact: true })).toContainText(`${metricOf(data, "focus_score")}/ 100`);
});

test("each change says which way it went, and whether that is good", async ({ page }) => {
  await mockHub(page);
  await page.goto("/insights?range=7d");
  const labels: Record<string, string> = {
    daily_average: "Screen time", focused_time: "Focused time", focus_score: "Focus score", pickups: "Pickups a day",
    sleep: "Sleep a night", late_night: "After 11 pm, a night",
  };
  const changes = FIXTURES["7d"].changes;
  expect(changes).toHaveLength(6);
  for (const change of changes) {
    const chip = page.getByRole("region", { name: labels[change.id], exact: true }).locator(".change");
    if (change.direction === "same") {
      await expect(chip).toHaveClass(/change-same/);
      continue;
    }
    const amount = change.unit === "score" ? `${Math.round(Math.abs(change.delta))} points` : `${Math.abs(change.change_pct ?? 0)}%`;
    await expect(chip).toContainText(`${change.direction} ${amount}`);
    await expect(chip).toHaveClass(change.direction === change.better ? /change-good/ : /change-bad/);
    await expect(chip).toContainText(change.direction === change.better ? "a good change" : "a change the wrong way"); // for screen readers
  }
  // Its title has both day averages (not "9h 25m in the 7 days before", which reads as that week's total).
  const screen = changes.find((change) => change.id === "daily_average");
  await expect(page.getByRole("region", { name: "Screen time", exact: true }).locator(".change")).toHaveAttribute(
    "title",
    `Screen time a day: ${minutes(screen?.now ?? 0)} against ${minutes(screen?.before ?? 0)} in the 7 days before`,
  );
});

test("a change from nothing is in its own unit, and a half point shows as one", async ({ page }) => {
  await mockHub(page, (range) => {
    const base = answerFor(range);
    const changes = base.changes.map((change) =>
      change.id === "pickups"
        ? { ...change, now: 5, before: 0, delta: 5, change_pct: null, direction: "up" } // no pickups at all before
        : change.id === "focus_score"
          ? { ...change, now: 64, before: 64.5, delta: -0.5, change_pct: -1, direction: "down" } // -0.503, sent as -0.5
          : change,
    );
    return { ...base, changes };
  });
  await page.goto("/insights?range=7d");
  const pickups = page.getByRole("region", { name: "Pickups a day" }).locator(".change");
  await expect(pickups).toContainText("up 5 pickups");
  await expect(pickups).toHaveAttribute("title", "Phone pickups a day: 5 pickups against 0 pickups in the 7 days before");
  await expect(page.getByRole("region", { name: "Focus score", exact: true }).locator(".change")).toContainText("down 1 point on");
});

test("devices get their kind's color, and two of a kind never share one", () => {
  expect(deviceColors(["android", "windows", "macos", "ios"])).toEqual([DEVICE_COLORS.android, DEVICE_COLORS.windows, DEVICE_COLORS.macos, DEVICE_COLORS.ios]);
  const two = deviceColors(["android", "android", "windows", "windows"]);
  expect(new Set(two).size).toBe(4);
  expect([two[0], two[2]]).toEqual([DEVICE_COLORS.android, DEVICE_COLORS.windows]);
  // A second phone, or a browser, never takes the color of a kind that is in the chart.
  const mixed = deviceColors(["browser", "android", "android", "windows"]);
  expect([mixed[1], mixed[3]]).toEqual([DEVICE_COLORS.android, DEVICE_COLORS.windows]);
  expect([mixed[0], mixed[2]]).not.toContain(DEVICE_COLORS.windows);
  expect(new Set(mixed).size).toBe(4);
  expect(new Set(deviceColors(Array(12).fill("android"))).size).toBe(8); // more devices than colors: all are used
});

test("the range picker, the tabs and the address keep the same view", async ({ page }) => {
  const asked = await mockHub(page);
  await page.goto("/insights");
  const picker = page.getByRole("group", { name: "Time range" });
  await expect(picker.getByRole("button", { name: "7 days" })).toHaveAttribute("aria-pressed", "true");
  await picker.getByRole("button", { name: "30 days" }).click();
  await expect(page).toHaveURL(/range=30d/);
  await expect(page.getByRole("region", { name: "By category" })).toContainText("Last 30 days");
  expect(asked).toContain("30d");
  await page.getByRole("tab", { name: "Focus and Sleep" }).click();
  await expect(page).toHaveURL(/tab=focus/);
  await expect(page).toHaveURL(/range=30d/); // switching tabs keeps the range
  await page.reload();
  await expect(page.getByRole("tab", { name: "Focus and Sleep" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("group", { name: "Time range" }).getByRole("button", { name: "30 days" })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("tab", { name: "Overview" }).click();
  await expect(page.getByRole("region", { name: "By category" }).locator(".chart canvas").first()).toBeVisible();

  await page.getByRole("group", { name: "Time range" }).getByRole("button", { name: "Today" }).click();
  await page.getByRole("group", { name: "Time range" }).getByRole("button", { name: "Custom" }).click();
  const form = page.getByRole("form", { name: "Custom range" });
  const from = form.getByLabel("From", { exact: true });
  const to = form.getByLabel("To", { exact: true });
  await expect(from).toHaveValue(TODAY); // the fields start from the range shown now (today), not the one at load
  await expect(to).toHaveValue(TODAY);
  // The status line is there before any problem, so a screen reader hears the problem when it comes.
  await expect(form.getByRole("status")).toHaveText("");
  await from.fill("2026-09-01");
  await to.fill("2026-08-30");
  await expect(form.getByRole("status")).toHaveText("The last day is before the first.");
  await expect(form.getByRole("button", { name: "Show" })).toBeDisabled();
  await expect(form.getByRole("button", { name: "Show" })).toHaveCSS("opacity", "0.55"); // and looks it
  await expect(form.getByRole("button", { name: "Show" })).toHaveAccessibleDescription("The last day is before the first.");
  await to.fill("2026-09-10");
  await expect(form.getByRole("status")).toHaveText("");
  await form.getByRole("button", { name: "Show" }).click();
  await expect(page).toHaveURL(/range=2026-09-01\.\.2026-09-10/);
  await expect(page.getByRole("region", { name: "By category" })).toContainText("Sep 1 to Sep 10");
  await page.goBack(); // Back to today: the custom fields close
  await expect(page).toHaveURL(/range=today/);
  await expect(page.getByRole("group", { name: "Time range" }).getByRole("button", { name: "Today" })).toHaveAttribute("aria-pressed", "true");
  await expect(form).toBeHidden();

  await page.goto("/insights?range=2026-01-01..2026-09-25&tab=nonsense"); // too long, and no such tab
  await expect(page.getByRole("group", { name: "Time range" }).getByRole("button", { name: "7 days" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  for (const unreal of ["2026-02-28..2026-02-30", "1969-12-30..1970-01-02"]) {
    await page.goto(`/insights?range=${unreal}`); // a day that doesn't exist, and one before the hub's first
    await expect(page.getByRole("group", { name: "Time range" }).getByRole("button", { name: "7 days" })).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByRole("region", { name: "By category" }).locator(".chart canvas").first()).toBeVisible();
    expect(asked).not.toContain(unreal);
  }
});

test("at midnight the days move on by themselves", async ({ page }) => {
  let answers = 0;
  await mockHub(
    page,
    (range) => {
      if (range !== "7d") return answerFor(range);
      answers += 1;
      if (answers === 1) return FIXTURES["7d"];
      const metrics = FIXTURES["7d"].metrics.map((item) => (item.id === "screen_time" ? { ...item, value: 100 } : item));
      return { ...FIXTURES["7d"], metrics, range: { ...FIXTURES["7d"].range, first: "2026-09-20", last: "2026-09-26" } };
    },
    { fixedClock: false },
  );
  await page.clock.install({ time: new Date("2026-09-25T23:59:45-04:00") });
  await page.goto("/insights?range=7d");
  const screen = page.getByRole("region", { name: "Screen time", exact: true });
  await expect(screen.locator(".stat-value")).toContainText(minutes(Number(metricOf(FIXTURES["7d"], "screen_time"))));
  await page.clock.runFor(35_000); // past midnight, and not yet a minute
  await expect(screen.locator(".stat-value")).toContainText("1h 40m"); // 20 to 26 September, asked for again
  expect(answers).toBe(2);
});

test("a range with today in it is asked for again every minute, and one that is over isn't", async ({ page }) => {
  const asked = await mockHub(page, answerFor, { fixedClock: false });
  await page.clock.install({ time: new Date("2026-09-25T14:00:00-04:00") });
  await page.goto("/insights?range=7d");
  await expect(page.getByRole("region", { name: "Screen time", exact: true }).locator(".stat-value")).toBeVisible();
  await page.clock.runFor(61_000);
  await expect.poll(() => asked.filter((range) => range === "7d").length).toBe(2);
  const past = "2026-09-01..2026-09-10";
  await page.goto(`/insights?range=${past}`);
  await expect(page.getByRole("region", { name: "Screen time", exact: true }).locator(".stat-value")).toBeVisible();
  await page.clock.runFor(125_000);
  expect(asked.filter((range) => range === past)).toHaveLength(1);
});

test("the best and toughest days say why", async ({ page }) => {
  await mockHub(page);
  await page.goto("/insights?range=7d");
  const data = FIXTURES["7d"];
  const longDay = (day: string) => new Date(`${day}T12:00:00`).toLocaleDateString("en-US", { weekday: "long", day: "numeric", month: "long" });
  const best = page.getByRole("region", { name: "Best day" });
  await expect(best).toContainText(longDay(String(metricOf(data, "best_day"))));
  await expect(best).toContainText(`The most focused time: ${minutes(Number(metricOf(data, "best_day_focused")))}`);
  const toughest = page.getByRole("region", { name: "Toughest day" });
  await expect(toughest).toContainText(longDay(String(metricOf(data, "toughest_day"))));
  await expect(toughest).toContainText(`The most screen time after 11 pm: ${minutes(Number(metricOf(data, "toughest_day_late")))}`);
});

test("a range with nothing recorded, and a hub that can't answer", async ({ page }) => {
  const empty = (range: string): Overview => {
    const base = answerFor(range);
    return {
      ...base, changes: [],
      metrics: base.metrics.map((item) => ({ ...item, value: null })),
      series: Object.fromEntries(Object.entries(base.series).map(([key, series]) => [key, {
        ...series, items: series.items ? [] : series.items, cells: series.cells ? [] : series.cells,
        lines: series.lines?.map((line) => ({ ...line, values: line.values.map(() => null) })) ?? series.lines,
      }])),
    };
  };
  await mockHub(page, empty);
  await page.goto("/insights?range=7d");
  await expect(page.getByRole("region", { name: "Screen time", exact: true })).toContainText("No data yet");
  await expect(page.getByRole("region", { name: "By category" })).toContainText("No screen time in this range.");
  await expect(page.locator(".change")).toHaveCount(0);

  await page.unrouteAll({ behavior: "ignoreErrors" });
  await mockHub(page, () => "fail");
  await page.goto("/insights?range=30d");
  await expect(page.getByText("The overview couldn't load: The hub had a problem")).toBeVisible();
  await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
});

for (const scheme of ["light", "dark"] as const) {
  test(`the Insights page passes axe (${scheme})`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await mockHub(page);
    await page.goto("/insights?range=7d");
    await page.waitForFunction(chartsDrawn);
    await settled(page);
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations.map((violation) => `${violation.id}: ${violation.nodes.map((node) => node.target).join(", ")}`)).toEqual([]);
  });
}
