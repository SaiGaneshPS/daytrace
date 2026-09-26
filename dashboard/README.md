# Dashboard (PWA)

Vite + React + TypeScript. The hub serves the build at `/`, and it can be installed as an app.

```bash
npm ci
npm run build      # type-check + production build into dist/, which the hub serves
npm run dev        # dev server with hot reload; /api goes to the hub (DAYTRACE_HUB, default http://localhost:8765)
npm run gen:api    # regenerate src/api/schema.d.ts from the hub running on this computer (port 8765)
```

- **On the hub's computer:** open `http://localhost:<port>` (8765 for the personal hub). The dashboard there is
  trusted and needs no pairing.
- **Other devices:** open `http://<pc-name>.local:<port>` or the PC's address. The browser pairs as a viewer on the
  Devices page (DT-32).
- **Serving the build:** the hub looks for `dashboard/dist` next to itself; set `DAYTRACE_DASHBOARD_DIR` to serve
  another folder. Every page reloads (unknown paths get `index.html`), and `/api` stays the API. Before the first
  build the hub shows a page saying how to build.
- **Installing and offline:** the service worker makes the app installable and caches it. Browsers allow that only
  over HTTPS or on localhost, so phones can install it once the hub has HTTPS (DT-47); over plain HTTP it works as a
  normal web page.
- **API calls:** go through `src/api/client.ts`. Paths, queries and replies are typed from the schema; GETs retry
  when the hub is busy or unreachable; errors show as toasts; `useApi()` gives loading and error states.
- **Icons:** `public/icons` holds the app icon (192 and 512 px, a maskable 512 px, the Apple touch icon, and an SVG
  favicon).

| File | Ticket |
|---|---|
| `vite.config.ts`, `src/main.tsx`, `src/App.tsx`, `src/api/*`, `public/icons/*` | DT-30 |
| `pages/Today.tsx`, `components/Timeline.tsx`, `components/StatCard.tsx` | DT-31 |
| `pages/Devices.tsx`, `components/QrCode.tsx` | DT-32 |
| `pages/Story.tsx`, `pages/Ask.tsx`, `components/ChatBox.tsx` | DT-33 |
| `pages/Insights.tsx`, `pages/Wrapped.tsx` | DT-34 |
| `components/OfflineBanner.tsx` | DT-35 |
| `pages/Privacy.tsx` | DT-36 |
