// DT-56: the Insights Focus and Sleep tab, with the hub mocked by its own answers for 14 seeded days
// (e2e/fixtures, made and checked by the hub's tests): every chart and how fast it draws, the focus calendar
// (days with no data shown as such), the gauge's formula, estimated sleep always marked, the late-night pattern with
// its size, strength and "correlation, not cause", errors, and accessibility. The clock is fixed at 21:00 on
// Friday 25 September 2026 in Toronto, as when the fixtures were made.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

type Line = { name: string; category?: string | null; values: (number | null)[] };
type Tab = {
  metrics: { id: string; label: string; value: number | string | null; estimated?: boolean }[];
  series: Record<string, { x?: string[] | null; lines?: Line[] | null; estimated?: boolean; explain?: string; points?: unknown[] | null;
    stats?: Record<string, number | null> | null; note?: string | null }>;
};
const FIXTURES = JSON.parse(readFileSync(join(process.cwd(), "e2e", "fixtures", "insights-focus-sleep.json"), "utf-8")) as { focus: Record<string, Tab>; sleep: Record<string, Tab> };

// The page clock is fixed, and charts animate by it: with reduced motion every chart draws its last frame at once.
test.use({ timezoneId: "America/Toronto", locale: "en-US", reducedMotion: "reduce" });

const metricOf = (data: Tab, id: string) => data.metrics.find((item) => item.id === id)?.value;
function minutes(value: number): string {
  const whole = Math.round(value);
  const hours = Math.floor(whole / 60);
  const rest = whole % 60;
  return !hours ? `${rest}m` : rest ? `${hours}h ${rest}m` : `${hours}h`;
}
/** A day as the page writes it (the browser's language data decides "Sat 19" or "19 Sat"). */
function dayIn(page: Page, day: string, style: "short" | "long"): Promise<string> {
  return page.evaluate(
    ([iso, kind]) => {
      const [year, month, date] = iso.split("-").map(Number);
      const options: Intl.DateTimeFormatOptions = kind === "short" ? { weekday: "short", day: "numeric" } : { weekday: "long", day: "numeric", month: "long" };
      return new Date(year, month - 1, date).toLocaleDateString([], options);
    },
    [day, style] as const,
  );
}

type Answers = { focus?: (range: string) => object | "fail"; sleep?: (range: string) => object | "fail" };

async function mockHub(page: Page, answers: Answers = {}) {
  const asked: string[] = [];
  const reply = (found: object | "fail") =>
    found === "fail" ? { status: 500, json: { error: { code: "internal_error", message: "The hub had a problem", details: [] } } } : { json: found };
  await page.clock.setFixedTime(new Date("2026-09-25T21:00:00-04:00"));
  await page.route("**/api/**", (route) => route.fulfill({ status: 404, json: { error: { code: "not_found", message: "Not mocked" } } }));
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", profile: "demo", version: "0.1.0", local: true } }));
  for (const tab of ["focus", "sleep"] as const) {
    await page.route(`**/api/v1/insights/${tab}?**`, (route) => {
      const range = new URL(route.request().url()).searchParams.get("range") ?? "";
      asked.push(`${tab} ${range}`);
      return route.fulfill(reply(answers[tab]?.(range) ?? FIXTURES[tab][range] ?? FIXTURES[tab]["14d"]));
    });
  }
  return asked;
}

const chartsDrawn = () =>
  [...document.querySelectorAll(".chart")].filter((chart) =>
    [...chart.querySelectorAll("canvas")].some((canvas) => {
      if (!canvas.width || !canvas.height) return false;
      const pixels = canvas.getContext("2d")?.getImageData(0, 0, canvas.width, canvas.height).data ?? [];
      for (let alpha = 3; alpha < pixels.length; alpha += 4 * 37) if (pixels[alpha]) return true;
      return false;
    }),
  ).length >= 6;

async function settled(page: Page) {
  await page.evaluate(() =>
    Promise.all(document.getAnimations().filter((a) => a.effect?.getTiming().iterations !== Infinity).map((a) => a.finished.catch(() => undefined))),
  );
}

const FOURTEEN = "2026-09-12..2026-09-25"; // the fixture's 14 days, as a custom range

