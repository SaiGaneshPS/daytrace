// DT-54: Wrapped, with the hub mocked by its own answer for the seeded week of 14 to 20 September 2026
// (e2e/fixtures/streaks-wrapped.json, made and checked by the hub's tests): the card's numbers, the week in the
// address, the PNG download, the share sheet, plain lines, waiting and errors, the card holding its text at any size,
// and accessibility. The pages run under the hub's Content-Security-Policy (vite preview sends it), so an image
// the hub would refuse fails here too. The clock starts at 21:00 on Friday 25 September 2026 in Toronto.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";
import type { components } from "../src/api/schema";

type Wrapped = components["schemas"]["Wrapped"];
const FIXTURE = (JSON.parse(readFileSync(join(process.cwd(), "e2e", "fixtures", "streaks-wrapped.json"), "utf-8")) as { wrapped: Wrapped }).wrapped;
const WEEKS: Record<string, [string, string]> = {
  "2026-W37": ["2026-09-07", "2026-09-13"],
  "2026-W38": ["2026-09-14", "2026-09-20"],
  "2026-W39": ["2026-09-21", "2026-09-27"], // this week
};

test.use({ timezoneId: "America/Toronto", locale: "en-US" });

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
const metric = (id: string) => FIXTURE.metrics.find((item) => item.id === id)?.value as number;

type Answer = Wrapped | "fail" | Promise<Wrapped>;
async function mockHub(page: Page, answer?: (week: string) => Answer) {
  const asked: string[] = [];
  // The clock runs (from Friday 21:00): a fixed one stops performance.now(), and the card's reveal with it.
  await page.clock.install({ time: new Date("2026-09-25T21:00:00-04:00") });
  await page.route("**/api/**", (route) => route.fulfill({ status: 404, json: { error: { code: "not_found", message: "Not mocked" } } }));
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", profile: "demo", version: "0.1.0", local: true } }));
  await page.route("**/api/v1/wrapped?**", async (route) => {
    const week = new URL(route.request().url()).searchParams.get("week") ?? "";
    asked.push(week);
    const [first, last] = WEEKS[week] ?? [FIXTURE.first, FIXTURE.last];
    const found = await (answer?.(week) ?? { ...FIXTURE, week, first, last, in_progress: week === "2026-W39" });
    if (found === "fail") return route.fulfill({ status: 500, json: { error: { code: "internal_error", message: "The hub had a problem", details: [] } } });
    return route.fulfill({ json: found });
  });
  return asked;
}

/** Counts each drawing of an image (canvas.toBlob), and each copy html-to-image made of the card while a piece was still coming in. */
async function watchDrawing(page: Page) {
  await page.addInitScript(() => {
    const seen = { drawn: 0, copiedHalfShown: 0, copied: 0 };
    (window as unknown as { seen: typeof seen }).seen = seen;
    const toBlob = HTMLCanvasElement.prototype.toBlob;
    HTMLCanvasElement.prototype.toBlob = function (...args: Parameters<HTMLCanvasElement["toBlob"]>) {
      seen.drawn += 1;
      return toBlob.apply(this, args);
    };
    const cloneNode = Node.prototype.cloneNode;
    Node.prototype.cloneNode = function (deep?: boolean) {
      if (this instanceof HTMLElement && this.classList.contains("wrapped-card")) {
        seen.copied += 1;
        if (this.getAnimations({ subtree: true }).some((animation) => animation.playState !== "finished")) seen.copiedHalfShown += 1;
      }
      return cloneNode.call(this, deep);
    };
  });
}
const seen = (page: Page) => page.evaluate(() => (window as unknown as { seen: { drawn: number; copiedHalfShown: number; copied: number } }).seen);

test("the card is the hub's week, with the model's three lines", async ({ page }) => {
  const asked = await mockHub(page);
  await page.goto("/wrapped");
  const card = page.getByRole("article", { name: `Your week, ${await longDayIn(page, "2026-09-14")} to ${await longDayIn(page, "2026-09-20")}` });
  await expect(card).toBeVisible();
  expect(asked).toEqual(["2026-W38"]); // the last whole week
  await expect(card.locator(".wrapped-eyebrow")).toHaveText(`Your week${await longDayIn(page, "2026-09-14")} to ${await longDayIn(page, "2026-09-20")}`);
  await expect(card.locator(".wrapped-hero")).toHaveText(`${minutes(metric("screen_time"))}on screens, ${minutes(metric("daily_average"))} a day`);
  const stats = card.locator(".wrapped-stats div");
  await expect(stats).toHaveText([
    `Focused${minutes(metric("focused_time"))}`,
    `Focus score${Math.round(metric("focus_score"))}`,
    `Sleep a night${minutes(metric("sleep"))}`,
    `After 11 pm${minutes(metric("late_night"))} a night`,
  ]);
  await expect(card.getByRole("region", { name: "Top apps" }).locator("li")).toHaveText(FIXTURE.top_apps.map((app) => `${app.app}${minutes(app.minutes)}`));
  await expect(card.getByRole("region", { name: "Streaks this week" }).locator("li")).toHaveText(
    FIXTURE.streaks.map((streak) => `${streak.name}${streak.met} of ${streak.days_with_data} days`),
  );
  await expect(card.getByRole("list", { name: "The week in three lines" }).locator("li")).toHaveText(FIXTURE.lines);
  await expect(card.locator(".wrapped-foot")).toHaveText(`Lines by ${FIXTURE.model}, on this computer · Daytrace`);
  await expect(page.locator(".wrapped-reason")).toHaveCount(0);
  const box = (await card.boundingBox())!;
  expect(box.height / box.width).toBeGreaterThanOrEqual(16 / 9 - 0.01); // at least 9:16, a phone story's shape
});

