import assert from "node:assert/strict";
import test from "node:test";
import { isMissingCompanyConfig, companySettingsPayload } from "./opsSettings.ts";

test("only explicit missing config permits onboarding; auth, transport and unrelated 404s remain errors", () => {
  assert.equal(isMissingCompanyConfig({status:404, detail:"No company config yet"}), true);
  for (const error of [new Error("offline"), {status:403, detail:"No company config yet"},
    {status:404, detail:"not_found"}, {status:500, detail:"No company config yet"}, null]) {
    assert.equal(isMissingCompanyConfig(error), false);
  }
});
const form = {name:" Bakery ", timezone:"UTC", daily_cycle_hour:9};
test("save uses existing upsert fields and excludes response IDs and unedited configuration", () => {
  const payload = companySettingsPayload({...form, id:"cfg", workspace_id:"other", goals:{keep:true}, kpis:{keep:true}});
  assert.equal(payload.name, "Bakery");
  assert.equal(payload.daily_cycle_hour, 9);
  for (const key of ["id", "workspace_id", "goals", "kpis"]) assert.equal(key in payload, false);
});
test("invalid onboarding input cannot start a save", () => {
  for (const values of [{name:" "}, {timezone:"not/a/timezone"}, {daily_cycle_hour:24},
    {daily_cycle_hour:""}, {daily_cycle_hour:1.5}]) assert.throws(() => companySettingsPayload({...form,...values}));
});
