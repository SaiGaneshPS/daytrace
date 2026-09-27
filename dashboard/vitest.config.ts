// DT-59: the dashboard's unit tests (Vitest, in jsdom): the Insights tabs rendered from the hub's own fixtures, with
// every chart's option captured, so the numbers they draw and say can be checked exactly. The e2e tests
// (Playwright, e2e/) check the built pages in a browser.
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["src/__tests__/setup.ts"],
  },
});
