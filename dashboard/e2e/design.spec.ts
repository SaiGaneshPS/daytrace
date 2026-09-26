// DT-52: the design system in a real browser: layout on a 1440 px desktop and a 360 px phone, touch targets,
// accessibility (axe, light and dark), tabs, counting numbers, the chart card and reduced motion.
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

const HEALTH = { status: "ok", profile: "demo", version: "0.1.0" };
const PAGES = [
  { label: "Today", path: "/", title: "Daytrace" },
  { label: "Story", path: "/story", title: "Story - Daytrace" },
  { label: "Ask", path: "/ask", title: "Ask - Daytrace" },
  { label: "Insights", path: "/insights", title: "Insights - Daytrace" },
  { label: "Wrapped", path: "/wrapped", title: "Wrapped - Daytrace" },
  { label: "Devices", path: "/devices", title: "Devices - Daytrace" },
  { label: "Privacy", path: "/privacy", title: "Privacy - Daytrace" },
];

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: HEALTH }));
});

async function expectNoAxeViolations(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(results.violations.map((v) => `${v.id} (${v.nodes.length}): ${v.help}`)).toEqual([]);
}

test("desktop: the sidebar leads to every page", async ({ page, isMobile }) => {
  test.skip(isMobile, "desktop layout");
  await page.goto("/");
  const sidebar = page.locator(".sidebar");
  await expect(sidebar).toBeVisible();
  await expect(page.locator(".bottom-nav")).toBeHidden();
  await expect(sidebar.locator(".hub-status")).toHaveText("demo hub");
  for (const item of PAGES) {
    await sidebar.getByRole("link", { name: item.label }).click();
    await expect(page).toHaveURL(item.path);
    await expect(page).toHaveTitle(item.title);
    await expect(sidebar.getByRole("link", { name: item.label })).toHaveAttribute("aria-current", "page");
  }
});

test("phone: the bottom nav leads to every page, the rest through More", async ({ page, isMobile }) => {
  test.skip(!isMobile, "phone layout");
  await page.goto("/");
  const nav = page.locator(".bottom-nav");
  await expect(nav).toBeVisible();
  await expect(page.locator(".sidebar")).toBeHidden();
  await nav.getByRole("link", { name: "Story" }).tap();
  await expect(page).toHaveURL("/story");
  await nav.getByRole("button", { name: "More" }).tap();
  const sheet = page.getByRole("dialog", { name: "More pages" });
  await expect(sheet).toBeVisible();
  await expect(sheet.getByRole("link", { name: "Wrapped" })).toBeFocused();
  await sheet.getByRole("link", { name: "Devices" }).tap();
  await expect(page).toHaveURL("/devices");
  await expect(sheet).toBeHidden();
  await expect(nav.getByRole("button", { name: "More" })).toHaveClass(/active/);
  await nav.getByRole("button", { name: "More" }).tap();
  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();
});

test("phone: every control can be tapped (44 px or more)", async ({ page, isMobile }) => {
  test.skip(!isMobile, "touch targets");
  await page.goto("/styleguide");
  await expect(page.locator(".chart canvas")).toBeVisible();
  const controls = page.locator(".bottom-nav a, .bottom-nav button, .topbar a, main button, main [role=tab]");
  const sizes = await controls.evaluateAll((elements) =>
    elements.map((element) => {
      const box = element.getBoundingClientRect();
      return { name: element.textContent?.trim() || element.getAttribute("aria-label"), width: box.width, height: box.height };
    }),
  );
  expect(sizes.length).toBeGreaterThan(8);
  expect(sizes.filter((size) => size.width < 44 || size.height < 44)).toEqual([]);
});

for (const scheme of ["light", "dark"] as const) {
  test(`accessibility (axe) in ${scheme}`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    for (const path of ["/styleguide", "/", "/devices"]) {
      await page.goto(path);
      await expect(page.locator("main h1")).toBeVisible();
      if (path === "/styleguide") await expect(page.locator(".chart canvas")).toBeVisible();
      await expectNoAxeViolations(page);
    }
  });
}

test("dark mode switches the tokens", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/styleguide");
  await expect(page.locator("body")).toHaveCSS("background-color", "rgb(11, 16, 32)");
  await page.emulateMedia({ colorScheme: "light" });
  await expect(page.locator("body")).toHaveCSS("background-color", "rgb(245, 246, 251)");
});

