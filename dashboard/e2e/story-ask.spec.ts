// DT-33: the Story and Ask pages with the hub mocked: the story typing out with its model and facts, the mini
// charts, the template story, the AI offline (and coming back), a slow story, the chat (chips, Enter, Stop, a 503,
// a declined question, a template answer, keeping the conversation), nothing leaving this computer, and
// accessibility. The clock is fixed at 14:00 on 25 September 2026 in Toronto.
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, type Request, test } from "@playwright/test";

const DAY = "2026-09-25";
const MODEL = "google/gemma-4-e4b";

const STATUS = { base_url: "http://127.0.0.1:1234/v1", model: MODEL, reachable: true, tool_calling: true, models: [MODEL], error: null };
const OFFLINE = { ...STATUS, model: null, reachable: false, tool_calling: null, models: [], error: "No model server answers at http://127.0.0.1:1234/v1. Start LM Studio's server." };

const STORY_TEXT =
  "You started your day at 07:18 after 7 hours 50 minutes of sleep. Your screen time came to 2 hours 35 minutes, " +
  "most of it in Visual Studio Code (125 minutes). You picked up your phone 4 times and kept a focus score of 71.";
const FACTS = [
  { label: "screen time", value: 155, unit: "minutes" },
  { label: "time in Visual Studio Code", value: 125, unit: "minutes" },
  { label: "first screen use", value: "07:18", unit: "time" },
  { label: "sleep last night", value: 470, unit: "minutes" },
  { label: "phone pickups", value: 4, unit: "times" },
  { label: "focus score (0 to 100)", value: 71, unit: "score" },
];
const story = (date = DAY, overrides: object = {}) => ({
  date, tz: "America/Toronto", story: STORY_TEXT, facts_used: FACTS, model: MODEL, cached: false, fallback: false,
  reason: null, in_progress: date === DAY, ...overrides,
});
const TEMPLATE = { model: null, fallback: true, reason: "The local model isn't reachable, so this story comes from a template." };

const SUMMARY = {
  date: DAY, tz: "America/Toronto", in_progress: true, screen_minutes: 155, phone_minutes: 30, computer_minutes: 125,
  focused_minutes: 125, focus_score: 71, work_or_study_minutes: 130, distracted_minutes: 45, pickups: 4,
  switches_per_hour: 1.3, sleep_minutes: 470, sleep_estimated: false, steps: null, estimated: false, screen_estimated: false,
  top_apps: [
    { app: "Visual Studio Code", category: "work", minutes: 125 },
    { app: "Instagram", category: "social", minutes: 20 },
    { app: "YouTube", category: "video", minutes: 10 },
  ],
};

const ANSWER = {
  answer: "Last week you watched 1 hour 50 minutes of YouTube after 11 pm, 45 of them on Tuesday night.",
  facts_used: [
    { label: "time in apps matching YouTube between 23:00 and 03:00, from Monday 2026-09-14 to Sunday 2026-09-20", value: 110, unit: "minutes" },
    { label: "YouTube after 11 pm on Tuesday 2026-09-15", value: 45, unit: "minutes" },
  ],
  tools_called: ["get_totals"],
  chart: {
    kind: "bar", title: "YouTube after 11 pm per day", unit: "minutes",
    points: [{ label: "2026-09-14", value: 30 }, { label: "2026-09-15", value: 45 }, { label: "2026-09-16", value: 20 }, { label: "2026-09-19", value: 15 }],
  },
  model: MODEL, fallback: false, declined: false, reason: null,
};

test.use({ timezoneId: "America/Toronto", locale: "en-US" });

type Mocks = { status?: () => object; story?: (date: string) => object | Promise<object>; ask?: (question: string) => { status?: number; json: object } | Promise<{ status?: number; json: object }> };

