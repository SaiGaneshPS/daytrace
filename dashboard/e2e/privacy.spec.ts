// DT-36: the Privacy page, with the hub mocked: the network check (0 internet connections, every connection by network,
// asked again every 15 s), where the data lives (the folder only on the hub computer), the redaction rules (the hub's
// own, from e2e/fixtures/privacy.json: switch one off, keep your words, try a title, hide what is stored after asking),
// the export download, and delete-all, which asks for the phrase every time and is only offered on the hub computer.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";
import type { components } from "../src/api/schema";

type Rules = components["schemas"]["RedactionRules"];
const FIXTURE = JSON.parse(readFileSync(join(process.cwd(), "e2e", "fixtures", "privacy.json"), "utf-8")) as {
  rules: Rules;
  check: components["schemas"]["TitleCheckResult"];
};
const PHRASE = "delete all my daytrace data";

test.use({ timezoneId: "America/Toronto", locale: "en-US" });

const NETWORK = {
  since: "2026-09-25T12:00:00Z",
  internet_connections: 0,
  outgoing: { localhost: 6, lan: 0, tailscale: 0, internet: 0 },
  blocked: { count: 1, destinations: [{ host: "8.8.8.8", port: 443, count: 1, last: "2026-09-25T13:00:00Z" }] },
  incoming: { localhost: 120, lan: 34, tailscale: 0, internet: 0 },
  refused: { localhost: 0, lan: 2, tailscale: 0, internet: 0 },
  listening: ["127.0.0.1:8767", "192.168.2.179:8767"],
  guarded: true,
};
const STORAGE = {
  profile: "demo",
  folder: "D:\\Hackathon\\data",
  file: "daytrace-demo.db",
  size_bytes: 13_002_342,
  events: 1234,
  first_event: "2026-09-12T12:00:00Z",
  last_event: "2026-09-25T17:00:00Z",
  devices: 2,
};
const DEVICE = (device_id: string, name: string, device_type: string, event_count: number) => ({
  device_id, name, device_type, has_token: true, paired_at: "2026-09-12T12:00:00Z", last_seen: "2026-09-25T17:59:00Z",
  revoked_at: null, last_seq: 12, event_count, events_24h: 10,
});
const DEVICES = [DEVICE("android-1", "Galaxy phone", "android", 900), DEVICE("windows-1", "Desktop", "windows", 334),
  DEVICE("viewer-1", "Phone browser", "viewer", 0), { ...DEVICE("ios-1", "Old iPhone", "ios", 50), revoked_at: "2026-09-20T12:00:00Z" }];

type Mocks = { local?: boolean; network?: () => object | "fail"; rules?: () => Rules; put?: (body: unknown) => { status: number; json: object };
  stored?: () => number; clock?: "install" };

async function mockHub(page: Page, mocks: Mocks = {}) {
  const asked: string[] = [];
  const sent: { path: string; body: unknown }[] = [];
  if (mocks.clock === "install") await page.clock.install({ time: new Date("2026-09-25T14:00:00-04:00") });
  else await page.clock.setFixedTime(new Date("2026-09-25T14:00:00-04:00"));
  const failure = { status: 500, json: { error: { code: "internal_error", message: "The hub had a problem", details: [] } } };
  await page.route("**/api/**", (route) => route.fulfill({ status: 404, json: { error: { code: "not_found", message: "Not mocked" } } }));
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", profile: "demo", version: "0.1.0", local: mocks.local ?? true } }));
  await page.route("**/api/v1/privacy/network", (route) => {
    asked.push("network");
    const found = mocks.network?.() ?? NETWORK;
    return route.fulfill(found === "fail" ? failure : { json: found });
  });
  await page.route("**/api/v1/privacy/storage", (route) => {
    asked.push("storage");
    return route.fulfill({ json: { ...STORAGE, folder: mocks.local === false ? null : STORAGE.folder } });
  });
  await page.route("**/api/v1/devices", (route) => route.fulfill({ json: { devices: DEVICES } }));
  await page.route("**/api/v1/privacy/redaction", (route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON();
      sent.push({ path: "redaction", body });
      return route.fulfill(mocks.put?.(body) ?? { status: 200, json: mocks.rules?.() ?? FIXTURE.rules });
    }
    asked.push("rules");
    return route.fulfill({ json: mocks.rules?.() ?? FIXTURE.rules });
  });
  await page.route("**/api/v1/privacy/redaction/stored", (route) => {
    asked.push("stored");
    return route.fulfill({ json: { matches: mocks.stored?.() ?? 0 } });
  });
  await page.route("**/api/v1/privacy/redaction/check", (route) => {
    sent.push({ path: "check", body: route.request().postDataJSON() });
    return route.fulfill({ json: FIXTURE.check });
  });
  await page.route("**/api/v1/privacy/redaction/apply", (route) => {
    sent.push({ path: "apply", body: route.request().postDataJSON() });
    return route.fulfill({ json: { redacted: 3 } });
  });
  await page.route("**/api/v1/privacy/delete", (route) => {
    sent.push({ path: "delete", body: route.request().postDataJSON() });
    return route.fulfill({ json: { deleted: { events: 1234, devices: 2, nudge_log: 4, settings: 1 }, wiped: true } });
  });
  await page.route("**/api/v1/privacy/export", (route) =>
    route.fulfill({
      status: 200,
      headers: { "Content-Type": "application/json", "Content-Disposition": 'attachment; filename="daytrace-demo-export-2026-09-25.json"' },
      body: JSON.stringify({ daytrace_export: 1, profile: "demo", tables: { events: [] } }),
    }),
  );
  return { asked, sent };
}

