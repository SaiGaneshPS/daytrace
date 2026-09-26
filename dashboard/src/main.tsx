// DT-30: the entry point. The service worker makes the dashboard installable and caches the app itself; browsers
// allow it only over HTTPS or on localhost (DT-47 brings HTTPS to phones), and elsewhere this does nothing.
// DT-35 adds offline data and its banner.
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { registerSW } from "virtual:pwa-register";
import App from "./App";
import "./styles.css";

registerSW({ immediate: true });

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
