// DT-34: the Insights page with the hub mocked by the hub's own answers for 14 seeded days (e2e/fixtures, made by the
// hub's code): every Overview chart and how fast it shows, the totals agreeing, the change chips, the range picker
// and the address keeping the view, the best and toughest days, an empty range, a hub that can't answer, and
// accessibility. The clock is fixed at 21:00 on Friday 25 September 2026 in Toronto, as when the fixture was made.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

type Overview = {
  range: { first: string; last: string; days: number; label: string };
  in_progress: boolean;
  metrics: { id: string; value: number | string | null; explain: string }[];
  series: Record<string, { lines?: { values: (number | null)[] }[] | null; items?: { value: number }[] | null; cells?: unknown[] | null }>;
  changes: { id: string; direction: string; better: string; change_pct: number | null; delta: number; unit: string }[];
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

async function mockHub(page: Page, answer: (range: string) => object | "fail" = answerFor) {
  const asked: string[] = [];
  await page.clock.setFixedTime(new Date("2026-09-25T21:00:00-04:00"));
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

/** Whether all four Overview charts have drawn (a chart may draw on more than one canvas). */
const chartsDrawn = () => [...document.querySelectorAll(".chart")].filter((chart) => chart.querySelector("canvas")).length >= 4;

async function settled(page: Page) {
  await page.evaluate(() =>
    Promise.all(document.getAnimations().filter((a) => a.effect?.getTiming().iterations !== Infinity).map((a) => a.finished.catch(() => undefined))),
  );
}

test("every Overview chart shows 14 seeded days in under 1.5 s", async ({ page }) => {
  await mockHub(page);
  await page.goto(`/insights?range=${FOURTEEN}`);
  await page.waitForFunction(chartsDrawn);
  const shownAfter = await page.evaluate(() => performance.now()); // since the page started loading
  expect(shownAfter).toBeLessThan(1500);
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
    const amount = change.unit === "score" ? `${Math.abs(Math.round(change.delta))} points` : `${Math.abs(change.change_pct ?? 0)}%`;
    await expect(chip).toContainText(`${change.direction} ${amount}`);
    await expect(chip).toHaveClass(change.direction === change.better ? /change-good/ : /change-bad/);
    await expect(chip).toContainText(change.direction === change.better ? "a good change" : "a change the wrong way"); // for screen readers
  }
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

  await page.getByRole("group", { name: "Time range" }).getByRole("button", { name: "Custom" }).click();
  const form = page.getByRole("form", { name: "Custom range" });
  await form.getByLabel("From", { exact: true }).fill("2026-09-01");
  await form.getByLabel("To", { exact: true }).fill("2026-08-30");
  await expect(form.getByRole("status")).toHaveText("The last day is before the first.");
  await expect(form.getByRole("button", { name: "Show" })).toBeDisabled();
  await form.getByLabel("To", { exact: true }).fill("2026-09-10");
  await form.getByRole("button", { name: "Show" }).click();
  await expect(page).toHaveURL(/range=2026-09-01\.\.2026-09-10/);
  await expect(page.getByRole("region", { name: "By category" })).toContainText("Sep 1 to Sep 10");

  await page.goto("/insights?range=2026-01-01..2026-09-25&tab=nonsense"); // too long, and no such tab
  await expect(page.getByRole("group", { name: "Time range" }).getByRole("button", { name: "7 days" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
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
