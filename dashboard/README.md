# Dashboard (PWA)

Vite + React + TypeScript. The hub serves the build at `/`, and it can be installed as an app.

```bash
npm ci
npm run build      # type-check + production build into dist/, which the hub serves
npm run dev        # dev server with hot reload; /api goes to the hub (DAYTRACE_HUB, default http://localhost:8765)
npm run gen:api    # regenerate src/api/schema.d.ts from the hub running on this computer (port 8765)
npm run test:e2e   # Playwright tests of the build (run npm run build first); uses the installed Edge
```

- **On the hub's computer:** open `http://localhost:<port>` (8765 for the personal hub). The dashboard there is
  trusted and needs no pairing.
- **Other devices:** open `http://<pc-name>.local:<port>` or the PC's address. The browser pairs as a viewer on the
  Devices page (DT-32).
- **Serving the build:** the hub looks for `dashboard/dist` next to itself; set `DAYTRACE_DASHBOARD_DIR` (an absolute
  path) to serve another folder, for example when the hub was installed on its own. Every page reloads (a page load
  of an unknown path gets `index.html`), and `/api` stays the API. Before the first build the hub shows a page saying
  how to build.
- **Installing and offline:** the service worker makes the app installable and caches it; a new build takes over on
  the next load. Browsers allow that only over HTTPS or on localhost, so phones can install it once the hub has HTTPS
  (DT-47); over plain HTTP it works as a normal web page.
- **API calls:** go through `src/api/client.ts`. Paths, queries, bodies and replies are typed from the schema (a
  required path parameter or body is a compile error when missing); GETs retry when the hub is busy or unreachable;
  errors show as toasts; `useApi()` gives loading and error states.
- **Dev server:** its `/api` proxy answers only this computer, since the hub trusts requests from it.
- **Design system (DT-52):** colors, sizes and timing are tokens in `src/theme/tokens.css` (light and dark, text at
  4.5:1 or more). Charts use one ECharts theme built from them (`src/theme/charts.ts`: `useEChart`, category colors
  and patterns), and motion presets live in `src/theme/motion.ts` (everything respects reduced motion). The pieces:
  `ChartCard`, `AnimatedNumber`, `Tabs`, `Skeleton`, `BottomNav`. Open `/styleguide` to see them all. The category
  palette is mirrored in `android/app/src/main/res/values/colors.xml`, and a test checks the two match.
- **Tests:** `e2e/` holds Playwright tests of the build on a 1440 px desktop and a 360 px touch phone, with the hub's
  API mocked: layout, touch targets, accessibility (axe, light and dark), tabs, charts, motion, a page failing to
  load, and the Today page. `npm run test:e2e` type-checks them first (tsconfig.e2e.json). `e2e/live-hub.spec.ts` runs
  against a real demo or shared-dev hub when `DAYTRACE_E2E_HUB` is set (it refuses the personal hub). On Windows they use the installed Microsoft Edge
  (no browser download); on macOS or Linux, run `npx playwright install chromium` once. CI uses Playwright's Chromium.
- **Icons:** `public/icons` holds the app icon (192 and 512 px, a maskable 512 px, the Apple touch icon, and an SVG
  favicon).

| File | Ticket |
|---|---|
| `vite.config.ts`, `src/main.tsx`, `src/App.tsx`, `src/api/*`, `public/icons/*` | DT-30 |
| `src/theme/*`, `ChartCard`, `AnimatedNumber`, `Tabs`, `Skeleton`, `BottomNav`, `pages/Styleguide.tsx`, `e2e/`, `playwright.config.ts` | DT-52 |
| `pages/Today.tsx`, `components/Timeline.tsx`, `components/StatCard.tsx` | DT-31 |
| `pages/Devices.tsx`, `components/QrCode.tsx` | DT-32 |
| `pages/Story.tsx`, `pages/Ask.tsx`, `components/ChatBox.tsx` | DT-33 |
| `pages/Insights.tsx`, `pages/Wrapped.tsx` | DT-34 |
| `components/OfflineBanner.tsx` | DT-35 |
| `pages/Privacy.tsx` | DT-36 |
