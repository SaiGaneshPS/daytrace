// DT-32: the Devices page with the hub mocked: the hub computer's pairing code (countdown, the two QR codes, a token
// given right there, a code running out, a new device ending it), the device list and revoking, and pairing from a
// phone (typing the code, the camera's link, which survives a reload, and a token for Shortcuts with Copy). Also
// confetti drawn without a worker (the hub's Content-Security-Policy refuses blob: workers), touch targets and axe.
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

const NOW = new Date("2026-09-25T14:00:00-04:00").getTime();
const ago = (seconds: number) => new Date(NOW - seconds * 1000).toISOString();
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=", "base64");
const CODE = "493817";
const LAN = "http://192.168.2.179:8765";

type Device = {
  device_id: string; name: string; device_type: string; paired_at: string; last_seen: string | null; revoked_at: string | null;
  last_seq: number | null; event_count: number; events_24h: number;
};
const device = (device_id: string, name: string, device_type: string, extra: Partial<Device> = {}): Device => ({
  device_id, name, device_type, paired_at: ago(86_400 * 10), last_seen: null, revoked_at: null, last_seq: null,
  event_count: 0, events_24h: 0, ...extra,
});
const DEVICES = (): Device[] => [
  device("windows-1", "Desk PC", "windows", { last_seq: 812, event_count: 813, events_24h: 120 }), // the hub's own tracker
  device("android-1", "Galaxy phone", "android", { last_seen: ago(20), last_seq: 1203, event_count: 1204, events_24h: 64 }),
  device("seed-iphone", "iPhone (demo)", "ios", { last_seq: 171, event_count: 172, events_24h: 9 }),
  device("viewer-1", "Android phone (Chrome)", "viewer", { last_seen: ago(3 * 3600) }),
  device("browser-1", "Old laptop extension", "browser", { revoked_at: ago(86_400 * 3), last_seq: 57, event_count: 58 }),
];

test.use({ timezoneId: "America/Toronto", locale: "en-US" });

type Mocks = { local?: boolean | (() => boolean); devices?: () => Device[] | { status: number }; claim?: (body: Record<string, string>) => { status?: number; json: object } | Promise<{ status?: number; json: object }> };

async function mockHub(page: Page, mocks: Mocks = {}) {
  const seen = { starts: 0, claims: [] as Record<string, string>[], deleted: [] as string[], qr: [] as string[] };
  await page.route("**/api/**", (route) => route.fulfill({ status: 404, json: { error: { code: "not_found", message: "Not mocked" } } }));
  const local = () => (typeof mocks.local === "function" ? mocks.local() : (mocks.local ?? true));
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", profile: "demo", version: "0.1.0", local: local() } }));
  await page.route("**/api/v1/devices", (route) => {
    const reply = (mocks.devices ?? DEVICES)();
    if ("status" in reply) return route.fulfill({ status: reply.status, json: { error: { code: "unauthorized", message: "not paired" } } });
    return route.fulfill({ json: { devices: reply } });
  });
  await page.route("**/api/v1/pair/start", (route) => {
    seen.starts += 1;
    const code = seen.starts === 1 ? CODE : "802211";
    return route.fulfill({
      json: { code, expires_at: new Date(NOW + 300_000).toISOString(), url: LAN, urls: [LAN], mdns_url: "http://daytrace-desk.local:8765", qr: "/api/v1/pair/qr.png" },
    });
  });
  await page.route("**/api/v1/pair/qr.png?**", (route) => {
    seen.qr.push(new URL(route.request().url()).search);
    return route.fulfill({ body: PNG, contentType: "image/png" });
  });
  await page.route("**/api/v1/pair/claim", async (route) => {
    const body = route.request().postDataJSON() as Record<string, string>;
    seen.claims.push(body);
    const reply = mocks.claim ? await mocks.claim(body) : {
      status: 201,
      json: { device_id: body.device_type === "ios" ? "iphone-1" : body.device_type === "browser" ? "browser-2" : "viewer-2", device_type: body.device_type, name: body.device_name, token: "tok-SECRET-1234", profile: "demo" },
    };
    await route.fulfill({ status: reply.status ?? 201, json: reply.json });
  });
  await page.route("**/api/v1/devices/*", (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    seen.deleted.push(route.request().url().split("/").pop() ?? "");
    return route.fulfill({ status: 204 });
  });
  return seen;
}

