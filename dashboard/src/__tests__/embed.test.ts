// DT-58: inside the Android app the dashboard has no service worker (the app checks every request its page makes),
// and removes one an earlier visit registered; a browser gets it as before.
import { describe, expect, it, vi } from "vitest";
import { startServiceWorker } from "../embed";

function container(count: number) {
  const unregister = vi.fn(async () => true);
  const registrations = Array.from({ length: count }, () => ({ unregister }));
  return { container: { getRegistrations: vi.fn(async () => registrations) } as unknown as ServiceWorkerContainer, unregister };
}

describe("the service worker", () => {
  it("is registered in a browser", async () => {
    const register = vi.fn();
    const { container: sw, unregister } = container(0);
    await startServiceWorker(false, register, sw);
    expect(register).toHaveBeenCalledTimes(1);
    expect(unregister).not.toHaveBeenCalled();
  });

  it("is never registered inside the app, and one from before is removed", async () => {
    const register = vi.fn();
    const { container: sw, unregister } = container(2);
    await startServiceWorker(true, register, sw);
    expect(register).not.toHaveBeenCalled();
    expect(unregister).toHaveBeenCalledTimes(2);
  });

  it("inside the app, a WebView without service workers is fine", async () => {
    const register = vi.fn();
    await startServiceWorker(true, register, undefined);
    expect(register).not.toHaveBeenCalled();
  });
});
