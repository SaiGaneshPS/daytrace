# Dashboard (PWA)

Vite + React + TypeScript. Served by the hub at `/` (DT-30) and installable on phones.

```bash
npm ci
npm run dev        # dev server (DT-30 adds the proxy to the hub)
npm run build      # type-check + production build into dist/
npm run gen:api    # regenerate src/api/schema.d.ts from a running hub (DT-30)
```

| File | Ticket |
|---|---|
| `vite.config.ts`, `src/main.tsx`, `src/App.tsx`, `src/api/*`, `public/icons/*` | DT-30 |
| `pages/Today.tsx`, `components/Timeline.tsx`, `components/StatCard.tsx` | DT-31 |
| `pages/Devices.tsx`, `components/QrCode.tsx` | DT-32 |
| `pages/Story.tsx`, `pages/Ask.tsx`, `components/ChatBox.tsx` | DT-33 |
| `pages/Insights.tsx`, `pages/Wrapped.tsx` | DT-34 |
| `components/OfflineBanner.tsx` | DT-35 |
| `pages/Privacy.tsx` | DT-36 |