/** Mocks the hub: anything not mocked here answers 404 at once (so no page waits on a real hub). */
async function mockHub(page: Page, mocks: Mocks = {}) {
  const seen = { status: 0, story: [] as string[], asked: [] as { question: string; tz: string }[] };
  await page.clock.setFixedTime(new Date("2026-09-25T14:00:00-04:00"));
  await page.route("**/api/**", (route) => route.fulfill({ status: 404, json: { error: { code: "not_found", message: "Not mocked" } } }));
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", profile: "demo", version: "0.1.0" } }));
  await page.route("**/api/v1/ai/status", (route) => {
    seen.status += 1;
    return route.fulfill({ json: mocks.status ? mocks.status() : STATUS });
  });
  await page.route("**/api/v1/story?**", async (route) => {
    const date = new URL(route.request().url()).searchParams.get("date") ?? DAY;
    seen.story.push(date);
    await route.fulfill({ json: mocks.story ? await mocks.story(date) : story(date) });
  });
  await page.route("**/api/v1/insights/day?**", (route) => {
    const date = new URL(route.request().url()).searchParams.get("date");
    return route.fulfill({ json: { ...SUMMARY, date, in_progress: date === DAY } });
  });
  await page.route("**/api/v1/ask", async (route) => {
    const body = route.request().postDataJSON() as { question: string; tz: string };
    seen.asked.push(body);
    const reply = mocks.ask ? await mocks.ask(body.question) : { json: ANSWER };
    await route.fulfill({ status: reply.status ?? 200, json: reply.json });
  });
  return seen;
}

/** Wait for fades and slides to finish, so axe measures colors as they end up (not halfway through a fade). */
async function settled(page: Page) {
  await page.evaluate(() =>
    Promise.all(
      document.getAnimations().filter((a) => a.effect?.getTiming().iterations !== Infinity).map((a) => a.finished.catch(() => undefined)),
    ),
  );
}

// --- story -----------------------------------------------------------------------------------------------------

test("the story types itself out, names the model on this device, and lists its facts with the exact numbers", async ({ page }) => {
  await mockHub(page);
  await page.goto("/story");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Today's story");
  const text = page.locator(".story-text");
  await expect(text).toHaveAttribute("data-typing", "true"); // still typing
  await expect(text.locator(".visually-hidden")).toHaveText(STORY_TEXT); // screen readers get it whole at once
  await expect(text.locator("[aria-hidden=true]")).toHaveText(STORY_TEXT, { timeout: 5_000 });
  await expect(text).not.toHaveAttribute("data-typing");

  const card = page.getByRole("region", { name: "Story" });
  await expect(card.locator(".badge-local")).toHaveText("Generated on this device");
  await expect(card.locator(".model-name")).toHaveText(MODEL);
  await expect(card).toContainText("this is the story so far");
  await card.getByText("Facts used").click();
  const facts = card.locator(".facts li");
  await expect(facts).toHaveCount(FACTS.length);
  for (const [label, value] of [
    ["screen time", "155 min (2h 35m)"],
    ["time in Visual Studio Code", "125 min (2h 5m)"],
    ["first screen use", "07:18"],
    ["sleep last night", "470 min (7h 50m)"],
    ["phone pickups", "4 times"],
    ["focus score (0 to 100)", "71 out of 100"],
  ]) {
    await expect(facts.filter({ hasText: label }).locator(".fact-value")).toHaveText(value);
  }
});

test("the mini charts show the top apps and the focus score's parts", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockHub(page);
  await page.goto("/story");
  await expect(page.getByRole("img", { name: /^Top 3 apps and sites by minutes/ })).toBeVisible();
  const focus = page.getByRole("region", { name: "Focus and distraction" });
  await expect(focus.getByRole("img", { name: /^Work or study, focused and distracted minutes/ })).toBeVisible();
  await expect(focus).toContainText("Focus score 71 out of 100");
});

test("a template story says it was written without the AI, and why", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockHub(page, { story: (date) => story(date, TEMPLATE) });
  await page.goto("/story");
  const card = page.getByRole("region", { name: "Story" });
  await expect(card.locator(".story-text")).toContainText("You started your day at 07:18");
  await expect(card.locator(".badge-template")).toHaveText("Written from the facts, without the AI");
  await expect(card).toContainText(TEMPLATE.reason);
  await expect(card.locator(".badge-local")).toHaveCount(0);
});

