export type CompanyJourneyInput = {
  entryGoal?: string | null;
  hasActiveJobs: boolean;
  activeWorkState?: CompanyWorkState;
  preparingClarification?: boolean;
  ideaConfirmed: boolean;
  ideaState?: string;
  projectIntent?: "startup" | "personal";
  hasPlan: boolean;
  planState?: string;
  productCount: number;
  hasAcceptedBuild: boolean;
  websitePublished: boolean;
  hasProductBuildAttempt?: boolean;
  hasAcceptedProductBuild?: boolean;
};

export type CompanyWorkState = "none" | "queued" | "running" | "unknown";

/** Only an actual running receipt supports an execution claim. */
export function companyWorkState(jobs: unknown): CompanyWorkState {
  if (!Array.isArray(jobs)) return "unknown";
  if (!jobs.length) return "none";
  const statuses = jobs.map(job => job && typeof job === "object" ? job.status : undefined);
  if (statuses.includes("running")) return "running";
  const queued = ["pending", "scheduled", "leased", "retry_wait"];
  const terminal = ["succeeded", "done", "failed_dead", "failed", "poison", "cancelled"];
  if (statuses.some(status => !queued.includes(status) && !terminal.includes(status))) return "unknown";
  return statuses.some(status => queued.includes(status)) ? "queued" : "none";
}

export type CompanyJourneyStep = {
  workState?: CompanyWorkState;
  key: "work" | "clarify" | "research" | "plan" | "build" | "review" | "launch" | "gtm";
  eyebrow: string;
  title: string;
  detail: string;
  path: "tasks" | "idea" | "research" | "plan" | "builds" | "site" | "analytics" | "gtm";
  cta: string;
};

const EXECUTABLE_PLAN_STATES = new Set(["approved", "executing", "completed", "confirmed"]);
export const JOURNEY_OUTCOME = "real product use";

/** The plans API returns versions newest first; accept only a valid list and
 * select by version so a reordered or malformed response cannot advance Idea. */
export function currentPlanState(data: unknown): string | undefined {
  if (!data || typeof data !== "object" || !("items" in data) || !Array.isArray(data.items)) return undefined;
  const plans = data.items.filter((item): item is { version: number; state: string } =>
    !!item && typeof item === "object" && typeof item.version === "number"
    && Number.isFinite(item.version) && typeof item.state === "string" && item.state.length > 0);
  return plans.reduce<{ version: number; state: string } | undefined>(
    (latest, item) => !latest || item.version > latest.version ? item : latest, undefined)?.state;
}

export function ideaJourneyProgress(state: string | undefined, hasCompletedValidation: boolean, planState?: string): number {
  if (["approved", "executing", "completed", "confirmed"].includes(planState || "")) return 4;
  if (planState) return 3;
  if (state === "confirmed") return 2;
  if (hasCompletedValidation || ["validating", "reviewing"].includes(state || "")) return 1;
  return 0;
}