const region = (page: Page, name: string) => page.getByRole("region", { name, exact: true });

test("the network check says 0 internet connections, with every connection by network, asked again every 15 s", async ({ page }) => {
  const { asked } = await mockHub(page, { clock: "install" });
  await page.goto("/privacy");
  const card = region(page, "Internet connections");
  await expect(card.locator(".net-check")).toHaveClass(/net-clean/);
  await expect(card.locator(".net-check")).toContainText("0 internet connections");
  await expect(card.locator(".net-since")).toContainText("since the hub started, Fri, Sep 25");
  const row = (label: string) => card.locator(".privacy-facts div").filter({ has: page.getByText(label, { exact: true }) }).locator("dd");
  await expect(row("Served")).toHaveText("120 from this computer, 34 from your home network");
  await expect(row("Made (to the local model)")).toHaveText("6 from this computer");
  await expect(row("Refused coming in")).toHaveText("2 from your home network");
  await expect(row("Blocked going out")).toHaveText("1 request to 8.8.8.8:443");
  await expect(row("Listening on")).toHaveText("127.0.0.1:8767, 192.168.2.179:8767");
  await expect(row("Socket guard")).toHaveText(/^On: nothing in the hub process/);
  const count = () => asked.filter((item) => item === "network").length;
  const first = count();
  await page.clock.runFor(15_500);
  await expect.poll(count).toBe(first + 1);
});

test("a connection to the internet would show in red", async ({ page }) => {
  await mockHub(page, { network: () => ({ ...NETWORK, internet_connections: 1 }) });
  await page.goto("/privacy");
  await expect(region(page, "Internet connections").locator(".net-check")).toHaveClass(/net-bad/);
  await expect(region(page, "Internet connections").locator(".net-check")).toContainText("1 internet connection");
});

test("where the data lives, with the folder only on the hub computer", async ({ page }) => {
  await mockHub(page);
  await page.goto("/privacy");
  const card = region(page, "Where your data lives");
  const row = (label: string) => card.locator(".privacy-facts div").filter({ has: page.getByText(label, { exact: true }) }).locator("dd");
  await expect(row("Profile")).toHaveText("demo");
  await expect(row("Folder")).toHaveText(STORAGE.folder);
  await expect(row("File")).toHaveText(STORAGE.file);
  await expect(row("Size")).toHaveText("12.4 MB");
  await expect(row("Events")).toHaveText("1,234 events, September 12, 2026 to September 25, 2026");
  await expect(card.getByRole("heading", { name: "2 devices sending data" })).toBeVisible(); // not the viewer, not the revoked one
  await expect(card.locator(".privacy-devices li")).toHaveText(["Galaxy phoneAndroid phone, 900 events", "DesktopWindows PC, 334 events"]);
  await expect(card).toContainText("1 browser can read the dashboard, without sending anything.");
  await expect(card.getByRole("link", { name: "Pair or revoke devices" })).toHaveAttribute("href", "/devices");
});

test("on a phone the folder isn't named, and export and delete aren't offered", async ({ page }) => {
  await mockHub(page, { local: false });
  await page.goto("/privacy");
  await expect(region(page, "Where your data lives").locator(".privacy-facts")).toContainText("On the hub computer (its folder shows there)");
  const card = region(page, "Export or delete");
  await expect(card).toContainText("Export and delete work only on the hub computer itself");
  await expect(card.getByRole("link", { name: "Export all (JSON)" })).toHaveCount(0);
  await expect(card.getByRole("button", { name: "Delete all data" })).toHaveCount(0);
});

