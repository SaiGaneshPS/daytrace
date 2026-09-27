// DT-31: Today against a real hub, for the claims mocks can't prove: a seeded day renders in under a second, its
// total is the sum of its blocks, and a new session shows within 5 seconds. Opt-in: set DAYTRACE_E2E_HUB to a demo or
// shared-dev hub serving this build, for example
//   daytrace-hub seed --profile demo && daytrace-hub run --profile demo
//   DAYTRACE_E2E_HUB=http://localhost:8767 npm run test:e2e
// It pairs a test device and sends it an event, so it refuses the personal profile.
import { expect, type Page, test } from "@playwright/test";

const HUB = process.env.DAYTRACE_E2E_HUB;
test.skip(!HUB, "set DAYTRACE_E2E_HUB to run these against a real hub");
test.use({ baseURL: HUB });

type Lane = { counted: boolean; sessions: { seconds: number }[] };

function formatMinutes(minutes: number): string {
  const whole = Math.round(minutes);
  const hours = Math.floor(whole / 60);
  const rest = whole % 60;
  if (!hours) return `${rest}m`;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

async function notPersonal(page: Page) {
  const health = (await (await page.request.get("/api/v1/health")).json()) as { profile: string };
  expect(health.profile, "these tests write test data: never run them against the personal hub").not.toBe("personal");
}

test("a seeded day renders in under a second, and its total is the sum of its blocks", async ({ page, isMobile }) => {
  test.skip(isMobile, "once is enough");
  await notPersonal(page);
  await page.goto("/");
  await expect(page.locator(".timeline-chart canvas")).toBeVisible();
  const rendered = await page.evaluate(() => performance.now()); // since the navigation started
  expect(rendered).toBeLessThan(1_000);

  const [day, tz] = await page.evaluate(() => {
    const now = new Date();
    const pad = (n: number) => String(n).padStart(2, "0");
    return [`${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`, Intl.DateTimeFormat().resolvedOptions().timeZone];
  });
  const data = (await (await page.request.get(`/api/v1/timeline?date=${day}&tz=${encodeURIComponent(tz)}`)).json()) as {
    lanes: Lane[];
    totals: { seconds: number; minutes: number };
  };
  const blocks = data.lanes.filter((lane) => lane.counted).flatMap((lane) => lane.sessions).reduce((sum, s) => sum + s.seconds, 0);
  expect(blocks).toBe(data.totals.seconds);
  await expect(page.getByRole("region", { name: /^Screen time/ }).locator(".stat-value")).toContainText(formatMinutes(data.totals.minutes));
});

test("a new session shows within 5 seconds", async ({ page, isMobile }) => {
  test.skip(isMobile, "once is enough");
  await notPersonal(page);
  await page.goto("/");
  await expect(page.locator(".timeline-chart canvas")).toBeVisible();
  const { code } = (await (await page.request.post("/api/v1/pair/start")).json()) as { code: string };
  const name = `E2E live ${Date.now()}`;
  const claimed = (await (await page.request.post("/api/v1/pair/claim", { data: { code, device_name: name, device_type: "android" } })).json()) as {
    device_id: string;
    token: string;
  };
  try {
    const end = new Date();
    const start = new Date(end.getTime() - 90_000);
    const sent = await page.request.post("/api/v1/events", {
      headers: { Authorization: `Bearer ${claimed.token}` },
      data: { events: [{ device_id: claimed.device_id, seq: 1, kind: "app_session", source: "usagestats", start: start.toISOString(), end: end.toISOString(), app: "E2E Live", app_id: "app.daytrace.e2e" }] },
    });
    expect(sent.ok()).toBe(true);
    const posted = Date.now();
    await expect(page.getByRole("list", { name: "Devices" }).getByRole("listitem").filter({ hasText: name })).toBeVisible({ timeout: 5_000 });
    expect(Date.now() - posted).toBeLessThan(5_000);
  } finally {
    await page.request.delete(`/api/v1/devices/${claimed.device_id}`); // revoke the test device
  }
});
