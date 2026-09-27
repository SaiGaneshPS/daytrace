// DT-30: React, the PWA (manifest, icons, service worker) and the dev proxy to the hub.
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

// Where `npm run dev` sends /api: DAYTRACE_HUB, else the personal hub on this computer. The proxy's requests come
// from this computer, which the hub trusts without pairing, so the proxy serves only this computer too: a phone
// reaching the dev server (npm run dev -- --host) gets a 404 for /api instead of the owner's trust.
const env = (globalThis as unknown as { process?: { env: Record<string, string | undefined> } }).process?.env ?? {};
const hub = env.DAYTRACE_HUB ?? "http://localhost:8765";
const fromThisComputer = (address: string | undefined) =>
  address === "127.0.0.1" || address === "::1" || address === "::ffff:127.0.0.1";
// The hub's Content-Security-Policy (hub/daytrace_hub/app.py DASHBOARD_CSP; a hub test keeps them the same). `vite
// preview`, which the e2e tests run against, sends it too, so they catch anything the hub's pages would refuse.
const HUB_CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; manifest-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'";

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      injectRegister: false, // main.tsx registers it (virtual:pwa-register)
      includeManifestIcons: false, // the icons are in globPatterns already
      manifest: {
        name: "Daytrace",
        short_name: "Daytrace",
        description: "Where did your day go? Your screen time across your devices, kept on your own network.",
        start_url: "/",
        scope: "/",
        display: "standalone",
        background_color: "#ffffff",
        theme_color: "#4f46e5",
        icons: [
          { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
          { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
          { src: "/icons/maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
        ],
      },
      workbox: {
        globPatterns: ["**/*.{js,css,html,svg,png}"], // the plugin adds the manifest itself
        navigateFallback: "/index.html", // every page of the app opens from the cached shell
        navigateFallbackDenylist: [/^\/api\//, /^\/openapi\.json$/, /^\/docs/, /^\/redoc/],
        cleanupOutdatedCaches: true,
        // A new build takes over at once and the page reloads (autoUpdate), instead of waiting for every tab and
        // installed window to close; the first visit is controlled without a reload. The plugin sets these
        // itself only when it injects the registration.
        skipWaiting: true,
        clientsClaim: true,
      },
    }),
  ],
  // ECharts alone is about 600 kB; it is split out and loaded only by pages with charts.
  build: { chunkSizeWarningLimit: 700 },
  preview: { headers: { "Content-Security-Policy": HUB_CSP } },
  server: {
    host: "localhost",
    proxy: {
      "/api": {
        target: hub,
        changeOrigin: true,
        bypass: (request) => (fromThisComputer(request.socket?.remoteAddress) ? undefined : false),
        // The hub refuses requests from other sites' pages (DT-45): through the proxy, the dev server's page is the
        // hub's own, so it gets the hub's own origin.
        configure: (proxy) => {
          proxy.on("proxyReq", (proxied) => {
            if (proxied.getHeader("origin")) proxied.setHeader("origin", new URL(hub).origin);
          });
        },
      },
    },
  },
});