test("a built-in rule switched off and your own words are saved together", async ({ page }) => {
  let saved: Rules | null = null;
  const { sent, asked } = await mockHub(page, {
    rules: () => saved ?? FIXTURE.rules,
    put: (body) => {
      const choice = body as { disabled: string[]; custom: { name: string; words: string[] }[] };
      saved = {
        ...FIXTURE.rules,
        rules: [
          ...FIXTURE.rules.rules.map((rule) => ({ ...rule, enabled: !choice.disabled.includes(rule.id) })),
          ...choice.custom.map((rule, index) => ({ id: `custom-${index + 1}`, name: rule.name, description: "", builtin: false, enabled: true, words: rule.words })),
        ],
      };
      return { status: 200, json: saved };
    },
  });
  await page.goto("/privacy");
  const card = region(page, "Hide sensitive titles");
  const save = card.getByRole("button", { name: "Save the rules" });
  await expect(save).toBeDisabled(); // nothing changed yet
  const banking = card.getByRole("switch", { name: /Banking and payments/ });
  await expect(banking).toBeChecked();
  await banking.uncheck();
  await card.getByRole("button", { name: "Add a rule" }).click();
  await expect(save).toBeDisabled(); // a rule needs a word
  await card.getByLabel("Rule name").fill("Work projects");
  const word = card.getByLabel("A word or phrase to hide in Work projects");
  for (const text of ["Falcon", "Project X", "falcon"]) {
    await word.fill(text);
    await word.press("Enter");
  }
  await expect(card.locator(".word-chip")).toHaveText(["Falcon", "Project X"]); // the same word once, whatever its case
  await card.getByRole("button", { name: "Remove Project X" }).click();
  await save.click();
  await expect(card.locator(".field-note").first()).toHaveText("Saved. New titles follow these rules from now on.");
  expect(sent.filter((item) => item.path === "redaction").map((item) => item.body)).toEqual([
    { disabled: ["banking"], custom: [{ name: "Work projects", words: ["Falcon"] }] },
  ]);
  await expect(banking).not.toBeChecked();
  await expect(card.locator(".word-chip")).toHaveText(["Falcon"]);
  await expect(save).toBeDisabled();
  await expect.poll(() => asked.filter((item) => item === "stored").length).toBe(2); // counted again with the new rules
});

test("the hub's reason shows when it refuses the rules", async ({ page }) => {
  await mockHub(page, { put: () => ({ status: 400, json: { error: { code: "bad_request", message: "Work: 'x' has no letters or digits to look for", details: [] } } }) });
  await page.goto("/privacy");
  const card = region(page, "Hide sensitive titles");
  await card.getByRole("switch", { name: /Password managers/ }).uncheck();
  await card.getByRole("button", { name: "Save the rules" }).click();
  const note = card.locator(".redaction-actions + .field-note");
  await expect(note).toHaveText("Work: 'x' has no letters or digits to look for");
  await expect(note).not.toHaveClass(/field-hint/);
  await card.getByRole("button", { name: "Undo changes" }).click();
  await expect(card.getByRole("switch", { name: /Password managers/ })).toBeChecked();
});

test("a title is tried against the rules in force", async ({ page }) => {
  const { sent } = await mockHub(page);
  await page.goto("/privacy");
  const card = region(page, "Hide sensitive titles");
  await card.getByLabel("Try a window or event title").fill("MyBank - Account Summary");
  await card.getByRole("button", { name: "Check" }).click();
  await expect(card.locator(".try-title .field-note")).toHaveText('Stored as "[redacted]", by the Banking and payments rule.');
  expect(sent).toContainEqual({ path: "check", body: { title: "MyBank - Account Summary" } });
});

test("what is already stored is hidden only after asking, and then counted again", async ({ page }) => {
  let matches = 3;
  const { sent } = await mockHub(page, { stored: () => matches });
  await page.goto("/privacy");
  const card = region(page, "Hide sensitive titles");
  await expect(card.locator(".stored-matches")).toContainText("3 stored events match these rules");
  await card.getByRole("button", { name: "Hide them in stored data" }).click();
  const confirm = card.getByRole("group", { name: "Hide stored words" });
  await expect(confirm).toContainText("This can't be undone");
  await confirm.getByRole("button", { name: "Keep them" }).click();
  expect(sent.filter((item) => item.path === "apply")).toEqual([]);
  await card.getByRole("button", { name: "Hide them in stored data" }).click();
  matches = 0;
  await confirm.getByRole("button", { name: "Hide 3 events" }).click();
  await expect(card.locator(".redaction-actions + .field-note")).toHaveText("Hidden in 3 stored events.");
  await expect(card.locator(".stored-matches")).toContainText("Nothing already stored matches these rules.");
  expect(sent.filter((item) => item.path === "apply").map((item) => item.body)).toEqual([{ confirm: true }]);
});

