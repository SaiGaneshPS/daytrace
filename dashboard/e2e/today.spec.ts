// DT-31: the Today page with the hub mocked: hero numbers, the timeline, live refresh, the sub-tabs, picking a day,
// an empty day, and accessibility. The clock is fixed at 14:00 on 25 September 2026 in Toronto.
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, type Route, test } from "@playwright/test";

const DAY = "2026-09-25";
const LIVE = /(^|\s)live(\s|$)/; // the "live" class itself, not "live-dot"
const at = (clock: string, day = DAY) => `${day}T${clock}-04:00`;

type Session = { start: string; end: string; seconds: number; minutes: number; app: string; app_id: string | null; title: string | null; category: string; kind: string; estimated: boolean };
const session = (start: string, end: string, app: string, category: string, estimated = false): Session => {
  const seconds = (Date.parse(at(end)) - Date.parse(at(start))) / 1000;
  return { start: at(start), end: at(end), seconds, minutes: Math.round((seconds / 60) * 100) / 100, app, app_id: null, title: null, category, kind: "app", estimated };
};

function lane(device_id: string, device_type: string, name: string, sessions: Session[], counted = true) {
  const seconds = sessions.reduce((sum, s) => sum + s.seconds, 0);
  return { device_id, device_type, name, counted, seconds, minutes: seconds / 60, sessions };
}

function timeline(extra: ReturnType<typeof lane>[] = [], estimated = false) {
  const lanes = [
    lane("windows-1", "windows", "Desk PC", [session("09:00:00", "11:05:00", "Visual Studio Code", "work"), session("13:00:00", "13:20:00", "Steam", "games")]),
    lane("android-1", "android", "Galaxy phone", [session("12:00:00", "12:30:00", "Instagram", "social", estimated), session("13:30:00", "13:45:00", "YouTube", "video")]),
    lane("browser-1", "browser", "Edge", [session("10:00:00", "10:10:00", "github.com", "work")], false),
    ...extra,
  ];
  const counted = lanes.filter((l) => l.counted);
  const seconds = counted.reduce((sum, l) => sum + l.seconds, 0);
  return {
    date: DAY, tz: "America/Toronto", lanes,
    calendar: [{ start: at("15:00:00"), end: at("16:00:00"), title: "Study: algorithms", all_day: false, device_id: "android-1" }],
    sleep: [{ start: at("23:30:00", "2026-09-24"), end: at("06:57:00"), minutes: 447, stage: "asleep", estimated: false, device_id: "android-1" }],
    meals: [{ time: at("12:40:00"), items: ["roti", "dal"], text: null, meal_type: "lunch", device_id: "android-1" }],
    totals: {
      seconds, minutes: seconds / 60,
      by_device_seconds: Object.fromEntries(counted.map((l) => [l.device_id, l.seconds])),
      by_device: Object.fromEntries(counted.map((l) => [l.device_id, l.minutes])),
      any_screen_seconds: seconds - 600, any_screen_minutes: seconds / 60 - 10, sleep_seconds: 26820, sleep_minutes: 447,
    },
    meta: { unit: "minutes", range: { start: at("00:00:00"), end: at("00:00:00", "2026-09-26"), tz: "America/Toronto" }, source: "real", estimated },
  };
}

const SUMMARY = {
  date: DAY, tz: "America/Toronto", in_progress: true, screen_minutes: 190, phone_minutes: 45, computer_minutes: 145,
  focused_minutes: 125, focus_score: 71, pickups: 4, switches_per_hour: 1.3, sleep_minutes: 447, sleep_estimated: true,
  steps: 8412, estimated: true,
  top_apps: [
    { app: "Visual Studio Code", category: "work", minutes: 125 },
    { app: "Instagram", category: "social", minutes: 30 },
    { app: "Steam", category: "games", minutes: 20 },
    { app: "YouTube", category: "video", minutes: 15 },
  ],
};

