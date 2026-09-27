// DT-54: the Streaks page and Today's streak strip, with the hub mocked by its own answers for 14 seeded days
// (e2e/fixtures/streaks-wrapped.json, made and checked by the hub's tests): each streak's flame, days, today and best,
// its rule and the days that counted, the goals as rings with a target changed in place, the badges with confetti
// once per badge, a first-timer, errors, and accessibility. The clock is fixed at 21:00 on Friday 25 September 2026
// in Toronto, as when the fixture was made.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";
import type { components } from "../src/api/schema";

type StreakList = components["schemas"]["StreakList"];
type GoalList = components["schemas"]["GoalList"];
type AchievementList = components["schemas"]["AchievementList"];
const FIXTURE = JSON.parse(readFileSync(join(process.cwd(), "e2e", "fixtures", "streaks-wrapped.json"), "utf-8")) as {
  streaks: StreakList;
  goals: GoalList;
  achievements: AchievementList;
};

test.use({ timezoneId: "America/Toronto", locale: "en-US" });

function minutes(value: number): string {
  const whole = Math.round(value);
  const hours = Math.floor(whole / 60);
  const rest = whole % 60;
  return !hours ? `${rest}m` : rest ? `${hours}h ${rest}m` : `${hours}h`;
}
const days = (count: number) => `${count} ${count === 1 ? "day" : "days"}`;
function longDayIn(page: Page, day: string): Promise<string> {
  return page.evaluate((iso) => {
    const [year, month, date] = iso.split("-").map(Number);
    return new Date(year, month - 1, date).toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
  }, day);
}

type Reply = object | "fail";
type Answers = { streaks?: (days: string) => Reply; goals?: () => Reply; achievements?: () => Reply; put?: (id: string, body: { target: number | string }) => { status: number; json: object } };

async function mockHub(page: Page, answers: Answers = {}) {
  const asked: string[] = [];
  const reply = (found: Reply) =>
    found === "fail" ? { status: 500, json: { error: { code: "internal_error", message: "The hub had a problem", details: [] } } } : { json: found };
  await page.clock.setFixedTime(new Date("2026-09-25T21:00:00-04:00"));
  await page.route("**/api/**", (route) => route.fulfill({ status: 404, json: { error: { code: "not_found", message: "Not mocked" } } }));
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", profile: "demo", version: "0.1.0", local: true } }));
  await page.route("**/api/v1/streaks?**", (route) => {
    const count = new URL(route.request().url()).searchParams.get("days") ?? "";
    asked.push(`streaks ${count}`);
    return route.fulfill(reply(answers.streaks?.(count) ?? FIXTURE.streaks));
  });
  await page.route("**/api/v1/goals?**", (route) => {
    asked.push("goals");
    return route.fulfill(reply(answers.goals?.() ?? FIXTURE.goals));
  });
  await page.route("**/api/v1/goals/*?**", (route) => {
    const request = route.request();
    const id = new URL(request.url()).pathname.split("/").pop() ?? "";
    const body = request.postDataJSON() as { target: number | string };
    asked.push(`${request.method()} ${id} ${JSON.stringify(body)}`);
    return route.fulfill(answers.put?.(id, body) ?? { status: 200, json: FIXTURE.goals.goals.find((goal) => goal.id === id) ?? {} });
  });
  await page.route("**/api/v1/achievements?**", (route) => {
    asked.push("achievements");
    return route.fulfill(reply(answers.achievements?.() ?? FIXTURE.achievements));
  });
  return asked;
}

const card = (page: Page, name: string) => page.locator(".streak-card").filter({ has: page.locator(".streak-name", { hasText: name }) });

