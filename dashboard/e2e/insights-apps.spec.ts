// DT-55: the Insights Apps and Devices tab, with the hub mocked by its own answers for 14 seeded days
// (e2e/fixtures, made and checked by the hub's tests): every chart and how fast it draws, the treemap adding up to
// the Overview, the leaderboard in order with each app's week and change, one app's detail (from the leaderboard,
// the treemap and the address), switching between devices, the sync strip showing gaps as no data, errors, and accessibility.
// The clock is fixed at 21:00 on Friday 25 September 2026 in Toronto, as when the fixtures were made.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

type Item = { name: string; value: number; category: string | null; spark?: (number | null)[] | null; children?: Item[] | null; change?: Change | null };
type Change = { id: string; direction: string; better: string; change_pct: number | null; delta: number; now: number; before: number; unit: string; label: string };
type Tab = {
  range: { first: string; last: string; days: number; label: string };
  metrics: { id: string; label: string; value: number | string | null }[];
  series: Record<string, { x?: string[] | null; y?: string[] | null; items?: Item[] | null; cells?: { x: number; y: number; value: number }[] | null;
    links?: { source: string; target: string; value: number }[] | null; lines?: { key?: string | null; values: (number | null)[] }[] | null }>;
};
const read = (name: string) => JSON.parse(readFileSync(join(process.cwd(), "e2e", "fixtures", name), "utf-8"));
const APPS = read("insights-apps-devices.json") as { apps: Record<string, Tab>; devices: Record<string, Tab>; detail: Record<string, Tab & { app: string }> };
const OVERVIEW = read("insights-overview.json") as Record<string, Tab>;

// The page clock is fixed, and charts animate by it, so an animation would stay on its first frame (a Sankey's
// flows not drawn at all): with reduced motion every chart draws its last frame at once.
test.use({ timezoneId: "America/Toronto", locale: "en-US", reducedMotion: "reduce" });

const metricOf = (data: Tab, id: string) => data.metrics.find((item) => item.id === id)?.value;
function minutes(value: number): string {
  const whole = Math.round(value);
  const hours = Math.floor(whole / 60);
  const rest = whole % 60;
  return !hours ? `${rest}m` : rest ? `${hours}h ${rest}m` : `${hours}h`;
}

type Answers = { apps?: (range: string) => object | "fail"; devices?: (range: string) => object | "fail"; detail?: (app: string, range: string) => object | "fail" };

async function mockHub(page: Page, answers: Answers = {}) {
  const asked: string[] = [];
  const pick = (table: Record<string, Tab>, range: string) => table[range] ?? table["7d"];
  const reply = (found: object | "fail") =>
    found === "fail" ? { status: 500, json: { error: { code: "internal_error", message: "The hub had a problem", details: [] } } } : { json: found };
  await page.clock.setFixedTime(new Date("2026-09-25T21:00:00-04:00"));
  await page.route("**/api/**", (route) => route.fulfill({ status: 404, json: { error: { code: "not_found", message: "Not mocked" } } }));
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", profile: "demo", version: "0.1.0", local: true } }));
  await page.route("**/api/v1/insights/apps?**", (route) => {
    const range = new URL(route.request().url()).searchParams.get("range") ?? "";
    asked.push(`apps ${range}`);
    return route.fulfill(reply(answers.apps?.(range) ?? pick(APPS.apps, range)));
  });
  await page.route("**/api/v1/insights/devices?**", (route) => {
    const range = new URL(route.request().url()).searchParams.get("range") ?? "";
    asked.push(`devices ${range}`);
    return route.fulfill(reply(answers.devices?.(range) ?? pick(APPS.devices, range)));
  });
  await page.route("**/api/v1/insights/apps/detail?**", (route) => {
    const params = new URL(route.request().url()).searchParams;
    const app = params.get("app") ?? "";
    asked.push(`detail ${app}`);
    const base = APPS.detail["7d"];
    return route.fulfill(reply(answers.detail?.(app, params.get("range") ?? "") ?? { ...base, app }));
  });
  return asked;
}

