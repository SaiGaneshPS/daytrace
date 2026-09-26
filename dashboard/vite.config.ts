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
  server: {
    host: "localhost",
    proxy: {
      "/api": {
        target: hub,
        changeOrigin: true,
        bypass: (request) => (fromThisComputer(request.socket?.remoteAddress) ? undefined : false),
      },
    },
  },
});