async function settled(page: Page) {
  await page.evaluate(() =>
    Promise.all(document.getAnimations().filter((a) => a.effect?.getTiming().iterations !== Infinity).map((a) => a.finished.catch(() => undefined))),
  );
}

// --- the hub computer -------------------------------------------------------------------------------------------

test("the hub computer shows a code with its time left, both QR codes and its addresses", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.clock.setFixedTime(NOW);
  const seen = await mockHub(page);
  await page.goto("/devices");
  await page.getByRole("button", { name: "Show a pairing code" }).click();
  const digits = page.locator(".code-digits");
  await expect(digits).toHaveText("493 817");
  await expect(digits).toHaveAttribute("aria-label", "Pairing code 4 9 3 8 1 7");
  await expect(page.locator(".pair-code")).toContainText("5:00 left");
  await expect(page.getByRole("img", { name: "QR code for the Daytrace app" })).toBeVisible();
  const card = page.getByRole("region", { name: "Pair a device" });
  await expect(card.locator(".addresses")).toContainText(LAN);
  await expect(card.locator(".addresses")).toContainText("http://daytrace-desk.local:8765");
  await page.getByRole("tab", { name: "Phone browser" }).click();
  await expect(page.getByRole("img", { name: "QR code that opens this page on a phone and pairs its browser" })).toBeVisible();
  await expect(card).toContainText(`${LAN}/devices`);
  expect(seen.qr).toEqual([`?for=app&code=${CODE}`, `?for=browser&code=${CODE}`]); // a new code, a new picture
});

test("a token given on the hub computer is shown once, with the hub's network address", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const seen = await mockHub(page);
  await page.goto("/devices");
  await page.getByRole("button", { name: "Show a pairing code" }).click();
  await page.getByRole("tab", { name: "Token" }).click();
  await expect(page.getByRole("radio", { name: /Get a token for iPhone Shortcuts/ })).toBeChecked();
  await expect(page.getByLabel("Name in the device list")).toHaveValue("iPhone Shortcuts");
  await page.getByRole("button", { name: "Get the token" }).click();
  expect(seen.claims).toEqual([{ code: CODE, device_name: "iPhone Shortcuts", device_type: "ios" }]);
  const reveal = page.locator(".token-reveal");
  await expect(reveal.locator(".token")).toHaveText("tok-SECRET-1234");
  await expect(reveal).toContainText(`Hub address: ${LAN}`); // not localhost: the iPhone needs the network address
  await expect(reveal.getByRole("link", { name: "How to set up the iPhone Shortcuts" })).toBeVisible();
  await expect(page.locator(".code-digits")).toHaveCount(0); // the code is used up
  await reveal.getByRole("button", { name: "Done, I've saved it" }).click();
  await expect(page.locator(".token")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Show a pairing code" })).toBeVisible();
});

test("a code runs out after 5 minutes, and a new one can be shown", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.clock.install({ time: NOW });
  const seen = await mockHub(page);
  await page.goto("/devices");
  await page.getByRole("button", { name: "Show a pairing code" }).click();
  await expect(page.locator(".pair-code")).toContainText("5:00 left");
  await page.clock.runFor(61_000);
  await expect(page.locator(".pair-code")).toContainText("3:59 left");
  await page.clock.runFor(240_000);
  await expect(page.locator(".pair-code")).toContainText("This code has run out.");
  await expect(page.locator(".qr")).toHaveCount(0); // an old picture is never shown to scan
  await page.getByRole("button", { name: "New code" }).click();
  await expect(page.locator(".code-digits")).toHaveText("802 211");
  await expect(page.locator(".pair-code")).toContainText("5:00 left");
  expect(seen.starts).toBe(2);
});