const DEVICES = {
  devices: [
    { device_id: "windows-1", name: "Desk PC", device_type: "windows", paired_at: at("08:00:00"), last_seen: at("12:00:00"), revoked_at: null, last_seq: 10, event_count: 10, events_24h: 10 },
    { device_id: "android-1", name: "Galaxy phone", device_type: "android", paired_at: at("08:00:00"), last_seen: at("13:59:40"), revoked_at: null, last_seq: 5, event_count: 5, events_24h: 5 },
  ],
};

test.use({ timezoneId: "America/Toronto", locale: "en-US" });

async function mockHub(page: Page, timelines: (count: number, url: URL) => object = () => timeline()) {
  const seen = { timeline: [] as URL[] };
  await page.clock.setFixedTime(new Date("2026-09-25T14:00:00-04:00"));
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", profile: "demo", version: "0.1.0" } }));
  await page.route("**/api/v1/timeline?**", (route: Route) => {
    const url = new URL(route.request().url());
    seen.timeline.push(url);
    return route.fulfill({ json: timelines(seen.timeline.length, url) });
  });
  await page.route("**/api/v1/insights/day?**", (route) => {
    const date = new URL(route.request().url()).searchParams.get("date");
    return route.fulfill({ json: { ...SUMMARY, date, in_progress: date === DAY } }); // only today is still going
  });
  await page.route("**/api/v1/devices", (route) => route.fulfill({ json: DEVICES }));
  return seen;
}

test("the hero numbers are the hub's, and the total is the sum of the blocks", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const seen = await mockHub(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Today");
  const hero = page.getByRole("region", { name: "Screen time so far" });
  const blocks = timeline().lanes.filter((l) => l.counted).flatMap((l) => l.sessions).reduce((sum, s) => sum + s.seconds, 0);
  expect(blocks).toBe(timeline().totals.seconds);
  await expect(hero.locator(".stat-value")).toContainText("3h 10m"); // 190 minutes of blocks
  await expect(page.getByRole("region", { name: "Phone and computer" })).toContainText("45m");
  await expect(page.getByRole("region", { name: "Phone and computer" })).toContainText("2h 25m on computers");
  await expect(page.getByRole("region", { name: "Focused time so far" })).toContainText("2h 5m");
  await expect(page.getByRole("region", { name: "Focused time so far" })).toContainText("Focus score 71 out of 100");
  await expect(page.getByRole("region", { name: "Phone pickups so far" })).toContainText("4");
  expect(seen.timeline[0].searchParams.get("date")).toBe(DAY);
  expect(seen.timeline[0].searchParams.get("tz")).toBe("America/Toronto");
});

test("the timeline shows every device, the categories and who is live", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockHub(page);
  await page.goto("/");
  const card = page.getByRole("region", { name: "Timeline" });
  await expect(card.locator(".timeline-chart canvas")).toBeVisible();
  await expect(card.locator(".timeline-chart")).toHaveAttribute("aria-label", /Timeline of 2026-09-25/);
  const chips = card.getByRole("list", { name: "Devices" }).getByRole("listitem");
  await expect(chips).toHaveCount(3);
  await expect(chips.filter({ hasText: "Galaxy phone" })).toContainText("45m");
  await expect(chips.filter({ hasText: "Galaxy phone" }).locator(".live-dot")).toHaveClass(LIVE); // synced 20 s ago
  await expect(chips.filter({ hasText: "Desk PC" }).locator(".live-dot")).not.toHaveClass(LIVE); // 2 hours ago
  await expect(chips.filter({ hasText: "Edge" })).toContainText("sites");
  const legend = card.getByRole("list", { name: "Categories" });
  for (const name of ["Work", "Games", "Social", "Video"]) await expect(legend).toContainText(name);
});

