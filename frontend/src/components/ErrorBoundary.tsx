import { Component, type ErrorInfo, type ReactNode } from "react";

type FallbackRender = (reset: () => void) => ReactNode;

interface Props {
  children: ReactNode;
  fallback?: ReactNode | FallbackRender;
  onError?: (error: Error, info: ErrorInfo) => void;
}

interface State {
  hasError: boolean;
}

/**
 * React error boundary. Catches render-time errors in descendants and renders
 * a fallback UI instead of unmounting the whole tree.
 *
 * `fallback` may be a static React node or a render prop that receives a
 * `reset` function so the UI can offer a retry button.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    this.props.onError?.(error, info);
    // Browser-side log only. The backend has structured JSON logging; there is
    // no frontend telemetry pipeline yet, so console.error is the safest sink.
    console.error("ErrorBoundary caught error:", error, info.componentStack);
  }

  reset = () => {
    this.setState({ hasError: false });
  };

  render() {
    if (this.state.hasError) {
      if (typeof this.props.fallback === "function") {
        return this.props.fallback(this.reset);
      }
      return this.props.fallback ?? null;
    }
    return this.props.children;
  }
}
