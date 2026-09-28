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