/** Whether the tab's four charts have painted something (a chart may use several canvases, made before it draws). */
const chartsDrawn = () =>
  [...document.querySelectorAll(".chart")].filter((chart) =>
    [...chart.querySelectorAll("canvas")].some((canvas) => {
      if (!canvas.width || !canvas.height) return false;
      const pixels = canvas.getContext("2d")?.getImageData(0, 0, canvas.width, canvas.height).data ?? [];
      for (let alpha = 3; alpha < pixels.length; alpha += 4 * 37) if (pixels[alpha]) return true;
      return false;
    }),
  ).length >= 3;

async function settled(page: Page) {
  await page.evaluate(() =>
    Promise.all(document.getAnimations().filter((a) => a.effect?.getTiming().iterations !== Infinity).map((a) => a.finished.catch(() => undefined))),
  );
}

test("every chart draws 14 seeded days within 1.5 s of the hub's answers", async ({ page }) => {
  await mockHub(page);
  let answeredAt: number | null = null;
  page.on("response", (response) => {
    if (/\/api\/v1\/insights\/(apps|devices)\?/.test(response.url())) answeredAt = performance.now(); // the later of the two
  });
  await page.goto("/insights?tab=apps&range=14d");
  await page.waitForFunction(chartsDrawn);
  expect(answeredAt).not.toBeNull();
  expect(performance.now() - (answeredAt ?? 0)).toBeLessThan(1500);
  for (const title of ["Categories and their apps", "Each device by day", "Switching between devices"]) {
    await expect(page.getByRole("region", { name: title }).locator(".chart canvas").first()).toBeVisible();
  }
  await expect(page.getByRole("region", { name: "When each device sent data" }).locator(".sync-row")).toHaveCount(4);
});

test("the treemap adds up to the Overview's total, and the cards are the hub's numbers", async ({ page }) => {
  for (const range of ["7d", "14d"]) {
    const treemap = APPS.apps[range].series.treemap.items ?? [];
    const total = Number(metricOf(OVERVIEW[range], "screen_time"));
    expect(Math.abs(treemap.reduce((sum, item) => sum + item.value, 0) - total)).toBeLessThanOrEqual(0.005 * treemap.length); // each box rounded
  }
  await mockHub(page);
  await page.goto("/insights?tab=apps&range=7d");
  const apps = APPS.apps["7d"];
  const devices = APPS.devices["7d"];
  await expect(page.getByRole("region", { name: "Apps and sites used" })).toContainText(String(metricOf(apps, "apps_used")));
  const top = page.getByRole("region", { name: "Most used" });
  await expect(top).toContainText(minutes(Number(metricOf(apps, "top_app_time"))));
  await expect(top).toContainText(String(metricOf(apps, "top_app")));
  await expect(page.getByRole("region", { name: "On the phone" })).toContainText(`${metricOf(devices, "phone_share")}%`);
  const handoffs = page.getByRole("region", { name: "Device switches" });
  await expect(handoffs).toContainText(String(metricOf(devices, "handoffs")));
  await expect(handoffs).toContainText(`Most: ${metricOf(devices, "top_handoff")}`);
});

test("the leaderboard is in order, with each app's week and change", async ({ page }) => {
  await mockHub(page);
  await page.goto("/insights?tab=apps&range=7d");
  const items = APPS.apps["7d"].series.leaderboard.items ?? [];
  expect(items.map((item) => item.value)).toEqual([...items.map((item) => item.value)].sort((a, b) => b - a));
  const rows = page.getByRole("region", { name: "Top apps and sites" }).locator(".leader");
  await expect(rows).toHaveCount(items.length);
  for (const [index, item] of items.entries()) {
    const row = rows.nth(index);
    await expect(row.locator(".leader-app")).toHaveText(item.name);
    await expect(row.locator(".leader-time")).toHaveText(minutes(item.value));
    await expect(row.locator(".spark")).toBeVisible();
    await expect(row.locator(".leader-spark")).toHaveAttribute("title", /^Sat 19: .*, Fri 25: /); // the 7 days to the range's end
    const change = item.change;
    if (!change || change.direction === "same") continue;
    const tone = change.better === "neutral" ? /change-neutral/ : change.direction === change.better ? /change-good/ : /change-bad/;
    await expect(row.locator(".change")).toHaveClass(tone);
  }
});

