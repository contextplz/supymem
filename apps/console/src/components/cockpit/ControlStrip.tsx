import Link from "@/components/chat/ChatLink";
import type { CreditBalance } from "@/lib/api-types";
import styles from "./cockpit.module.css";

export type ControlData = {
  you_own_this?: boolean;
  paused?: boolean;
  domain_policy?: string;
  posts_need_you?: boolean;
  landing?: { url?: string | null; simulated?: boolean; href?: string };
  plan_href?: string;
  research_href?: string | null;
  work_href?: string;
  now_href?: string;
  settings_href?: string;
  files?: string;
  credits?: CreditBalance | null;
};

export function ControlStrip({
  ventureId,
  control,
  pausing,
  pauseNote,
  onTogglePause,
  paused,
}: {
  ventureId: string;
  control?: ControlData | null;
  pausing?: boolean;
  pauseNote?: string;
  onTogglePause?: () => void;
  paused?: boolean;
}) {
  const credits = control?.credits;
  const available = credits && typeof credits.available === "number" ? credits.available : null;
  const landingHref = control?.landing?.href || `/v/${ventureId}/site`;
  const planHref = control?.plan_href || `/v/${ventureId}/charter`;
  const workHref = control?.work_href || `/v/${ventureId}/tasks`;
  const nowHref = control?.now_href || `/v/${ventureId}/now`;
  const settingsHref = control?.settings_href || `/v/${ventureId}/settings`;
  const researchHref = control?.research_href;

  return (
    <section className={styles.control} aria-labelledby="control-heading">
      <div className={styles.sectionHeader}>
        <h2 id="control-heading">You stay in control</h2>
        {onTogglePause ? (
          <button type="button" className="ghost sm" data-usage-control={paused ? "venture_resume" : "venture_pause"} disabled={pausing} onClick={onTogglePause}>
            {pausing ? "Working…" : paused ? "Resume everything" : "Pause everything"}
          </button>
        ) : null}
      </div>
      <p className={styles.controlLede}>
        {control?.domain_policy ||
          "Your domain, repo, and hosting stay yours. Pause anytime. Nothing public posts unless you say so."}
      </p>
      <div className={styles.controlLinks}>
        <Link href={landingHref}>Landing page</Link>
        <Link href={planHref}>Plan</Link>
        {researchHref ? <Link href={researchHref}>Research</Link> : null}
        <Link href={nowHref}>What is running</Link>
        <Link href={workHref}>All work</Link>
        <Link href={`${settingsHref}#computer-use`}>Mac tasks</Link>
        <Link href={settingsHref}>Export / I&apos;m stuck</Link>
      </div>
      {control?.files ? <p className={styles.detail}>Files: {control.files}</p> : null}
      {pauseNote ? <p className={styles.pauseNote}>{pauseNote}</p> : null}
    </section>
  );
}