test("a device paired while the code is out ends the code and is announced", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const state = { paired: false };
  await mockHub(page, {
    devices: () => (state.paired ? [...DEVICES(), device("viewer-2", "Galaxy phone (Chrome)", "viewer", { last_seen: new Date().toISOString() })] : DEVICES()),
  });
  await page.goto("/devices");
  await expect(page.getByRole("list", { name: "Paired devices" }).getByRole("listitem")).toHaveCount(4);
  await page.getByRole("button", { name: "Show a pairing code" }).click();
  await expect(page.locator(".code-digits")).toBeVisible();
  state.paired = true; // the phone claims the code
  await expect(page.locator(".pair-success")).toHaveText("Galaxy phone (Chrome) is paired.", { timeout: 6_000 }); // checked every 3 s
  await expect(page.locator(".code-digits")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Pair another device" })).toBeVisible();
});

// --- the device list --------------------------------------------------------------------------------------------

test("each device shows its platform, when it was in touch, and what it sent", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.clock.setFixedTime(NOW);
  await mockHub(page);
  await page.goto("/devices");
  const list = page.getByRole("list", { name: "Paired devices" });
  await expect(list.getByRole("listitem")).toHaveCount(4);
  const card = (name: string) => list.getByRole("listitem").filter({ hasText: name });
  await expect(card("Galaxy phone")).toContainText("Android phone / android-1");
  await expect(card("Galaxy phone")).toContainText("Last seen just now");
  await expect(card("Galaxy phone").locator(".live-dot.live")).toHaveCount(1);
  await expect(card("Galaxy phone")).toContainText("synced in the last minute");
  await expect(card("Galaxy phone").locator(".device-stats")).toContainText("Last seq1203Last 24 h64All events1,204");
  await expect(card("Desk PC")).toContainText("Tracked on the hub computer");
  await expect(card("iPhone (demo)")).toContainText("Demo data, made on the hub");
  await expect(card("Android phone (Chrome)")).toContainText("Last seen 3 h ago");
  await expect(card("Android phone (Chrome)").locator(".live-dot")).toHaveCount(0);
  await expect(card("Android phone (Chrome)").locator(".platform-viewer")).toHaveCount(1);
  const revoked = page.locator(".revoked");
  await expect(revoked.locator("summary")).toHaveText(/Revoked devices\s*1/);
  await revoked.locator("summary").click();
  await expect(revoked).toContainText("Old laptop extension");
  await expect(revoked).toContainText("revoked 3 days ago, 58 events kept");
});