test("an app that is neither work nor a distraction changes without a verdict", async ({ page }) => {
  const base = APPS.apps["7d"];
  const leaderboard = base.series.leaderboard;
  const first = (leaderboard.items ?? [])[0];
  const neutral = { ...first, category: "comms", change: { ...(first.change as Change), better: "neutral", direction: "up", change_pct: 12 } };
  await mockHub(page, {
    apps: () => ({ ...base, series: { ...base.series, leaderboard: { ...leaderboard, items: [neutral, ...(leaderboard.items ?? []).slice(1)] } } }),
  });
  await page.goto("/insights?tab=apps&range=7d");
  const chip = page.locator(".leader").first().locator(".change");
  await expect(chip).toHaveClass(/change-neutral/);
  await expect(chip).toContainText("up 12%");
  await expect(chip).not.toContainText("good change");
  await expect(chip).not.toContainText("wrong way");
});

test("one app's detail opens from the leaderboard, the treemap and the address", async ({ page }) => {
  const asked = await mockHub(page);
  await page.goto("/insights?tab=apps&range=7d");
  const leader = (APPS.apps["7d"].series.leaderboard.items ?? [])[0];
  const detailData = APPS.detail["7d"];
  await page.getByRole("button", { name: `Details for ${leader.name}` }).click();
  const drawer = page.getByRole("dialog", { name: leader.name });
  await expect(drawer).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`app=${encodeURIComponent(leader.name).replace(/%20/g, "(\\+|%20)")}`));
  await expect(drawer.getByRole("region", { name: "In the range" })).toContainText(minutes(Number(metricOf(detailData, "total"))));
  await expect(drawer.getByRole("region", { name: "In the range" })).toContainText(minutes(leader.value)); // the leaderboard's own number
  await expect(drawer.getByRole("region", { name: "Days used" })).toContainText(String(metricOf(detailData, "days_used")));
  await expect(drawer.getByRole("region", { name: "Longest stretch" })).toContainText(minutes(Number(metricOf(detailData, "longest"))));
  await expect(drawer.getByRole("region", { name: "Each day" }).locator(".chart canvas").first()).toBeVisible();
  await expect(drawer.getByRole("region", { name: "When in the day" }).locator(".chart canvas").first()).toBeVisible();
  expect(asked).toContain(`detail ${leader.name}`);
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(page).not.toHaveURL(/app=/);

  // A reload with the app in the address opens it again, and Back closes it.
  await page.goto(`/insights?tab=apps&range=7d&app=${encodeURIComponent("netflix.com")}`);
  await expect(page.getByRole("dialog", { name: "netflix.com" })).toBeVisible();
  await page.getByRole("dialog", { name: "netflix.com" }).getByRole("button", { name: "Close" }).click();
  await expect(page.getByRole("dialog")).toBeHidden();
  await page.goBack();
  await expect(page.getByRole("dialog", { name: "netflix.com" })).toBeVisible();
  await page.goBack();
  await expect(page.getByRole("dialog")).toBeHidden();

  // In the treemap, an app opens its detail: the biggest box (Visual Studio Code, at the left inside Work).
  const treemap = page.getByRole("region", { name: "Categories and their apps" }).locator(".chart");
  await page.waitForFunction(chartsDrawn);
  const box = await treemap.boundingBox();
  if (!box) throw new Error("no treemap");
  await treemap.click({ position: { x: box.width * 0.12, y: box.height * 0.4 } }); // scrolled to first
  await expect(page.getByRole("dialog", { name: "Visual Studio Code" })).toBeVisible();
  await expect(page).toHaveURL(/app=Visual(\+|%20)Studio(\+|%20)Code/);
});

