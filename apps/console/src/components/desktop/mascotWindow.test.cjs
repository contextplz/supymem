const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../../../../../src-tauri");
const html = fs.readFileSync(path.join(root, "splashscreen/swing/index.html"), "utf8");
const rust = fs.readFileSync(path.join(root, "src/mascot.rs"), "utf8");
const config = JSON.parse(fs.readFileSync(path.join(root, "tauri.conf.json"), "utf8"));
const ts = require('typescript');
const mascot = {exports: {}};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(root, 'splashscreen/swing/mascot-player.js'), 'utf8'), {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText, mascot);

test("mascot tests load when the optional SVG poster is absent", () => {
  const filename = path.join(__dirname, "mascotWindow.test.cjs");
  const optionalPoster = path.join(root, "splashscreen/swing/poster.svg");
  const filesystem = {
    ...fs,
    existsSync: file => file === optionalPoster ? false : fs.existsSync(file),
    readFileSync: (file, ...options) => {
      if (file === optionalPoster) throw Object.assign(new Error("optional poster absent"), {code: "ENOENT"});
      return fs.readFileSync(file, ...options);
    },
  };
  assert.doesNotThrow(() => vm.runInNewContext(fs.readFileSync(filename, "utf8"), {
    __dirname,
    require: name => name === "node:fs" ? filesystem : name === "node:test" ? () => {} : require(name),
  }));
});