test("the week is in the address, and the arrows step through the weeks up to this one", async ({ page }) => {
  const asked = await mockHub(page);
  await page.goto("/wrapped");
  const before = page.getByRole("button", { name: "The week before" });
  const after = page.getByRole("button", { name: "The week after" });
  await expect(page.locator(".week-name")).toHaveText(`${await longDayIn(page, "2026-09-14")} to ${await longDayIn(page, "2026-09-20")}`);
  await before.click();
  await expect(page).toHaveURL("/wrapped?week=2026-W37");
  await expect(page.getByRole("article")).toHaveAccessibleName(`Your week, ${await longDayIn(page, "2026-09-07")} to ${await longDayIn(page, "2026-09-13")}`);
  await after.click();
  await after.click();
  await expect(page).toHaveURL("/wrapped?week=2026-W39");
  await expect(page.locator(".wrapped-eyebrow")).toContainText("Your week, so far"); // this week isn't over
  await expect(after).toBeDisabled();
  await page.goBack();
  await expect(page).toHaveURL("/wrapped?week=2026-W38");
  await expect(page.getByRole("article")).toHaveAccessibleName(`Your week, ${await longDayIn(page, "2026-09-14")} to ${await longDayIn(page, "2026-09-20")}`);
  expect(asked).toEqual(["2026-W38", "2026-W37", "2026-W38", "2026-W39", "2026-W38"]); // the hub keeps each week (cached), so asking again is cheap
});

for (const week of ["2026-W40", "2026-W54", "soon"]) {
  test(`?week=${week} (a week to come, or none) shows the last whole week`, async ({ page }) => {
    const asked = await mockHub(page);
    await page.goto(`/wrapped?week=${week}`);
    await expect(page.getByRole("article")).toBeVisible();
    expect(asked).toEqual(["2026-W38"]);
  });
}

test("Save as image downloads the card as a PNG, drawn once it is fully shown", async ({ page }) => {
  await watchDrawing(page);
  await mockHub(page);
  await page.goto("/wrapped");
  const save = page.getByRole("button", { name: "Save as image" });
  await expect(save).toBeEnabled();
  await expect.poll(async () => (await seen(page)).drawn).toBe(1); // drawn ahead, after the reveal
  const download = page.waitForEvent("download");
  await save.click();
  const file = await download;
  expect(file.suggestedFilename()).toBe("daytrace-wrapped-2026-W38.png");
  const png = readFileSync((await file.path())!);
  expect([...png.subarray(0, 8)]).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  const box = (await page.locator(".wrapped-card").boundingBox())!;
  expect(Math.abs(png.readUInt32BE(16) - 2 * box.width)).toBeLessThanOrEqual(2); // twice the card, sharp on a phone
  expect(Math.abs(png.readUInt32BE(20) - 2 * box.height)).toBeLessThanOrEqual(2);
  await expect(page.locator(".wrapped-actions .field-note")).toHaveText("Saved as daytrace-wrapped-2026-W38.png.");
  expect(await seen(page)).toEqual({ drawn: 1, copied: 1, copiedHalfShown: 0 });
});

test("Share hands the share sheet the PNG it drew before the tap", async ({ page }) => {
  await watchDrawing(page);
  await page.addInitScript(() => {
    const shared: object[] = [];
    (window as unknown as { shared: object[] }).shared = shared;
    Object.defineProperty(navigator, "canShare", { configurable: true, value: (data: ShareData) => Boolean(data.files?.length) });
    Object.defineProperty(navigator, "share", {
      configurable: true,
      value: async (data: ShareData) => {
        const file = data.files![0];
        shared.push({ name: file.name, type: file.type, title: data.title, head: [...new Uint8Array(await file.slice(0, 4).arrayBuffer())] });
      },
    });
  });
  await mockHub(page);
  await page.goto("/wrapped");
  await expect.poll(async () => (await seen(page)).drawn).toBe(1);
  await page.getByRole("button", { name: "Share" }).click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { shared: object[] }).shared)).toEqual([
    { name: "daytrace-wrapped-2026-W38.png", type: "image/png", title: "My week on Daytrace", head: [0x89, 0x50, 0x4e, 0x47] },
  ]);
  expect((await seen(page)).drawn).toBe(1); // the share sheet didn't wait for a drawing
  await expect(page.locator(".wrapped-actions .field-note")).toHaveText("");
});

