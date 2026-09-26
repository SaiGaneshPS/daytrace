// DT-52: the phone navigation: four pages and "More" at the bottom of the screen, within thumb reach, every target
// at least 44 px. "More" opens a sheet with the other pages. Hidden on wide screens, where the sidebar takes over.
// The icons live here too, so the sidebar shows the same ones.
import { AnimatePresence, motion } from "motion/react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { NavLink, useLocation } from "react-router";
import { EASE } from "../theme/motion";

export type IconName = "today" | "story" | "ask" | "insights" | "wrapped" | "devices" | "privacy" | "more";
export type NavItem = { path: string; label: string; icon: IconName };

const PATHS: Record<IconName, ReactNode> = {
  today: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3.5 2" />
    </>
  ),
  story: (
    <>
      <path d="M5 4.5h10.5A3.5 3.5 0 0 1 19 8v12H8.5A3.5 3.5 0 0 1 5 16.5z" />
      <path d="M5 16.5A3.5 3.5 0 0 1 8.5 13H19M9 8.5h6" />
    </>
  ),
  ask: <path d="M20.5 12a8.5 8.5 0 0 1-12.4 7.6L3.5 20.5l1-4.4A8.5 8.5 0 1 1 20.5 12zM8.5 12h.01M12 12h.01M15.5 12h.01" />,
  insights: <path d="M4 20h16M6.5 16.5v-5M11.5 16.5V5.5M16.5 16.5v-8" />,
  wrapped: (
    <>
      <path d="M11 3.5l1.8 4.9 4.9 1.8-4.9 1.8L11 16.9l-1.8-4.9-4.9-1.8 4.9-1.8z" />
      <path d="M18 15l.8 2.2 2.2.8-2.2.8L18 21l-.8-2.2-2.2-.8 2.2-.8z" />
    </>
  ),
  devices: (
    <>
      <rect x="2.5" y="4.5" width="13" height="9.5" rx="1.5" />
      <path d="M1.5 17.5h15" />
      <rect x="17.5" y="8" width="5" height="12" rx="1.2" />
    </>
  ),
  privacy: (
    <>
      <path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.4-7.5 9.5-4.3-1.1-7.5-4.9-7.5-9.5V6z" />
      <path d="M9 12l2.2 2.2L15.5 10" />
    </>
  ),
  more: <path d="M5 12h.01M12 12h.01M19 12h.01" strokeWidth="3.2" />,
};

export function Icon({ name, size = 24 }: { name: IconName; size?: number }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {PATHS[name]}
    </svg>
  );
}

const PRIMARY = 4;

export default function BottomNav({ items }: { items: NavItem[] }) {
  const location = useLocation();
  const [open, setOpen] = useState(false);
  const sheet = useRef<HTMLDivElement>(null);
  const moreButton = useRef<HTMLButtonElement>(null);
  const primary = items.slice(0, PRIMARY);
  const more = items.slice(PRIMARY);
  const inMore = more.some((item) => item.path === location.pathname);

  useEffect(() => setOpen(false), [location.pathname]);

  useEffect(() => {
    if (!open) return;
    sheet.current?.querySelector<HTMLElement>("a")?.focus();
    const button = moreButton.current;
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && setOpen(false);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      button?.focus();
    };
  }, [open]);

  return (
    <>
      <nav className="bottom-nav" aria-label="Pages">
        {primary.map((item) => (
          <NavLink key={item.path} to={item.path} end className="bottom-nav-item">
            <Icon name={item.icon} />
            <span>{item.label}</span>
          </NavLink>
        ))}
        {more.length > 0 && (
          <button
            ref={moreButton}
            type="button"
            className={`bottom-nav-item${inMore ? " active" : ""}`}
            aria-expanded={open}
            aria-haspopup="dialog"
            onClick={() => setOpen((shown) => !shown)}
          >
            <Icon name="more" />
            <span>More</span>
          </button>
        )}
      </nav>
      <AnimatePresence>
        {open && (
          <>
            <motion.div
              className="sheet-backdrop"
              onClick={() => setOpen(false)}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
            />
            <motion.div
              ref={sheet}
              className="sheet"
              role="dialog"
              aria-modal="true"
              aria-label="More pages"
              initial={{ y: "100%" }}
              animate={{ y: 0, transition: { duration: 0.28, ease: EASE } }}
              exit={{ y: "100%", transition: { duration: 0.18, ease: EASE } }}
            >
              <span className="sheet-handle" aria-hidden="true" />
              {more.map((item) => (
                <NavLink key={item.path} to={item.path} end className="sheet-item">
                  <Icon name={item.icon} />
                  <span>{item.label}</span>
                </NavLink>
              ))}
            </motion.div>
          </>
        )}
      </AnimatePresence>
    </>
  );
}