test("switching between devices lists the most common switches", async ({ page }) => {
  await mockHub(page);
  await page.goto("/insights?tab=apps&range=7d");
  const links = APPS.devices["7d"].series.handoffs.links ?? [];
  const list = page.getByRole("region", { name: "Switching between devices" }).locator(".handoff-list li");
  await expect(list).toHaveCount(Math.min(5, links.length));
  for (const [index, link] of links.slice(0, 5).entries()) {
    await expect(list.nth(index)).toContainText(`${link.source}, ${link.target}`);
    await expect(list.nth(index)).toContainText(link.value === 1 ? "once" : `${link.value} times`);
  }
});

test("a day a device sent nothing shows as no data on the strip, never as none used", async ({ page }) => {
  const base = APPS.devices["7d"];
  const strip = base.series.sync;
  const iphone = (strip.y ?? []).indexOf("iPhone (demo)");
  const cells = (strip.cells ?? []).map((cell) => (cell.y === iphone && cell.x === 1 ? { ...cell, value: 0 } : cell)); // Sun 20: nothing
  const spare = (strip.y ?? []).length;
  const unpaired = [0, 1, 2].map((x) => ({ x: x + 4, y: spare, value: 0 })); // paired on Wed 23, and nothing since
  await mockHub(page, {
    devices: () => ({ ...base, series: { ...base.series, sync: { ...strip, y: [...(strip.y ?? []), "Spare phone"], cells: [...cells, ...unpaired] } } }),
  });
  await page.goto("/insights?tab=apps&range=7d");
  const rows = page.getByRole("region", { name: "When each device sent data" }).locator(".sync-row");
  const phone = rows.filter({ hasText: "iPhone (demo)" });
  await expect(phone.locator(".sync-none")).toHaveCount(1);
  await expect(phone.locator(".sync-sent")).toHaveCount(6);
  await expect(phone.locator(".sync-cell").nth(1)).toHaveAttribute("title", "Sun 20: no data");
  await expect(phone.locator(".visually-hidden")).toHaveText("Sent data on 6 of 7 days; no data on Sun 20.");
  const extra = rows.filter({ hasText: "Spare phone" });
  await expect(extra.locator(".sync-off")).toHaveCount(4);
  await expect(extra.locator(".sync-none")).toHaveCount(3);
  await expect(extra.locator(".visually-hidden")).toHaveText("Sent data on 0 of 7 days; no data on Wed 23, Thu 24, Fri 25.");
});

test("when the apps can't load, the devices still show, and Try again asks again", async ({ page }) => {
  let fail = true;
  const asked = await mockHub(page, { apps: (range) => (fail ? "fail" : APPS.apps[range] ?? APPS.apps["7d"]) });
  await page.goto("/insights?tab=apps&range=7d");
  await expect(page.getByText("The apps couldn't load: The hub had a problem")).toBeVisible();
  await expect(page.getByRole("region", { name: "Each device by day" }).locator(".chart canvas").first()).toBeVisible();
  await expect(page.getByRole("region", { name: "Apps and sites used" })).toContainText("Couldn't load");
  fail = false;
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByRole("region", { name: "Top apps and sites" }).locator(".leader")).toHaveCount(10);
  expect(asked.filter((item) => item === "apps 7d")).toHaveLength(2);
});

for (const scheme of ["light", "dark"] as const) {
  test(`the Apps and Devices tab and an app's detail pass axe (${scheme})`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await mockHub(page);
    await page.goto("/insights?tab=apps&range=7d");
    await page.waitForFunction(chartsDrawn);
    await settled(page);
    const tab = await new AxeBuilder({ page }).analyze();
    expect(tab.violations.map((violation) => `${violation.id}: ${violation.nodes.map((node) => node.target).join(", ")}`)).toEqual([]);
    await page.getByRole("button", { name: /^Details for/ }).first().click();
    await expect(page.getByRole("dialog").locator(".chart canvas").first()).toBeVisible();
    await settled(page);
    const drawer = await new AxeBuilder({ page }).analyze();
    expect(drawer.violations.map((violation) => `${violation.id}: ${violation.nodes.map((node) => node.target).join(", ")}`)).toEqual([]);
  });
}