test("today refreshes by itself, and a new device shows within 5 seconds", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const tablet = lane("android-2", "android", "Tablet", [session("13:50:00", "13:58:00", "WhatsApp", "comms")]);
  const seen = await mockHub(page, (count) => (count >= 2 ? timeline([tablet]) : timeline()));
  await page.goto("/");
  const chips = page.getByRole("list", { name: "Devices" }).getByRole("listitem");
  await expect(chips).toHaveCount(3);
  await expect(chips.filter({ hasText: "Tablet" })).toBeVisible({ timeout: 5_000 });
  await expect(page.getByRole("region", { name: "Screen time so far" }).locator(".stat-value")).toContainText("3h 18m");
  expect(seen.timeline.length).toBeGreaterThanOrEqual(2);
});

test("the sub-tabs: apps, devices and health", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockHub(page);
  await page.goto("/");
  const tabs = page.getByRole("tablist", { name: "Today views" });
  await tabs.getByRole("tab", { name: "Apps" }).click();
  const apps = page.getByRole("region", { name: "Top apps and sites" });
  await expect(apps.locator(".chart")).toHaveAttribute("aria-label", /Top 4 apps and sites/);
  await expect(apps.getByText("Estimated").first()).toBeVisible();
  await tabs.getByRole("tab", { name: "Devices" }).click();
  const devices = page.getByRole("region", { name: "Minutes per device" });
  await expect(devices.locator(".chart canvas")).toBeVisible();
  await expect(devices).toContainText("3h 10m in all");
  await tabs.getByRole("tab", { name: "Health" }).click();
  const sleep = page.getByRole("region", { name: "Last night's sleep" });
  await expect(sleep).toContainText("7h 27m");
  await expect(sleep.getByRole("img", { name: "Slept 7 hours 27 minutes" })).toBeVisible();
  await expect(sleep.getByText("Estimated").first()).toBeVisible();
  await expect(page.getByRole("region", { name: "Steps" }).getByRole("img", { name: "8,412 steps" })).toBeVisible();
});

test("another day can be picked, and it doesn't refresh", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const seen = await mockHub(page, (_, url) => ({ ...timeline(), date: url.searchParams.get("date") }));
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Next day" })).toBeDisabled();
  await page.getByRole("button", { name: "Previous day" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Thursday, September 24");
  await expect.poll(() => seen.timeline.at(-1)?.searchParams.get("date")).toBe("2026-09-24");
  await expect(page.getByRole("region", { name: "Screen time", exact: true })).toBeVisible(); // a finished day: not "so far"
  const requests = seen.timeline.length;
  await page.waitForTimeout(4_000);
  expect(seen.timeline.length).toBe(requests); // past days don't poll
  await page.getByRole("button", { name: "Today" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Today");
});

test("a day with nothing recorded says so", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockHub(page, () => ({
    ...timeline(), lanes: [], calendar: [], sleep: [], meals: [],
    totals: { seconds: 0, minutes: 0, by_device_seconds: {}, by_device: {}, any_screen_seconds: 0, any_screen_minutes: 0, sleep_seconds: 0, sleep_minutes: 0 },
  }));
  await page.goto("/");
  await expect(page.getByRole("region", { name: "Timeline" })).toContainText("Nothing recorded on this day yet.");
  await expect(page.getByRole("region", { name: "Screen time so far" })).toContainText("No data yet");
});

for (const scheme of ["light", "dark"] as const) {
  test(`accessibility (axe) with data, in ${scheme}`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await mockHub(page);
    await page.goto("/");
    await expect(page.locator(".timeline-chart canvas")).toBeVisible();
    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
    expect(results.violations.map((v) => `${v.id} (${v.nodes.length}): ${v.help}`)).toEqual([]);
  });
}

test("screenshots of Today for review", async ({ page }, testInfo) => {
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await mockHub(page);
    await page.goto("/");
    await expect(page.locator(".timeline-chart canvas")).toBeVisible();
    await page.waitForTimeout(300);
    await page.screenshot({ path: testInfo.outputPath(`today-${scheme}.png`), fullPage: true });
  }
});
