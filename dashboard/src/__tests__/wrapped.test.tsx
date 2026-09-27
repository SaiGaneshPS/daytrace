// DT-54: the Wrapped card rendered from the hub's own answer (the e2e fixture, checked by the hub's tests), with
// useApi mocked: lines that repeat (the plain lines pad with the same one) each show, without React mixing them up.
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import fixture from "../../e2e/fixtures/streaks-wrapped.json";
import Wrapped from "../pages/Wrapped";

let answer: unknown = fixture.wrapped;

vi.mock("../api/client", async (importOriginal) => {
  const real = await importOriginal<typeof import("../api/client")>();
  return { ...real, useApi: () => ({ data: answer, error: undefined, current: true, loading: false, reload: () => undefined }) };
});

afterEach(() => {
  answer = fixture.wrapped;
  vi.restoreAllMocks();
});

describe("Wrapped", () => {
  it("shows every line, even two the same, with no key clash", () => {
    const warned = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const padding = "Every number here comes from your own devices.";
    answer = { ...fixture.wrapped, lines: ["You were on screens for 65 hours 30 minutes.", padding, padding], model: null, fallback: true };
    render(
      <MemoryRouter initialEntries={["/wrapped?week=2026-W38"]}>
        <Wrapped />
      </MemoryRouter>,
    );
    const lines = within(screen.getByRole("list", { name: "The week in three lines" })).getAllByRole("listitem");
    expect(lines.map((line) => line.textContent)).toEqual(["You were on screens for 65 hours 30 minutes.", padding, padding]);
    expect(warned.mock.calls.flat().join(" ")).not.toMatch(/same key/);
  });
});
