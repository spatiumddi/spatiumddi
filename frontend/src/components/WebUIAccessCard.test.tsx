/**
 * @vitest-environment jsdom
 *
 * Web UI source restriction — the two acknowledgement paths (#285, #1013).
 *
 * The failure mode this pins is a modal or a checkbox that never appears:
 * both 422s arrive as the same status with a different sentence, and if the
 * component routes on prose rather than on the acknowledgement field each one
 * names, the operator is offered a tick the server will refuse. `tsc` is
 * happy either way, and so is a reading of the diff — the identical defect on
 * the SSH side was found by review, not by the compiler.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

const setWebUIAccess = vi.fn();
const access = {
  allowed_cidrs: [] as string[],
  open: true,
  caller_ip: "203.0.113.9",
  caller_covered: true,
};

vi.mock("@/lib/api", async () => {
  // The REAL formatApiError: a stub would return whatever err.message the
  // component read and hide the defect this file exists for.
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    firewallApi: {
      getWebUIAccess: () => Promise.resolve(access),
      setWebUIAccess: (b: unknown) => setWebUIAccess(b),
    },
    applianceApi: { getRemoteAccess: () => Promise.resolve(doors) },
  };
});

/** What the door report resolves to; `undefined` models it being unreadable
 *  (a 403 on a deploy where the caller lacks appliance-read), which must
 *  leave the card rendering rather than blanking it. */
let doors: unknown;

vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
  useQuery: ({ queryKey }: { queryKey: unknown[] }) => ({
    data: queryKey[0] === "appliance" ? doors : access,
  }),
  useMutation: ({ mutationFn, onSuccess }: never) => {
    const fn = mutationFn as () => Promise<unknown>;
    return {
      isPending: false,
      isError: state.isError,
      error: state.error,
      // Modelled, not stubbed: the component calls reset() to clear a stale
      // refusal when the list changes, and a no-op stub would let the
      // latching test pass while the real thing still latched.
      reset: () => {
        state.isError = false;
        state.error = null;
        rerender?.();
      },
      mutate: () =>
        fn().then(
          (r) => (onSuccess as ((r: unknown) => void) | undefined)?.(r),
          (e) => {
            state.isError = true;
            state.error = e;
            rerender?.();
          },
        ),
    };
  },
}));

/** The mutation mock has to survive a re-render to expose isError/error the
 *  way React Query would, so the state lives outside it. */
const state: { isError: boolean; error: unknown } = {
  isError: false,
  error: null,
};
let rerender: (() => void) | undefined;

const { WebUIAccessCard } = await import("./WebUIAccessCard");

function axios422(detail: string) {
  return Object.assign(new Error("Request failed with status code 422"), {
    isAxiosError: true,
    response: { status: 422, data: { detail } },
  });
}

const LOCKOUT_DETAIL =
  "Refusing to restrict the Web UI: your current source IP (203.0.113.9) is " +
  "not covered by the allow-list, so this would lock you out of the very " +
  "session making the change. Add your IP / network to the list, or pass " +
  "override_lockout=true. SSH is not source-restricted, and the appliance " +
  "console recovers this either way.";

/** Note it contains NEITHER "lock you out" NOR "override_lockout" — routing
 *  on prose would send this to the wrong tick. */
const CONSOLE_ONLY_DETAIL =
  "Refusing to close the last remote way in. After this change neither the " +
  "Web UI source restriction nor the SSH source restriction would admit the " +
  "address you are connecting from (203.0.113.9). Unless you can reach one " +
  "of those networks another way, the appliance console becomes the only " +
  "way into this fleet — and a VM with no console attached would need a " +
  "rebuild. Web UI allows 10.0.0.0/8; SSH allows 10.0.0.0/8. Add your " +
  "network to one of them, or re-send with acknowledge_console_only=true.";

function report(over: { sshAdmits?: boolean; sshRestricted?: boolean } = {}) {
  return {
    caller_ip: "203.0.113.9",
    web_ui: {
      name: "web_ui",
      restricted: false,
      allowed_cidrs: [],
      admits: true,
    },
    ssh: {
      name: "ssh",
      restricted: over.sshRestricted ?? true,
      allowed_cidrs: ["10.0.0.0/8"],
      admits: over.sshAdmits ?? false,
    },
    console_only: false,
  };
}

function openModal() {
  const { rerender: r } = render(<WebUIAccessCard />);
  rerender = () => r(<WebUIAccessCard />);
  fireEvent.click(screen.getByRole("button", { name: /edit/i }));
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  doors = undefined;
  state.isError = false;
  state.error = null;
  rerender = undefined;
});