test("each streak is the hub's: its days, what today needs, its best, and a flame that grows with the run", async ({ page }) => {
  const asked = await mockHub(page);
  await page.goto("/streaks");
  const streaks = FIXTURE.streaks.streaks;
  await expect(page.locator(".streak-card")).toHaveCount(streaks.length);
  for (const streak of streaks) {
    const shown = card(page, streak.name);
    await expect(shown.locator(".streak-count")).toHaveText(days(streak.current));
    await expect(shown.locator(".streak-best")).toHaveText(`Best ${days(streak.best)}`);
    const today = streak.today === "met" ? "Done for today" : streak.today === "at_risk" ? `${minutes(streak.remaining?.value ?? 0)} left under the limit today` : null;
    if (today) await expect(shown.locator(".streak-today")).toHaveText(today);
    await expect(shown.locator(".flame")).toHaveClass(new RegExp(`flame-${streak.today === "at_risk" ? "at_risk" : "lit"}`));
  }
  // Balanced is at risk with 42m of its hour of social apps left: said in words, and its flame dims.
  await expect(card(page, "Balanced").locator(".streak-today")).toHaveText("42m left under the limit today");
  const width = (name: string) => card(page, name).locator(".flame svg").evaluate((flame) => flame.getBoundingClientRect().width);
  const left = (name: string) => card(page, name).locator(".streak-name").evaluate((text) => text.getBoundingClientRect().left - text.closest(".streak-card")!.getBoundingClientRect().left);
  expect(await left("Logged it")).toBeCloseTo(await left("Focus flame"), 0); // the names line up whatever the flames' sizes
  expect(await width("Logged it")).toBeGreaterThan((await width("Focus flame")) * 1.2); // 14 days burns bigger than 3
  expect(asked).toContain("streaks 30");
  await expect(page.getByRole("heading", { name: "Your first streak starts today" })).toHaveCount(0);
});

test("opening a streak shows its rule, what it needs, and the days that counted", async ({ page }) => {
  await mockHub(page);
  await page.goto("/streaks");
  const streak = FIXTURE.streaks.streaks.find((item) => item.id === "focus_flame")!;
  const shown = card(page, streak.name);
  const head = shown.getByRole("button");
  await expect(head).toHaveAttribute("aria-expanded", "false");
  await head.click();
  await expect(head).toHaveAttribute("aria-expanded", "true");
  const detail = page.locator(`#${await head.getAttribute("aria-controls")}`);
  await expect(detail).toContainText(`${streak.rule}.`);
  await expect(detail).toContainText(`Counts on days with ${streak.needs}`);
  await expect(detail.locator(".streak-day")).toHaveCount(streak.days.length);
  await expect(detail.locator(".day-met")).toHaveCount(streak.days.filter((day) => day.status === "met").length);
  await expect(detail.locator(".day-missed")).toHaveCount(streak.days.filter((day) => day.status === "missed").length);
  await expect(detail.locator(".day-counted")).toHaveCount(streak.counted.length);
  const met = streak.days.filter((day) => day.status === "met").length;
  const [first, last] = [streak.counted[0], streak.counted[streak.counted.length - 1]];
  const [bestFirst, bestLast] = [streak.best_dates[0], streak.best_dates[streak.best_dates.length - 1]];
  await expect(detail.locator("p.muted")).toHaveText(
    `Met on ${met} of the last ${days(streak.days.length)}. This run: ${await longDayIn(page, first)} to ${await longDayIn(page, last)}.` +
      ` The best: ${await longDayIn(page, bestFirst)} to ${await longDayIn(page, bestLast)}.`,
  );
  const day = streak.days[streak.days.length - 1];
  await expect(detail.locator(".streak-day").last()).toHaveAttribute("title", new RegExp(`: met \\(${minutes(day.value ?? 0)}\\)$`));
  await head.click();
  await expect(detail).toHaveCount(0);
});

test("a one-day run is named as one day", async ({ page }) => {
  await mockHub(page);
  await page.goto("/streaks");
  const streak = FIXTURE.streaks.streaks.find((item) => item.id === "balanced")!;
  expect(streak.counted).toHaveLength(1);
  await card(page, streak.name).getByRole("button").click();
  const day = await longDayIn(page, streak.counted[0]);
  await expect(card(page, streak.name).locator(".streak-detail p.muted")).toContainText(`This run: ${day}. The best: ${await longDayIn(page, streak.best_dates[0])}.`);
});