test("with the AI offline, the story page says why, and the story and charts still work", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const state = { offline: true };
  const seen = await mockHub(page, { status: () => (state.offline ? OFFLINE : STATUS), story: (date) => story(date, TEMPLATE) });
  await page.goto("/story");
  const notice = page.locator(".ai-offline");
  await expect(notice).toContainText("The local AI is offline");
  await expect(notice).toContainText(OFFLINE.error);
  await expect(page.locator(".story-text")).toContainText("07:18");
  await expect(page.getByRole("img", { name: /^Top 3 apps/ })).toBeVisible();
  state.offline = false; // the model server is started
  const before = seen.status;
  await notice.getByRole("button", { name: "Check again" }).click();
  await expect(notice).toHaveCount(0);
  expect(seen.status).toBe(before + 1);
});

test("a story that takes longer than other requests keeps waiting, and shows how long", async ({ page }) => {
  test.slow();
  await page.emulateMedia({ reducedMotion: "reduce" });
  const seen = await mockHub(page, {
    story: async (date) => {
      await new Promise((resolve) => setTimeout(resolve, 17_000)); // past the 15 s every other request gets
      return story(date);
    },
  });
  await page.goto("/story");
  const card = page.getByRole("region", { name: "Story" });
  await expect(card).toContainText(/Writing the story on this device \(\d+ s\)/, { timeout: 8_000 });
  await expect(card.locator(".story-text")).toContainText("07:18", { timeout: 30_000 });
  expect(seen.story).toEqual([DAY]); // one request: not cut off and sent again
});

test("another day can be picked, and its story replaces today's", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const seen = await mockHub(page, { story: (date) => story(date, { story: date === DAY ? STORY_TEXT : "Yesterday you spent 60 minutes in Minecraft." , facts_used: date === DAY ? FACTS : [{ label: "time in Minecraft", value: 60, unit: "minutes" }] }) });
  await page.goto("/story");
  await expect(page.locator(".story-text")).toContainText("07:18");
  await page.getByRole("button", { name: "Previous day" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Thursday, September 24");
  await expect(page.locator(".story-text")).toHaveText(/Yesterday you spent 60 minutes in Minecraft\./);
  await expect(page.getByRole("region", { name: "Story" })).not.toContainText("story so far");
  expect(seen.story).toEqual([DAY, "2026-09-24"]);
});

test("a story that can't load says so and can be tried again", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const state = { fail: true };
  await mockHub(page);
  await page.route("**/api/v1/story?**", (route) =>
    state.fail
      ? route.fulfill({ status: 400, json: { error: { code: "bad_request", message: "unknown time zone" } } })
      : route.fulfill({ json: story() }),
  );
  await page.goto("/story");
  const card = page.getByRole("region", { name: "Story" });
  await expect(card).toContainText("The story couldn't load: unknown time zone");
  state.fail = false;
  await card.getByRole("button", { name: "Try again" }).click();
  await expect(card.locator(".story-text")).toContainText("07:18");
});

// --- ask -------------------------------------------------------------------------------------------------------

test("an example question is answered with the model, the tools, a chart and the facts", async ({ page }) => {
  const seen = await mockHub(page);
  await page.goto("/ask");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Ask your day");
  await expect(page.locator(".page-head .model-name")).toHaveText(MODEL);
  await page.getByRole("button", { name: "How much YouTube did I watch last week?" }).click();
  const log = page.getByRole("list", { name: "Questions and answers" });
  await expect(log.locator(".bubble-question")).toHaveText("How much YouTube did I watch last week?");
  await expect(log.locator(".answer-text [aria-hidden=true]")).toHaveText(ANSWER.answer, { timeout: 5_000 });
  expect(seen.asked).toEqual([{ question: "How much YouTube did I watch last week?", tz: "America/Toronto" }]);
  const answer = log.locator(".bubble-answer");
  await expect(answer.locator(".badge-local")).toHaveText("Generated on this device");
  await expect(answer.getByRole("list", { name: "Looked up" })).toHaveText("Screen time");
  await expect(answer.locator("figcaption")).toHaveText("YouTube after 11 pm per day");
  await expect(answer.getByRole("img", { name: "YouTube after 11 pm per day, in minutes" })).toBeVisible();
  await answer.getByText("Facts used").click();
  await expect(answer.locator(".facts li")).toHaveCount(2);
  await expect(answer.locator(".facts li").first().locator(".fact-value")).toHaveText("110 min (1h 50m)");
});

