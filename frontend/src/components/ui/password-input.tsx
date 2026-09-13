import { forwardRef, useId, useState } from "react";
import type { InputHTMLAttributes } from "react";
import { Eye, EyeOff } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * A password field with a reveal toggle (#1074).
 *
 * There are 60+ `type="password"` inputs in this app and none of them
 * could be read back, which is worst on the forced first-login Change
 * Password screen: the operator cannot navigate away, the password has to
 * satisfy a policy they are reading off the rules beside the field, and a
 * mistyped character is indistinguishable from a policy failure. #995
 * added a keyboard-layout step to the installer precisely because symbols
 * land elsewhere on AZERTY — this is how you find that out in two seconds
 * rather than after three failed logins.
 *
 * Deliberate choices, each of which is a way to get this subtly wrong:
 *
 * - **`type="button"` on the toggle.** A bare `<button>` inside a `<form>`
 *   defaults to `submit`, so the first click would submit a half-typed
 *   form. This is the single most likely bug in a component like this.
 * - **Revealed state is local and starts false on every mount.** It is
 *   never persisted and never lifted to a parent: a "show passwords"
 *   preference that survives a reload is a shoulder-surfing gift, and a
 *   shared one cannot express "reveal the new password while Confirm
 *   stays masked", which is the common case.
 * - **`autoComplete` is untouched when the type flips.** Password
 *   managers key off the attribute rather than the type, so forwarding it
 *   unchanged is what keeps them working while revealed.
 * - **The caller's `className` still lands on the input**, because the
 *   Confirm field carries a conditional mismatch border and a wrapper
 *   that swallowed it would silently drop the error state.
 */
export const PasswordInput = forwardRef<
  HTMLInputElement,
  Omit<InputHTMLAttributes<HTMLInputElement>, "type">
>(function PasswordInput({ className, id, ...props }, ref) {
  const [revealed, setRevealed] = useState(false);
  const generatedId = useId();
  const inputId = id ?? generatedId;

  return (
    <div className="relative">
      <input
        {...props}
        id={inputId}
        ref={ref}
        type={revealed ? "text" : "password"}
        className={cn(
          "w-full rounded-md border bg-background py-2 pl-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring",
          // Room for the button so a long value does not run under it.
          "pr-10",
          className,
        )}
      />
      <button
        // Not optional — see the component docstring.
        type="button"
        onClick={() => setRevealed((v) => !v)}
        // The control describes the ACTION, and aria-pressed carries the
        // state, so a screen reader announces both without the label
        // having to encode it twice.
        aria-label={revealed ? "Hide password" : "Show password"}
        aria-pressed={revealed}
        aria-controls={inputId}
        className="absolute inset-y-0 right-0 flex items-center rounded-r-md px-3 text-muted-foreground hover:text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
      >
        {revealed ? (
          <EyeOff className="h-4 w-4" aria-hidden="true" />
        ) : (
          <Eye className="h-4 w-4" aria-hidden="true" />
        )}
      </button>
    </div>
  );
});
