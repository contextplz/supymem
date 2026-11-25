"use client";

import { FormEvent, useEffect, useState, type CSSProperties, type ReactNode } from "react";

const TEAM_SIZES = ["1-10", "11-50", "51-200", "201-1000", "1000+"] as const;

const fieldStyle: CSSProperties = {
  width: "100%",
  borderRadius: 12,
  border: "1px solid rgba(255,255,255,0.1)",
  background: "rgba(255,255,255,0.03)",
  padding: "12px 16px",
  fontSize: 14,
  color: "#fff",
  outline: "none",
  boxSizing: "border-box",
};

function waitlistUrl(): string {
  if (typeof window === "undefined") return "/api/v1/waitlist";
  const host = window.location.hostname.toLowerCase();
  if (host === "supymem.com" || host.endsWith(".supymem.com")) return "/api/v1/waitlist";
  return "https://api.supymem.com/api/v1/waitlist";
}

export function WaitlistLanding() {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [jobRole, setJobRole] = useState("");
  const [company, setCompany] = useState("");
  const [teamSize, setTeamSize] = useState("");
  const [useCase, setUseCase] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [doneMessage, setDoneMessage] = useState<string | null>(null);

  useEffect(() => {
    const root = document.documentElement;
    const body = document.body;
    root.classList.remove("light");
    root.classList.add("dark");
    const prevBg = body.style.background;
    const prevColor = body.style.color;
    body.style.background = "#0C0C0E";
    body.style.color = "#FFFFFF";
    return () => {
      body.style.background = prevBg;
      body.style.color = prevColor;
    };
  }, []);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const params = new URLSearchParams(window.location.search);
      const payload = {
        name: name.trim(),
        email: email.trim().toLowerCase(),
        job_role: jobRole.trim() || undefined,
        company: company.trim() || undefined,
        team_size: teamSize || undefined,
        use_case: useCase.trim() || undefined,
        source: "landing",
        utm_source: params.get("utm_source") || undefined,
        utm_medium: params.get("utm_medium") || undefined,
        utm_campaign: params.get("utm_campaign") || undefined,
        referrer_url: document.referrer || undefined,
        landing_page: window.location.href,
      };
      const res = await fetch(waitlistUrl(), {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(payload),
      });
      let data: { message?: string; detail?: string } | null = null;
      try {
        data = (await res.json()) as { message?: string; detail?: string };
      } catch {
        data = null;
      }
      if (!res.ok) {
        throw new Error(data?.detail || "Could not join the waitlist. Please try again.");
      }
      setDoneMessage(data?.message || "You're on the waitlist.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not join the waitlist.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main
      className="landing-root"
      style={{
        position: "relative",
        display: "flex",
        minHeight: "100vh",
        alignItems: "center",
        justifyContent: "center",
        overflow: "hidden",
        padding: "64px 16px",
        background: "#0C0C0E",
        color: "#FFFFFF",
        fontFamily: 'Inter, "DM Sans", system-ui, sans-serif',
      }}
    >
      <div
        id="waitlist"
        style={{
          display: "flex",
          width: "100%",
          maxWidth: 448,
          flexDirection: "column",
          alignItems: "center",
          textAlign: "center",
        }}
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src="/supymem-logo.svg" alt="Supymem" width={72} height={72} style={{ borderRadius: 16 }} />
        <h1
          style={{
            marginTop: 20,
            fontSize: 34,
            fontWeight: 600,
            letterSpacing: "-0.03em",
            color: "#fff",
          }}
        >
          supymem
        </h1>
        <p style={{ marginTop: 12, fontSize: 14, lineHeight: 1.6, color: "rgba(255,255,255,0.55)" }}>
          Join the waitlist for early access.
        </p>

        {doneMessage ? (
          <div
            style={{
              marginTop: 32,
              width: "100%",
              borderRadius: 16,
              border: "1px solid rgba(255,255,255,0.1)",
              background: "rgba(255,255,255,0.03)",
              padding: "24px 20px",
              textAlign: "left",
            }}
          >
            <p style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>{doneMessage}</p>
            <p style={{ margin: "8px 0 0", fontSize: 13, color: "rgba(255,255,255,0.5)" }}>
              We&apos;ll reach out when it&apos;s your turn.
            </p>
          </div>
        ) : (
          <form onSubmit={onSubmit} style={{ marginTop: 32, width: "100%", textAlign: "left" }}>
            <Field label="Name" htmlFor="waitlist-name">
              <input
                id="waitlist-name"
                required
                autoComplete="name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                style={fieldStyle}
                placeholder="Ada Lovelace"
              />
            </Field>
            <Field label="Work email" htmlFor="waitlist-email">
              <input
                id="waitlist-email"
                type="email"
                required
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                style={fieldStyle}
                placeholder="ada@company.com"
              />
            </Field>
            <Field label="Job role" htmlFor="waitlist-role" optional>
              <input
                id="waitlist-role"
                autoComplete="organization-title"
                value={jobRole}
                onChange={(e) => setJobRole(e.target.value)}
                style={fieldStyle}
                placeholder="Engineering Manager"
              />
            </Field>
            <Field label="Company" htmlFor="waitlist-company" optional>
              <input
                id="waitlist-company"
                autoComplete="organization"
                value={company}
                onChange={(e) => setCompany(e.target.value)}
                style={fieldStyle}
                placeholder="Acme Inc."
              />
            </Field>
            <Field label="Team size" htmlFor="waitlist-team-size" optional>
              <select
                id="waitlist-team-size"
                value={teamSize}
                onChange={(e) => setTeamSize(e.target.value)}
                style={{ ...fieldStyle, appearance: "none" }}
              >
                <option value="" style={{ background: "#0C0C0E" }}>
                  Select…
                </option>
                {TEAM_SIZES.map((size) => (
                  <option key={size} value={size} style={{ background: "#0C0C0E" }}>
                    {size}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="What are you hoping to use Supymem for?" htmlFor="waitlist-use-case" optional>
              <textarea
                id="waitlist-use-case"
                rows={3}
                value={useCase}
                onChange={(e) => setUseCase(e.target.value)}
                style={{ ...fieldStyle, resize: "none" }}
                placeholder="Onboarding, PR context, company memory…"
              />
            </Field>
            {error ? (
              <p
                style={{
                  borderRadius: 12,
                  border: "1px solid rgba(248,113,113,0.2)",
                  background: "rgba(239,68,68,0.1)",
                  padding: "8px 12px",
                  fontSize: 13,
                  color: "#fecaca",
                }}
              >
                {error}
              </p>
            ) : null}
            <button
              type="submit"
              disabled={submitting}
              style={{
                marginTop: 8,
                width: "100%",
                border: 0,
                borderRadius: 999,
                background: "#fff",
                color: "#0C0C0E",
                padding: "14px 28px",
                fontSize: 14,
                fontWeight: 600,
                cursor: submitting ? "not-allowed" : "pointer",
                opacity: submitting ? 0.6 : 1,
              }}
            >
              {submitting ? "Joining…" : "Join waitlist"}
            </button>
          </form>
        )}
      </div>
    </main>
  );
}

function Field({
  label,
  htmlFor,
  optional,
  children,
}: {
  label: string;
  htmlFor: string;
  optional?: boolean;
  children: ReactNode;
}) {
  return (
    <div style={{ marginBottom: 12 }}>
      <label
        htmlFor={htmlFor}
        style={{ display: "block", marginBottom: 6, fontSize: 12, fontWeight: 500, color: "rgba(255,255,255,0.6)" }}
      >
        {label}
        {optional ? <span style={{ color: "rgba(255,255,255,0.35)" }}> (optional)</span> : null}
      </label>
      {children}
    </div>
  );
}
