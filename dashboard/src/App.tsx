// DT-1 skeleton: navigation and one route per page. DT-30 refines the shell (auth, offline, layout).
import { BrowserRouter, NavLink, Route, Routes } from "react-router";
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

export default function App() {
  return (
    <BrowserRouter>
      <nav>
        {pages.map((page) => (
          <NavLink key={page.path} to={page.path} end>
            {page.label}
          </NavLink>
        ))}
      </nav>
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
    </BrowserRouter>
  );
}