test("tabs: arrow keys, Home and End", async ({ page }) => {
  await page.goto("/styleguide");
  const tabs = page.getByRole("tablist", { name: "Time range" });
  await tabs.getByRole("tab", { name: "Day" }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(tabs.getByRole("tab", { name: "Week" })).toHaveAttribute("aria-selected", "true");
  await expect(tabs.getByRole("tab", { name: "Week" })).toBeFocused();
  await expect(page.getByRole("tabpanel")).toHaveText("Seven days side by side.");
  await page.keyboard.press("End");
  await expect(tabs.getByRole("tab", { name: "Month" })).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("ArrowRight"); // wraps round
  await expect(tabs.getByRole("tab", { name: "Day" })).toHaveAttribute("aria-selected", "true");
});

test("tabs: a swipe moves to the next tab on a phone", async ({ page, isMobile }) => {
  test.skip(!isMobile, "swiping is for touch screens");
  await page.goto("/styleguide");
  const panel = page.getByRole("tabpanel");
  await panel.scrollIntoViewIfNeeded();
  const box = (await panel.boundingBox())!;
  const y = box.y + box.height / 2;
  await page.mouse.move(box.x + box.width * 0.8, y);
  await page.mouse.down();
  for (let step = 1; step <= 8; step++) await page.mouse.move(box.x + box.width * (0.8 - step * 0.07), y);
  await page.mouse.up();
  await expect(page.getByRole("tab", { name: "Week" })).toHaveAttribute("aria-selected", "true");
});

test("numbers count up, and jump straight there with reduced motion", async ({ page }) => {
  await page.goto("/styleguide");
  const number = page.locator(".hero-number .number");
  const shown = number.locator("span[aria-hidden=true]");
  await expect(number.locator(".visually-hidden")).toHaveText("155"); // screen readers get the value at once
  await expect(shown).toHaveText("155"); // after counting up

  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.getByRole("button", { name: "Change the number" }).click();
  const target = await number.locator(".visually-hidden").textContent();
  expect(await shown.textContent()).toBe(target); // no counting
});

test("the chart card explains its metric and shows its state", async ({ page }) => {
  await page.goto("/styleguide");
  const card = page.getByRole("region", { name: "Screen time by category" });
  await expect(card.locator(".chart[aria-label]")).toHaveAttribute("aria-label", /Sample screen time by category/);
  await expect(card.getByText("Estimated", { exact: false }).first()).toBeVisible();
  const about = card.getByRole("button", { name: "About Screen time by category" });
  await about.click();
  await expect(about).toHaveAttribute("aria-expanded", "true");
  await expect(card.getByRole("status")).toContainText("counted once per device");
  await page.keyboard.press("Escape");
  await expect(about).toHaveAttribute("aria-expanded", "false");
  await expect(card.getByRole("status")).toBeEmpty(); // still there, so the next opening is announced
  await expect(page.getByRole("region", { name: "Loading example" })).toHaveAttribute("aria-busy", "true");
});

test("reduced motion stops the shimmer and the live dot", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/styleguide");
  await expect(page.locator(".skeleton").first()).toHaveCSS("animation-name", "none");
  const dot = page.locator(".hub-status.ok .live-dot").first();
  expect(await dot.evaluate((element) => getComputedStyle(element, "::after").animationName)).toBe("none");
});

test("a hub that doesn't answer is shown, with a way to try again", async ({ page }) => {
  await page.unroute("**/api/v1/health");
  await page.route("**/api/v1/health", (route) => route.fulfill({ status: 500, json: { error: { code: "x", message: "down" } } }));
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("down");
  await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
  await expect(page.locator(".hub-status").filter({ visible: true })).toHaveText("Hub unreachable");
});

test("screenshots for review", async ({ page }, testInfo) => {
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await page.goto("/styleguide");
    await expect(page.locator(".chart canvas")).toBeVisible();
    await page.waitForTimeout(300); // the chart's own entrance
    await page.screenshot({ path: testInfo.outputPath(`styleguide-${scheme}.png`), fullPage: true });
  }
});

test("the Android palette matches the tokens", async ({ isMobile }) => {
  test.skip(isMobile, "a file check: once is enough");
  const { readFileSync } = await import("node:fs");
  const css = readFileSync(new URL("../src/theme/tokens.css", import.meta.url), "utf-8");
  const xml = readFileSync(new URL("../../android/app/src/main/res/values/colors.xml", import.meta.url), "utf-8");
  const [light, dark] = [css.slice(0, css.indexOf("@media")), css.slice(css.indexOf(':root[data-theme="dark"]'))];
  const android = Object.fromEntries([...xml.matchAll(/<color name="([a-z_]+)">#FF([0-9A-F]{6})<\/color>/g)].map((m) => [m[1], `#${m[2]}`]));
  const checked: string[] = [];
  for (const [block, suffix] of [[light, ""], [dark, "_dark"]] as const) {
    for (const match of block.matchAll(/--cat-([a-z]+)(-ink)?: (#[0-9a-f]{6});/g)) {
      const name = `category_${match[1]}${match[2] ? "_ink" : ""}${suffix}`;
      expect(android[name], name).toBe(match[3].toUpperCase());
      checked.push(name);
    }
  }
  expect(checked).toHaveLength(32);
});
