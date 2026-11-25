"use client";

import { Component, type ReactNode } from "react";
import { announceDiagnostic } from "@/lib/uiDiagnostics";
import { recordWebviewError } from "@/lib/webviewDiagnostics";

type Props = { children: ReactNode };
type State = { failed: boolean };

/** Keep the usage collector mounted if shell chrome or an overlay fails to render. */
export class AppShellErrorBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  componentDidCatch(error: Error) {
    recordWebviewError("render_error", error);
    // On the first render the collector's effect may not have subscribed yet.
    // Dispatch after commit; the diagnostic emitter discards exception text.
    window.setTimeout(() => announceDiagnostic("render_error", "render", error), 0);
  }

  render() {
    if (this.state.failed) {
      return (
        <main className="login-gate" role="alert" style={{ gridColumn: "1 / -1" }}>
          <div className="login-card">
            <h1>Supymem could not show this screen</h1>
            <p>Your saved company work is retained. Try the screen again; Supymem will send a sanitized diagnostic when connected.</p>
            <button type="button" className="primary" onClick={() => this.setState({ failed: false })}>Try again</button>
          </div>
        </main>
      );
    }
    return this.props.children;
  }
}
