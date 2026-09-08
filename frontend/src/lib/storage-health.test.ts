/**
 * Chip labels for storage redundancy (#999 Part A).
 *
 * These are presentation only — the classification lives on the server
 * (see the module docstring) — but the wording is what an operator at a
 * glance actually acts on, and the one that matters is that a mirror
 * rebuilding onto a replacement still says DEGRADED. "Rebuilding 43%"
 * on its own reads as recovery already achieved, when in fact there is
 * exactly one copy of the data until it finishes.
 */

import { describe, expect, it } from "vitest";

import {
  formatEta,
  formatMdLevel,
  mdChipLabel,
  mpathChipLabel,
  storageChipClass,
  storageSeverityClass,
} from "./storage-health";

const healthy = {
  level: "raid1",
  state: "clean",
  members_in_sync: 2,
  members_expected: 2,
  sync: null,
};

describe("mdChipLabel", () => {
  it("names the level and says clean when whole", () => {
    expect(mdChipLabel(healthy)).toBe("RAID1 clean");
  });

  it("carries the member counts on a degraded array", () => {
    expect(
      mdChipLabel({ ...healthy, state: "degraded", members_in_sync: 1 }),
    ).toBe("RAID1 DEGRADED — 1 of 2");
  });

  it("still says DEGRADED while rebuilding", () => {
    expect(
      mdChipLabel({
        ...healthy,
        state: "degraded",
        members_in_sync: 1,
        sync: { action: "recover", percent: 43 },
      }),
    ).toBe("RAID1 DEGRADED — 1 of 2 · rebuilding 43%");
  });

  it("shows a scrub on an intact array as the scrub it is", () => {
    expect(
      mdChipLabel({
        ...healthy,
        state: "syncing",
        sync: { action: "check", percent: 12 },
      }),
    ).toBe("RAID1 check 12%");
  });

  it("refuses to call an unreadable array clean", () => {
    expect(
      mdChipLabel({ ...healthy, state: "unknown", members_expected: null }),
    ).toBe("RAID1 state unknown");
  });

  it("names a failed array", () => {
    expect(
      mdChipLabel({ ...healthy, state: "failed", members_in_sync: 0 }),
    ).toBe("RAID1 FAILED — 0 of 2");
  });

  it("leaves a non-raidN level alone rather than upper-casing it", () => {
    expect(formatMdLevel("linear")).toBe("linear");
    expect(formatMdLevel("raid10")).toBe("RAID10");
  });
});

describe("mpathChipLabel", () => {
  it("reports every path when none is faulted", () => {
    expect(mpathChipLabel({ paths_total: 4, paths_faulted: 0 })).toBe(
      "mpath 4/4 paths",
    );
  });

  it("reports the surviving count when some are faulted", () => {
    expect(mpathChipLabel({ paths_total: 4, paths_faulted: 2 })).toBe(
      "mpath 2/4 paths",
    );
  });
});

describe("formatEta", () => {
  it("is empty for an unknown ETA rather than '0s'", () => {
    // A rebuild with sync_speed 0 has no computable ETA; rendering it as
    // zero would claim it is about to finish.
    expect(formatEta(null)).toBe("");
    expect(formatEta(undefined)).toBe("");
  });

  it("scales units", () => {
    expect(formatEta(45)).toBe("45s");
    expect(formatEta(150)).toBe("2m 30s");
    expect(formatEta(3900)).toBe("1h 5m");
  });
});

describe("storageSeverityClass", () => {
  it("falls back to the healthy style for no finding", () => {
    expect(storageSeverityClass(null)).toContain("emerald");
    expect(storageSeverityClass("critical")).toContain("rose");
    expect(storageSeverityClass("warning")).toContain("amber");
  });
});

describe("storageChipClass", () => {
  it("earns green for an md array with nothing wrong", () => {
    // For md the state IS known, so the absence of a finding is a real
    // statement about the array.
    expect(storageChipClass(null, "md")).toContain("emerald");
  });

  it("never claims a quiet multipath map is healthy", () => {
    // The only per-path signal we can read is the SCSI device state,
    // which stays `running` when multipathd fails a path — so a map with
    // no finding may have silently lost half its paths.
    expect(storageChipClass(null, "multipath")).not.toContain("emerald");
    expect(storageChipClass(null, "multipath")).toContain("muted");
  });

  it("still colours a multipath map that IS complaining", () => {
    expect(storageChipClass("critical", "multipath")).toContain("rose");
    expect(storageChipClass("warning", "multipath")).toContain("amber");
  });
});
