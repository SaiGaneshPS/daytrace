// DT-52: the design system in a real browser: layout on a 1440 px desktop and a 360 px phone, touch targets,
// accessibility (axe, light and dark), tabs, counting numbers, charts, the chart card, reduced motion, and a page
// that fails to load.
import { readFileSync } from "node:fs";
import AxeBuilder from "@axe-core/playwright";
import { expect, type Locator, type Page, test } from "@playwright/test";

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

/** Drag a finger (a pointer on the touch-emulated phone) from right to left across an element. */
async function swipeLeft(page: Page, element: Locator) {
  await element.scrollIntoViewIfNeeded();
  const box = (await element.boundingBox())!;
  const y = box.y + box.height / 2;
  await page.mouse.move(box.x + box.width * 0.8, y);
  await page.mouse.down();
  for (let step = 1; step <= 8; step++) await page.mouse.move(box.x + box.width * (0.8 - step * 0.07), y);
  await page.mouse.up();
}

/** Whether a canvas has a pixel of exactly this color (the base color between a mark's pattern stripes). */
function canvasHas(canvas: Locator, hex: string): Promise<boolean> {
  return canvas.evaluate((element, target) => {
    const drawing = element as HTMLCanvasElement;
    const pixels = drawing.getContext("2d")!.getImageData(0, 0, drawing.width, drawing.height).data;
    const [r, g, b] = [1, 3, 5].map((i) => parseInt(target.slice(i, i + 2), 16));
    for (let i = 0; i < pixels.length; i += 4) {
      if (pixels[i] === r && pixels[i + 1] === g && pixels[i + 2] === b && pixels[i + 3] === 255) return true;
    }
    return false;
  }, hex);
}

// --- layout and navigation ---------------------------------------------------------------------------------------

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
});

