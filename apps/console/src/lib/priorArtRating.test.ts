import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
import vm from "node:vm";

const require = createRequire(import.meta.url), ts = require("typescript");
type Node = { type: string; props: Record<string, any> };
function harness(onSubmit: (ratings: unknown, note: string) => Promise<boolean>) {
  const hooks: any[] = [];
  let cursor = 0;
  const react = {
    useState(initial: unknown) {
      const index = cursor++;
      if (!(index in hooks)) hooks[index] = initial;
      return [hooks[index], (value: any) => { hooks[index] = typeof value === "function" ? value(hooks[index]) : value; }];
    },
    useRef(initial: unknown) { const index = cursor++; return hooks[index] ||= { current: initial }; },
  };
  const jsx = (type: string, props: Node["props"]) => ({ type, props });
  const exports: any = {};
  const path = fileURLToPath(new URL("../components/research/PriorArtRating.tsx", import.meta.url));
  const output = ts.transpileModule(readFileSync(path, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  vm.runInNewContext(output, { exports, require(id: string) {
    if (id === "react") return react;
    if (id === "react/jsx-runtime") return { jsx, jsxs: jsx };
    if (id === "@/components/primitives/ErrorNotice") return { ErrorNotice: "ErrorNotice" };
    throw new Error(id);
  } });
  function nodes(tree: any): Node[] {
    if (!tree || typeof tree !== "object") return [];
    if (Array.isArray(tree)) return tree.flatMap(nodes);
    return tree.props ? [tree, ...nodes(tree.props.children)] : [];
  }
  return {
    render() { cursor = 0; return nodes(exports.PriorArtRating({ onSubmit })); },
    change(control: string, value: string) {
      this.render().find(node => node.props["data-usage-control"] === control)!.props.onChange({ target: { value } });
    },
    complete() {
      this.change("research_prior_rating_useful", "4");
      this.change("research_prior_rating_new", "3");
      this.change("research_prior_rating_actionable", "5");
    },
    submit() { this.render().find(node => node.type === "form")!.props.onSubmit({ preventDefault() {} }); },
  };
}
const settle = () => new Promise(setImmediate);

test("research usefulness starts optional, quiet and unsubmitted with three native ratings", () => {
  let calls = 0;
  const h = harness(async () => { calls++; return true; }), nodes = h.render();
  assert.equal(nodes.filter(node => node.type === "select").length, 3);
  for (const select of nodes.filter(node => node.type === "select")) {
    assert.equal(select.props.value, "");
    assert.equal(select.props.required, true);
    assert.deepEqual(Array.from(select.props.children[1], (option: Node) => option.props.value), ["1", "2", "3", "4", "5"]);
  }
  const note = nodes.find(node => node.type === "textarea")!, button = nodes.find(node => node.type === "button")!;
  assert.equal(note.props.maxLength, 500);
  assert.equal(button.props.disabled, true);
  assert.equal(button.props.style.background, "transparent");
  assert.deepEqual(nodes.filter(node => ["select", "textarea", "button"].includes(node.type)).map(node => node.props["data-usage-control"]),
    ["research_prior_rating_useful", "research_prior_rating_new", "research_prior_rating_actionable", "research_prior_rating_note", "research_prior_rating_submit"]);
  assert.equal(calls, 0);
  h.submit(); assert.equal(calls, 0);
});

test("explicit rating submission sends one bounded payload and acknowledges only confirmed success", async () => {
  const calls: { ratings: unknown; note: string }[] = [];
  let finish!: (value: boolean) => void;
  const h = harness((ratings, note) => { calls.push({ ratings, note }); return new Promise(resolve => finish = resolve); });
  h.complete(); h.change("research_prior_rating_note", "  " + "x".repeat(600));
  const oldSubmit = h.render().find(node => node.type === "form")!.props.onSubmit;
  assert.equal(h.render().find(node => node.type === "button")!.props.disabled, false);
  h.submit(); h.submit();
  assert.equal(calls.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].ratings)), { useful: 4, new: 3, actionable: 5 });
  assert.ok(calls[0].note.length <= 500);
  assert.ok(h.render().filter(node => ["select", "textarea", "button"].includes(node.type)).every(node => node.props.disabled));
  assert.ok(!h.render().some(node => node.props.role === "status"));
  finish(true); await settle();
  assert.ok(h.render().some(node => node.props.role === "status" && node.props.children === "Thanks. Your feedback was saved."));
  assert.ok(!h.render().some(node => node.type === "button"));
  h.submit(); assert.equal(calls.length, 1);
  oldSubmit({ preventDefault() {} }); assert.equal(calls.length, 1);
});

test("rejected or failed feedback preserves ratings and can be retried without an automatic post", async () => {
  for (const throws of [false, true]) {
    let calls = 0;
    const h = harness(async () => { calls++; if (calls === 1) { if (throws) throw new Error("private error"); return false; } return true; });
    h.complete(); h.change("research_prior_rating_note", "More pricing detail");
    h.submit(); await settle();
    assert.equal(calls, 1);
    assert.ok(h.render().some(node => node.type === "ErrorNotice" && node.props.error === "Your feedback could not be saved. Try again."
      && node.props.source === "research.priorArtRating.error"));
    assert.ok(!h.render().some(node => node.props.role === "status"));
    assert.equal(h.render().find(node => node.type === "textarea")!.props.value, "More pricing detail");
    assert.deepEqual(h.render().filter(node => node.type === "select").map(node => node.props.value), ["4", "3", "5"]);
    h.submit(); await settle();
    assert.equal(calls, 2);
    assert.ok(h.render().some(node => node.props.role === "status"));
  }
});

test("invalid ratings do not invoke the parent callback and a new report starts fresh", () => {
  let calls = 0;
  const submit = async () => { calls++; return true; };
  const h = harness(submit); h.complete();
  for (const value of ["0", "6", "", "3.5", "invalid"]) {
    h.change("research_prior_rating_new", value); h.submit();
    assert.equal(calls, 0, value);
  }
  assert.ok(harness(submit).render().filter(node => node.type === "select").every(node => node.props.value === ""));
});
