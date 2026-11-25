import { consoleErrorEvent } from "./lib/errorEvent";

// Next.js calls this for every uncaught server render / route / action error. Closed labels only: the
// message, digest, query string and request headers never leave the process.
export function onRequestError(
  error: unknown,
  request: { method: string; path: string },
  context: { routePath: string; routeType: string },
) {
  console.error(consoleErrorEvent(error, request.method, context.routePath, context.routeType));
}
