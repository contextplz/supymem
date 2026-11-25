import assert from "node:assert/strict";
import test from "node:test";
import {
  websiteEmptyState,
  websiteHeaderStatus,
  publicationFailureMessage,
  websiteSurfaceState,
  publishedPageAuditTarget,
} from "./sitePresentation.ts";

test("copy audit follows the displayed artifact-bound live page, not a stale First Look URL", () => {
  const firstLook = "https://gateway.supymem.com/w/wsp_example/site/";
  const live = { id: "dep_current", state: "live", url: "https://venture.supymem.com/" };
  assert.deepEqual(publishedPageAuditTarget(firstLook, live, "app_current"), {
    url: live.url,
    binding: { appId: "app_current", deploymentId: live.id },
  });
  assert.deepEqual(publishedPageAuditTarget(firstLook, { ...live, state: "deploying" }, "app_current"), { url: firstLook });
  assert.deepEqual(publishedPageAuditTarget(firstLook, null), { url: firstLook });
});

test("a pre-plan venture does not claim that a landing page is being built", () => {
  assert.deepEqual(
    websiteEmptyState({ acceptedPreviewBusy: false, stillBuilding: false }),
    {
      title: "No landing page yet",
      body: "Approve the execution plan and start the build first. A verified preview will appear here when the landing-page task is complete.",
    },
  );
});

test("loading cannot announce no landing page, including the initial render", () => {
  assert.equal(websiteEmptyState({loading: true, acceptedPreviewBusy: false, stillBuilding: false}).title, "Loading your page");
});

test("publication failures explain the founder's next step without raw provider codes", () => {
  assert.match(publicationFailureMessage("hosting_build_not_accepted"), /accept the latest verified build/);
  assert.match(publicationFailureMessage("hosting_provider_failed"), /accepted build remains saved/);
  assert.doesNotMatch(publicationFailureMessage("hosting_provider_failed"), /hosting_/);
});

test("an active landing-page phase reports real work in progress", () => {
  assert.deepEqual(
    websiteEmptyState({ acceptedPreviewBusy: false, stillBuilding: true }),
    {
      title: "Landing page build in progress",
      body: "Cole is building it now. This page will update when there is a real preview, not a white screen or a local-only draft.",
    },
  );
});

test("an accepted unpublished build is described as a private preview", () => {
  assert.deepEqual(
    websiteHeaderStatus({
      acceptedPreview: true,
      published: false,
      ventureStatus: { code: "live", label: "Live" },
    }),
    {
      code: "private_preview",
      label: "Private preview",
      detail: "This accepted build is visible only to you until you publish it.",
    },
  );
});

test("a published build retains the venture's operational status", () => {
  const live = { code: "live", label: "Live" };
  assert.equal(
    websiteHeaderStatus({ acceptedPreview: true, published: true, ventureStatus: live }),
    live,
  );
});

test("a venture without an accepted build retains its real status", () => {
  const building = { code: "building", label: "Building" };
  assert.equal(
    websiteHeaderStatus({ acceptedPreview: false, published: false, ventureStatus: building }),
    building,
  );
});

test("an accepted build takes precedence over an older simulated First Look URL", () => {
  assert.deepEqual(
    websiteSurfaceState({
      acceptedPreview: true,
      reachable: true,
      stillBuilding: false,
      published: false,
      simulated: true,
    }),
    {
      showAcceptedPreview: true,
      showHostedPreview: false,
      displayPublished: false,
      displaySimulated: false,
      useStoredUrl: false,
    },
  );
});

test("a live artifact-bound deployment replaces the private evidence preview", () => {
  assert.deepEqual(
    websiteSurfaceState({
      acceptedPreview: true,
      reachable: true,
      stillBuilding: false,
      published: true,
      simulated: false,
    }),
    {
      showAcceptedPreview: false,
      showHostedPreview: true,
      displayPublished: true,
      displaySimulated: false,
      useStoredUrl: true,
    },
  );
});

test("a stored site remains visible before a reviewed build exists", () => {
  assert.deepEqual(
    websiteSurfaceState({
      acceptedPreview: false,
      reachable: true,
      stillBuilding: false,
      published: true,
      simulated: false,
    }),
    {
      showAcceptedPreview: false,
      showHostedPreview: true,
      displayPublished: true,
      displaySimulated: false,
      useStoredUrl: true,
    },
  );
});

test("a finished build awaiting acceptance links to its review instead of saying no landing page", () => {
  const view = websiteEmptyState({ acceptedPreviewBusy: false, stillBuilding: false, reviewHref: "/v/v1/builds?app=a&run=b#build-review" });
  assert.match(view.title, /ready to review/);
  assert.equal(view.href, "/v/v1/builds?app=a&run=b#build-review");
  assert.doesNotMatch(view.body, /Approve the execution plan/);
});