test("Enter asks the typed question, and Shift and Enter start a new line", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const seen = await mockHub(page);
  await page.goto("/ask");
  const input = page.getByRole("textbox", { name: "Ask about your day" });
  await input.fill("How much YouTube");
  await input.press("Shift+Enter");
  await input.pressSequentially("last week?");
  await expect(input).toHaveValue("How much YouTube\nlast week?");
  await input.press("Enter");
  await expect(page.locator(".bubble-question")).toHaveText("How much YouTube last week?".replace(" last", "\nlast"));
  await expect(input).toHaveValue("");
  await expect(page.locator(".answer-text")).toContainText("1 hour 50 minutes");
  expect(seen.asked).toHaveLength(1);
});

test("a question that isn't about your day is declined, without a model badge", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const declined = { ...ANSWER, answer: "I can only answer questions about your own days.", facts_used: [], tools_called: [], chart: null, declined: true };
  await mockHub(page, { ask: () => ({ json: declined }) });
  await page.goto("/ask");
  await page.getByRole("textbox", { name: "Ask about your day" }).fill("What is the capital of France?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  const answer = page.locator(".bubble-answer");
  await expect(answer).toContainText("I can only answer questions about your own days.");
  await expect(answer.getByText("Not about your day")).toBeVisible();
  await expect(answer.locator(".badge-local, .facts, .tools")).toHaveCount(0);
});

test("an answer from the facts alone says so, and why", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const reason = "The model's answer used numbers that aren't in the facts, twice.";
  await mockHub(page, { ask: () => ({ json: { ...ANSWER, answer: "From the facts: YouTube after 11 pm last week, 110 minutes.", model: null, fallback: true, reason } }) });
  await page.goto("/ask");
  await page.getByRole("button", { name: "How much YouTube did I watch last week?" }).click();
  const answer = page.locator(".bubble-answer");
  await expect(answer.locator(".badge-template")).toHaveText("Written from the facts, without the AI");
  await expect(answer).toContainText(reason);
});

test("with the AI offline, Ask says why, offers no questions, and recovers by itself", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.clock.install({ time: new Date("2026-09-25T14:00:00-04:00") });
  const state = { offline: true };
  await mockHub(page, { status: () => (state.offline ? OFFLINE : STATUS) });
  await page.goto("/ask");
  await expect(page.locator(".ai-offline")).toContainText(OFFLINE.error);
  await expect(page.getByRole("textbox", { name: "Ask about your day" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Where did my afternoon go?" })).toBeDisabled();
  await expect(page.locator(".page-head .model-name")).toHaveCount(0);
  state.offline = false; // the model server is started; no click needed
  await page.clock.runFor(31_000);
  await expect(page.locator(".ai-offline")).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "Ask about your day" })).toBeEnabled();
});

test("a model that can't call tools is named, and questions wait for one that can", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockHub(page, { status: () => ({ ...STATUS, tool_calling: false }) });
  await page.goto("/ask");
  await expect(page.locator(".ai-offline")).toContainText("This model can't look things up");
  await expect(page.locator(".ai-offline")).toContainText(`${MODEL} doesn't call tools`);
  await expect(page.getByRole("textbox", { name: "Ask about your day" })).toBeDisabled();
});

test("when the model stops answering mid-question, the answer says so and can be asked again", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const state = { down: true };
  const seen = await mockHub(page, {
    ask: () => (state.down ? { status: 503, json: { error: { code: "ai_unavailable", message: "The model server stopped answering." } } } : { json: ANSWER }),
  });
  await page.goto("/ask");
  await page.getByRole("button", { name: "How did I sleep last night?" }).click();
  const failed = page.locator(".bubble-failed");
  await expect(failed).toContainText("The local AI is offline: The model server stopped answering.");
  state.down = false;
  await failed.getByRole("button", { name: "Ask again" }).click();
  await expect(page.locator(".answer-text")).toContainText("1 hour 50 minutes");
  await expect(page.locator(".turn")).toHaveCount(1); // the failed turn was replaced, not repeated
  expect(seen.asked.map((body) => body.question)).toEqual(["How did I sleep last night?", "How did I sleep last night?"]);
});

