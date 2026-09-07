/**
 * @vitest-environment jsdom
 *
 * SSH lockdown — the wiring around a lockout-capable switch (#1009).
 *
 * `ssh_lockdown` retires the appliance's always-open port-22 floor, so the
 * three things pinned here are the ones whose failure is invisible: an
 * acknowledgement path that can never open, a toggle that cannot be turned
 * off, and a forced save that latches. `tsc` is happy with all three, and so
 * is a reading of the diff — each was found by review, not by the compiler.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { PlatformSettings } from "@/lib/api";

const update = vi.fn();
vi.mock("@/lib/api", async () => {
  // formatApiError is the thing under test in one case, so use the REAL one:
  // a stub would pass whatever err.message the component read and hide
  // exactly the defect this file exists for.
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    settingsApi: { update: (p: unknown) => update(p) },
  };
});

vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({ setQueryData: vi.fn() }),
  useMutation: ({ mutationFn, onSuccess, onError }: never) => {
    const fn = mutationFn as (p: unknown) => Promise<unknown>;
    return {
      isPending: false,
      mutate: (p: unknown) =>
        fn(p).then(
          (r) => (onSuccess as ((r: unknown) => void) | undefined)?.(r),
          (e) => (onError as ((e: unknown) => void) | undefined)?.(e),
        ),
    };
  },
}));

const { SSHSection } = await import("./SSHSection");

/** The shape an AxiosError actually has: `message` is the generic status
 *  line, and the server's sentence lives under `response.data.detail`. */
function axios422(detail: string) {
  return Object.assign(new Error("Request failed with status code 422"), {
    isAxiosError: true,
    response: { status: 422, data: { detail } },
  });
}

const LOCKOUT_DETAIL =
  "Your own address is not inside the allowed networks, so enforcing this " +
  "restriction may close your SSH access to every appliance. Re-send with " +
  "ssh_lockdown_force to proceed.";

function values(over: Partial<PlatformSettings> = {}): PlatformSettings {
  return {
    ssh_authorized_keys: [],
    ssh_password_auth_enabled: true,
    ssh_allow_root_login: false,
    ssh_port: 22,
    ssh_allowed_source_networks: [],
    ssh_lockdown: false,
    ...over,
  } as PlatformSettings;
}

function setup(over: Partial<PlatformSettings> = {}) {
  render(
    <SSHSection
      values={values(over)}
      isSuperadmin
      applianceMode
      inputCls="input"
    />,
  );
  // Toggle renders a role="switch" <button> with aria-checked — not an
  // <input>, so `.checked` and getByRole("checkbox") both miss it.
  const enforce = screen.getByRole("switch", {
    name: /enforce source restriction/i,
  }) as HTMLButtonElement;
  return {
    enforce,
    isOn: () => enforce.getAttribute("aria-checked") === "true",
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("the enforcement toggle", () => {
  it("cannot be turned on without a network", () => {
    const { enforce, isOn } = setup({ ssh_allowed_source_networks: [] });
    expect(enforce.disabled).toBe(true);
    expect(isOn()).toBe(false);
  });

  it("can always be turned OFF, even with no networks left", () => {
    // Gating both directions on the list stranded an operator who removed
    // their last CIDR: checked AND disabled, Save 422s, no way out without
    // re-adding a CIDR or reloading.
    const { enforce, isOn } = setup({
      ssh_allowed_source_networks: [],
      ssh_lockdown: true,
    });
    expect(isOn()).toBe(true);
    expect(enforce.disabled).toBe(false);
    fireEvent.click(enforce);
    expect(isOn()).toBe(false);
  });

  it("says why saving as-is would be refused", () => {
    setup({ ssh_allowed_source_networks: [], ssh_lockdown: true });
    expect(screen.getByText(/close SSH from everywhere/i)).toBeTruthy();
  });

  it("is available once a network exists", () => {
    const { enforce } = setup({ ssh_allowed_source_networks: ["10.0.0.0/8"] });
    expect(enforce.disabled).toBe(false);
  });
});

describe("the self-lockout acknowledgement", () => {
  async function saveFrom(over: Partial<PlatformSettings>) {
    const { enforce } = setup(over);
    fireEvent.click(enforce);
    fireEvent.click(screen.getByRole("button", { name: /save ssh settings/i }));
    return enforce;
  }

  it("opens the confirmation on the server's 422, quoting its own words", async () => {
    // The defect: on an AxiosError `err.message` is always the generic status
    // line, never the detail — so matching on it never fired and the modal
    // (and ssh_lockdown_force with it) was unreachable from the UI.
    update.mockRejectedValueOnce(axios422(LOCKOUT_DETAIL));
    await saveFrom({ ssh_allowed_source_networks: ["10.0.0.0/8"] });

    await screen.findByText(/not inside the allowed networks/i);
    expect(
      screen.getByRole("button", { name: /enforce anyway/i }),
    ).toBeTruthy();
  });

  it("re-sends with the acknowledgement once confirmed", async () => {
    update.mockRejectedValueOnce(axios422(LOCKOUT_DETAIL));
    update.mockResolvedValueOnce(
      values({
        ssh_allowed_source_networks: ["10.0.0.0/8"],
        ssh_lockdown: true,
      }),
    );
    await saveFrom({ ssh_allowed_source_networks: ["10.0.0.0/8"] });
    await screen.findByText(/not inside the allowed networks/i);

    fireEvent.click(
      screen.getByLabelText(/I understand this may close my SSH access/i),
    );
    fireEvent.click(screen.getByRole("button", { name: /enforce anyway/i }));

    await waitFor(() => expect(update).toHaveBeenCalledTimes(2));
    expect(update.mock.calls[0][0].ssh_lockdown_force).toBeUndefined();
    expect(update.mock.calls[1][0].ssh_lockdown_force).toBe(true);
  });

  it("never latches the acknowledgement onto a later save", async () => {
    // It used to be remembered in state and cleared only on success, so a
    // forced save that failed for ANY other reason left every subsequent
    // save silently forced.
    update.mockRejectedValueOnce(axios422(LOCKOUT_DETAIL));
    update.mockRejectedValueOnce(axios422("something else entirely"));
    await saveFrom({ ssh_allowed_source_networks: ["10.0.0.0/8"] });
    await screen.findByText(/not inside the allowed networks/i);
    fireEvent.click(
      screen.getByLabelText(/I understand this may close my SSH access/i),
    );
    fireEvent.click(screen.getByRole("button", { name: /enforce anyway/i }));
    await waitFor(() => expect(update).toHaveBeenCalledTimes(2));

    update.mockResolvedValueOnce(values());
    fireEvent.click(screen.getByRole("button", { name: /save ssh settings/i }));
    await waitFor(() => expect(update).toHaveBeenCalledTimes(3));
    expect(update.mock.calls[2][0].ssh_lockdown_force).toBeUndefined();
  });

  it("shows an unrelated failure as an error, not as a confirmation", async () => {
    update.mockRejectedValueOnce(axios422("ssh_port must be 1-65535"));
    await saveFrom({ ssh_allowed_source_networks: ["10.0.0.0/8"] });

    await screen.findByText(/ssh_port must be 1-65535/i);
    expect(
      screen.queryByRole("button", { name: /enforce anyway/i }),
    ).toBeNull();
  });
});
