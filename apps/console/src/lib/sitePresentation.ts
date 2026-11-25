export type VentureStatus = { code?: string; label?: string; detail?: string } | null;

export type WebsiteSurfaceState = {
  showAcceptedPreview: boolean;
  showHostedPreview: boolean;
  displayPublished: boolean;
  displaySimulated: boolean;
  useStoredUrl: boolean;
};

/** Audit the displayed deployment by ID, not an older First Look URL. */
export function publishedPageAuditTarget(
  storedUrl: string,
  acceptedDeployment: { id: string; state: string; url?: string | null } | null,
  appId?: string,
): { url: string; binding?: { appId: string; deploymentId: string } } {
  if (acceptedDeployment?.state === "live" && acceptedDeployment.url && appId) {
    return {
      url: acceptedDeployment.url.trim(),
      binding: { appId, deploymentId: acceptedDeployment.id },
    };
  }
  return { url: storedUrl.trim() };
}

/**
 * Keep a newly accepted build ahead of an older First Look site record.
 *
 * A First Look record is useful before a reviewed build exists, but it is not
 * evidence that the accepted source was deployed. Until a deployment is
 * explicitly bound to that artifact, showing the old URL or its provenance as
 * the product would mislead the founder.
 */
export function websiteSurfaceState(input: {
  acceptedPreview: boolean;
  reachable: boolean;
  stillBuilding: boolean;
  published: boolean;
  simulated: boolean;
}): WebsiteSurfaceState {
  if (input.acceptedPreview && input.published && input.reachable) {
    return {
      showAcceptedPreview: false,
      showHostedPreview: true,
      displayPublished: true,
      displaySimulated: false,
      useStoredUrl: true,
    };
  }
  if (input.acceptedPreview) {
    return {
      showAcceptedPreview: true,
      showHostedPreview: false,
      displayPublished: false,
      displaySimulated: false,
      useStoredUrl: false,
    };
  }
  return {
    showAcceptedPreview: false,
    showHostedPreview: input.reachable && !input.stillBuilding,
    displayPublished: input.published,
    displaySimulated: input.simulated,
    useStoredUrl: true,
  };
}

export function websiteEmptyState(input: {
  loading?: boolean;
  acceptedPreviewBusy: boolean;
  stillBuilding: boolean;
  /** Link to a finished build whose review is still pending. */
  reviewHref?: string;
}): { title: string; body: string; href?: string; linkLabel?: string } {
  if (input.loading) return { title: "Loading your page", body: "Checking the saved page and publication status." };
  if (input.acceptedPreviewBusy) {
    return {
      title: "Checking the private preview",
      body: "Checking whether this venture has an accepted build with verified QA evidence.",
    };
  }
  if (input.reviewHref) {
    return {
      title: "Your landing page build is ready to review",
      body: "The build finished. Review its QA evidence and accept it, and the verified preview will appear here.",
      href: input.reviewHref,
      linkLabel: "Review and accept the build",
    };
  }
  if (input.stillBuilding) {
    return {
      title: "Landing page build in progress",
      body: "Cole is building it now. This page will update when there is a real preview, not a white screen or a local-only draft.",
    };
  }
  return {
    title: "No landing page yet",
    body: "Approve the execution plan and start the build first. A verified preview will appear here when the landing-page task is complete.",
  };
}

/** The Website header describes publication state when a verified build exists. */
export function websiteHeaderStatus(input: {
  acceptedPreview: boolean;
  published: boolean;
  ventureStatus: VentureStatus;
}): VentureStatus {
  if (input.acceptedPreview && !input.published) {
    return {
      code: "private_preview",
      label: "Private preview",
      detail: "This accepted build is visible only to you until you publish it.",
    };
  }
  return input.ventureStatus;
}

/** Stable hosting codes become founder actions; provider details stay internal. */
export function publicationFailureMessage(code?: string | null): string {
  if (code === "hosting_conversion_verification_required") {
    return "Enable this venture’s waitlist and test its public submission on this page before publishing. Verification lasts 24 hours.";
  }
  if (code === "hosting_conversion_link_required" || code === "hosting_conversion_contract_unverified") {
    return "The accepted build needs a reviewed early-access link to this venture’s waitlist. Update the plan and build, then run QA and review it before publishing.";
  }
  if (code === "hosting_provider_unavailable" || code === "hosting_provider_configuration_invalid") {
    return "Supymem hosting is temporarily unavailable. Your accepted build is still private and safe.";
  }
  if (
    code === "hosting_build_not_accepted" ||
    code === "hosting_source_receipt_invalid" ||
    code === "hosting_source_manifest_changed"
  ) {
    return "This build no longer matches its accepted QA evidence. Review and accept the latest verified build before publishing.";
  }
  if (code === "hosting_public_state_uncertain") {
    return "Supymem could not confirm whether the public address was restored. Keep this screen open and contact Supymem support before publishing again.";
  }
  return "Publication did not complete. Supymem restored the previous public version when one existed, and your accepted build remains saved.";
}
