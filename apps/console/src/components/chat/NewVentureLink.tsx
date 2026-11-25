"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import type { ComponentProps } from "react";

export function NewVentureLink({ onClick, ...props }: Omit<ComponentProps<typeof Link>, "href">) {
  const router = useRouter();
  return <Link {...props} href="/new?fresh=1" onClick={event => {
    onClick?.(event);
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    router.push(`/new?fresh=${crypto.randomUUID()}`);
  }} />;
}