test("closing the share sheet says nothing, and a browser that can't share images says to save instead", async ({ page }) => {
  await page.addInitScript(() => {
    const state = { can: true };
    (window as unknown as { state: typeof state }).state = state;
    Object.defineProperty(navigator, "canShare", { configurable: true, value: () => state.can });
    Object.defineProperty(navigator, "share", { configurable: true, value: async () => Promise.reject(new DOMException("Share canceled", "AbortError")) });
  });
  await mockHub(page);
  await page.goto("/wrapped");
  const share = page.getByRole("button", { name: "Share" });
  await share.click();
  await expect(share).toBeEnabled();
  await expect(page.locator(".wrapped-actions .field-note")).toHaveText("");
  await page.evaluate(() => ((window as unknown as { state: { can: boolean } }).state.can = false));
  await share.click();
  await expect(page.locator(".wrapped-actions .field-note")).toHaveText("This browser can't share images: save it instead.");
});

test("without file sharing there is no Share button, only Save", async ({ page }) => {
  await page.addInitScript(() => {
    delete (Navigator.prototype as Partial<Navigator>).share;
    delete (Navigator.prototype as Partial<Navigator>).canShare;
  });
  await mockHub(page);
  await page.goto("/wrapped");
  await expect(page.getByRole("button", { name: "Save as image" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Share" })).toHaveCount(0);
});

test("plain lines say so, and why", async ({ page }) => {
  const reason = "the lines from the local model could not be used, even after a retry: it had 1 line instead of 3";
  await mockHub(page, () => ({ ...FIXTURE, model: null, fallback: true, reason }));
  await page.goto("/wrapped");
  await expect(page.locator(".wrapped-foot")).toHaveText("Plain lines from your numbers · Daytrace");
  await expect(page.locator(".wrapped-reason")).toHaveText(`Plain lines: ${reason}.`);
});

test("while the week is written it says so, and a failure can be tried again", async ({ page }) => {
  let release: () => void = () => undefined;
  const written = new Promise<Wrapped>((resolve) => (release = () => resolve(FIXTURE)));
  let fail = true;
  const asked = await mockHub(page, () => (fail ? "fail" : written));
  await page.goto("/wrapped");
  await expect(page.getByText("This week couldn't load: The hub had a problem")).toBeVisible();
  fail = false;
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Writing your week" })).toBeVisible();
  release();
  await expect(page.getByRole("article")).toBeVisible();
  expect(asked).toEqual(["2026-W38", "2026-W38"]);
});

test("the card holds all its text, and the week picker stays on one row, at every width and text size", async ({ page, isMobile }) => {
  test.skip(isMobile, "sets its own sizes");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockHub(page);
  await page.goto("/wrapped");
  await expect(page.getByRole("article")).toBeVisible();
  const failures: string[] = [];
  for (const width of [280, 320, 360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    for (const scale of [1, 1.5, 2]) {
      await page.evaluate((value) => (document.documentElement.style.fontSize = `${value * 100}%`), scale);
      const spills = await page.evaluate(() => {
        const card = document.querySelector(".wrapped-card")!.getBoundingClientRect();
        return [...document.querySelectorAll(".wrapped-card *")]
          .filter((element) => {
            const box = element.getBoundingClientRect();
            return box.width > 0 && (box.bottom > card.bottom + 0.5 || box.right > card.right + 0.5 || box.left < card.left - 0.5);
          })
          .map((element) => (element.textContent ?? "").trim().slice(0, 30));
      });
      for (const spill of spills) failures.push(`${width} px, text ${scale * 100}%: "${spill}" spills out of the card`);
      const arrows = await page.locator(".week-picker .icon-button").evaluateAll((buttons) => buttons.map((button) => Math.round(button.getBoundingClientRect().top)));
      if (arrows[0] !== arrows[1]) failures.push(`${width} px, text ${scale * 100}%: the week's arrows are on different rows`);
    }
  }
  expect(failures).toEqual([]);
});

for (const scheme of ["light", "dark"] as const) {
  test(`Wrapped passes axe (${scheme})`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await mockHub(page);
    await page.goto("/wrapped");
    await expect(page.getByRole("button", { name: "Save as image" })).toBeEnabled();
    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
    expect(results.violations.map((v) => `${v.id} (${v.nodes.length}): ${v.help} ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)).toEqual([]);
  });
}