test("every chart draws 14 seeded days within 1.5 s of the hub's answers", async ({ page }) => {
  await mockHub(page);
  let answeredAt: number | null = null;
  page.on("response", (response) => {
    if (/\/api\/v1\/insights\/(focus|sleep)\?/.test(response.url())) answeredAt = performance.now();
  });
  await page.goto(`/insights?tab=focus&range=${FOURTEEN}`);
  await page.waitForFunction(chartsDrawn);
  expect(answeredAt).not.toBeNull();
  expect(performance.now() - (answeredAt ?? 0)).toBeLessThan(1500);
  for (const title of ["Focus score", "When distractions happen", "App switches an hour", "Sleep each night", "Bedtime and wake time", "Late nights and the next day's focus"]) {
    await expect(page.getByRole("region", { name: title, exact: true }).locator(".chart canvas").first()).toBeVisible();
  }
});

test("the cards are the hub's numbers", async ({ page }) => {
  await mockHub(page);
  await page.goto(`/insights?tab=focus&range=${FOURTEEN}`);
  const focus = FIXTURES.focus["14d"];
  const sleep = FIXTURES.sleep["14d"];
  await expect(page.getByRole("region", { name: "Focused time", exact: true })).toContainText(minutes(Number(metricOf(focus, "focused_time"))));
  const score = page.getByRole("region", { name: "Focus score, on average" });
  await expect(score).toContainText(`${metricOf(focus, "focus_score")}/ 100`);
  await expect(score).toContainText(`Best: ${await dayIn(page, String(metricOf(focus, "best_day")), "long")} (${metricOf(focus, "best_score")})`);
  const night = page.getByRole("region", { name: "Sleep a night" });
  await expect(night).toContainText(minutes(Number(metricOf(sleep, "sleep"))));
  await expect(night).toContainText(`0 of ${metricOf(sleep, "nights")} nights estimated`);
  await expect(night.locator(".badge-estimated")).toHaveCount(0); // every seeded night was measured
  await expect(page.getByRole("region", { name: "After 11 pm, a night" })).toContainText(minutes(Number(metricOf(sleep, "late_night"))));
});

test("the calendar has a square for each day, darkest on the most focused, and says which days have no data", async ({ page }) => {
  const base = FIXTURES.focus["14d"];
  const byDay = base.series.focus_by_day;
  const lines = (byDay.lines ?? []).map((line) => ({ ...line, values: line.values.map((value, index) => (index === 3 ? null : value)) })); // the 15th unknown
  await mockHub(page, { focus: () => ({ ...base, series: { ...base.series, focus_by_day: { ...byDay, lines } } }) });
  await page.goto(`/insights?tab=focus&range=${FOURTEEN}`);
  const calendar = page.getByRole("region", { name: "Focused time each day" });
  const days = calendar.locator(".cal-grid .cal-day");
  await expect(days).toHaveCount(14);
  await expect(calendar.locator(".cal-grid .cal-none")).toHaveCount(1);
  const focused = (byDay.lines ?? [])[0].values;
  const most = focused.indexOf(Math.max(...focused.map((value) => value ?? 0)));
  const bestDay = (byDay.x ?? [])[most];
  const summary = `most on ${await dayIn(page, bestDay, "long")} (${minutes(focused[most] ?? 0)}); 13 of 14 days with data, none on ${await dayIn(page, "2026-09-15", "short")}.`;
  await expect(calendar.getByRole("img")).toHaveAttribute("aria-label", `Focused minutes each day: ${summary}`);
  await expect(calendar.locator(`.cal-day[title^="${await dayIn(page, bestDay, "long")}:"]`)).toHaveAttribute("style", /--level: 1;/);
  await expect(calendar.locator(`.cal-day[title^="${await dayIn(page, "2026-09-15", "long")}:"]`)).toHaveAttribute("title", /: no data$/);
});

test("the gauge explains how the focus score is worked out", async ({ page }) => {
  await mockHub(page);
  await page.goto(`/insights?tab=focus&range=${FOURTEEN}`);
  const gauge = page.getByRole("region", { name: "Focus score", exact: true });
  await gauge.getByRole("button", { name: "About Focus score" }).click();
  await expect(gauge.getByRole("status")).toContainText("100 × focused time ÷ (work or study time + distracted time)");
  await expect(gauge.locator(".chart")).toHaveAttribute("aria-label", /Focus score/);
});

