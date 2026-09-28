// DT-58: embed mode, for the Android app's Dashboard tab: no sidebar, top bar or bottom nav, for the whole visit.
import { expect, type Page, test } from "@playwright/test";

async function mockHub(page: Page) {
  await page.route("**/api/v1/**", (route) => {
    if (new URL(route.request().url()).pathname === "/api/v1/health") {
      return route.fulfill({ json: { status: "ok", profile: "personal", version: "0.1.0", local: false } });
    }
    return route.fulfill({ status: 404, json: { error: { code: "not_found", message: "not in this test" } } });
  });
}

test("inside the app the dashboard leaves out its own navigation, on every page it opens", async ({ page }) => {
  await mockHub(page);
  await page.goto("/?embed=1");
  await expect(page.locator("main")).toBeVisible();
  await expect(page.locator(".sidebar, .topbar, .bottom-nav")).toHaveCount(0);
  await expect(page.locator(".app")).toHaveClass(/embedded/);
  // The app opens the next page without the query: still embedded.
  await page.goto("/story");
  await expect(page.locator("main")).toBeVisible();
  await expect(page.locator(".sidebar, .topbar, .bottom-nav")).toHaveCount(0);
});

test("a browser that never asked keeps its navigation", async ({ page }) => {
  await mockHub(page);
  await page.goto("/story");
  await expect(page.locator("main")).toBeVisible();
  await expect(page.locator(".app")).not.toHaveClass(/embedded/);
  expect(await page.locator(".sidebar, .topbar, .bottom-nav").count()).toBeGreaterThan(0);
});

test("inside the app, Devices doesn't offer to pair this browser or install the dashboard", async ({ page }) => {
  await mockHub(page);
  await page.goto("/devices?embed=1");
  await expect(page.getByText("This phone is paired with your hub through the Daytrace app.")).toBeVisible();
  await expect(page.getByText("Get a token for iPhone Shortcuts")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Pair this device" })).toHaveCount(0);
});