describe("the other door (#1013)", () => {
  it("warns in the modal when SSH already excludes you", () => {
    doors = report();
    openModal();
    expect(screen.getByText(/SSH is also source-restricted/i)).toBeTruthy();
  });

  it("says nothing when SSH still admits you", () => {
    doors = report({ sshAdmits: true });
    openModal();
    expect(screen.queryByText(/SSH is also source-restricted/i)).toBeNull();
  });

  it("renders normally when the door report cannot be read", () => {
    doors = undefined;
    openModal();
    expect(screen.getByText(/Allowed source ranges/i)).toBeTruthy();
    expect(screen.queryByText(/SSH is also source-restricted/i)).toBeNull();
  });
});

describe("the acknowledgement routing", () => {
  async function saveWith(detail: string) {
    setWebUIAccess.mockRejectedValueOnce(axios422(detail));
    openModal();
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "10.0.0.0/8" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
  }

  it("offers the per-door tick for the per-door 422", async () => {
    await saveWith(LOCKOUT_DETAIL);
    await screen.findByText(/cut off this session/i);
    expect(screen.queryByText(/last remote way in/i)).toBeNull();
  });

  it("offers the escalation tick for the escalation 422", async () => {
    await saveWith(CONSOLE_ONLY_DETAIL);
    await screen.findByText(/closes the last remote way in/i);
    // …and NOT the smaller one, whose flag the server refuses for this case.
    expect(screen.queryByText(/cut off this session/i)).toBeNull();
  });

  it("keeps Save disabled until the escalation is ticked", async () => {
    await saveWith(CONSOLE_ONLY_DETAIL);
    await screen.findByText(/closes the last remote way in/i);
    const save = screen.getByRole("button", {
      name: /^save$/i,
    }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);

    fireEvent.click(screen.getByRole("checkbox"));
    await waitFor(() =>
      expect(
        (screen.getByRole("button", { name: /^save$/i }) as HTMLButtonElement)
          .disabled,
      ).toBe(false),
    );
  });

  it("sends the escalation flag once ticked, and not the smaller one", async () => {
    await saveWith(CONSOLE_ONLY_DETAIL);
    await screen.findByText(/closes the last remote way in/i);
    fireEvent.click(screen.getByRole("checkbox"));
    setWebUIAccess.mockResolvedValueOnce(access);
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(setWebUIAccess).toHaveBeenCalledTimes(2));
    expect(setWebUIAccess.mock.calls[1][0]).toEqual({
      allowed_cidrs: ["10.0.0.0/8"],
      override_lockout: false,
      acknowledge_console_only: true,
    });
  });
});

describe("an acknowledgement belongs to the list that earned it", () => {
  async function acknowledgeThen(edit: () => void) {
    setWebUIAccess.mockRejectedValueOnce(axios422(CONSOLE_ONLY_DETAIL));
    openModal();
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "10.0.0.0/8" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    await screen.findByText(/closes the last remote way in/i);
    fireEvent.click(screen.getByRole("checkbox"));
    edit();
  }

  it("drops the escalation tick when the list is retyped", async () => {
    // Acknowledge for list A, edit to list B, Save: the flag must not carry
    // over, or list B is applied past a guard the server never evaluated.
    await acknowledgeThen(() =>
      fireEvent.change(screen.getByRole("textbox"), {
        target: { value: "192.168.0.0/16" },
      }),
    );
    setWebUIAccess.mockResolvedValueOnce(access);
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(setWebUIAccess).toHaveBeenCalledTimes(2));
    expect(setWebUIAccess.mock.calls[1][0]).toEqual({
      allowed_cidrs: ["192.168.0.0/16"],
      override_lockout: false,
      acknowledge_console_only: false,
    });
  });

  it("clears the stale refusal with it", async () => {
    // The warning described list A. Leaving it on screen next to list B
    // would offer a tick for a state nothing has evaluated.
    await acknowledgeThen(() =>
      fireEvent.change(screen.getByRole("textbox"), {
        target: { value: "192.168.0.0/16" },
      }),
    );
    await waitFor(() =>
      expect(screen.queryByText(/closes the last remote way in/i)).toBeNull(),
    );
  });

  it("drops it when the list is cleared with the button", async () => {
    await acknowledgeThen(() =>
      fireEvent.click(screen.getByRole("button", { name: /open to all/i })),
    );
    setWebUIAccess.mockResolvedValueOnce(access);
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(setWebUIAccess).toHaveBeenCalledTimes(2));
    expect(setWebUIAccess.mock.calls[1][0].acknowledge_console_only).toBe(
      false,
    );
  });

  it("drops it when Add my IP changes the list", async () => {
    await acknowledgeThen(() =>
      fireEvent.click(screen.getByRole("button", { name: /add my ip/i })),
    );
    setWebUIAccess.mockResolvedValueOnce(access);
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(setWebUIAccess).toHaveBeenCalledTimes(2));
    expect(setWebUIAccess.mock.calls[1][0].acknowledge_console_only).toBe(
      false,
    );
  });
});
