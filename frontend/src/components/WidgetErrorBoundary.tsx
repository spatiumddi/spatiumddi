import { AlertTriangle } from "lucide-react";
import { type ReactNode } from "react";

import { ErrorBoundary } from "./ErrorBoundary";

interface WidgetErrorBoundaryProps {
  children: ReactNode;
  title?: string;
}

/**
 * Card-sized error boundary for dashboard panels and other self-contained
 * widgets. Keeps a single failed panel from taking down the whole page.
 */
export function WidgetErrorBoundary({
  children,
  title = "This panel",
}: WidgetErrorBoundaryProps) {
  return (
    <ErrorBoundary
      fallback={(reset) => (
        <div
          className="rounded-lg border bg-card p-4"
          role="alert"
          aria-live="polite"
        >
          <div className="flex items-start gap-2 text-sm text-destructive">
            <AlertTriangle
              className="mt-0.5 h-4 w-4 shrink-0"
              aria-hidden="true"
            />
            <div className="flex-1">
              <p className="font-medium">{title} failed to load.</p>
              <button
                type="button"
                onClick={reset}
                className="mt-2 text-xs text-primary hover:underline"
              >
                Try again
              </button>
            </div>
          </div>
        </div>
      )}
    >
      {children}
    </ErrorBoundary>
  );
}