test("a question being answered shows progress, and Stop cancels it", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockHub(page, {
    ask: async () => {
      await new Promise((resolve) => setTimeout(resolve, 10_000));
      return { json: ANSWER };
    },
  });
  await page.goto("/ask");
  await page.getByRole("button", { name: "Where did my afternoon go?" }).click();
  await expect(page.locator(".thinking")).toContainText("Looking at your data on this device");
  await expect(page.getByRole("button", { name: "Where did my afternoon go?" })).toBeDisabled(); // one at a time
  await page.getByRole("button", { name: "Stop" }).click();
  await expect(page.locator(".bubble-failed")).toContainText("Stopped.");
  await expect(page.getByRole("button", { name: "Ask", exact: true })).toBeVisible();
});

test("the conversation stays when you leave the page and come back, until it is cleared", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockHub(page);
  await page.goto("/ask");
  await page.getByRole("button", { name: "How much YouTube did I watch last week?" }).click();
  await expect(page.locator(".answer-text")).toContainText("1 hour 50 minutes");
  await page.goto("/story");
  await expect(page.locator(".story-text")).toContainText("07:18");
  await page.goto("/ask");
  await expect(page.locator(".bubble-question")).toHaveText("How much YouTube did I watch last week?");
  await expect(page.locator(".answer-text")).toContainText("1 hour 50 minutes");
  await page.getByRole("button", { name: "Clear the conversation" }).click();
  await expect(page.locator(".turn")).toHaveCount(0);
  await expect(page.getByText("Ask anything about your days.")).toBeVisible();
});

// --- both ------------------------------------------------------------------------------------------------------

test("neither page sends anything beyond this computer", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const outside: string[] = [];
  page.on("request", (request: Request) => {
    const host = new URL(request.url()).hostname;
    if (!["localhost", "127.0.0.1"].includes(host)) outside.push(request.url());
  });
  await mockHub(page);
  await page.goto("/story");
  await expect(page.locator(".story-text")).toContainText("07:18");
  await page.goto("/ask");
  await page.getByRole("button", { name: "How much YouTube did I watch last week?" }).click();
  await expect(page.locator(".answer-text")).toContainText("1 hour 50 minutes");
  expect(outside).toEqual([]); // no fonts, scripts or calls from the internet: the pages work offline
});

for (const scheme of ["light", "dark"] as const) {
  test(`accessibility (axe) of Story and Ask with content, in ${scheme}`, async ({ page }) => {
    test.slow(); // two pages, each scanned in full
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await mockHub(page);
    await page.goto("/story");
    await expect(page.locator(".story-text")).toContainText("07:18");
    await page.getByText("Facts used").click();
    await settled(page);
    let results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
    expect(results.violations.map((v) => `story: ${v.id} (${v.nodes.length}): ${v.help}`)).toEqual([]);

    await page.goto("/ask");
    await page.getByRole("button", { name: "How much YouTube did I watch last week?" }).click();
    await expect(page.locator(".answer-text")).toContainText("1 hour 50 minutes");
    await page.locator(".bubble-answer").getByText("Facts used").click();
    await settled(page);
    results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
    expect(results.violations.map((v) => `ask: ${v.id} (${v.nodes.length}): ${v.help}`)).toEqual([]);
  });
}

test("screenshots of Story and Ask for review", async ({ page }, testInfo) => {
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await mockHub(page);
    await page.goto("/story");
    await expect(page.locator(".story-text")).toContainText("07:18");
    await settled(page);
    await testInfo.attach(`story-${scheme}`, { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });
    await page.evaluate(() => sessionStorage.clear()); // one question per picture (the conversation is kept otherwise)
    await page.goto("/ask");
    await page.getByRole("button", { name: "How much YouTube did I watch last week?" }).click();
    await expect(page.locator(".answer-text")).toContainText("1 hour 50 minutes");
    await settled(page);
    await testInfo.attach(`ask-${scheme}`, { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });
  }
});
