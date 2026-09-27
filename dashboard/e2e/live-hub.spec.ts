// DT-31, DT-32 and DT-33: Today, Devices, Story and Ask against a real hub, for the claims mocks can't prove: a seeded
// day renders in under a second, its total is the sum of its blocks, a new session shows within 5 seconds, a browser
// on the network pairs with the hub computer's code (and is told to pair again once revoked), and the story and
// answers come from the hub's local model with their facts (skipped without one). Opt-in: set DAYTRACE_E2E_HUB to a
// demo hub running on this computer and serving this build, for example
//   daytrace-hub seed --profile demo && daytrace-hub run --profile demo
//   DAYTRACE_E2E_HUB=http://localhost:8767 npm run test:e2e
// Pairing and revoking only work from the hub's own computer. Each run leaves a revoked "E2E live" device with a
// 90 second session and a revoked "E2E browser" viewer in that hub (re-seeding the demo profile clears them), so it
// refuses the personal profile.
import { expect, type Page, test } from "@playwright/test";

const HUB = process.env.DAYTRACE_E2E_HUB;
test.skip(!HUB, "set DAYTRACE_E2E_HUB to run these against a real hub");
test.use({ baseURL: HUB });
// One after another: the hub has a single pairing code at a time, and two tests pair.
test.describe.configure({ mode: "serial" });

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

// Yesterday: always a whole seeded day, even just after midnight (when today has hardly begun).
test("a seeded day renders in under a second, and its total is the sum of its blocks", async ({ page, isMobile }) => {
  test.skip(isMobile, "once is enough");
  await notPersonal(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Today" })).toBeVisible();
  const clicked = await page.evaluate(() => performance.now());
  await page.getByRole("button", { name: "Previous day" }).click();
  // Checked on every frame, so the time is when the chart appeared (not when a slower poll noticed it).
  const appeared = await page.waitForFunction(() => (document.querySelector(".timeline-chart canvas") ? performance.now() : 0), undefined, {
    polling: "raf",
  });
  expect((await appeared.jsonValue()) - clicked).toBeLessThan(1_000); // from the click to the day on screen

  const [day, tz] = await page.evaluate(() => {
    const yesterday = new Date();
    yesterday.setDate(yesterday.getDate() - 1);
    const pad = (n: number) => String(n).padStart(2, "0");
    return [`${yesterday.getFullYear()}-${pad(yesterday.getMonth() + 1)}-${pad(yesterday.getDate())}`, Intl.DateTimeFormat().resolvedOptions().timeZone];
  });
  const data = (await (await page.request.get(`/api/v1/timeline?date=${day}&tz=${encodeURIComponent(tz)}`)).json()) as {
    lanes: Lane[];
    totals: { seconds: number; minutes: number };
  };
  const blocks = data.lanes.filter((lane) => lane.counted).flatMap((lane) => lane.sessions).reduce((sum, s) => sum + s.seconds, 0);
  expect(blocks).toBe(data.totals.seconds);
  expect(blocks).toBeGreaterThan(0); // a seeded day, not an empty one
  await expect(page.getByRole("region", { name: /^Screen time/ }).locator(".stat-value")).toContainText(formatMinutes(data.totals.minutes));
});

test("a new session shows within 5 seconds", async ({ page, isMobile }) => {
  test.skip(isMobile, "once is enough");
  await notPersonal(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Today" })).toBeVisible(); // today may still be empty
  const started = await page.request.post("/api/v1/pair/start");
  expect(started.ok(), "pairing only works on the hub's own computer: run the hub locally").toBe(true);
  const { code } = (await started.json()) as { code: string };
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

// DT-33: the story and an answer from the hub's real local model (skipped when the hub has no usable model).
test("the story and an answer come from the local model on this computer, with their facts", async ({ page, isMobile }) => {
  test.skip(isMobile, "once is enough");
  test.setTimeout(600_000); // a small model can take a minute or two per reply
  await notPersonal(page);
  const status = (await (await page.request.get("/api/v1/ai/status")).json()) as { reachable: boolean; model: string | null; tool_calling: boolean | null };
  test.skip(!status.reachable || !status.model || !status.tool_calling, "needs the hub's local model running (DAYTRACE_LLM_BASE_URL)");

  await page.goto("/story");
  await page.getByRole("button", { name: "Previous day" }).click(); // yesterday: a whole seeded day with facts
  const card = page.getByRole("region", { name: "Story" });
  await expect(card.locator(".story-text")).toBeVisible({ timeout: 300_000 });
  // The model wrote it (and says so by name), unless its story failed the number check twice and the template did.
  await expect(card.locator(".model-name, .badge-template")).toBeVisible();
  if (await card.locator(".model-name").count()) await expect(card.locator(".model-name")).toHaveText(status.model!);
  expect(await card.locator(".facts li").count()).toBeGreaterThan(0);

  await page.goto("/ask");
  await page.getByRole("button", { name: "How did I sleep last night?" }).click();
  const answer = page.locator(".bubble-answer").last();
  await expect(answer.locator(".answer-text")).toBeVisible({ timeout: 300_000 });
  await expect(answer.locator(".model-name, .badge-template")).toBeVisible();
  await expect(answer.locator(".facts")).toBeVisible();
});


// DT-32: a browser on the network (this computer's own network address, which the hub treats like a phone's) pairs
// with the code the hub computer shows, and is told to pair again once it is revoked there.
test("a browser on the network pairs with the hub computer's code, and revoking it there unpairs it", async ({ page, browser, isMobile }) => {
  test.skip(isMobile, "once is enough");
  await notPersonal(page);
  await page.goto("/devices");
  await page.getByRole("button", { name: /Show a pairing code|Pair another device/ }).click();
  const code = ((await page.locator(".code-digits").textContent()) ?? "").replace(/\s/g, "");
  const lan = await page.locator(".addresses code").first().textContent();
  test.skip(!lan || !lan.startsWith("http"), "the hub has no network address to pair from");
  const phone = await (await browser.newContext({ baseURL: lan ?? undefined })).newPage();
  try {
    await phone.goto("/devices");
    await expect(phone.getByRole("region", { name: "Pair this device" })).toBeVisible(); // not the hub computer
    await phone.getByLabel("Pairing code").fill(code);
    await phone.getByLabel("Name in the device list").fill("E2E browser");
    await phone.getByRole("button", { name: "Pair this browser" }).click();
    await expect(phone.locator(".pair-success")).toContainText("This browser is paired as E2E browser.");
    await expect(page.locator(".pair-success")).toHaveText("E2E browser is paired.", { timeout: 6_000 });

    await page.getByRole("button", { name: "Revoke E2E browser" }).first().click();
    await page.getByRole("dialog").getByRole("button", { name: "Revoke", exact: true }).click();
    await expect(page.getByRole("button", { name: "Revoke E2E browser" })).toHaveCount(0);
    await phone.goto("/");
    await expect(phone.getByRole("alert").filter({ hasText: "isn't paired" })).toBeVisible();
  } finally {
    await phone.context().close();
  }
});