test("goals are rings with the hub's numbers, and a target can be changed in place", async ({ page }) => {
  let target = 240;
  const asked = await mockHub(page, {
    goals: () => ({ ...FIXTURE.goals, goals: FIXTURE.goals.goals.map((goal) => (goal.id === "focus_target" ? { ...goal, target } : goal)) }),
    put: (id, body) => {
      target = Number(body.target);
      return { status: 200, json: { ...FIXTURE.goals.goals.find((goal) => goal.id === id), target } };
    },
  });
  await page.goto("/streaks");
  const focus = page.getByRole("region", { name: "Focused time" });
  await expect(focus.getByRole("img")).toHaveAttribute("aria-label", "Focused time: 6h 25m against a target of 4h, done");
  await expect(page.getByRole("region", { name: "Social apps" }).getByRole("img")).toHaveAttribute("aria-label", "Social apps: 18m against a limit of 1h, not yet");
  await expect(page.getByRole("region", { name: "Bedtime" }).getByRole("img")).toHaveAttribute("aria-label", "Bedtime: 23:25 against a limit of 23:30, done");
  const ringFill = (name: string) =>
    page.getByRole("region", { name }).locator(".ring-value").evaluate((circle) => Number(circle.getAttribute("stroke-dashoffset")) / Number(circle.getAttribute("stroke-dasharray")));
  expect(await ringFill("Focused time")).toBeCloseTo(0, 3); // full
  expect(await ringFill("Social apps")).toBeCloseTo(0.7, 3); // 30% of the limit used

  await focus.getByRole("button", { name: "Change the target" }).click();
  const field = focus.getByLabel("Minutes");
  await expect(field).toHaveValue("240");
  await expect(focus.locator(".field-note")).toHaveText("From 10 to 720 minutes.");
  await field.fill("300");
  const before = asked.filter((item) => item.startsWith("streaks")).length;
  await focus.getByRole("button", { name: "Save" }).click();
  await expect(focus.getByRole("img")).toHaveAttribute("aria-label", "Focused time: 6h 25m against a target of 5h, done");
  await expect(focus.getByRole("button", { name: "Change the target" })).toBeVisible();
  expect(asked).toContain('PUT focus_target {"target":300}');
  await expect.poll(() => asked.filter((item) => item.startsWith("streaks")).length).toBe(before + 1); // its streak is judged again
});

test("a bedtime is set as a time, and the hub's reason shows when a target is refused", async ({ page }) => {
  const asked = await mockHub(page, {
    put: () => ({ status: 400, json: { error: { code: "bad_request", message: "bedtime must be between 20:00 and 03:00", details: [] } } }),
  });
  await page.goto("/streaks");
  const bedtime = page.getByRole("region", { name: "Bedtime" });
  await bedtime.getByRole("button", { name: "Change the limit" }).click();
  const field = bedtime.getByLabel("Asleep by");
  await expect(field).toHaveAttribute("type", "time");
  await expect(bedtime.locator(".field-note")).toHaveText("Between 20:00 and 03:00.");
  await field.fill("19:00");
  await bedtime.getByRole("button", { name: "Save" }).click();
  await expect(bedtime.locator(".field-note")).toHaveText("bedtime must be between 20:00 and 03:00");
  expect(asked).toContain('PUT bedtime {"target":"19:00"}');
  await bedtime.getByRole("button", { name: "Cancel" }).click();
  await expect(bedtime.getByRole("img")).toHaveAttribute("aria-label", /against a limit of 23:30/); // unchanged
});

test("badges: earned ones say when, the rest how far, and each new one is celebrated once", async ({ page }) => {
  const badges = FIXTURE.achievements;
  let list = badges.achievements;
  await mockHub(page, { achievements: () => ({ ...badges, unlocked: list.filter((badge) => badge.unlocked).length, achievements: list }) });
  await page.goto("/streaks");
  await expect(page.getByRole("heading", { name: `Badges (${badges.unlocked} of ${list.length})` })).toBeVisible();
  const tiles = page.locator(".badge-tile");
  await expect(tiles).toHaveCount(list.length);
  for (const [index, badge] of list.entries()) {
    const tile = tiles.nth(index);
    await expect(tile.locator("strong")).toHaveText(badge.name);
    if (badge.unlocked) await expect(tile.locator(".badge-earned")).toHaveText(`Earned ${await longDayIn(page, badge.earned_on ?? "")}`);
    else if (badge.progress) await expect(tile.locator(".badge-progress-words")).toHaveText(`${badge.progress.value} of ${badge.progress.target} ${badge.progress.unit}`);
    await expect(tile.locator(".visually-hidden")).toHaveText(badge.unlocked ? "Unlocked." : "Not yet.");
  }
  // First visit: every unlocked badge pops, with confetti drawn on the page.
  await expect(page.locator(".badge-new")).toHaveCount(badges.unlocked);
  await expect.poll(() => page.evaluate(() => document.querySelectorAll("body > canvas").length)).toBeGreaterThan(0);
  const unlocked = list.filter((badge) => badge.unlocked).map((badge) => badge.id);
  expect((await page.evaluate(() => JSON.parse(localStorage.getItem("daytrace.badges.celebrated") ?? "[]"))).sort()).toEqual([...unlocked].sort());

  // Again: nothing pops.
  await page.reload();
  await expect(tiles).toHaveCount(list.length);
  await expect(page.locator(".badge-tile.badge-unlocked")).toHaveCount(badges.unlocked);
  await expect(page.locator(".badge-new")).toHaveCount(0);

  // A badge earned since pops on its own.
  list = list.map((badge) => (badge.id === "perfect_week" ? { ...badge, unlocked: true, earned_on: "2026-09-25", progress: null } : badge));
  await page.reload();
  await expect(page.locator(".badge-new")).toHaveCount(1);
  await expect(page.locator(".badge-new")).toHaveAttribute("data-badge", "perfect_week");
});

