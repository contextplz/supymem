"use client";

import Link from "next/link";
import type { ComponentProps, MouseEvent } from "react";
import { routeInChat } from "@/lib/chatRouteAction";
import { PageResultLink, pageResultKind } from "./PageResultLink";

export function captureChatLink(event: MouseEvent<HTMLElement>) {
  if (event.defaultPrevented) return;
  const anchor = (event.target as HTMLElement).closest?.("a[href]");
  if (anchor && !anchor.hasAttribute("data-chat-route-native") && routeInChat(anchor.getAttribute("href") || "")) { event.preventDefault(); event.stopPropagation(); }
}

export default function ChatLink({ onClick, ...props }: ComponentProps<typeof Link>) {
  return <Link {...props} onClick={event => {
    onClick?.(event);
    if (event.defaultPrevented) return;
    if (!event.currentTarget.hasAttribute("data-chat-route-native") && routeInChat(event.currentTarget.getAttribute("href") || "")) { event.preventDefault(); return; }
  }} />;
}

export function ChatAnchor({ onClick, "data-usage-control": usageControl, ...props }: ComponentProps<"a"> & {"data-usage-control"?: string}) {
  if (pageResultKind(props.href)) return <PageResultLink href={props.href!} className={props.className} data-usage-control={usageControl} />;
  return <a {...props} data-usage-control={usageControl} onClick={event => {
    onClick?.(event);
    if (event.defaultPrevented) return;
    if (!event.currentTarget.hasAttribute("data-chat-route-native") && routeInChat(props.href || "")) { event.preventDefault(); return; }
  }} />;
}
