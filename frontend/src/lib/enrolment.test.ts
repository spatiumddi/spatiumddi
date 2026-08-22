/**
 * The enrolment URI is parsed by a shipped client in another repo
 * (spatiumddi-mobile), so these tests pin a CONTRACT rather than an
 * implementation detail. A change that breaks one of them breaks scanning
 * on a device nobody in this repo can see.
 */

import { describe, expect, it } from "vitest";

import {
  buildEnrolmentUri,
  connectionFromLocation,
  displayFingerprint,
  formatHost,
  isDefaultPort,
  isValidPort,
  normaliseFingerprint,
} from "./enrolment";

const FP = "a".repeat(64);

function params(uri: string): URLSearchParams {
  expect(uri.startsWith("spatiumddi://enrol?")).toBe(true);
  return new URLSearchParams(uri.slice("spatiumddi://enrol?".length));
}

describe("buildEnrolmentUri", () => {
  it("carries host, token and fingerprint", () => {
    const p = params(
      buildEnrolmentUri({
        host: "ddi.internal.example",
        port: 8443,
        scheme: "https",
        token: "sddi_abc123",
        fingerprint: FP,
      }),
    );
    expect(p.get("host")).toBe("ddi.internal.example");
    expect(p.get("port")).toBe("8443");
    expect(p.get("token")).toBe("sddi_abc123");
    expect(p.get("fingerprint")).toBe(FP);
  });

  it("omits the port when it is the scheme default", () => {
    expect(
      params(buildEnrolmentUri({ host: "h", port: 443, scheme: "https" })).has(
        "port",
      ),
    ).toBe(false);
    expect(
      params(buildEnrolmentUri({ host: "h", port: 80, scheme: "http" })).has(
        "port",
      ),
    ).toBe(false);
    // …but a non-default port on either scheme is carried.
    expect(
      params(buildEnrolmentUri({ host: "h", port: 80, scheme: "https" })).get(
        "port",
      ),
    ).toBe("80");
  });

  it("states the scheme only when it is http", () => {
    // https is the assumed default; being explicit about the INSECURE case is
    // the right way round, since a code that silently downgrades is the
    // failure worth preventing.
    expect(
      params(buildEnrolmentUri({ host: "h", port: null, scheme: "https" })).has(
        "scheme",
      ),
    ).toBe(false);
    expect(
      params(buildEnrolmentUri({ host: "h", port: null, scheme: "http" })).get(
        "scheme",
      ),
    ).toBe("http");
  });

  it("omits token and fingerprint when absent — a server-only code", () => {
    const p = params(
      buildEnrolmentUri({ host: "h", port: null, scheme: "https" }),
    );
    expect(p.has("token")).toBe(false);
    expect(p.has("fingerprint")).toBe(false);
  });

  it("brackets an IPv6 literal so it cannot be reparsed as host:port", () => {
    const p = params(
      buildEnrolmentUri({ host: "2001:db8::1", port: 8443, scheme: "https" }),
    );
    expect(p.get("host")).toBe("[2001:db8::1]");
    expect(p.get("port")).toBe("8443");
  });

  it("does not double-bracket an already-bracketed literal", () => {
    expect(
      params(
        buildEnrolmentUri({
          host: "[2001:db8::1]",
          port: null,
          scheme: "https",
        }),
      ).get("host"),
    ).toBe("[2001:db8::1]");
  });

  it("escapes reserved characters rather than truncating the URI", () => {
    // A token is opaque; if one ever contained '&' or '#', naive
    // concatenation would silently drop everything after it.
    const token = "sddi_a&b#c d";
    const uri = buildEnrolmentUri({
      host: "h",
      port: null,
      scheme: "https",
      token,
    });
    expect(uri).not.toContain("#");
    expect(params(uri).get("token")).toBe(token);
  });

  it("refuses a port that is not a real port number", () => {
    // The port arrives from a free-text field via `Number(...)`, which answers
    // NaN for anything unparseable. Emitting it would put a literal
    // ``port=NaN`` in the code, which fails on the phone — where the operator
    // has no way to see what went wrong.
    for (const port of [NaN, 0, -1, 70000, 8443.5]) {
      expect(() =>
        buildEnrolmentUri({ host: "h", port, scheme: "https" }),
      ).toThrow();
    }
    expect(isValidPort(1)).toBe(true);
    expect(isValidPort(65535)).toBe(true);
    expect(isValidPort(65536)).toBe(false);
    expect(isValidPort(NaN)).toBe(false);
  });

  it("refuses to build without a host", () => {
    expect(() =>
      buildEnrolmentUri({ host: "  ", port: null, scheme: "https" }),
    ).toThrow();
  });
});

