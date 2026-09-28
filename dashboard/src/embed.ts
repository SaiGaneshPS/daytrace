// DT-58: embed mode, for the Android app's own Dashboard tab. The app opens a page with ?embed=1 and has its own
// tabs, so the dashboard leaves out its sidebar, top bar and bottom nav. The choice lasts the whole visit (moving
// between pages drops the query), in sessionStorage.

const KEY = "daytrace.embed";

function readEmbedded(): boolean {
  try {
    if (new URLSearchParams(window.location.search).get("embed") === "1") {
      sessionStorage.setItem(KEY, "1");
      return true;
    }
    return sessionStorage.getItem(KEY) === "1";
  } catch {
    return false; // storage blocked: only the page that asked is embedded
  }
}

/** True inside the Daytrace app's Dashboard tab. Read once, when the page loads. */
export const embedded: boolean = typeof window !== "undefined" && readEmbedded();

/**
 * The service worker (DT-30) makes the dashboard installable and caches the app itself. Inside the app there is
 * none: the app checks every request its page makes, and a service worker could serve an old copy while the hub is
 * off. One an earlier visit registered is removed. [register] is vite-plugin-pwa's registerSW.
 */
export async function startServiceWorker(
  inApp: boolean,
  register: () => unknown,
  container: ServiceWorkerContainer | undefined = typeof navigator === "undefined" ? undefined : navigator.serviceWorker,
): Promise<void> {
  if (!inApp) {
    register();
    return;
  }
  if (!container) return;
  const registrations = await container.getRegistrations();
  await Promise.all(registrations.map((registration) => registration.unregister()));
}
