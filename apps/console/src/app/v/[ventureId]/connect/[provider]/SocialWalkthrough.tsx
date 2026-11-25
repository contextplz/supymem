"use client";

import { useState } from "react";
import styles from "./connect.module.css";

export type WalkthroughStep = {
  id: string;
  title: string;
  summary: string;
  url?: string | null;
  open_label?: string | null;
  look_for?: string[];
  cursor?: string;
  click?: string;
  expected_after?: string;
  keep_in_mind?: string[];
};

export function SocialWalkthrough({ steps }: { steps: WalkthroughStep[] }) {
  const [openId, setOpenId] = useState<string | null>(null);

  return (
    <ol className={styles.walk}>
      {steps.map((step) => {
        const expanded = openId === step.id;
        return (
          <li key={step.id} className={styles.walkCard}>
            <div className={styles.walkHead}>
              <div>
                <h2 className={styles.walkTitle}>{step.title}</h2>
                <p className={styles.walkSummary}>{step.summary}</p>
              </div>
              <div className={styles.walkBtns}>
                {step.url ? (
                  <a href={step.url} target="_blank" rel="noreferrer" className="primary sm">
                    {step.open_label || "Open"}
                  </a>
                ) : null}
                <button
                  type="button"
                  className="ghost sm"
                  aria-expanded={expanded}
                  onClick={() => setOpenId(expanded ? null : step.id)}
                >
                  {expanded ? "Hide details" : "Explain in detail"}
                </button>
              </div>
            </div>
            {expanded ? (
              <div className={styles.detail}>
                {step.look_for && step.look_for.length > 0 ? (
                  <section>
                    <h3>What you should see on the screen</h3>
                    <ul>
                      {step.look_for.map((line) => (
                        <li key={line}>{line}</li>
                      ))}
                    </ul>
                  </section>
                ) : null}
                {step.cursor ? (
                  <section>
                    <h3>Where to move the cursor</h3>
                    <p>{step.cursor}</p>
                  </section>
                ) : null}
                {step.click ? (
                  <section>
                    <h3>What to click</h3>
                    <p>{step.click}</p>
                  </section>
                ) : null}
                {step.expected_after ? (
                  <section>
                    <h3>What should happen next</h3>
                    <p>{step.expected_after}</p>
                  </section>
                ) : null}
                {step.keep_in_mind && step.keep_in_mind.length > 0 ? (
                  <section>
                    <h3>Keep in mind</h3>
                    <ul>
                      {step.keep_in_mind.map((line) => (
                        <li key={line}>{line}</li>
                      ))}
                    </ul>
                  </section>
                ) : null}
              </div>
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}
