// DT-30: the app shell: navigation, the hub's status, pairing and error notices, one route per page.
// Each page is filled in by its own ticket (Today DT-31, Devices DT-32, Story and Ask DT-33, Insights and Wrapped
// DT-34, Privacy DT-36; Streaks joins with DT-54).
import { useEffect, useState } from "react";
import { BrowserRouter, Link, NavLink, Route, Routes, useLocation } from "react-router";
import { PAIRED_EVENT, UNPAIRED_EVENT, dismissToast, useApi, useToasts } from "./api/client";
import Ask from "./pages/Ask";
import Devices from "./pages/Devices";
import Insights from "./pages/Insights";
import Privacy from "./pages/Privacy";
import Story from "./pages/Story";
import Today from "./pages/Today";
import Wrapped from "./pages/Wrapped";

const pages = [
  { path: "/", label: "Today", element: <Today /> },
  { path: "/story", label: "Story", element: <Story /> },
  { path: "/ask", label: "Ask", element: <Ask /> },
  { path: "/insights", label: "Insights", element: <Insights /> },
  { path: "/wrapped", label: "Wrapped", element: <Wrapped /> },
  { path: "/devices", label: "Devices", element: <Devices /> },
  { path: "/privacy", label: "Privacy", element: <Privacy /> },
];

function Toasts() {
  const toasts = useToasts();
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((item) => (
        <div key={item.id} className={`toast toast-${item.kind}`}>
          <span>{item.message}</span>
          <button type="button" onClick={() => dismissToast(item.id)} aria-label="Dismiss">
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

function Shell() {
  const location = useLocation();
  const health = useApi("/api/v1/health", { quiet: true });
  const [unpaired, setUnpaired] = useState(false);

  useEffect(() => {
    const onUnpaired = () => setUnpaired(true);
    const onPaired = () => setUnpaired(false);
    window.addEventListener(UNPAIRED_EVENT, onUnpaired);
    window.addEventListener(PAIRED_EVENT, onPaired);
    return () => {
      window.removeEventListener(UNPAIRED_EVENT, onUnpaired);
      window.removeEventListener(PAIRED_EVENT, onPaired);
    };
  }, []);

  useEffect(() => {
    const page = pages.find((item) => item.path === location.pathname);
    document.title = page && page.path !== "/" ? `${page.label} - Daytrace` : "Daytrace";
  }, [location.pathname]);

  const status = health.data
    ? { text: `${health.data.profile} hub`, className: "ok" }
    : health.loading
      ? { text: "Connecting...", className: "" }
      : { text: "Hub unreachable", className: "bad" };

  return (
    <>
      <header className="topbar">
        <Link to="/" className="brand">
          <img src="/icons/icon.svg" alt="" width="28" height="28" />
          <span>Daytrace</span>
        </Link>
        <span className={`hub-status ${status.className}`} title={health.data ? `version ${health.data.version}` : undefined}>
          {status.text}
        </span>
      </header>
      <nav aria-label="Pages">
        {pages.map((page) => (
          <NavLink key={page.path} to={page.path} end>
            {page.label}
          </NavLink>
        ))}
      </nav>
      {health.error && !health.loading && (
        <div className="notice bad" role="alert">
          <span>{health.error.message}</span>
          <button type="button" onClick={health.reload}>
            Try again
          </button>
        </div>
      )}
      {unpaired && location.pathname !== "/devices" && (
        <div className="notice" role="alert">
          <span>This browser isn&apos;t paired with the hub yet, so it can&apos;t show your data.</span>
          <Link to="/devices">Pair it</Link>
        </div>
      )}
      <main>
        <Routes>
          {pages.map((page) => (
            <Route key={page.path} path={page.path} element={page.element} />
          ))}
          <Route
            path="*"
            element={
              <section>
                <h1>Page not found</h1>
                <p className="muted">That page doesn&apos;t exist. Pick one from the menu above.</p>
              </section>
            }
          />
        </Routes>
      </main>
      <Toasts />
    </>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Shell />
    </BrowserRouter>
  );
}