test("someone new sees where to start instead of a row of zeros", async ({ page }) => {
  const fresh = { ...FIXTURE.streaks, streaks: FIXTURE.streaks.streaks.map((streak) => ({ ...streak, current: 0, best: 0, counted: [], best_dates: [], today: "no_data" as const })) };
  await mockHub(page, { streaks: () => fresh });
  await page.goto("/streaks");
  await expect(page.getByRole("heading", { name: "Your first streak starts today" })).toBeVisible();
  await expect(card(page, "Focus flame").locator(".streak-today")).toHaveText(`Nothing yet today: it needs ${FIXTURE.streaks.streaks[0].needs}`);
  await expect(card(page, "Focus flame").locator(".flame")).toHaveClass(/flame-out/);
});

test("when the streaks can't load it says so, and the goals and badges still show", async ({ page }) => {
  await mockHub(page, { streaks: () => "fail" });
  await page.goto("/streaks");
  await expect(page.getByText("The streaks couldn't load: The hub had a problem")).toBeVisible();
  await expect(page.locator(".goal-card")).toHaveCount(FIXTURE.goals.goals.length);
  await expect(page.locator(".badge-tile")).toHaveCount(FIXTURE.achievements.achievements.length);
});

test("Today shows each streak at a glance, and leads to the rest", async ({ page }) => {
  const asked = await mockHub(page);
  await page.goto("/");
  const strip = page.getByRole("region", { name: "Streaks" });
  const chips = strip.locator(".streak-chip");
  await expect(chips).toHaveCount(FIXTURE.streaks.streaks.length);
  for (const [index, streak] of FIXTURE.streaks.streaks.entries()) {
    await expect(chips.nth(index)).toContainText(`${streak.name} ${days(streak.current)}`);
  }
  await expect(strip.locator(".streak-chip").filter({ hasText: "Balanced" })).toContainText("42m left under the limit today");
  expect(asked).toContain("streaks 1");
  await strip.getByRole("link", { name: "All streaks and goals" }).click();
  await expect(page).toHaveURL("/streaks");
  await expect(page.getByRole("heading", { name: "Streaks", level: 1 })).toBeVisible();
});

test("Today stays as it was when the hub has no streaks to show", async ({ page }) => {
  await mockHub(page, { streaks: () => "fail" });
  await page.goto("/");
  await expect(page.locator(".stat-grid")).toBeVisible();
  await expect(page.locator(".streak-strip")).toHaveCount(0);
});

for (const scheme of ["light", "dark"] as const) {
  test(`Streaks passes axe (${scheme}), with a streak and a goal open`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await mockHub(page);
    await page.goto("/streaks");
    await card(page, "Balanced").getByRole("button").click();
    await page.getByRole("region", { name: "Social apps" }).getByRole("button", { name: "Change the limit" }).click();
    await page.evaluate(() =>
      Promise.all(document.getAnimations().filter((a) => a.effect?.getTiming().iterations !== Infinity).map((a) => a.finished.catch(() => undefined))),
    );
    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
    expect(results.violations.map((v) => `${v.id} (${v.nodes.length}): ${v.help} ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)).toEqual([]);
  });
}

test("phone: every control on Streaks can be tapped (44 px or more)", async ({ page, isMobile }) => {
  test.skip(!isMobile, "touch targets");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockHub(page);
  await page.goto("/streaks");
  await page.getByRole("region", { name: "Focused time" }).getByRole("button", { name: "Change the target" }).click();
  const controls = page.locator("main button, main input, main a");
  await expect(controls.first()).toBeVisible();
  for (const control of await controls.all()) {
    const box = await control.boundingBox();
    if (!box) continue;
    expect(Math.min(box.width, box.height), await control.evaluate((element) => element.outerHTML.slice(0, 80))).toBeGreaterThanOrEqual(44);
  }
});