test("phone: the More sheet keeps focus and makes the page behind inert", async ({ page, isMobile }) => {
  test.skip(!isMobile, "phone layout");
  await page.goto("/");
  const more = page.locator(".bottom-nav").getByRole("button", { name: "More" });
  await more.tap();
  const sheet = page.getByRole("dialog", { name: "More pages" });
  await expect(sheet.getByRole("link", { name: "Wrapped" })).toBeFocused();
  await expect(page.locator(".main-column")).toHaveAttribute("inert", "");
  for (const name of ["Devices", "Privacy", "Wrapped"]) {
    await page.keyboard.press("Tab"); // round and round inside the sheet
    await expect(sheet.getByRole("link", { name })).toBeFocused();
  }
  await page.keyboard.press("Shift+Tab");
  await expect(sheet.getByRole("link", { name: "Privacy" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();
  await expect(more).toBeFocused(); // back where it was opened
  await expect(page.locator(".main-column")).not.toHaveAttribute("inert", "");
});

test("phone: every control can be tapped (44 px or more)", async ({ page, isMobile }) => {
  test.skip(!isMobile, "touch targets");
  await page.goto("/styleguide");
  await expect(page.locator(".chart canvas").first()).toBeVisible();
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

test("a page that fails to load says so, and the menus keep working", async ({ page, isMobile }) => {
  test.skip(isMobile, "once is enough");
  await page.route("**/assets/Insights-*.js", (route) => route.abort());
  await page.goto("/");
  const sidebar = page.locator(".sidebar");
  await sidebar.getByRole("link", { name: "Insights" }).click();
  await expect(page.getByRole("alert")).toContainText("This page couldn't load");
  await expect(page.getByRole("button", { name: "Reload" })).toBeVisible();
  await sidebar.getByRole("link", { name: "Story" }).click();
  await expect(page.locator("main h1")).toHaveText("Story");
});

// --- accessibility and themes ------------------------------------------------------------------------------------

for (const scheme of ["light", "dark"] as const) {
  test(`accessibility (axe) in ${scheme}`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    for (const path of ["/styleguide", "/", "/devices"]) {
      await page.goto(path);
      await expect(page.locator("main h1")).toBeVisible();
      if (path === "/styleguide") await expect(page.locator(".chart canvas").first()).toBeVisible();
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

test("charts take the new theme's colors when the scheme changes", async ({ page, isMobile }) => {
  test.skip(isMobile, "once is enough");
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
  await page.goto("/styleguide");
  const canvas = page.getByRole("region", { name: "Screen time by category" }).locator("canvas").first();
  await expect(canvas).toBeVisible();
  await expect.poll(() => canvasHas(canvas, "#3b82f6")).toBe(true); // light work blue
  await page.emulateMedia({ colorScheme: "dark" });
  const redrawn = page.getByRole("region", { name: "Screen time by category" }).locator("canvas").first();
  await expect.poll(() => canvasHas(redrawn, "#60a5fa")).toBe(true); // dark work blue
  expect(await canvasHas(redrawn, "#3b82f6")).toBe(false);
});

// --- components --------------------------------------------------------------------------------------------------

test("tabs: arrow keys, Home and End, and a visible focus ring on the panel", async ({ page }) => {
  await page.goto("/styleguide");
  const tabs = page.getByRole("tablist", { name: "Time range" });
  await tabs.getByRole("tab", { name: "Day" }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(tabs.getByRole("tab", { name: "Week" })).toHaveAttribute("aria-selected", "true");
  await expect(tabs.getByRole("tab", { name: "Week" })).toBeFocused();
  await expect(page.getByRole("tabpanel")).toContainText("Seven days side by side.");
  await page.keyboard.press("End");
  await expect(tabs.getByRole("tab", { name: "Month" })).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("ArrowRight"); // wraps round
  await expect(tabs.getByRole("tab", { name: "Day" })).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("tabpanel")).toBeFocused();
  await expect(page.getByRole("tabpanel")).toHaveCSS("outline-style", "solid");
});

test("tabs: a swipe moves to the next tab on a phone, but not one across a chart", async ({ page, isMobile }) => {
  test.skip(!isMobile, "swiping is for touch screens");
  await page.goto("/styleguide");
  await swipeLeft(page, page.getByRole("tabpanel"));
  await expect(page.getByRole("tab", { name: "Week" })).toHaveAttribute("aria-selected", "true");
  const chart = page.getByRole("tabpanel").locator(".chart");
  await expect(chart.locator("canvas")).toBeVisible();
  await swipeLeft(page, chart); // scrubbing the chart
  await expect(page.getByRole("tab", { name: "Week" })).toHaveAttribute("aria-selected", "true");
});

test("numbers count up, and jump straight there with reduced motion", async ({ page }) => {
  await page.goto("/styleguide");
  const number = page.locator(".hero-number .number");
  const shown = number.locator("span[aria-hidden=true]");
  await expect(number.locator(".visually-hidden")).toHaveText("155"); // screen readers get the value at once
  await expect(shown).toHaveText("155"); // once it has counted up

  const seen = await number.evaluate(async (element) => {
    const target = element.querySelector("[aria-hidden=true]")!;
    const values = new Set<string>();
    const observer = new MutationObserver(() => values.add(target.textContent ?? ""));
    observer.observe(target, { characterData: true, childList: true, subtree: true });
    [...document.querySelectorAll("button")].find((button) => button.textContent === "Change the number")!.click();
    await new Promise((resolve) => setTimeout(resolve, 1200));
    observer.disconnect();
    return [...values];
  });
  expect(seen.length).toBeGreaterThan(3); // it passed through values on the way
  expect(seen.at(-1)).toBe("240");
  expect(seen.every((value) => /^\d+$/.test(value))).toBe(true); // whole numbers all the way

  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.getByRole("button", { name: "Change the number" }).click();
  await expect(number.locator(".visually-hidden")).toHaveText("90");
  const [visible, spoken] = await number.evaluate((element) => [
    element.querySelector("[aria-hidden=true]")!.textContent,
    element.querySelector(".visually-hidden")!.textContent,
  ]);
  expect(visible).toBe(spoken); // no counting: the same render shows the new value
});

test("the chart card explains its metric and shows its state", async ({ page }) => {
  await page.goto("/styleguide");
  const card = page.getByRole("region", { name: "Screen time by category" });
  await expect(card.locator(".chart")).toHaveAttribute("aria-label", /Sample screen time by category/);
  await expect(card.getByText("Estimated", { exact: false }).first()).toBeVisible();
  const about = card.getByRole("button", { name: "About Screen time by category" });
  await about.click();
  await expect(about).toHaveAttribute("aria-expanded", "true");
  await expect(card.getByRole("status")).toContainText("counted once per device");
  await page.keyboard.press("Escape");
  await expect(about).toHaveAttribute("aria-expanded", "false");
  await expect(card.getByRole("status")).toBeEmpty(); // still there, so the next opening is announced
});

test("a chart appears once its card finishes loading", async ({ page }) => {
  await page.goto("/styleguide");
  const card = page.getByRole("region", { name: "Loading example" });
  await expect(card).toHaveAttribute("aria-busy", "true");
  await card.getByRole("button", { name: "Finish loading" }).click();
  await expect(card).not.toHaveAttribute("aria-busy", /./);
  await expect(card.locator(".chart canvas")).toBeVisible();
  await expect(card.locator(".chart")).toHaveAttribute("aria-label", /Sample share of the day by category/);
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

// --- files and screenshots ---------------------------------------------------------------------------------------

test("the Android palette matches the tokens, and both dark copies match", async ({ isMobile }) => {
  test.skip(isMobile, "a file check: once is enough");
  const css = readFileSync(new URL("../src/theme/tokens.css", import.meta.url), "utf-8");
  const xml = readFileSync(new URL("../../android/app/src/main/res/values/colors.xml", import.meta.url), "utf-8");
  const forced = css.indexOf(':root[data-theme="dark"]');
  const blocks = { light: css.slice(0, css.indexOf("@media")), system: css.slice(css.indexOf("@media"), forced), forced: css.slice(forced) };
  const variables = (block: string) =>
    Object.fromEntries([...block.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map((match) => [match[1], match[2].trim()]));
  expect(variables(blocks.forced)).toEqual(variables(blocks.system)); // the forced dark theme is the system one
  const android = Object.fromEntries([...xml.matchAll(/<color name="([a-z_]+)">#FF([0-9A-F]{6})<\/color>/g)].map((m) => [m[1], `#${m[2]}`]));
  const checked: string[] = [];
  for (const [block, suffix] of [[blocks.light, ""], [blocks.system, "_dark"]] as const) {
    for (const match of block.matchAll(/--cat-([a-z]+)(-ink)?: (#[0-9a-f]{6});/g)) {
      const name = `category_${match[1]}${match[2] ? "_ink" : ""}${suffix}`;
      expect(android[name], name).toBe(match[3].toUpperCase());
      checked.push(name);
    }
  }
  expect(checked).toHaveLength(32);
});

test("screenshots for review", async ({ page }, testInfo) => {
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await page.goto("/styleguide");
    await expect(page.locator(".chart canvas").first()).toBeVisible();
    await page.waitForTimeout(300); // the chart's own entrance
    await page.screenshot({ path: testInfo.outputPath(`styleguide-${scheme}.png`), fullPage: true });
  }
});