test("revoking asks first, and the device leaves the list", async ({ page }) => {
  const state = { revoked: false };
  const seen = await mockHub(page, {
    devices: () => DEVICES().map((item) => (item.device_id === "android-1" && state.revoked ? { ...item, revoked_at: new Date().toISOString() } : item)),
  });
  await page.goto("/devices");
  const list = page.getByRole("list", { name: "Paired devices" });
  await page.getByRole("button", { name: "Revoke Galaxy phone" }).click();
  const dialog = page.getByRole("dialog", { name: "Revoke Galaxy phone?" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Keep it" })).toBeFocused(); // the safe choice is the default
  await dialog.getByRole("button", { name: "Keep it" }).click();
  await expect(dialog).toBeHidden();
  expect(seen.deleted).toEqual([]);
  await page.getByRole("button", { name: "Revoke Galaxy phone" }).click();
  state.revoked = true;
  await dialog.getByRole("button", { name: "Revoke", exact: true }).click();
  await expect(dialog).toBeHidden();
  expect(seen.deleted).toEqual(["android-1"]);
  await expect(page.getByText("Galaxy phone is revoked. Its data stays.")).toBeVisible();
  await expect(list.getByRole("listitem")).toHaveCount(3);
  await expect(page.locator(".revoked summary")).toHaveText(/Revoked devices\s*2/);
});

// --- pairing from a phone ---------------------------------------------------------------------------------------

test("a phone's browser pairs by typing the code, and then shows your data", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const state = { paired: false };
  const seen = await mockHub(page, { local: false, devices: () => (state.paired ? DEVICES() : { status: 401 }) });
  await page.goto("/devices");
  await expect(page.getByRole("region", { name: "Pair this device" })).toBeVisible();
  await expect(page.getByText("Pair this browser (above) to see your devices.")).toBeVisible();
  await expect(page.getByRole("button", { name: /^Revoke/ })).toHaveCount(0); // only the hub computer revokes
  await expect(page.getByRole("radio", { name: /View the dashboard in this browser/ })).toBeChecked();
  await page.getByLabel("Pairing code").fill("493 817");
  await page.getByLabel("Name in the device list").fill("Sai's Galaxy");
  state.paired = true;
  await page.getByRole("button", { name: "Pair this browser" }).click();
  await expect(page.locator(".pair-success")).toContainText("This browser is paired as Sai's Galaxy.");
  expect(seen.claims).toEqual([{ code: CODE, device_name: "Sai's Galaxy", device_type: "viewer" }]);
  expect(await page.evaluate(() => localStorage.getItem("daytrace.token"))).toBe("tok-SECRET-1234");
  await expect(page.getByRole("list", { name: "Paired devices" }).getByRole("listitem")).toHaveCount(4);
  await expect(page.getByText("Devices can be revoked on the hub computer.")).toBeVisible();
  await page.locator(".pair-success").getByRole("link", { name: "Today" }).click();
  await expect(page).toHaveURL(/\/$/);
});

test("a wrong code says so, and the form stays", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockHub(page, { local: false, devices: () => ({ status: 401 }), claim: () => ({ status: 400, json: { error: { code: "invalid_code", message: "wrong code; 4 tries left" } } }) });
  await page.goto("/devices");
  await page.getByLabel("Pairing code").fill("111111");
  await page.getByRole("button", { name: "Pair this browser" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "wrong code; 4 tries left" })).toBeVisible();
  await page.getByLabel("Pairing code").fill("12");
  await page.getByRole("button", { name: "Pair this browser" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "The code is the 6 digits" })).toBeVisible();
});

test("the camera's link pairs at once and takes the code out of the address", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const seen = await mockHub(page, { local: false, devices: () => ({ status: 401 }) });
  await page.goto(`/devices#pair=${CODE}`);
  await expect(page.locator(".pair-success")).toContainText("This browser is paired");
  expect(seen.claims).toHaveLength(1);
  expect(seen.claims[0]).toMatchObject({ code: CODE, device_type: "viewer" });
  expect(new URL(page.url()).hash).toBe("");
  expect(await page.evaluate(() => sessionStorage.getItem("daytrace.pairLink"))).toBeNull(); // tried: forgotten
});

test("the camera's link still pairs after a reload before it was used (a service worker taking over)", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const state = { calls: 0 };
  await mockHub(page, {
    local: false,
    devices: () => ({ status: 401 }),
    claim: async (body) => {
      state.calls += 1;
      if (state.calls === 1) await new Promise(() => undefined); // the page reloads before this one answers
      return { status: 201, json: { device_id: "viewer-2", device_type: "viewer", name: body.device_name, token: "tok-2", profile: "demo" } };
    },
  });
  await page.goto(`/devices#pair=${CODE}`);
  await expect.poll(() => state.calls).toBe(1);
  expect(new URL(page.url()).hash).toBe(""); // already out of the address
  await page.reload();
  await expect(page.locator(".pair-success")).toContainText("This browser is paired");
  expect(state.calls).toBe(2);
});

