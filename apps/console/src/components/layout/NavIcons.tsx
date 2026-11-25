import type { ReactNode } from "react";

type IconProps = { className?: string };

function Svg({ className, children }: IconProps & { children: ReactNode }) {
  return (
    <svg
      className={className}
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      {children}
    </svg>
  );
}

export const NAV_ICONS = {
  sidebar: (props: IconProps) => (
    <Svg {...props}>
      <rect x="1.5" y="2" width="13" height="12" rx="1.5" />
      <path d="M6 2v12" />
    </Svg>
  ),
  ventures: (p: IconProps) => (
    <Svg {...p}>
      <rect x="1.5" y="1.5" width="5.5" height="5.5" rx="1" />
      <rect x="9" y="1.5" width="5.5" height="5.5" rx="1" />
      <rect x="1.5" y="9" width="5.5" height="5.5" rx="1" />
      <rect x="9" y="9" width="5.5" height="5.5" rx="1" />
    </Svg>
  ),
  newVenture: (p: IconProps) => (
    <Svg {...p}>
      <path d="M8 2.5v11M2.5 8h11" />
    </Svg>
  ),
  lab: (p: IconProps) => (
    <Svg {...p}>
      <path d="M6 2.5h4M7 2.5v3.2L3.4 13.2a1 1 0 0 0 .9 1.3h7.4a1 1 0 0 0 .9-1.3L9 5.7V2.5" />
    </Svg>
  ),
  ops: (p: IconProps) => (
    <Svg {...p}>
      <circle cx="8" cy="8" r="2.2" />
      <path d="M8 2.4v1.6M8 12v1.6M2.4 8h1.6M12 8h1.6M4 4l1.1 1.1M10.9 10.9 12 12M12 4l-1.1 1.1M5.1 10.9 4 12" />
    </Svg>
  ),
  cockpit: (p: IconProps) => (
    <Svg {...p}>
      <path d="M3 11.5a5.5 5.5 0 0 1 10 0" />
      <path d="M8 11.5V7.2" />
      <circle cx="8" cy="11.5" r="1.1" fill="currentColor" stroke="none" />
    </Svg>
  ),
  idea: (p: IconProps) => (
    <Svg {...p}>
      <path d="M8 2.4a4 4 0 0 0-2.2 7.3V11h4.4V9.7A4 4 0 0 0 8 2.4z" />
      <path d="M6.4 12.4h3.2M6.8 13.6h2.4" />
    </Svg>
  ),
  now: (p: IconProps) => (
    <Svg {...p}>
      <polygon points="4.2,2.8 13,8 4.2,13.2" />
    </Svg>
  ),
  work: (p: IconProps) => (
    <Svg {...p}>
      <path d="M3 5.2h10v8.2H3z" />
      <path d="M5.5 5.2V3.8h5v1.4" />
      <path d="M3 8.4h10" />
    </Svg>
  ),
  team: (p: IconProps) => (
    <Svg {...p}>
      <circle cx="6" cy="5.4" r="2" />
      <path d="M2.4 13.2c.3-2.2 1.8-3.4 3.6-3.4s3.3 1.2 3.6 3.4" />
      <circle cx="11.2" cy="6" r="1.6" />
      <path d="M10.4 9.8c1.4.2 2.4 1.2 2.7 2.8" />
    </Svg>
  ),
  website: (p: IconProps) => (
    <Svg {...p}>
      <circle cx="8" cy="8" r="5.5" />
      <path d="M2.5 8h11M8 2.5c1.6 1.6 2.4 3.4 2.4 5.5S9.6 11.9 8 13.5C6.4 11.9 5.6 10.1 5.6 8S6.4 4.1 8 2.5z" />
    </Svg>
  ),
  research: (p: IconProps) => (
    <Svg {...p}>
      <circle cx="7" cy="7" r="4.2" />
      <path d="M10.2 10.2 13.4 13.4" />
    </Svg>
  ),
  market: (p: IconProps) => (
    <Svg {...p}>
      <path d="M2.4 12.6h11.2" />
      <path d="M3.6 12.6V7.2h2.4v5.4M6.8 12.6V4.2h2.4v8.4M10 12.6V8.4h2.4v4.2" />
    </Svg>
  ),
  experiments: (p: IconProps) => (
    <Svg {...p}>
      <path d="M6 2.6h4M6.8 2.6v4L3.6 12.2a1 1 0 0 0 .9 1.4h7a1 1 0 0 0 .9-1.4L9.2 6.6v-4" />
    </Svg>
  ),
  feedback: (p: IconProps) => (
    <Svg {...p}>
      <path d="M3 3.4h10v7.2H6.6L3 13.2V3.4z" />
    </Svg>
  ),
  plan: (p: IconProps) => (
    <Svg {...p}>
      <path d="M4.2 2.6h7.6v10.8H4.2z" />
      <path d="M6 5.4h4M6 8h4M6 10.6h2.4" />
    </Svg>
  ),
  execution: (p: IconProps) => (
    <Svg {...p}>
      <path d="M3 8.2 6.2 11.4 13 4.2" />
    </Svg>
  ),
  builds: (p: IconProps) => (
    <Svg {...p}>
      <path d="M2.6 11.2 8 13.4l5.4-2.2" />
      <path d="M2.6 8.4 8 10.6l5.4-2.2" />
      <path d="M2.6 5.6 8 7.8l5.4-2.2L8 3.4z" />
    </Svg>
  ),
  analytics: (p: IconProps) => (
    <Svg {...p}>
      <path d="M2.6 12.6V3.8" />
      <path d="M2.6 12.6H13.4" />
      <path d="M4.8 9.2 7.2 6.6 9.4 8.4 12.6 4.4" />
    </Svg>
  ),
  import: (p: IconProps) => (
    <Svg {...p}>
      <path d="M8 2.6v8" />
      <path d="M5.2 8.2 8 11l2.8-2.8" />
      <path d="M3 13.2h10" />
    </Svg>
  ),
  evolution: (p: IconProps) => (
    <Svg {...p}>
      <path d="M3 10.6c1.2-3.4 3-5.2 5-5.2 2.6 0 3.4 2.8 5.2 2.8 1.2 0 2-.6 2.4-1.4" />
      <path d="M11.2 4.4 13.6 6.8 11.4 9" />
    </Svg>
  ),
  people: (p: IconProps) => (
    <Svg {...p}>
      <circle cx="8" cy="5.2" r="2.2" />
      <path d="M3.4 13.2c.4-2.8 2.2-4.2 4.6-4.2s4.2 1.4 4.6 4.2" />
    </Svg>
  ),
  connections: (p: IconProps) => (
    <Svg {...p}>
      <circle cx="4.2" cy="8" r="2" />
      <circle cx="11.8" cy="4.8" r="2" />
      <circle cx="11.8" cy="11.2" r="2" />
      <path d="M6 7.2 10 5.6M6 8.8l4 2.4" />
    </Svg>
  ),
  settings: (p: IconProps) => (
    <Svg {...p}>
      <circle cx="8" cy="8" r="2" />
      <path d="m6.5 1.5-.4 1.7-1.2.7-1.7-.5-1.5 2.6L3 7.2v1.6L1.7 10l1.5 2.6 1.7-.5 1.2.7.4 1.7h3l.4-1.7 1.2-.7 1.7.5 1.5-2.6-1.3-1.2V7.2L14.3 6l-1.5-2.6-1.7.5-1.2-.7-.4-1.7z" />
    </Svg>
  ),
} as const;

export type NavIconId = keyof typeof NAV_ICONS;