test("export downloads the hub's JSON on the hub computer", async ({ page }) => {
  await mockHub(page);
  await page.goto("/privacy");
  const download = page.waitForEvent("download");
  await region(page, "Export or delete").getByRole("link", { name: "Export all (JSON)" }).click();
  const file = await download;
  expect(file.suggestedFilename()).toBe("daytrace-demo-export-2026-09-25.json");
  const body = JSON.parse(readFileSync((await file.path())!, "utf-8"));
  expect(body).toMatchObject({ daytrace_export: 1, profile: "demo" });
});

test("delete asks for the phrase every time, then says what was deleted", async ({ page }) => {
  const { sent, asked } = await mockHub(page);
  await page.goto("/privacy");
  const card = region(page, "Export or delete");
  await card.getByRole("button", { name: "Delete all data" }).click();
  const dialog = page.getByRole("dialog", { name: "Delete all your data?" });
  const phrase = dialog.getByLabel(`Type ${PHRASE} to confirm`);
  const remove = dialog.getByRole("button", { name: "Delete everything" });
  await expect(phrase).toBeFocused();
  await expect(remove).toBeDisabled();
  await phrase.fill("delete all my daytrace dat");
  await expect(remove).toBeDisabled(); // nearly is not enough
  await phrase.fill(PHRASE.toUpperCase());
  await expect(remove).toBeDisabled(); // exactly the phrase
  await phrase.fill(PHRASE);
  await dialog.getByRole("button", { name: "Keep my data" }).click();
  await expect(dialog).toBeHidden();
  expect(sent.filter((item) => item.path === "delete")).toEqual([]);

  await card.getByRole("button", { name: "Delete all data" }).click();
  await expect(phrase).toHaveValue(""); // typed before doesn't count: asked again
  await expect(remove).toBeDisabled();
  await phrase.fill(PHRASE);
  await dialog.getByRole("checkbox", { name: /Keep my redaction rules/ }).uncheck();
  const before = asked.filter((item) => item === "storage").length;
  await remove.click();
  await expect(dialog).toBeHidden();
  await expect(card.locator(".field-note")).toHaveText("Deleted 1,241 rows, 1,234 events among them, and overwritten in the file.");
  expect(sent.filter((item) => item.path === "delete").map((item) => item.body)).toEqual([{ confirm: PHRASE, keep_redaction_rules: false }]);
  await expect.poll(() => asked.filter((item) => item === "storage").length).toBe(before + 1); // shown again, emptied
});

test("when the network check can't load it says so, and the rest still shows", async ({ page }) => {
  await mockHub(page, { network: () => "fail" });
  await page.goto("/privacy");
  await expect(region(page, "Internet connections")).toContainText("The network check couldn't load: The hub had a problem");
  await expect(region(page, "Where your data lives").locator(".privacy-facts")).toContainText("demo");
});

for (const scheme of ["light", "dark"] as const) {
  test(`Privacy passes axe (${scheme}), with a rule of your own and the delete dialog open`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await mockHub(page, { stored: () => 2 });
    await page.goto("/privacy");
    const card = region(page, "Hide sensitive titles");
    await card.getByRole("button", { name: "Add a rule" }).click();
    await card.getByLabel("A word or phrase to hide in Your words").fill("Falcon");
    await card.getByLabel("A word or phrase to hide in Your words").press("Enter");
    const check = async () => {
      await page.evaluate(() =>
        Promise.all(document.getAnimations().filter((a) => a.effect?.getTiming().iterations !== Infinity).map((a) => a.finished.catch(() => undefined))),
      );
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(results.violations.map((v) => `${v.id} (${v.nodes.length}): ${v.help} ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)).toEqual([]);
    };
    await check();
    await region(page, "Export or delete").getByRole("button", { name: "Delete all data" }).click();
    await check();
  });
}

test("phone: every control on Privacy can be tapped (44 px or more)", async ({ page, isMobile }) => {
  test.skip(!isMobile, "touch targets");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockHub(page, { stored: () => 2 });
  await page.goto("/privacy");
  const card = region(page, "Hide sensitive titles");
  await card.getByRole("button", { name: "Add a rule" }).click();
  await card.getByLabel("A word or phrase to hide in Your words").fill("Falcon");
  await card.getByLabel("A word or phrase to hide in Your words").press("Enter");
  await expect(card.locator(".word-chip")).toHaveCount(1);
  const controls = page.locator("main button, main a, main input:not([type=checkbox])");
  for (const control of await controls.all()) {
    const box = await control.boundingBox();
    if (!box) continue;
    expect(Math.min(box.width, box.height), await control.evaluate((element) => element.outerHTML.slice(0, 80))).toBeGreaterThanOrEqual(44);
  }
  for (const toggle of await page.locator("main input[type=checkbox]:visible").all()) {
    const box = await toggle.evaluate((element) => element.closest("label")!.getBoundingClientRect().height);
    expect(box).toBeGreaterThanOrEqual(44); // the whole row is the target
  }
});
