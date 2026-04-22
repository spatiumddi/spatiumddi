/**
 * Copy text to the clipboard with a fallback for insecure origins.
 *
 * `navigator.clipboard.writeText` is only available on **secure
 * contexts** — HTTPS, or `localhost` / `127.0.0.1`. On plain HTTP
 * against a LAN hostname (common for Docker Compose deploys where
 * the operator hasn't terminated TLS yet) `navigator.clipboard` is
 * `undefined` and the promise rejects, so a copy button that only
 * calls the modern API silently does nothing.
 *
 * The textarea-plus-`execCommand` fallback still works on insecure
 * origins and every browser we care about. It's deprecated, but no
 * browser has dropped it, and the replacement (clipboard API) is
 * gated on the secure-context requirement above — so it's the only
 * option for that audience.
 *
 * Returns `true` on success, `false` if neither path worked (caller
 * should surface an error and let the user select-and-copy manually).
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  // Prefer the async clipboard API — it's the permission-aware path
  // and handles large strings / unicode without the textarea hack.
  if (
    typeof navigator !== "undefined" &&
    navigator.clipboard &&
    typeof navigator.clipboard.writeText === "function" &&
    window.isSecureContext
  ) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Fall through to the legacy path. Some environments report a
      // `navigator.clipboard` that's present but rejects on use
      // (permissions-policy, iframe sandbox, etc.).
    }
  }

  // Legacy fallback — works on insecure origins but requires a DOM
  // element the user (transitively) clicked. Callers are usually
  // inside a click handler so this works.
  if (typeof document === "undefined") return false;

  const ta = document.createElement("textarea");
  ta.value = text;
  // Keep it off-screen and out of the tab order so focus / scroll
  // doesn't jump when we select + execCommand.
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.top = "0";
  ta.style.left = "0";
  ta.style.width = "2em";
  ta.style.height = "2em";
  ta.style.padding = "0";
  ta.style.border = "none";
  ta.style.outline = "none";
  ta.style.boxShadow = "none";
  ta.style.background = "transparent";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  try {
    ta.select();
    ta.setSelectionRange(0, text.length);
    // execCommand is deprecated but the only option on insecure
    // origins; eslint complaint silenced where it's called.
    const ok = document.execCommand("copy");
    return ok;
  } catch {
    return false;
  } finally {
    document.body.removeChild(ta);
  }
}
