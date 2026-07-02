/**
 * In-memory access-token store (#484 / #400 L1).
 *
 * SECURITY: the JWT access token lives ONLY in this module-level variable —
 * never in localStorage / sessionStorage / a cookie readable by JS. An XSS
 * foothold in the SPA therefore cannot exfiltrate a durable credential: the
 * access token is short-lived and vanishes on tab close, and the long-lived
 * refresh token is an HttpOnly cookie the backend sets on /auth/login and
 * rotates on /auth/refresh (invisible to script entirely).
 *
 * Because the token is memory-only it does NOT survive a full page reload.
 * On boot the app performs one silent /auth/refresh (the HttpOnly cookie
 * rides that request) to mint a fresh access token — see ``useAuth``.
 *
 * Kept dependency-free (no import from ``lib/api``) so it can be imported by
 * both the axios client and the auth hook without a circular import.
 */

type AuthSnapshot = {
  /** The current access token, or null when unauthenticated. */
  accessToken: string | null;
  /** True once the boot-time silent refresh has resolved (either way). Until
   *  then a protected route should render a loader rather than bounce to
   *  /login, so a real session isn't mistaken for a logged-out one. */
  booted: boolean;
};

let snapshot: AuthSnapshot = { accessToken: null, booted: false };
const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

/** Stable snapshot for ``useSyncExternalStore`` — the reference only changes
 *  when the state actually changes, so React re-renders exactly when needed. */
export function getAuthSnapshot(): AuthSnapshot {
  return snapshot;
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getAccessToken(): string | null {
  return snapshot.accessToken;
}

export function setAccessToken(token: string | null): void {
  if (snapshot.accessToken === token) return;
  snapshot = { ...snapshot, accessToken: token };
  emit();
}

export function markBooted(): void {
  if (snapshot.booted) return;
  snapshot = { ...snapshot, booted: true };
  emit();
}
