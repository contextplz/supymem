import assert from "node:assert/strict";
import test from "node:test";
import { browserProfileWireAvailable, browserProfileWireSupported } from "./browserProfileWireAvailability.ts";

test("local marker is exact parser support, never inferred from installation, readiness or app version", () => {
  assert.equal(browserProfileWireSupported({browser_cuai_text_v1: true}), true);
  for (const value of [null, undefined, {}, [], true, {browser_cuai_text_v1: false},
    {browser_cuai_text_v1: "true"}, {browser_cuai_text_v1: 1}, {available: true},
    {version: "0.1.81", ready: true}, {browser_cuai_text_v1: true, available: true}]) {
    assert.equal(browserProfileWireSupported(value), false);
  }
});

test("v2 requires its compiled marker while additive known markers preserve v1", () => {
  const markers={browser_cuai_text_v1:true,browser_cuai_text_v2:true,pagebuilder_reference_v1:true};
  assert.equal(browserProfileWireSupported(markers),true);
  assert.equal(browserProfileWireSupported(markers,"browser_cuai_text_v2"),true);
  assert.equal(browserProfileWireSupported({browser_cuai_text_v1:true},"browser_cuai_text_v2"),false);
  assert.equal(browserProfileWireSupported({...markers,browser_cuai_text_v2:"true"},"browser_cuai_text_v2"),false);
  assert.equal(browserProfileWireSupported(markers,"future"),false);
});

test("actual quiet native IPC refuses old, absent and malformed builds and accepts the exact current marker", async () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "window");
  const calls: Array<{command: string; args: unknown}> = [];
  try {
    Object.defineProperty(globalThis, "window", {configurable: true, value: {}});
    assert.equal(await browserProfileWireAvailable(), false);
    for (const reply of [null, {}, {browser_cuai_text_v1: false}, {browser_cuai_text_v1: "true"}, {browser_cuai_text_v1: true}]) {
      Object.defineProperty(globalThis, "window", {configurable: true, value: {__TAURI__: {core: {invoke: async (command: string, args?: unknown) => {
        calls.push({command, args}); return reply;
      }}}}});
      assert.equal(await browserProfileWireAvailable(), reply?.browser_cuai_text_v1 === true);
    }
    Object.defineProperty(globalThis, "window", {configurable: true, value: {__TAURI_INTERNALS__: {invoke: async () => {
      throw new Error("unknown command device_work_order_capabilities");
    }}}});
    assert.equal(await browserProfileWireAvailable(), false);
    assert.equal(calls.length, 5);
    assert.ok(calls.every(call => call.command === "device_work_order_capabilities" && call.args === undefined));
  } finally {
    if (original) Object.defineProperty(globalThis, "window", original);
    else Reflect.deleteProperty(globalThis, "window");
  }
});


test("qualification requires its literal compiled marker without changing ordinary readiness", () => {
  for (const marker of [undefined, false, "true", 1, true]) {
    const value = {browser_cuai_text_v1: true, browser_cuai_read_only_v1: true, ...(marker === undefined ? {} : {browser_qualification_v1: marker})};
    assert.equal(browserProfileWireSupported(value), marker === undefined || typeof marker === "boolean");
    assert.equal(browserProfileWireSupported(value, "browser_cuai_text_v1", true), marker === undefined || typeof marker === "boolean");
    assert.equal(browserProfileWireSupported(value, "browser_cuai_text_v1", false, true), marker === true);
  }
  assert.equal(browserProfileWireSupported({browser_cuai_text_v1: true}, "browser_cuai_text_v1", false, true), false);
});

test("the exact marker set the shipped Mac app reports (device_order.rs NativeWorkOrderCapabilities) is supported", () => {
  // Mac app 0.1.105 reports product_build_v1 too; an unknown-to-console marker used to block every browser task.
  const shipped = {browser_cuai_text_v1: true, pagebuilder_reference_v1: true, browser_cuai_text_v2: true,
    browser_cuai_read_only_v1: true, browser_qualification_v1: true, product_build_v1: true};
  assert.equal(browserProfileWireSupported(shipped), true);
  assert.equal(browserProfileWireSupported(shipped, "browser_cuai_text_v2"), true);
  assert.equal(browserProfileWireSupported(shipped, "browser_cuai_text_v1", true), true);
  assert.equal(browserProfileWireSupported({...shipped, product_build_v1: "true"}), false);
});
