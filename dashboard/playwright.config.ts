// DT-52: end-to-end tests of the built dashboard (`npm run test:e2e`, after `npm run build`). They run against
// `vite preview` with the hub's API mocked in each test, so no hub is needed. Two screens: a 1440 px desktop and a
// 360 px touch phone. On Windows the installed Microsoft Edge is used (no browser download); elsewhere, and in CI,
// Playwright's own Chromium (`npx playwright install chromium` once).
import { defineConfig } from "@playwright/test";

const ci = Boolean(process.env.CI);
const edge = !ci && process.platform === "win32";

export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  fullyParallel: true,
  forbidOnly: ci,
  retries: ci ? 1 : 0,
  reporter: ci ? [["github"], ["list"]] : "list",
  use: {
    baseURL: "http://localhost:4173",
    channel: edge ? "msedge" : undefined,
    serviceWorkers: "block", // the tests mock the API; a service worker would answer before the mocks
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 900 } } },
    { name: "phone", use: { viewport: { width: 360, height: 780 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 } },
  ],
  webServer: {
    command: "npx vite preview --port 4173 --strictPort",
    url: "http://localhost:4173",
    reuseExistingServer: !ci,
    timeout: 60_000,
  },
});
