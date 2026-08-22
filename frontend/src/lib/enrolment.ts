/**
 * Enrolment URI for the native client (issue #906).
 *
 * The mobile client (spatiumddi-mobile) already parses this format, so the
 * shape below is a CONTRACT, not a local convention — see #906 for the
 * canonical table. Changing a parameter name here breaks a shipped parser in
 * another repo.
 *
 *   spatiumddi://enrol?host=…&port=…&scheme=…&token=…&fingerprint=…
 *
 * | param       | required | notes                                        |
 * |-------------|----------|----------------------------------------------|
 * | host        | yes      | hostname or IP literal (v4 or v6)            |
 * | port        | no       | omitted when it is the scheme default        |
 * | scheme      | no       | https assumed; http must be explicit         |
 * | token       | no       | omit for a code that only configures a server|
 * | fingerprint | no       | SHA-256 of the leaf cert, hex, colons optional|
 *
 * Everything here is pure so it can be tested without a DOM; the connection
 * details come from the browser (see `connectionFromLocation`) because the
 * address the operator actually reached the server on is the only thing
 * either side reliably knows. The server behind a proxy does not know its
 * own external URL, and would guess wrong on exactly the split-DNS and NAT
 * deployments this is most useful for.
 */

export type EnrolmentScheme = "https" | "http";

export interface EnrolmentConnection {
  host: string;
  /** `null` means "the scheme default", which is what gets omitted. */
  port: number | null;
  scheme: EnrolmentScheme;
}

export interface EnrolmentPayload extends EnrolmentConnection {
  token?: string | null;
  fingerprint?: string | null;
}

export const ENROLMENT_SCHEME = "spatiumddi";
export const ENROLMENT_ACTION = "enrol";

const DEFAULT_PORTS: Record<EnrolmentScheme, number> = {
  https: 443,
  http: 80,
};

/** A SHA-256 hex digest, with or without the conventional colon separators. */
const FINGERPRINT_RE = /^[0-9a-f]{2}(:?[0-9a-f]{2}){31}$/i;

/**
 * Strip separators and lower-case a SHA-256 fingerprint.
 *
 * Returns `null` for anything that is not 32 hex bytes. Rejecting rather than
 * passing the value through matters: a malformed fingerprint in the QR would
 * make the client report a mismatch against a certificate that is in fact
 * correct, and an operator taught to click through that warning is worse off
 * than one who was never offered the check.
 */
export function normaliseFingerprint(
  value: string | null | undefined,
): string | null {
  if (!value) return null;
  const trimmed = value.trim();
  if (!FINGERPRINT_RE.test(trimmed)) return null;
  return trimmed.replace(/:/g, "").toLowerCase();
}

/**
 * Is this host an IPv6 literal?
 *
 * Deliberately loose — anything containing a colon that is not a bracketed
 * form. A hostname cannot contain a colon, so a colon means either an IPv6
 * literal or garbage, and both want bracketing so the result is at least
 * unambiguous rather than silently reparsed as host:port by the client.
 */
export function isIPv6Literal(host: string): boolean {
  return host.includes(":") && !host.startsWith("[");
}

/** Bracket an IPv6 literal; leave hostnames and IPv4 alone. */
export function formatHost(host: string): string {
  const bare = host.trim().replace(/^\[|\]$/g, "");
  return isIPv6Literal(bare) ? `[${bare}]` : bare;
}

export function isDefaultPort(
  scheme: EnrolmentScheme,
  port: number | null,
): boolean {
  return port === null || port === DEFAULT_PORTS[scheme];
}

/**
 * Read the connection the operator is currently using.
 *
 * `window.location` is authoritative for "the address this browser reached
 * the server on" — which is the best available starting point, though not
 * necessarily the address the PHONE can reach (a laptop on a VPN and a
 * handset on wifi routinely disagree). The caller is expected to let the
 * operator correct it before the code is generated.
 */
export function connectionFromLocation(loc: Location): EnrolmentConnection {
  const scheme: EnrolmentScheme = loc.protocol === "http:" ? "http" : "https";
  // ``loc.port`` is "" when the URL used the scheme default. ``loc.hostname``
  // already strips the brackets from an IPv6 literal.
  const port = loc.port ? Number(loc.port) : null;
  return { host: loc.hostname, port, scheme };
}

/**
 * Build the enrolment URI.
 *
 * Uses `URLSearchParams` rather than hand-rolled concatenation so a token or
 * hostname containing a reserved character is escaped rather than silently
 * truncating the URI at the offending byte.
 */
export function buildEnrolmentUri(payload: EnrolmentPayload): string {
  const host = formatHost(payload.host);
  if (!host) throw new Error("host is required to build an enrolment URI");

  const params = new URLSearchParams();
  params.set("host", host);
  // Omit the port when it is the scheme default, per the format table — a
  // redundant ``port=443`` is not wrong, but the client shows the operator
  // what it parsed and the shorter form is the one they can eyeball.
  if (!isDefaultPort(payload.scheme, payload.port)) {
    params.set("port", String(payload.port));
  }
  // ``https`` is the assumed default, so only ``http`` needs stating. Being
  // explicit about the insecure case is the right way round: a code that
  // silently downgrades is the failure mode worth preventing.
  if (payload.scheme === "http") {
    params.set("scheme", "http");
  }
  if (payload.token) {
    params.set("token", payload.token);
  }
  const fingerprint = normaliseFingerprint(payload.fingerprint);
  if (fingerprint) {
    params.set("fingerprint", fingerprint);
  }
  return `${ENROLMENT_SCHEME}://${ENROLMENT_ACTION}?${params.toString()}`;
}

/**
 * Group a hex fingerprint into colon-separated bytes for display.
 *
 * The wire form is bare hex; this is only for showing an operator something
 * they can compare against what their browser's certificate viewer reports,
 * which uses the colon form.
 */
export function displayFingerprint(hex: string): string {
  return (hex.match(/.{2}/g) ?? []).join(":").toUpperCase();
}
