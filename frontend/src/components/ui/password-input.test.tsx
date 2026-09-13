/**
 * @vitest-environment jsdom
 *
 * PasswordInput — the reveal toggle (#1074).
 *
 * Each of these pins a way the component can be subtly wrong while
 * looking right in review and passing `tsc`:
 *
 * - a toggle that submits the form it sits in (a bare <button> defaults
 *   to type="submit");
 * - a reveal that leaks across mounts, i.e. a password shown on arrival;
 * - one toggle driving every field on the page;
 * - a wrapper that swallows the caller's className, silently dropping the
 *   Confirm field's mismatch border.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { PasswordInput } from "./password-input";

afterEach(cleanup);

describe("PasswordInput", () => {
  it("starts masked and flips to text on click", () => {
    render(<PasswordInput id="p" defaultValue="hunter2" />);
    const input = document.getElementById("p") as HTMLInputElement;
    expect(input.type).toBe("password");

    fireEvent.click(screen.getByRole("button", { name: "Show password" }));
    expect(input.type).toBe("text");

    fireEvent.click(screen.getByRole("button", { name: "Hide password" }));
    expect(input.type).toBe("password");
  });

  it("does not submit the form it is inside", () => {
    const onSubmit = vi.fn((e: React.FormEvent) => e.preventDefault());
    render(
      <form onSubmit={onSubmit}>
        <PasswordInput id="p" />
      </form>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Show password" }));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("carries the pressed state for assistive tech", () => {
    render(<PasswordInput id="p" />);
    const button = screen.getByRole("button");
    expect(button.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(button);
    expect(button.getAttribute("aria-pressed")).toBe("true");
  });

  it("never starts revealed — state does not survive a remount", () => {
    const { unmount } = render(<PasswordInput id="p" />);
    fireEvent.click(screen.getByRole("button", { name: "Show password" }));
    expect((document.getElementById("p") as HTMLInputElement).type).toBe(
      "text",
    );
    unmount();

    render(<PasswordInput id="p" />);
    expect((document.getElementById("p") as HTMLInputElement).type).toBe(
      "password",
    );
  });

  it("each field toggles independently", () => {
    render(
      <>
        <PasswordInput id="a" />
        <PasswordInput id="b" />
      </>,
    );
    const [first] = screen.getAllByRole("button", { name: "Show password" });
    fireEvent.click(first);

    expect((document.getElementById("a") as HTMLInputElement).type).toBe(
      "text",
    );
    expect((document.getElementById("b") as HTMLInputElement).type).toBe(
      "password",
    );
  });

  it("keeps the caller's className and autoComplete on the input", () => {
    render(
      <PasswordInput
        id="p"
        className="border-destructive"
        autoComplete="new-password"
      />,
    );
    const input = document.getElementById("p") as HTMLInputElement;
    // The Confirm field's mismatch border rides on className; a wrapper
    // that dropped it would lose the error state with no other symptom.
    expect(input.className).toContain("border-destructive");
    // Password managers key off the attribute, not the type, so it has to
    // survive the flip to text.
    expect(input.getAttribute("autocomplete")).toBe("new-password");
    fireEvent.click(screen.getByRole("button"));
    expect(input.getAttribute("autocomplete")).toBe("new-password");
  });
});
