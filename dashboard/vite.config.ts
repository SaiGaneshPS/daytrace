// DT-30: React, the PWA (manifest, icons, service worker) and the dev proxy to the hub.
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

// Where `npm run dev` sends /api: DAYTRACE_HUB, else the personal hub on this computer. Going through localhost
// keeps the dev dashboard trusted without pairing, as it is when the hub serves the build.
const env = (globalThis as unknown as { process?: { env: Record<string, string | undefined> } }).process?.env ?? {};
const hub = env.DAYTRACE_HUB ?? "http://localhost:8765";

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      injectRegister: false, // main.tsx registers it: no inline script, which the hub's CSP would block
      includeAssets: ["icons/icon.svg", "icons/apple-touch-icon.png"],
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
        globPatterns: ["**/*.{js,css,html,svg,png,webmanifest}"],
        navigateFallback: "/index.html", // every page of the app opens from the cached shell
        navigateFallbackDenylist: [/^\/api\//, /^\/openapi\.json$/, /^\/docs/, /^\/redoc/],
        cleanupOutdatedCaches: true,
      },
    }),
  ],
  server: {
    proxy: { "/api": { target: hub, changeOrigin: true } },
  },
});
