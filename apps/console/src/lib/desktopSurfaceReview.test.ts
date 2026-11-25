import assert from "node:assert/strict";
import test from "node:test";
import { reviewedSurfaceBody, type ReviewedDesktopSurface } from "./desktopSurfaceReview.ts";
const first: ReviewedDesktopSurface = {application_bundle:"com.apple.TextEdit", process_id:101, process_start_identity:"1001", window_id:11, window_title:"Owned"};
const second: ReviewedDesktopSurface = {application_bundle:"com.apple.Notes", process_id:202, process_start_identity:"2002", window_id:22, window_title:"Owned note"};
test("only explicit selection creates multi-window authority; first selection stays primary", () => {
  assert.equal(reviewedSurfaceBody(null,"v",1), undefined);
  const receipt={ventureId:"v",epoch:1,surfaces:[second,first]};
  assert.deepEqual(reviewedSurfaceBody(receipt,"v",1), {reviewed_surfaces:[second,first]});
  assert.throws(() => reviewedSurfaceBody({...receipt,surfaces:[]},"v",1));
});
test("stale account epoch including A to B to A and foreign venture deny selection", () => {
  const receipt={ventureId:"v",epoch:1,surfaces:[first]};
  assert.throws(() => reviewedSurfaceBody(receipt,"v",3));
  assert.throws(() => reviewedSurfaceBody(receipt,"other",1));
});
test("duplicate, ambiguous process custody and invalid start identity refuse", () => {
  for (const surfaces of [[first,first],[first,{...second,process_id:101}], [{...first,process_start_identity:"unknown"}],Array(9).fill(first)]) {
    assert.throws(() => reviewedSurfaceBody({ventureId:"v",epoch:1,surfaces},"v",1));
  }
});