export function companyJourneyStep(input: CompanyJourneyInput): CompanyJourneyStep {
  // A founder-confirmed GTM redirect can target an older Idea venture. Its
  // saved goal is the next action even if an earlier Idea job still exists.
  if (input.entryGoal === "gtm") {
    return {
      key: "gtm",
      eyebrow: "Go to market",
      title: "Find the next path to customers",
      detail: "Describe your existing product, its customers, and the problem it solves. Review the diagnosis and credit budget before starting any paid work.",
      path: "gtm",
      cta: "Open go to market",
    };
  }
  const work = input.activeWorkState || (input.hasActiveJobs ? "unknown" : "none");
  if (work !== "none") {
    return {
      key: "work", workState: work,
      eyebrow: work === "running" ? "Saved job reports running" : work === "queued" ? "Work is queued" : "Work status needs review",
      title: input.preparingClarification ? "Preparing your next question" : work === "running" ? "Company work is running" : work === "queued" ? "Company work is queued" : "Check saved company work status",
      detail: input.preparingClarification ? "Preparing your next question, usually under a minute." : work === "running" ? "Open Work to review the saved job and its results. Successful completion is still pending."
        : work === "queued" ? "Saved jobs are waiting to run. Execution and completed results are not yet confirmed."
        : "Current execution could not be confirmed. Open Work to review saved status before choosing the next step.",
      path: "tasks", cta: "Review saved work",
    };
  }
  if (input.projectIntent === "personal") {
    if (!input.ideaConfirmed) {
      return ["reviewing", "validating"].includes(input.ideaState || "") ? {
        key: "review", eyebrow: "Project review", title: "Review your saved personal project",
        detail: "Check the idea and answers before confirming. This step does not run market validation.",
        path: "idea", cta: "Review project",
      } : {
        key: "clarify", eyebrow: "Clarify your project", title: "Answer the next project question",
        detail: "Define the smallest useful version for your own use.",
        path: "idea", cta: "Continue clarification",
      };
    }
    return {
      key: "build", eyebrow: "Build your project", title: "Review a local build",
      detail: "Prepare the smallest useful version in your local workspace. Review the task and device access before approving it.",
      path: "idea", cta: "Review local build",
    };
  }
  if (!input.ideaConfirmed) {
    if (["validating", "reviewing"].includes(input.ideaState || "")) {
      return {
        key: "research",
        eyebrow: "Step 2 of 5 · Validate",
        title: "Review your idea validation",
        detail: "Supymem has finished clarification and is challenging the assumptions that matter before deeper research begins.",
        path: "idea",
        cta: "Open validation",
      };
    }
    return {
      key: "clarify",
      eyebrow: "Step 1 of 5 · Clarify",
      title: "Answer the next question about your idea",
      detail: "Supymem turns your starting idea into a focused MVP before deeper research or paid work begins.",
      path: "idea",
      cta: "Continue clarification",
    };
  }
  if (!input.hasPlan) {
    return {
      key: "research",
      eyebrow: "Step 2 of 5 · Validate",
      title: "Review the research and shape the MVP",
      detail: `Compare alternatives, test the assumptions that matter, then approve a small plan aimed at ${JOURNEY_OUTCOME}.`,
      path: "research",
      cta: "Continue to research",
    };
  }
  if (!EXECUTABLE_PLAN_STATES.has(input.planState || "")) {
    return {
      key: "plan",
      eyebrow: "Step 3 of 5 · Plan",
      title: "Review the execution plan",
      detail: "Confirm the scope, milestones, and resources before Supymem starts building.",
      path: "plan",
      cta: "Continue to plan",
    };
  }
  if (input.productCount < 1 || !input.hasAcceptedBuild) {
    return {
      key: "build",
      eyebrow: "Step 4 of 5 · Build",
      title: "Build and verify the first product milestone",
      detail: "Run the smallest release through independent QA, then review its evidence before accepting it.",
      path: "builds",
      cta: "Open builds and QA",
    };
  }
  if (input.hasProductBuildAttempt && !input.hasAcceptedProductBuild) {
    return {
      key: "build",
      eyebrow: "Step 4 of 5 · Build",
      title: "Finish and verify the application",
      detail: "The acquisition page is live, but the application build still needs an accepted result and functional testing.",
      path: "builds",
      cta: "Review application builds",
    };
  }
  if (input.hasProductBuildAttempt) {
    return {
      key: "review",
      eyebrow: "Step 5 of 5 · Application review",
      title: "Verify application hosting and customer workflows",
      detail: "An accepted application build still needs a live runtime and real account tests before Supymem can call the product launched.",
      path: "builds",
      cta: "Review application evidence",
    };
  }
  if (!input.websitePublished) {
    return {
      key: "review",
      eyebrow: "Step 5 of 5 · Launch decision",
      title: "Decide when to publish your accepted product",
      detail: "The product passed QA and has a saved milestone acceptance. It remains private until you choose to publish it.",
      path: "site",
      cta: "Open the private product",
    };
  }
  return {
    key: "launch",
    eyebrow: "Step 5 of 5 · Launch",
    title: "Review the live product and first-user signals",
    detail: "Measure real usage, collect feedback, and decide the next iteration from that evidence.",
    path: "analytics",
    cta: "Open product analytics",
  };
}


export type PublicationReceipt = { app_id?: string; build_artifact_id?: string | null; state?: string; url?: string | null };
export function acceptedArtifactIsPublished(appId: string, artifactId: string | null | undefined, receipt: PublicationReceipt): boolean {
  if (!artifactId || receipt.app_id !== appId || receipt.build_artifact_id !== artifactId || receipt.state !== "live") return false;
  try { const url = new URL(receipt.url || ""); return url.protocol === "https:" && !url.username && !url.password; }
  catch { return false; }
}
