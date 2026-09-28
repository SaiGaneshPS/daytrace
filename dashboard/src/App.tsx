// DT-30 + DT-52: the app shell. Wide screens get a sidebar; phones get a top bar and a bottom nav (BottomNav).
// DT-58: inside the Android app (?embed=1) there is none of them: the app has its own tabs.
// Pages fade in and their cards follow one after another; everything respects reduced motion (MotionConfig).
// Also here: the hub's status (a live dot), pairing and error notices, toasts, and page titles.
// Each page is filled in by its own ticket (Today DT-31, Devices DT-32, Story and Ask DT-33, Insights and Wrapped
// DT-34, Streaks and Wrapped DT-54, Privacy DT-36). /styleguide shows the design system (DT-52) and isn't in the menu.
import { AnimatePresence, MotionConfig, motion } from "motion/react";
import { Component, type ReactNode, Suspense, lazy, useEffect, useState } from "react";
import { BrowserRouter, Link, NavLink, Route, Routes, useLocation } from "react-router";
import { PAIRED_EVENT, UNPAIRED_EVENT, dismissToast, useApi, useToasts } from "./api/client";
import BottomNav, { Icon, type NavItem } from "./components/BottomNav";
import { embedded } from "./embed";
import Skeleton, { SkeletonText } from "./components/Skeleton";
import { pageVariants } from "./theme/motion";

// Pages load when first opened, so a page without charts never downloads the chart library.
const Today = lazy(() => import("./pages/Today"));
const Story = lazy(() => import("./pages/Story"));
const Ask = lazy(() => import("./pages/Ask"));
const Insights = lazy(() => import("./pages/Insights"));
const Streaks = lazy(() => import("./pages/Streaks"));
const Wrapped = lazy(() => import("./pages/Wrapped"));
const Devices = lazy(() => import("./pages/Devices"));
const Privacy = lazy(() => import("./pages/Privacy"));
const Styleguide = lazy(() => import("./pages/Styleguide"));

type Page = NavItem & { element: ReactNode };

const pages: Page[] = [
  { path: "/", label: "Today", icon: "today", element: <Today /> },
  { path: "/story", label: "Story", icon: "story", element: <Story /> },
  { path: "/ask", label: "Ask", icon: "ask", element: <Ask /> },
  { path: "/insights", label: "Insights", icon: "insights", element: <Insights /> },
  { path: "/streaks", label: "Streaks", icon: "streaks", element: <Streaks /> },
  { path: "/wrapped", label: "Wrapped", icon: "wrapped", element: <Wrapped /> },
  { path: "/devices", label: "Devices", icon: "devices", element: <Devices /> },
  { path: "/privacy", label: "Privacy", icon: "privacy", element: <Privacy /> },
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

/** A page that fails to load (the hub restarted with a new build, or can't be reached) shows this instead of
 * blanking the app; the menus keep working. A loaded page that breaks lands here too. */
class PageError extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <section className="card" role="alert">
        <h1>This page couldn&apos;t load</h1>
        <p className="muted">
          The hub may have restarted with a new version, or it can&apos;t be reached right now. Reloading tries again.
        </p>
        <button type="button" className="button" onClick={() => window.location.reload()}>
          Reload
        </button>
      </section>
    );
  }
}

function PageLoading() {
  return (
    <div className="stack" aria-busy="true">
      <span className="visually-hidden">Loading the page</span>
      <Skeleton height={140} radius={20} />
      <SkeletonText lines={3} />
    </div>
  );
}

function Brand() {
  return (
    <Link to="/" className="brand">
      <img src="/icons/icon.svg" alt="" width="32" height="32" />
      <span>Daytrace</span>
    </Link>
  );
}

function HubStatus({ text, state, title }: { text: string; state: "ok" | "bad" | "wait"; title?: string }) {
  return (
    <span className={`hub-status ${state}`} title={title}>
      <span className={`live-dot${state === "ok" ? " live" : ""}`} aria-hidden="true" />
      {text}
    </span>
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
    ? { text: `${health.data.profile} hub`, state: "ok" as const, title: `version ${health.data.version}` }
    : health.loading
      ? { text: "Connecting...", state: "wait" as const }
      : { text: "Hub unreachable", state: "bad" as const };

  return (
    <div className={embedded ? "app embedded" : "app"}>
      <a className="skip-link" href="#content">
        Skip to content
      </a>
      {!embedded && (
        <aside className="sidebar">
          <Brand />
          <nav aria-label="Pages" className="sidebar-nav">
            {pages.map((page) => (
              <NavLink key={page.path} to={page.path} end className="sidebar-item">
                <Icon name={page.icon} />
                <span>{page.label}</span>
              </NavLink>
            ))}
          </nav>
          <HubStatus {...status} />
        </aside>
      )}
      <div className="main-column">
        {!embedded && (
          <header className="topbar">
            <Brand />
            <HubStatus {...status} />
          </header>
        )}
        {health.error && !health.loading && (
          <div className="notice bad" role="alert">
            <span>{health.error.message}</span>
            <button type="button" onClick={health.reload}>
              Try again
            </button>
          </div>
        )}
        {unpaired && embedded && (
          <div className="notice" role="alert">
            <span>
              The hub didn&apos;t accept this phone&apos;s dashboard. In Daytrace, open More, then This phone, and pair
              with your hub again.
            </span>
          </div>
        )}
        {unpaired && !embedded && location.pathname !== "/devices" && (
          <div className="notice" role="alert">
            <span>This browser isn&apos;t paired with the hub yet, so it can&apos;t show your data.</span>
            <Link to="/devices">Pair it</Link>
          </div>
        )}
        <main id="content" tabIndex={-1}>
          <AnimatePresence mode="wait" initial={false}>
            <motion.div key={location.pathname} variants={pageVariants} initial="initial" animate="enter" exit="exit">
              <PageError>
                <Suspense fallback={<PageLoading />}>
                  <Routes location={location}>
                    {pages.map((page) => (
                      <Route key={page.path} path={page.path} element={page.element} />
                    ))}
                    <Route path="/styleguide" element={<Styleguide />} />
                    <Route
                      path="*"
                      element={
                        <section>
                          <h1>Page not found</h1>
                          <p className="muted">That page doesn&apos;t exist. Pick one from the menu.</p>
                        </section>
                      }
                    />
                  </Routes>
                </Suspense>
              </PageError>
            </motion.div>
          </AnimatePresence>
        </main>
      </div>
      {!embedded && <BottomNav items={pages} />}
      <Toasts />
    </div>
  );
}

export default function App() {
  return (
    <MotionConfig reducedMotion="user">
      <BrowserRouter>
        <Shell />
      </BrowserRouter>
    </MotionConfig>
  );
}
