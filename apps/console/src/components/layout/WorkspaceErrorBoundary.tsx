"use client";

import { Component, type ReactNode } from "react";
import { announceDiagnostic } from "@/lib/uiDiagnostics";
import { recordWebviewError } from "@/lib/webviewDiagnostics";

type Props = { children: ReactNode };
type State = { message: string | null };

export class WorkspaceErrorBoundary extends Component<Props, State> {
  state: State = { message: null };

  static getDerivedStateFromError(error: Error): State {
    return { message: error.message || "This screen hit an error." };
  }

  componentDidCatch(error: Error) {
    recordWebviewError("render_error", error);
    announceDiagnostic("render_error", "render", error);
  }

  render() {
    if (this.state.message) {
      return (
        <div style={{ padding: "2rem", maxWidth: 480 }}>
          <h2 style={{ margin: "0 0 8px" }}>This screen hit an error</h2>
          <p className="muted" style={{ marginBottom: 16 }}>
            The menu is still here. Your venture is safe. Try again, or open another tab.
          </p>
          <p className="muted" style={{ fontSize: "0.85rem" }}>
            If it happens again, reconnect in Settings and retry. Supymem will send a sanitized diagnostic when your connection is available.
          </p>
          <button type="button" className="primary" onClick={() => this.setState({ message: null })}>
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
