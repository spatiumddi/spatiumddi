import { useState, useEffect, useCallback } from "react";

/**
 * Like useState but persists to sessionStorage.
 *
 * - Serialises Sets as arrays (JSON doesn't support Set natively).
 * - Uses an initializer function so sessionStorage is only read once on mount.
 * - The setter is stable across renders — callers don't need to memoize.
 */
export function useSessionState<T>(
  key: string,
  initial: T,
): [T, (value: T | ((prev: T) => T)) => void] {
  const [state, setStateRaw] = useState<T>(() => {
    try {
      const raw = sessionStorage.getItem(key);
      if (raw === null) return initial;
      const parsed = JSON.parse(raw);
      // Re-hydrate Set<string>
      if (initial instanceof Set && Array.isArray(parsed)) {
        return new Set(parsed) as unknown as T;
      }
      return parsed as T;
    } catch {
      return initial;
    }
  });

  const setState = useCallback(
    (value: T | ((prev: T) => T)) => {
      setStateRaw((prev) => {
        const next =
          typeof value === "function"
            ? (value as (p: T) => T)(prev)
            : value;

        try {
          const toStore =
            next instanceof Set ? Array.from(next as Set<unknown>) : next;
          sessionStorage.setItem(key, JSON.stringify(toStore));
        } catch {
          // sessionStorage may be unavailable in private browsing
        }

        return next;
      });
    },
    [key],
  );

  // Sync to sessionStorage whenever the key changes (unlikely but safe).
  useEffect(() => {
    try {
      const toStore =
        state instanceof Set ? Array.from(state as Set<unknown>) : state;
      sessionStorage.setItem(key, JSON.stringify(toStore));
    } catch {
      // ignore
    }
  }, [key]); // intentionally omit `state` — we only want key-change sync

  return [state, setState];
}
