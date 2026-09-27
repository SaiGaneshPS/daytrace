// Every page at every size: widths from a small phone (280 px) to a desktop, text at 100%, 150% and 200% (like a
// phone's font size setting, or browser text zoom), in the system's font and in a wide one. On each, nothing may stick out past the screen, and nothing may
// be cut off by a box that hides what overflows it (the page never scrolls sideways; a row that scrolls on purpose
// is fine), and no word in a button, tab, label or heading may be split across two lines. This checks that layouts adapt to any device instead of fitting the sizes other tests
// happen to use. The hub is mocked with a full day, a story, an answer, a pairing code and 14 days of insights.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, type Page, test } from "@playwright/test";

const WIDTHS = [280, 320, 360, 412, 600, 768, 1024, 1440];
const TEXT_SCALES = [1, 1.5, 2];
// The system's own font, and a deliberately wide one: fonts differ between computers (Linux's are wider than
// Windows'), so a layout that only just fits one of them fails here on any machine, not only on some.
const FONTS = ["", 'Verdana, "DejaVu Sans", sans-serif'];
const DAY = "2026-09-25";
const at = (clock: string) => `${DAY}T${clock}-04:00`;

const session = (start: string, end: string, app: string, category: string) => {
  const seconds = (Date.parse(at(end)) - Date.parse(at(start))) / 1000;
  return { start: at(start), end: at(end), seconds, minutes: seconds / 60, app, app_id: null, title: null, category, kind: "app", estimated: false };
};
const lane = (device_id: string, device_type: string, name: string, sessions: ReturnType<typeof session>[]) => {
  const seconds = sessions.reduce((sum, s) => sum + s.seconds, 0);
  return { device_id, device_type, name, counted: true, last_seen: at("13:59:30"), seconds, minutes: seconds / 60, sessions };
};
const LANES = [
  lane("windows-1", "windows", "A desk computer with a rather long name", [session("09:00:00", "11:05:00", "Visual Studio Code", "work"), session("13:00:00", "13:20:00", "Steam", "games")]),
  lane("android-1", "android", "Galaxy phone", [session("12:00:00", "12:30:00", "Instagram", "social"), session("13:30:00", "13:45:00", "YouTube", "video")]),
];
const TIMELINE = {
  date: DAY, tz: "America/Toronto", lanes: LANES,
  calendar: [{ start: at("15:00:00"), end: at("16:00:00"), title: "Study: algorithms and data structures", all_day: false, device_id: "android-1" }],
  sleep: [{ start: "2026-09-24T23:30:00-04:00", end: at("06:57:00"), minutes: 447, stage: "asleep", estimated: false, device_id: "android-1" }],
  meals: [{ time: at("12:40:00"), items: ["roti", "dal"], text: null, meal_type: "lunch", device_id: "android-1" }],
  totals: { seconds: 11_400, minutes: 190, by_device_seconds: {}, by_device: {}, any_screen_seconds: 10_800, any_screen_minutes: 180, sleep_seconds: 26_820, sleep_minutes: 447 },
  meta: { unit: "minutes", range: { start: at("00:00:00"), end: "2026-09-26T00:00:00-04:00", tz: "America/Toronto" }, source: "real", estimated: false },
};
const SUMMARY = {
  date: DAY, tz: "America/Toronto", in_progress: true, screen_minutes: 190, screen_estimated: false, phone_minutes: 45, computer_minutes: 145,
  focused_minutes: 125, focus_score: 71, work_or_study_minutes: 130, distracted_minutes: 45, pickups: 4, switches_per_hour: 1.3,
  sleep_minutes: 447, sleep_estimated: false, steps: 8412, estimated: false,
  top_apps: [
    { app: "Visual Studio Code", category: "work", minutes: 125 },
    { app: "Instagram", category: "social", minutes: 30 },
    { app: "a-very-long-site-name.example.com", category: "video", minutes: 15 },
  ],
};
const FACTS = [
  { label: "time in apps matching YouTube between 23:00 and 03:00, from Monday 2026-09-14 to Sunday 2026-09-20", value: 110, unit: "minutes" },
  { label: "first screen use", value: "07:18", unit: "time" },
];
const STORY = {
  date: DAY, tz: "America/Toronto", story: "You started your day at 07:18 and spent 2 hours 5 minutes in Visual Studio Code before lunch.",
  facts_used: FACTS, model: "google/gemma-4-e4b-with-a-long-name", cached: false, fallback: false, reason: null, in_progress: true,
};
const ANSWER = {
  answer: "Last week you watched 1 hour 50 minutes of YouTube after 11 pm.", facts_used: FACTS, tools_called: ["get_totals", "get_sleep"],
  chart: { kind: "bar", title: "YouTube after 11 pm per day", unit: "minutes", points: [{ label: "2026-09-14", value: 30 }, { label: "2026-09-15", value: 45 }] },
  model: "google/gemma-4-e4b-with-a-long-name", fallback: false, declined: false, reason: null,
};
const DEVICE = (device_id: string, name: string, device_type: string, has_token = true) => ({
  device_id, name, device_type, has_token, paired_at: at("08:00:00"), last_seen: at("13:59:40"), revoked_at: null, last_seq: 123456, event_count: 1_234_567, events_24h: 64,
});
const LAN = "http://192.168.100.200:8765";
// The hub's own Overview answers for 14 seeded days (made by the hub's code: e2e/fixtures).
const INSIGHTS = JSON.parse(readFileSync(join(process.cwd(), "e2e", "fixtures", "insights-overview.json"), "utf-8")) as Record<string, object>;
const FOOD_CALENDAR = JSON.parse(readFileSync(join(process.cwd(), "e2e", "fixtures", "insights-food-calendar.json"), "utf-8")) as Record<string, Record<string, object>>;
const FOCUS_SLEEP = JSON.parse(readFileSync(join(process.cwd(), "e2e", "fixtures", "insights-focus-sleep.json"), "utf-8")) as Record<string, Record<string, object>>;
const APPS_DEVICES = JSON.parse(readFileSync(join(process.cwd(), "e2e", "fixtures", "insights-apps-devices.json"), "utf-8")) as Record<string, Record<string, object>>;
const STREAKS_WRAPPED = JSON.parse(readFileSync(join(process.cwd(), "e2e", "fixtures", "streaks-wrapped.json"), "utf-8")) as Record<string, object>;