describe("normaliseFingerprint", () => {
  it("accepts bare hex and the colon form, normalising to bare lower-case", () => {
    const colons = displayFingerprint(FP);
    expect(normaliseFingerprint(colons)).toBe(FP);
    expect(normaliseFingerprint(FP.toUpperCase())).toBe(FP);
    expect(normaliseFingerprint(`  ${FP}  `)).toBe(FP);
  });

  it("rejects anything that is not 32 hex bytes", () => {
    // Rejecting matters more than it looks: a malformed fingerprint would
    // make the client report a mismatch against a certificate that is in
    // fact correct, training the operator to click through the one warning
    // this feature exists to make meaningful.
    expect(normaliseFingerprint("")).toBeNull();
    expect(normaliseFingerprint(null)).toBeNull();
    expect(normaliseFingerprint("a".repeat(63))).toBeNull();
    expect(normaliseFingerprint("a".repeat(65))).toBeNull();
    expect(normaliseFingerprint("z".repeat(64))).toBeNull();
    expect(normaliseFingerprint("sha256:" + FP)).toBeNull();
  });

  it("keeps a malformed fingerprint out of the URI entirely", () => {
    const p = params(
      buildEnrolmentUri({
        host: "h",
        port: null,
        scheme: "https",
        fingerprint: "nonsense",
      }),
    );
    expect(p.has("fingerprint")).toBe(false);
  });
});

describe("connectionFromLocation", () => {
  it("reads scheme, host and explicit port from the browser's own URL", () => {
    expect(
      connectionFromLocation({
        protocol: "https:",
        hostname: "ddi.example",
        port: "8443",
      } as Location),
    ).toEqual({ scheme: "https", host: "ddi.example", port: 8443 });
  });

  it("reports a scheme-default port as null, not as an empty string", () => {
    expect(
      connectionFromLocation({
        protocol: "https:",
        hostname: "h",
        port: "",
      } as Location),
    ).toEqual({ scheme: "https", host: "h", port: null });
  });

  it("treats anything that is not http: as https", () => {
    expect(
      connectionFromLocation({
        protocol: "http:",
        hostname: "h",
        port: "",
      } as Location).scheme,
    ).toBe("http");
    expect(
      connectionFromLocation({
        protocol: "https:",
        hostname: "h",
        port: "",
      } as Location).scheme,
    ).toBe("https");
  });
});

describe("helpers", () => {
  it("formatHost leaves hostnames and IPv4 untouched", () => {
    expect(formatHost("ddi.example")).toBe("ddi.example");
    expect(formatHost("192.0.2.10")).toBe("192.0.2.10");
    expect(formatHost(" ddi.example ")).toBe("ddi.example");
  });

  it("isDefaultPort treats null as the default", () => {
    expect(isDefaultPort("https", null)).toBe(true);
    expect(isDefaultPort("https", 443)).toBe(true);
    expect(isDefaultPort("https", 8443)).toBe(false);
    expect(isDefaultPort("http", 80)).toBe(true);
  });

  it("displayFingerprint groups into colon-separated upper-case bytes", () => {
    const shown = displayFingerprint(FP);
    expect(shown).toBe(Array(32).fill("AA").join(":"));
    // …and round-trips back to the wire form.
    expect(normaliseFingerprint(shown)).toBe(FP);
  });
});
