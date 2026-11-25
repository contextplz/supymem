import assert from "node:assert/strict";
import test from "node:test";
import { correctionAvailabilityMatches } from "./privateCorrectionAvailability.ts";
import { apiFailure } from "./apiFailure.ts";

test("correction control needs server-qualified exact baseline, independent of historical reviewer label", () => {
  const owner={build:"b",artifact:"a",report:"r"};
  const value={build_id:"b",available:true,reason:"",source_artifact_id:"a",report_digest:"r"};
  assert.equal(correctionAvailabilityMatches(value,owner),true);
  for(const changed of [{available:false},{build_id:"other"},{source_artifact_id:"other"},{report_digest:"other"}])
    assert.equal(correctionAvailabilityMatches({...value,...changed},owner),false);
  assert.equal(correctionAvailabilityMatches(null,owner),false);
});
test("private correction stale and unsupported proof errors identify the real next gate", () => {
  const failure=(code:string)=>apiFailure({ok:false,status:409,data:{detail:{code}}},"saving a correction");
  assert.match(failure("hosting_build_not_accepted"),/current accepted source and QA proof/);
  assert.match(failure("private_correction_plan_changed"),/current approved plan/);
  assert.match(failure("private_correction_baseline_changed"),/current accepted result/);
  assert.match(failure("private_correction_use_published_revision"),/Website revision/);
});