test("a phone can get a token for iPhone Shortcuts, and copy it", async ({ page, context, browserName }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  if (browserName === "chromium") await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const seen = await mockHub(page, { local: false, devices: () => ({ status: 401 }) });
  await page.goto("/devices");
  await page.getByRole("radio", { name: /Get a token for iPhone Shortcuts/ }).check();
  await expect(page.getByLabel("Name in the device list")).toHaveValue("iPhone Shortcuts");
  await page.getByLabel("Pairing code").fill(CODE);
  await page.getByRole("button", { name: "Get the token" }).click();
  expect(seen.claims).toEqual([{ code: CODE, device_name: "iPhone Shortcuts", device_type: "ios" }]);
  const reveal = page.locator(".token-reveal");
  await expect(reveal.locator(".token")).toHaveText("tok-SECRET-1234");
  await expect(reveal).toContainText(`Hub address: ${new URL(page.url()).origin}`);
  expect(await page.evaluate(() => localStorage.getItem("daytrace.token"))).toBeNull(); // a token for Shortcuts, not this browser
  await reveal.getByRole("button", { name: "Copy the token" }).click();
  await expect(reveal.getByRole("button", { name: "Copied" })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("tok-SECRET-1234");
});

test("a browser that is already paired is told so, and offers a token first", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.addInitScript(() => localStorage.setItem("daytrace.token", "tok-existing"));
  await mockHub(page, { local: false });
  await page.goto("/devices");
  await expect(page.getByText("This browser is already paired.")).toBeVisible();
  await expect(page.getByRole("radio", { name: /Get a token for iPhone Shortcuts/ })).toBeChecked();
});

test("pairing celebrates with confetti drawn on the page, not in a blob: worker the hub's CSP refuses", async ({ page }) => {
  await page.addInitScript(() => {
    const counts = { workers: 0 };
    (window as unknown as { counts: typeof counts }).counts = counts;
    const Original = window.Worker;
    window.Worker = class extends Original {
      constructor(...args: ConstructorParameters<typeof Worker>) {
        counts.workers += 1;
        super(...args);
      }
    } as typeof Worker;
  });
  await mockHub(page, { local: false, devices: () => ({ status: 401 }) });
  await page.goto(`/devices#pair=${CODE}`);
  await expect(page.locator(".pair-success")).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.querySelectorAll("body > canvas").length)).toBeGreaterThan(0);
  expect(await page.evaluate(() => (window as unknown as { counts: { workers: number } }).counts.workers)).toBe(0);
});

// --- layout and accessibility -----------------------------------------------------------------------------------

test("phone: every control on Devices can be tapped (44 px or more)", async ({ page, isMobile }) => {
  test.skip(!isMobile, "touch targets");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockHub(page);
  const small = () =>
    page.locator("main button, main summary, main input, .bottom-nav a, .bottom-nav button").evaluateAll((elements) =>
      elements
        .filter((element) => (element as HTMLElement).offsetParent !== null)
        .map((element) => ({ name: element.textContent?.trim() || element.getAttribute("aria-label"), ...element.getBoundingClientRect().toJSON() }))
        .filter((box) => box.width < 44 || box.height < 44)
        .map((box) => `${box.name}: ${Math.round(box.width)}x${Math.round(box.height)}`),
    );
  await page.goto("/devices");
  await page.getByRole("button", { name: "Show a pairing code" }).click();
  await expect(page.locator(".code-digits")).toBeVisible();
  expect(await small()).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

for (const scheme of ["light", "dark"] as const) {
  test(`accessibility (axe) of Devices on the hub computer and on a phone, in ${scheme}`, async ({ page }) => {
    test.slow();
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await page.clock.setFixedTime(NOW);
    const state = { local: true };
    await mockHub(page, { local: () => state.local });
    await page.goto("/devices");
    await page.getByRole("button", { name: "Show a pairing code" }).click();
    await expect(page.locator(".code-digits")).toBeVisible();
    await page.locator(".revoked summary").click();
    await settled(page);
    let results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
    expect(results.violations.map((v) => `hub: ${v.id} (${v.nodes.length}): ${v.help}`)).toEqual([]);

    state.local = false;
    await page.goto("/devices");
    await expect(page.getByRole("region", { name: "Pair this device" })).toBeVisible();
    await settled(page);
    results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
    expect(results.violations.map((v) => `phone: ${v.id} (${v.nodes.length}): ${v.help}`)).toEqual([]);
  });
}

test("screenshots of Devices for review", async ({ page }, testInfo) => {
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await page.clock.setFixedTime(NOW);
    await mockHub(page);
    await page.goto("/devices");
    await page.getByRole("button", { name: "Show a pairing code" }).click();
    await expect(page.locator(".code-digits")).toBeVisible();
    await settled(page);
    await testInfo.attach(`devices-${scheme}`, { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });
  }
});