async function mockHub(page: Page, local: boolean) {
  await page.clock.setFixedTime(new Date("2026-09-25T14:00:00-04:00"));
  await page.route("**/api/**", (route) => route.fulfill({ status: 404, json: { error: { code: "not_found", message: "Not mocked" } } }));
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", profile: "demo", version: "0.1.0", local } }));
  await page.route("**/api/v1/timeline?**", (route) => route.fulfill({ json: TIMELINE }));
  await page.route("**/api/v1/insights/day?**", (route) => route.fulfill({ json: SUMMARY }));
  await page.route("**/api/v1/insights/overview?**", (route) => {
    const range = new URL(route.request().url()).searchParams.get("range") ?? "";
    return route.fulfill({ json: INSIGHTS[range] ?? INSIGHTS["7d"] });
  });
  for (const tab of ["apps", "devices", "focus", "sleep", "food", "calendar"]) {
    const answers = tab === "focus" || tab === "sleep" ? FOCUS_SLEEP : tab === "food" || tab === "calendar" ? FOOD_CALENDAR : APPS_DEVICES;
    await page.route(`**/api/v1/insights/${tab}?**`, (route) => {
      const range = new URL(route.request().url()).searchParams.get("range") ?? "";
      return route.fulfill({ json: answers[tab][range] ?? answers[tab]["7d"] });
    });
  }
  await page.route("**/api/v1/insights/apps/detail?**", (route) => route.fulfill({ json: APPS_DEVICES.detail["7d"] }));
  for (const name of ["streaks", "goals", "achievements", "wrapped"]) {
    await page.route(`**/api/v1/${name}?**`, (route) => route.fulfill({ json: STREAKS_WRAPPED[name] }));
  }
  await page.route("**/api/v1/ai/status", (route) => route.fulfill({ json: { base_url: "http://127.0.0.1:1234/v1", model: ANSWER.model, reachable: true, tool_calling: true, models: [ANSWER.model], error: null } }));
  await page.route("**/api/v1/story?**", (route) => route.fulfill({ json: STORY }));
  await page.route("**/api/v1/ask", (route) => route.fulfill({ json: ANSWER }));
  await page.route("**/api/v1/devices", (route) =>
    route.fulfill({ json: { devices: [DEVICE("android-1", "Galaxy phone", "android"), DEVICE("viewer-12", "A phone browser with a long name (Samsung Internet)", "viewer"), DEVICE("windows-1", "Desk PC", "windows", false)] } }),
  );
  await page.route("**/api/v1/pair/start", (route) =>
    route.fulfill({ json: { id: "a", code: "493817", expires_at: "2026-09-25T18:05:00Z", url: LAN, urls: [LAN], mdns_url: "http://daytrace-a-long-computer-name.local:8765", qr: "/api/v1/pair/qr.png" } }),
  );
  await page.route("**/api/v1/pair/status", (route) => route.fulfill({ json: { id: "a", active: true, used: false, claimed_by: null } }));
  await page.route("**/api/v1/pair/qr.png?**", (route) =>
    route.fulfill({ contentType: "image/png", body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=", "base64") }),
  );
}

/** What sticks out past the screen, or is cut off by a box that hides its overflow (and doesn't scroll). */
function problems(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const screen = document.documentElement.clientWidth;
    const found: string[] = [];
    const name = (element: Element) => {
      const text = (element.getAttribute("aria-label") || element.textContent || "").trim().replace(/\s+/g, " ").slice(0, 40);
      return `${element.tagName.toLowerCase()}${element.className && typeof element.className === "string" ? `.${element.className.split(" ")[0]}` : ""} "${text}"`;
    };
    if (document.documentElement.scrollWidth > screen + 1) found.push(`the page scrolls sideways (${document.documentElement.scrollWidth} > ${screen})`);
    for (const element of document.querySelectorAll("body *")) {
      if (element.closest("[aria-hidden=true], .visually-hidden, .info-pop:not(.open), svg, dialog:not([open])")) continue;
      const box = element.getBoundingClientRect();
      if (box.width < 1 || box.height < 1) continue;
      let clippedBy: Element | null = null;
      let scrolls = false;
      for (let parent = element.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
        const overflow = getComputedStyle(parent).overflowX;
        if (overflow === "auto" || overflow === "scroll") {
          scrolls = true; // a row that scrolls on purpose (a tab bar): what's past its edge can be reached
          break;
        }
        if ((overflow === "hidden" || overflow === "clip") && box.right > parent.getBoundingClientRect().right + 1) {
          clippedBy = parent;
          break;
        }
      }
      if (scrolls) continue;
      if (clippedBy) found.push(`${name(element)} is cut off by ${name(clippedBy)}`);
      else if (box.right > screen + 1) found.push(`${name(element)} sticks out past the screen (${Math.round(box.right)} > ${screen})`);
    }
    // Controls and headings keep their words whole: a word split across two lines ("Insigh / ts") fits, but reads
    // badly. Headings are held to it from 320 px, the width WCAG's reflow rule asks for: narrower, with 200% text, a
    // long heading word is wider than the screen itself and can only break (it still mustn't stick out or be cut).
    const whole = screen >= 320 ? "button, a, [role=tab], .badge, .stat-label, h1, h2, h3, .eyebrow" : "button, a, [role=tab], .badge, .stat-label";
    for (const element of document.querySelectorAll(whole)) {
      if (element.closest("[aria-hidden=true], .visually-hidden, svg, dialog:not([open])")) continue;
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const text = node.textContent ?? "";
        for (const match of text.matchAll(/\S+/g)) {
          const range = document.createRange();
          range.setStart(node, match.index ?? 0);
          range.setEnd(node, (match.index ?? 0) + match[0].length);
          const lines = new Set([...range.getClientRects()].filter((rect) => rect.width > 0).map((rect) => Math.round(rect.top)));
          if (lines.size > 1) found.push(`${name(element)}: the word "${match[0]}" is split across lines`);
        }
      }
    }
    return [...new Set(found)].slice(0, 12);
  });
}

