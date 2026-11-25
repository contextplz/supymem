import { isDesktopShell, waitForDesktopShell } from "./lib/desktop.ts";
import { installWebviewDiagnostics } from "./lib/webviewDiagnostics.ts";

if (isDesktopShell()) installWebviewDiagnostics();
else void waitForDesktopShell().then(connected => { if (connected) installWebviewDiagnostics(); });