test("estimated sleep is always marked", async ({ page }) => {
  const base = FIXTURES.sleep["14d"];
  const nights = base.series.sleep_by_night;
  const [measured, estimated] = nights.lines ?? [];
  const guessed = new Set([2, 9]); // two nights with no health data
  const sleep = {
    ...base,
    metrics: base.metrics.map((item) => (item.id === "estimated_nights" ? { ...item, value: 2 } : item.id === "sleep" ? { ...item, estimated: true } : item)),
    series: {
      ...base.series,
      sleep_by_night: {
        ...nights,
        estimated: true,
        lines: [
          { ...measured, values: measured.values.map((value, index) => (guessed.has(index) ? null : value)) },
          { ...estimated, values: measured.values.map((value, index) => (guessed.has(index) ? value : null)) },
        ],
      },
    },
  };
  await mockHub(page, { sleep: () => sleep });
  await page.goto(`/insights?tab=focus&range=${FOURTEEN}`);
  const night = page.getByRole("region", { name: "Sleep a night" });
  await expect(night.locator(".badge-estimated")).toBeVisible();
  await expect(night).toContainText("2 of 14 nights estimated");
  const bars = page.getByRole("region", { name: "Sleep each night" });
  await expect(bars.locator(".badge-estimated")).toBeVisible();
  await expect(bars.locator(".chart")).toHaveAttribute("aria-label", /striped bars estimated from the phone/);
  await expect(page.getByRole("region", { name: "Bedtime and wake time" }).locator(".chart")).toHaveAttribute("aria-label", /hollow points are estimated/);
});

test("the late-night pattern shows its size, strength and that it is not a cause", async ({ page }) => {
  await mockHub(page);
  await page.goto(`/insights?tab=focus&range=${FOURTEEN}`);
  const scatter = FIXTURES.focus["14d"].series.late_vs_focus;
  const card = page.getByRole("region", { name: "Late nights and the next day's focus" });
  await expect(card.locator(".pattern-stats")).toContainText(`${scatter.stats?.n} nights`);
  await expect(card.locator(".pattern-stats")).toContainText(`rho ${Number(scatter.stats?.rho).toFixed(2)}`);
  await expect(card.locator(".pattern-stats")).toContainText("p < 0.001");
  await expect(card.locator(".pattern-note")).toHaveText(String(scatter.note));
  expect(Number(scatter.stats?.rho)).toBeLessThan(-0.5); // the seeded pattern is there: later nights, less focus
});

test("with too few nights the pattern says so instead of a number", async ({ page }) => {
  const base = FIXTURES.focus["14d"];
  const scatter = base.series.late_vs_focus;
  const few = { ...scatter, points: (scatter.points ?? []).slice(0, 2), stats: { rho: null, p: null, n: 2, slope: -0.3, intercept: 70 } };
  await mockHub(page, { focus: () => ({ ...base, series: { ...base.series, late_vs_focus: few } }) });
  await page.goto(`/insights?tab=focus&range=${FOURTEEN}`);
  const stats = page.getByRole("region", { name: "Late nights and the next day's focus" }).locator(".pattern-stats");
  await expect(stats).toContainText("2 nights");
  await expect(stats).toContainText("Too few nights for a pattern yet (it needs 3).");
  await expect(stats).not.toContainText("rho");
});

test("when focus can't load, sleep still shows, and Try again asks again", async ({ page }) => {
  let fail = true;
  const asked = await mockHub(page, { focus: (range) => (fail ? "fail" : FIXTURES.focus[range] ?? FIXTURES.focus["14d"]) });
  await page.goto(`/insights?tab=focus&range=${FOURTEEN}`);
  await expect(page.getByText("Focus couldn't load: The hub had a problem")).toBeVisible();
  await expect(page.getByRole("region", { name: "Sleep each night" }).locator(".chart canvas").first()).toBeVisible();
  fail = false;
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByRole("region", { name: "Focus score", exact: true }).locator(".chart canvas").first()).toBeVisible();
  expect(asked.filter((item) => item === `focus ${FOURTEEN}`)).toHaveLength(2);
});

for (const scheme of ["light", "dark"] as const) {
  test(`the Focus and Sleep tab passes axe (${scheme})`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await mockHub(page);
    await page.goto(`/insights?tab=focus&range=${FOURTEEN}`);
    await page.waitForFunction(chartsDrawn);
    await settled(page);
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations.map((violation) => `${violation.id}: ${violation.nodes.map((node) => node.target).join(", ")}`)).toEqual([]);
  });
}