async function sweep(page: Page, ready: () => Promise<void>) {
  const failures: string[] = [];
  for (const font of FONTS) {
    await page.evaluate((family) => document.documentElement.style.setProperty("--font", family || null), font);
    for (const width of WIDTHS) {
      await page.setViewportSize({ width, height: 900 });
      for (const scale of TEXT_SCALES) {
        await page.evaluate((value) => (document.documentElement.style.fontSize = `${value * 100}%`), scale);
        await ready();
        await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))); // charts resize
        const where = `${width} px, text ${scale * 100}%${font ? ", wide font" : ""}`;
        for (const problem of await problems(page)) failures.push(`${where}: ${problem}`);
      }
    }
  }
  expect(failures).toEqual([]);
}

test.use({ timezoneId: "America/Toronto", locale: "en-US" });
test.describe.configure({ mode: "parallel" });

test.beforeEach(async ({ page, isMobile }) => {
  test.skip(isMobile, "the sweep sets its own sizes");
  await page.emulateMedia({ reducedMotion: "reduce" });
});

test("Today fits every screen and text size", async ({ page }) => {
  test.slow();
  await mockHub(page, true);
  await page.goto("/");
  await sweep(page, () => expect(page.locator(".timeline-chart canvas")).toBeVisible());
});

test("Story fits every screen and text size", async ({ page }) => {
  test.slow();
  await mockHub(page, true);
  await page.goto("/story");
  await page.getByText("Facts used").click();
  await sweep(page, () => expect(page.locator(".story-text")).toBeVisible());
});

