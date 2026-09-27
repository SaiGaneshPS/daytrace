// DT-57: the Insights Food and Calendar tab, with the hub mocked by its own answers for 14 seeded days
// (e2e/fixtures, made and checked by the hub's tests): every chart and how fast it draws, the meals as logged, late-night
// eating, each event split into what its time went to, the share of plans kept, meetings, errors, and accessibility.
// The clock is fixed at 21:00 on Friday 25 September 2026 in Toronto, as when the fixtures were made.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

type Item = { name: string; key?: string | null; value: number; share?: number | null; children?: Item[] | null };
type Tab = {
  metrics: { id: string; label: string; value: number | string | null; explain?: string }[];
  series: Record<string, { x?: string[] | null; items?: Item[] | null; points?: { x: number; y: number; label: string; group?: string | null }[] | null;
    lines?: { name: string; key?: string | null; values: (number | null)[] }[] | null }>;
};
const FIXTURES = JSON.parse(readFileSync(join(process.cwd(), "e2e", "fixtures", "insights-food-calendar.json"), "utf-8")) as { food: Record<string, Tab>; calendar: Record<string, Tab> };

// The page clock is fixed, and charts animate by it: with reduced motion every chart draws its last frame at once.
test.use({ timezoneId: "America/Toronto", locale: "en-US", reducedMotion: "reduce" });

const metricOf = (data: Tab, id: string) => data.metrics.find((item) => item.id === id)?.value;
function minutes(value: number): string {
  const whole = Math.round(value);
  const hours = Math.floor(whole / 60);
  const rest = whole % 60;
  return !hours ? `${rest}m` : rest ? `${hours}h ${rest}m` : `${hours}h`;
}
function longDayIn(page: Page, day: string): Promise<string> {
  return page.evaluate((iso) => {
    const [year, month, date] = iso.split("-").map(Number);
    return new Date(year, month - 1, date).toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
  }, day);
}

type Answers = { food?: (range: string) => object | "fail"; calendar?: (range: string) => object | "fail" };

async function mockHub(page: Page, answers: Answers = {}) {
  const asked: string[] = [];
  const reply = (found: object | "fail") =>
    found === "fail" ? { status: 500, json: { error: { code: "internal_error", message: "The hub had a problem", details: [] } } } : { json: found };
  await page.clock.setFixedTime(new Date("2026-09-25T21:00:00-04:00"));
  await page.route("**/api/**", (route) => route.fulfill({ status: 404, json: { error: { code: "not_found", message: "Not mocked" } } }));
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", profile: "demo", version: "0.1.0", local: true } }));
  for (const tab of ["food", "calendar"] as const) {
    await page.route(`**/api/v1/insights/${tab}?**`, (route) => {
      const range = new URL(route.request().url()).searchParams.get("range") ?? "";
      asked.push(`${tab} ${range}`);
      return route.fulfill(reply(answers[tab]?.(range) ?? FIXTURES[tab][range] ?? FIXTURES[tab]["7d"]));
    });
  }
  return asked;
}

/** Whether `count` charts have painted something (a chart may use several canvases, made before it draws). */
const drawn = (count: number) =>
  [...document.querySelectorAll(".chart")].filter((chart) =>
    [...chart.querySelectorAll("canvas")].some((canvas) => {
      if (!canvas.width || !canvas.height) return false;
      const pixels = canvas.getContext("2d")?.getImageData(0, 0, canvas.width, canvas.height).data ?? [];
      for (let alpha = 3; alpha < pixels.length; alpha += 4 * 37) if (pixels[alpha]) return true;
      return false;
    }),
  ).length >= count;

async function settled(page: Page) {
  await page.evaluate(() =>
    Promise.all(document.getAnimations().filter((a) => a.effect?.getTiming().iterations !== Infinity).map((a) => a.finished.catch(() => undefined))),
  );
}

const FOURTEEN = "2026-09-12..2026-09-25"; // the fixture's 14 days, as a custom range
const fourteen = { food: () => FIXTURES.food["14d"], calendar: () => FIXTURES.calendar["14d"] };

test("every chart draws 14 seeded days within 1.5 s of the hub's answers", async ({ page }) => {
  await mockHub(page, fourteen);
  let answeredAt: number | null = null;
  page.on("response", (response) => {
    if (/\/api\/v1\/insights\/(food|calendar)\?/.test(response.url())) answeredAt = performance.now();
  });
  await page.goto(`/insights?tab=food&range=${FOURTEEN}`);
  await page.waitForFunction(drawn, 5);
  expect(answeredAt).not.toBeNull();
  expect(performance.now() - (answeredAt ?? 0)).toBeLessThan(1500);
  for (const title of ["When you ate", "Meals by day", "Longest events", "Planned time and where it went", "Spent as planned"]) {
    await expect(page.getByRole("region", { name: title, exact: true }).locator(".chart canvas").first()).toBeVisible();
  }
});