test("local menu-bar mascot uses bundled assets before the development server branch", () => {
  const url = rust.slice(rust.indexOf("fn mascot_page_url("), rust.indexOf("#[derive"));
  assert.match(url, /if crate::local_stack::is_local\(\)\s*\{[\s\S]*?WebviewUrl::External\("tauri:\/\/localhost\/swing\/index\.html"/);
  assert.ok(url.indexOf("crate::local_stack::is_local()") < url.indexOf("cfg!(debug_assertions)"));
});

async function page({ supported = true, reduced = false, hidden = false, visible, deferred = false, posterLoaded = false } = {}) {
  const classes = new Set(["fallback"]);
  const classList = {
    add: (...names) => names.forEach(name => classes.add(name)),
    remove: name => classes.delete(name),
    contains: name => classes.has(name),
    toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
  };
  const spin = { style: {} };
  const events = {}, calls = [];
  const motion = { matches: reduced, addEventListener: (name, callback) => { events[`motion:${name}`] = callback; } };
  const canvas = { addEventListener: (name, callback) => { events[name] = callback; } };
  const preview = { naturalWidth: posterLoaded ? 48 : 0, addEventListener: (name, callback) => { events[name] = callback; } };
  const poster = { classList, querySelector: () => preview };
  const player = {
    clip: "Swing_Joy", speaking: false, playing: false,
    play(name, options) { calls.push(["play", name, options]); if (name) this.clip = name; this.playing = true; },
    pause() { calls.push(["pause"]); this.playing = false; },
    playOnce(name) { calls.push(["playOnce", name]); this.clip = name; this.speaking = false; },
    speak() { calls.push(["speak"]); this.speaking = true; this.clip = "Speak_Start"; },
    stopSpeaking() { calls.push(["stopSpeaking"]); if (this.speaking) { this.speaking = false; this.clip = "Speak_End"; } },
    setVoiceLevel(level) { calls.push(["level", level]); },
    dispose() { calls.push(["dispose"]); },
  };
  let resolve;
  const window = { __mascotVisible: visible, matchMedia: () => motion,
    addEventListener: (name, callback) => { events[name] = callback; } };
  const document = {
    hidden,
    documentElement: { classList },
    getElementById: name => ({ spin, swing: canvas, poster })[name],
    addEventListener: (name, callback) => { events[name] = callback; },
  };
  const script = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1].replace(/^\s*import[^;]+;/m, "");
  vm.runInNewContext(script, { window, document, setMascotState: mascot.exports.setMascotState, createMascotPlayer: (target, options) => {
    assert.equal(target, canvas);
    assert.equal(options.src, "./mascot_animations.glb");
    assert.equal(options.clip, "Swing_Joy");
    assert.equal(options.loop, true);
    events.clipEnd = options.onClipEnd;
    if (!supported) return Promise.reject(new Error("WebGL2 unavailable"));
    return deferred ? new Promise(done => { resolve = done; }) : Promise.resolve(player);
  } });
  await Promise.resolve(); await Promise.resolve();
  return { classes, window, document, events, spin, player, calls, motion, resolve: async () => { resolve(player); await Promise.resolve(); } };
}

test("hover keeps the mascot visible without a microphone glyph", () => {
  assert.doesNotMatch(html, /#mic|id="mic"/);
});

test("Charlie shows a static fallback without artwork, JavaScript or WebGL2", async () => {
  for (const file of [path.join(root, "splashscreen/swing/poster.png"), path.join(__dirname, "../../../public/charlie/poster.png")]) {
    if (!fs.existsSync(file)) continue;
    const png = fs.readFileSync(file);
    assert.equal(png.subarray(1, 4).toString(), "PNG");
    assert.ok(png.readUInt32BE(16) > 0);
    assert.ok(png.readUInt32BE(20) > 0);
  }
  assert.match(html, /<html[^>]+class="fallback"/);
  assert.match(html, /<div id="poster" aria-hidden="true">\s*<span>C<\/span>/);
  assert.match(html, /#poster \{[^}]*z-index: 1; display: block/);
  assert.match(html, /html:not\(\.fallback\) #poster \{ display: none/);
  assert.match(html, /html\.fallback #swing \{ visibility: hidden/);
  const posterPath = path.join(root, "splashscreen/swing/poster.svg");
  if (fs.existsSync(posterPath)) assert.match(fs.readFileSync(posterPath, "utf8"), /<image href="\.\/poster.png"/);
  const state = await page({ supported: false });
  state.window.__mascotHover(true);
  assert.ok(state.classes.has("fallback"));
  assert.ok(!state.classes.has("has-image"));
  const withPoster = await page({ supported: false, posterLoaded: true });
  assert.ok(withPoster.classes.has("has-image"));
});

test("the zero-dependency player loads the real clips and loops Swing_Joy", async () => {
  const state = await page();
  assert.ok(!state.classes.has("fallback"));
  assert.ok(state.player.playing);
  assert.equal(state.player.clip, "Swing_Joy");
  assert.match(html, /import \{ createMascotPlayer, setMascotState \} from "\.\/mascot-player.js"/);
  assert.match(html, /<canvas[^>]+aria-hidden="true"/);
  assert.doesNotMatch(html, /class="eyes"|class="face"|id="rod"|three|innerWidth/);
  for (const file of ["mascot-swing.glb", "assets/index-DRQsIXGy.js", "THREE-LICENSE.txt"]) {
    assert.ok(!fs.existsSync(path.join(root, "splashscreen/swing", file)), file);
  }
});

test("native actions nod once per clip, speak with levels, and return to the swing", async () => {
  const { window, player, calls } = await page();
  window.charlieMascot.set("listening");
  window.charlieMascot.set("listening");
  assert.equal(calls.filter(call => call[0] === "playOnce").length, 1);
  player.clip = "Swing_Joy";
  window.charlieMascot.set("listening");
  assert.equal(calls.filter(call => call[0] === "playOnce").length, 2);
  window.charlieMascot.set("idle");
  assert.equal(player.clip, "Swing_Joy");
  window.charlieMascot.set("speaking", 0.4);
  assert.equal(player.speaking, true);
  assert.deepEqual(calls.at(-2), ["level", 0.4]);
  window.charlieMascot.set("speaking", 4);
  assert.deepEqual(calls.at(-2), ["level", 1]);
  window.charlieMascot.set("idle");
  assert.equal(player.clip, "Speak_End");
  assert.equal(player.speaking, false);
  assert.ok(calls.some(call => call[0] === "level" && call[1] === null));
  const count = calls.length;
  window.charlieMascot.set("invalid");
  assert.equal(calls.length, count);
});

test("native lean stays bounded, including invalid inputs, independently of idle swing", async () => {
  const state = await page();
  for (const [input, expected] of [[0.45, 0.28], [-0.45, -0.28], [0.12, 0.12], ["bad", 0], [NaN, 0], [0, 0]]) {
    state.window.__mascotLean(input);
    assert.equal(state.spin.style.transform, `rotate(${expected}rad)`);
  }
  assert.match(html, /@media \(prefers-reduced-motion: reduce\) \{\s*#spin \{ transition: none; \}/);
});

test("the transparent canvas retains the native tile and character hit area", () => {
  const constant = name => Number(rust.match(new RegExp(`const ${name}: f64 = ([\\d.]+)`))[1]);
  assert.match(html, /#spin \{[^}]*width: 48px; height: 62px/);
  assert.match(html, /#swing \{[^}]*width: 48px; height: 62px/);
  assert.ok(constant("HIT_HALF_W") < constant("TILE_W") / 2);
  assert.ok(62 <= constant("HIT_H"));
  assert.equal(constant("TILE_W"), 96);
  assert.equal(constant("TILE_H"), 88);
});

test("both surfaces share the same player and any shipped art contains all eight animation clips", () => {
  const desktop = path.join(root, "splashscreen/swing");
  const consolePublic = path.join(__dirname, "../../../public/charlie");
  assert.ok(fs.readFileSync(path.join(desktop, "mascot-player.js")).equals(fs.readFileSync(path.join(__dirname, "../../lib/mascotPlayer.js"))));
  assert.ok(!fs.existsSync(path.join(consolePublic, "mascot-swing.glb")));
  const assets = [desktop, consolePublic].flatMap(dir => ["mascot_animations.glb", "poster.png"].map(name => path.join(dir, name)));
  if (!assets.some(file => fs.existsSync(file))) return;
  const [asset, poster, consoleAsset, consolePoster] = assets.map(file => fs.readFileSync(file));
  assert.equal(asset.subarray(0, 4).toString(), "glTF");
  assert.equal(asset.readUInt32LE(4), 2);
  const model = JSON.parse(asset.subarray(20, 20 + asset.readUInt32LE(12)).toString());
  assert.deepEqual(model.animations.map(clip => clip.name), ["Swing_Joy", "Listen_Nod", "Speak_Start", "Speak_Loop", "Speak_End", "Branch_Idle", "Branch_Fall", "Hand_Toss"]);
  const swing = model.animations.find(clip => clip.name === "Swing_Joy");
  assert.equal(Math.max(...swing.samplers.map(sampler => model.accessors[sampler.input].max[0])), 12);
  assert.ok(asset.equals(consoleAsset));
  assert.ok(poster.equals(consolePoster));
});

test("CSP still supports CharlieIntro's GLB and textures without unsafe scripts", () => {
  const directives = Object.fromEntries(config.app.security.csp.split(";").map(value => {
    const [name, ...sources] = value.trim().split(/\s+/);
    return [name, sources];
  }));
  assert.ok(directives["connect-src"].includes("'self'"));
  assert.ok(directives["connect-src"].includes("blob:"));
  assert.ok(directives["img-src"].includes("blob:"));
  assert.deepEqual(directives["script-src"], ["'self'"]);
  assert.deepEqual(directives["default-src"], ["'none'"]);
});

test("voice command is guarded for the trusted main window and uses the existing eval helper", () => {
  const lib = fs.readFileSync(path.join(root, "src/lib.rs"), "utf8");
  const command = lib.split("fn charlie_mascot_state(")[1].split("fn require_native_local_window(")[0];
  assert.match(command, /require_native_command_window\(&window, &app_state\)\?/);
  assert.match(command, /window.label\(\) != "main"/);
  assert.match(command, /charlie_voice_level\(level\)\?/);
  assert.match(command, /Duration::from_millis\(50\)/);
  assert.match(command, /mascot::set_state\(&app, state.name\(\), level\)/);
  assert.match(rust, /script\(app, format!\("window.charlieMascot&&window.charlieMascot.set/);
  assert.match(lib, /generate_handler!\[\s*charlie_mascot_state,/);
  assert.match(fs.readFileSync(path.join(root, "permissions/app.toml"), "utf8"), /"charlie_mascot_state"/);
});

test("the mascot panel never intercepts mouse events (pointer is polled natively)", () => {
  assert.match(rust, /panel\.setIgnoresMouseEvents\(true\)/);
  assert.match(rust, /host\.setIgnoresMouseEvents\(true\)/);
});

test("the host stays hidden on startup and every cached panel lookup", () => {
  const source = rust.split("#[cfg(test)]")[0];
  const install = source.slice(source.indexOf("pub fn install("));
  const panel = source.slice(source.indexOf("fn panel("), source.indexOf("fn set_frame("));
  assert.match(install, /\.visible\(false\)[\s\S]*?\.build\(\)[\s\S]*?let ns = panel\(app\)[\s\S]*?set_spaces\(ns, false\)/);
  assert.ok(panel.indexOf("HOST.load") < panel.indexOf(".ns_window()"));
  assert.ok(panel.indexOf("HOST.store") < panel.indexOf("OVERLAY.store"));
  assert.ok(panel.indexOf("host.orderOut(None)") < panel.indexOf("return Some"));
  const visibility = source.slice(source.indexOf("pub fn set_visible("), source.indexOf("fn script("));
  assert.match(visibility, /panel\(app\)[\s\S]*?ns\.orderFrontRegardless\(\)[\s\S]*?ns\.orderOut\(None\)/);
  assert.doesNotMatch(source, /\.show\(|\.set_visible\(true\)|\.visible\(true\)|host\.(?:orderFront|makeKey)/);
  assert.doesNotMatch(install, /\.always_on_top\(|\.visible_on_all_workspaces\(/);
  const lib = fs.readFileSync(path.join(root, "src/lib.rs"), "utf8");
  assert.match(lib, /"charlie_visibility" => \{\s*if let Err\(error\) = mascot::set_visible\(app, !mascot::is_visible\(\)\)/);
  for (const file of fs.readdirSync(path.join(root, "src")).filter(name => name.endsWith(".rs") && name !== "mascot.rs")) {
    const source = fs.readFileSync(path.join(root, "src", file), "utf8");
    assert.doesNotMatch(source, /get_(?:webview_)?window\("mascot"\)/, file);
  }
});

test("idle swing follows native visibility even when the hidden host marks the document hidden", async () => {
  assert.match(rust, /initialization_script\(format!\("window\.__mascotVisible=\{visible\}"\)\)/);
  assert.match(rust, /script\(app, format!\("window\.__mascotVisible=\{visible\}"\)\)/);
  const state = await page({ hidden: true, visible: true });
  assert.ok(!state.classes.has("paused"));
  state.window.__mascotVisible = false;
  assert.ok(state.classes.has("paused"));
  assert.equal(state.player.playing, false);
  state.window.__mascotVisible = undefined;
  assert.ok(state.classes.has("paused"));
  state.document.hidden = false;
  state.events.visibilitychange();
  assert.ok(!state.classes.has("paused"));
  state.document.hidden = true;
  state.events.visibilitychange();
  assert.ok(state.classes.has("paused"));
  state.window.__mascotVisible = true;
  assert.ok(!state.classes.has("paused"));
  assert.equal(state.player.playing, true);
});

test("reduced motion and context loss pause the player and keep the poster", async () => {
  const state = await page({ reduced: true, visible: true });
  assert.ok(state.classes.has("fallback"));
  assert.equal(state.player.playing, false);
  state.window.charlieMascot.set("speaking", 0.9);
  assert.equal(state.player.speaking, false);
  state.motion.matches = false;
  state.events["motion:change"]();
  assert.ok(!state.classes.has("fallback"));
  assert.equal(state.player.speaking, true);
  state.events.webglcontextlost();
  assert.ok(state.classes.has("fallback"));
  assert.equal(state.player.playing, false);
});

test("an action received before the GLB loads is applied after loading", async () => {
  const state = await page({ deferred: true });
  state.window.charlieMascot.set("speaking", 0.3);
  await state.resolve();
  assert.equal(state.player.speaking, true);
  assert.ok(state.calls.some(call => call[0] === "level" && call[1] === 0.3));
});

test("hover, drag and click polling use the panel frame without native mouse handlers", () => {
  const tick = rust.slice(rust.indexOf("fn tick("), rust.indexOf("fn context_menu("));
  assert.match(tick, /panel\(app\)/);
  assert.match(tick, /NSEvent::mouseLocation\(\)/);
  assert.match(tick, /NSEvent::pressedMouseButtons\(\)/);
  assert.match(tick, /hit\(frame, mouse\)/);
  assert.match(tick, /window\.__mascotHover/);
  assert.match(tick, /set_frame\(ns, &geo, x\)/);
  assert.match(tick, /click = drag\.moved <= CLICK_SLOP/);
  assert.match(tick, /crate::bring_main_forward\(app\)/);
});