test("Ask, with an answer, fits every screen and text size", async ({ page }) => {
  test.slow();
  await mockHub(page, true);
  await page.goto("/ask");
  await page.getByRole("button", { name: "How much YouTube did I watch last week?" }).click();
  await page.locator(".bubble-answer").getByText("Facts used").click();
  await sweep(page, () => expect(page.locator(".answer-text")).toBeVisible());
});

test("Devices on the hub computer, with a code, fits every screen and text size", async ({ page }) => {
  test.slow();
  await mockHub(page, true);
  await page.goto("/devices");
  await page.getByRole("button", { name: "Show a pairing code" }).click();
  await sweep(page, () => expect(page.locator(".code-digits")).toBeVisible());
});

test("Devices on a phone fits every screen and text size", async ({ page }) => {
  test.slow();
  await mockHub(page, false);
  await page.goto("/devices");
  await sweep(page, () => expect(page.getByRole("region", { name: "Pair this device" })).toBeVisible());
});

test("Insights, with a custom range open, fits every screen and text size", async ({ page }) => {
  test.slow();
  await mockHub(page, true);
  await page.goto("/insights?range=7d");
  await page.getByRole("group", { name: "Time range" }).getByRole("button", { name: "Custom" }).click();
  await sweep(page, () => expect(page.getByRole("region", { name: "When screens were on" }).locator(".chart canvas").first()).toBeVisible());
});

test("Insights, Apps and Devices, fits every screen and text size", async ({ page }) => {
  test.slow();
  await mockHub(page, true);
  await page.goto("/insights?tab=apps&range=7d");
  await sweep(page, () => expect(page.getByRole("region", { name: "Switching between devices" }).locator(".chart canvas").first()).toBeVisible());
});

test("Insights, Focus and Sleep, fits every screen and text size", async ({ page }) => {
  test.slow();
  await mockHub(page, true);
  await page.goto("/insights?tab=focus&range=7d");
  await sweep(page, () => expect(page.getByRole("region", { name: "Late nights and the next day's focus" }).locator(".chart canvas").first()).toBeVisible());
});

test("Insights, Food and Calendar, fits every screen and text size", async ({ page }) => {
  test.slow();
  await mockHub(page, true);
  await page.goto("/insights?tab=food&range=7d");
  await sweep(page, () => expect(page.getByRole("region", { name: "Longest events" }).locator(".chart canvas").first()).toBeVisible());
});

test("one app's detail fits every screen and text size", async ({ page }) => {
  test.slow();
  await mockHub(page, true);
  await page.goto("/insights?tab=apps&range=7d&app=Visual%20Studio%20Code");
  await sweep(page, () => expect(page.getByRole("dialog").getByRole("region", { name: "When in the day" }).locator(".chart canvas").first()).toBeVisible());
});

test("Streaks, with a streak and a goal open, fits every screen and text size", async ({ page }) => {
  test.slow();
  await mockHub(page, true);
  await page.goto("/streaks");
  await page.locator(".streak-head").first().click();
  await page.getByRole("region", { name: "Bedtime" }).getByRole("button", { name: "Change the limit" }).click();
  await sweep(page, () => expect(page.locator(".streak-detail")).toBeVisible());
});

test("Wrapped fits every screen and text size", async ({ page }) => {
  test.slow();
  await mockHub(page, true);
  await page.goto("/wrapped");
  await sweep(page, () => expect(page.getByRole("button", { name: "Save as image" })).toBeEnabled());
});

test("the style guide fits every screen and text size", async ({ page }) => {
  test.slow();
  await mockHub(page, true);
  await page.goto("/styleguide");
  await sweep(page, () => expect(page.locator(".chart canvas").first()).toBeVisible());
});