test("the cards and the foods are the hub's, as logged", async ({ page }) => {
  await mockHub(page);
  await page.goto("/insights?tab=food&range=7d");
  const food = FIXTURES.food["7d"];
  const calendar = FIXTURES.calendar["7d"];
  const meals = page.getByRole("region", { name: "Meals logged" });
  await expect(meals).toContainText(String(metricOf(food, "meals")));
  await expect(meals).toContainText(`Most logged: ${metricOf(food, "top_item")}`);
  await expect(page.getByRole("region", { name: "Late-night meals" })).toContainText("0");
  await expect(page.getByRole("region", { name: "Late-night meals" })).toContainText("22:00 to 04:00");
  const planned = page.getByRole("region", { name: "Planned time", exact: true });
  await expect(planned).toContainText(minutes(Number(metricOf(calendar, "planned"))));
  await expect(planned).toContainText(`Busiest: ${await longDayIn(page, String(metricOf(calendar, "busiest_day")))}`);
  await expect(page.getByRole("region", { name: "In meetings" })).toContainText("0m");
  const foods = page.getByRole("region", { name: "Most logged foods" }).locator(".top-foods li");
  const items = food.series.top_items.items ?? [];
  await expect(foods).toHaveCount(items.length);
  for (const [index, item] of items.entries()) {
    await expect(foods.nth(index)).toHaveText(`${item.name}${item.value === 1 ? "once" : `${item.value} times`}`); // no calories, just what was logged
  }
  await expect(page.getByRole("region", { name: "When you ate" }).locator(".chart")).toHaveAttribute("aria-label", /late-night eating \(22:00 to 04:00\) is shaded/);
});

test("late-night meals are counted", async ({ page }) => {
  const base = FIXTURES.food["7d"];
  await mockHub(page, { food: () => ({ ...base, metrics: base.metrics.map((item) => (item.id === "late_meals" ? { ...item, value: 3 } : item)) }) });
  await page.goto("/insights?tab=food&range=7d");
  await expect(page.getByRole("region", { name: "Late-night meals" }).locator(".stat-value .visually-hidden")).toHaveText("3"); // the value as read out
});

test("each event shows what its time went to, and a day nobody saw says so", async ({ page }) => {
  const base = FIXTURES.calendar["7d"];
  const blocks = base.series.blocks;
  const items = (blocks.items ?? []).map((item, index) => (index === 1 ? { ...item, children: null, share: null } : item)); // no screen data that day
  await mockHub(page, { calendar: () => ({ ...base, series: { ...base.series, blocks: { ...blocks, items } } }) });
  await page.goto("/insights?tab=food&range=7d");
  const card = page.getByRole("region", { name: "Longest events" });
  const rows = card.locator(".event-list li");
  await expect(rows).toHaveCount(items.length);
  for (const [index, item] of items.entries()) {
    await expect(rows.nth(index)).toContainText(item.name);
    await expect(rows.nth(index)).toContainText(await longDayIn(page, String(item.key)));
    await expect(rows.nth(index)).toContainText(item.share === null || item.share === undefined ? "no screen data" : `${item.share}% as planned`);
  }
  for (const item of blocks.items ?? []) {
    const parts = (item.children ?? []).reduce((sum, part) => sum + part.value, 0);
    expect(Math.abs(parts - item.value)).toBeLessThanOrEqual(0.02); // each event's parts add up to its length
  }
  await expect(card.locator(".chart")).toHaveAttribute("aria-label", /on plan, off plan, on other screens and with no screen/);
});

test("the share of plans kept is a gauge, and meetings show only when there were any", async ({ page }) => {
  await mockHub(page);
  await page.goto("/insights?tab=food&range=7d");
  const kept = page.getByRole("region", { name: "Spent as planned" });
  await expect(kept.locator(".chart")).toHaveAttribute("aria-label", new RegExp(`^Spent as planned: ${metricOf(FIXTURES.calendar["7d"], "on_plan")}% out of 100%`));
  const meetings = page.getByRole("region", { name: "Meetings each day" });
  await expect(meetings).toContainText("No time in meeting apps"); // the seed has none: said, not an empty chart
  await expect(meetings.locator(".chart")).toHaveCount(0);

  const base = FIXTURES.calendar["7d"];
  const line = (base.series.meetings_by_day.lines ?? [])[0];
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await mockHub(page, {
    calendar: () => ({ ...base, series: { ...base.series, meetings_by_day: { ...base.series.meetings_by_day, lines: [{ ...line, values: line.values.map((_, i) => (i === 2 ? 45 : 0)) }] } } }),
  });
  await page.reload();
  await expect(page.getByRole("region", { name: "Meetings each day" }).locator(".chart canvas").first()).toBeVisible();
});

test("when food can't load, the calendar still shows, and Try again asks again", async ({ page }) => {
  let fail = true;
  const asked = await mockHub(page, { food: (range) => (fail ? "fail" : FIXTURES.food[range] ?? FIXTURES.food["7d"]) });
  await page.goto("/insights?tab=food&range=7d");
  await expect(page.getByText("Food couldn't load: The hub had a problem")).toBeVisible();
  await expect(page.getByRole("region", { name: "Longest events" }).locator(".chart canvas").first()).toBeVisible();
  fail = false;
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByRole("region", { name: "When you ate" }).locator(".chart canvas").first()).toBeVisible();
  expect(asked.filter((item) => item === "food 7d")).toHaveLength(2);
});

for (const scheme of ["light", "dark"] as const) {
  test(`the Food and Calendar tab passes axe (${scheme})`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await mockHub(page);
    await page.goto("/insights?tab=food&range=7d");
    await page.waitForFunction(drawn, 5);
    await settled(page);
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations.map((violation) => `${violation.id}: ${violation.nodes.map((node) => node.target).join(", ")}`)).toEqual([]);
  });
}
