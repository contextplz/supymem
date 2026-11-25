//! Supymem agent cursor: a branded pointer that shows where Mac computer use is
//! acting while the founder's real cursor stays untouched.
//!
//! The runtime sidecar sends closed JSON events (see
//! `packages/desktop_runtime/agent_cursor.py`) as datagrams to a private Unix
//! socket. This module validates them and drives one transparent, click-through,
//! screen-capture-excluded overlay window per display through `eval`; the pages
//! have no IPC access. Windows are ordered front without activating the app, so
//! they never take focus or input.

use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::time::Duration;

#[path = "agent_cursor_custody.rs"]
mod custody;
use custody::FeedCustody;
#[path = "agent_cursor_readiness.rs"]
mod readiness;
#[path = "agent_cursor_receipt.rs"]
mod receipt;
pub use receipt::Rendered;

/// Same closed display copy as the renderer; arbitrary event labels never enter receipts.
fn activity(event: &Value) -> &'static str {
    if event["kind"] == "end" { return ""; }
    let label = event["label"].as_str().unwrap_or("").trim().trim_end_matches('…');
    for safe in ["Reading screen", "Reading page", "Capturing screen", "Opening page", "Opening folder", "Waiting", "Working on code", "Working in browser", "Saving note", "Updating Calendar", "Updating Reminders", "Exporting image", "Saving file", "Organizing files", "Working with files", "Running a script", "Clicking", "Double-clicking", "Right-clicking", "Typing", "Scrolling", "Dragging"] {
        if label == safe { return safe; }
    }
    if label.starts_with("Pressing ") { return "Using keyboard"; }
    if label.starts_with("Menu ") { return "Using menu"; }
    if label.starts_with("Opening ") { return "Opening app"; }
    if label.starts_with("Switching to ") { return "Switching app"; }
    match event["kind"].as_str().unwrap_or("") {
        "begin" => "Starting", "move" => "Moving", "click" => "Clicking", "type" => "Typing",
        "scroll" => "Scrolling", "read" => "Reading screen", "done" => "Checking result", "fail" => "Needs attention", _ => "",
    }
}

pub const SCHEMA_VERSION: u64 = 1;
const KINDS: [&str; 9] = ["begin", "move", "click", "type", "scroll", "read", "done", "fail", "end"];
const MAX_LABEL: usize = 40;
const COORD_LIMIT: f64 = 100_000.0;
pub const DEFAULT_IDLE_HIDE_SECS: u64 = 8;
/// A task that stops talking for this long no longer counts as running.
pub const STALE_TASK_SECS: u64 = 120;

fn renderer_origin_allowed(label: &str, url: &url::Url, dev: bool) -> bool {
    label.starts_with("agent-cursor-") && url.path() == "/agent-cursor/index.html"
        && url.username().is_empty() && url.password().is_none()
        && ((url.scheme() == "tauri" && url.host_str() == Some("localhost"))
            || (url.scheme() == "http" && url.host_str() == Some("tauri.localhost") && url.port().is_none())
            || (dev && url.scheme() == "http" && url.host_str() == Some("127.0.0.1") && url.port() == Some(3117)))
}

/// Window flags the overlay must always have. `build` applies exactly these and
/// the tests assert them, so a regression cannot silently make it grab input.
#[derive(Debug, PartialEq)]
pub struct OverlaySpec {
    pub transparent: bool,
    pub decorations: bool,
    pub shadow: bool,
    pub always_on_top: bool,
    pub visible_on_all_workspaces: bool,
    pub skip_taskbar: bool,
    pub resizable: bool,
    pub focused: bool,
    /// Created hidden: showing goes through `orderFrontRegardless`, never tao's activating show.
    pub visible: bool,
    pub ignore_cursor_events: bool,
    /// NSWindowSharingNone: excluded from screenshots so the agent never sees its own cursor.
    pub excluded_from_capture: bool,
}

pub const SPEC: OverlaySpec = OverlaySpec {
    transparent: true,
    decorations: false,
    shadow: false,
    always_on_top: true,
    visible_on_all_workspaces: true,
    skip_taskbar: true,
    resizable: false,
    focused: false,
    visible: false,
    ignore_cursor_events: true,
    excluded_from_capture: true,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Prefs {
    pub enabled: bool,
    pub idle_hide_secs: u64,
}

impl Default for Prefs {
    fn default() -> Self {
        Prefs { enabled: true, idle_hide_secs: DEFAULT_IDLE_HIDE_SECS }
    }
}

impl Prefs {
    pub fn new(enabled: bool, idle_hide_secs: u64) -> Self {
        Prefs { enabled, idle_hide_secs: idle_hide_secs.clamp(2, 300) }
    }
}

fn prefs_path(dir: &Path) -> PathBuf {
    dir.join("agent_cursor.json")
}

pub fn load_prefs(dir: &Path) -> Prefs {
    let Ok(raw) = std::fs::read_to_string(prefs_path(dir)) else { return Prefs::default() };
    let Ok(v) = serde_json::from_str::<Value>(&raw) else { return Prefs::default() };
    Prefs::new(
        v["enabled"].as_bool().unwrap_or(true),
        v["idle_hide_secs"].as_u64().unwrap_or(DEFAULT_IDLE_HIDE_SECS),
    )
}

pub fn save_prefs(dir: &Path, prefs: Prefs) -> std::io::Result<()> {
    std::fs::create_dir_all(dir)?;
    std::fs::write(
        prefs_path(dir),
        json!({ "enabled": prefs.enabled, "idle_hide_secs": prefs.idle_hide_secs }).to_string(),
    )
}

/// Overlay is hidden when switched off, or after `idle_hide_secs` without events
/// (`STALE_TASK_SECS` while a task is between its begin and end).
pub fn should_hide(prefs: Prefs, task_active: bool, idle: Duration) -> bool {
    let limit = if task_active { STALE_TASK_SECS } else { prefs.idle_hide_secs };
    !prefs.enabled || idle > Duration::from_secs(limit)
}

/// Per-process datagram socket the sidecar is told about via `SUPYMEM_AGENT_CURSOR_SOCK`.
pub fn socket_path() -> PathBuf {
    std::env::temp_dir().join(format!("supymem-cursor-{}.sock", std::process::id()))
}

/// Validate one datagram and return the normalized event (never forwards raw input).
pub fn parse_event(bytes: &[u8]) -> Option<Value> {
    let raw: Value = serde_json::from_slice(bytes).ok()?;
    if raw["v"].as_u64()? != SCHEMA_VERSION {
        return None;
    }
    let kind = raw["kind"].as_str().filter(|k| KINDS.contains(k))?;
    let mut out = json!({ "v": SCHEMA_VERSION, "kind": kind });
    if let Some(owner) = raw.get("owner") {
        let owner = owner.as_str()?;
        if owner.len() != 32 || !owner.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
            return None;
        }
        out["owner"] = json!(owner);
    }
    match (raw.get("x"), raw.get("y")) {
        (None, None) => {
            if matches!(kind, "move" | "click" | "type" | "scroll") {
                return None;
            }
        }
        (Some(x), Some(y)) => {
            let (x, y) = (x.as_f64()?, y.as_f64()?);
            if !x.is_finite() || !y.is_finite() || x.abs() > COORD_LIMIT || y.abs() > COORD_LIMIT {
                return None;
            }
            out["x"] = json!(x);
            out["y"] = json!(y);
        }
        _ => return None,
    }
    if let Some(label) = raw.get("label").and_then(Value::as_str) {
        let mut text: String = label.chars().map(|c| if c.is_control() { ' ' } else { c }).collect();
        text = text.trim().to_string();
        if text.chars().count() > MAX_LABEL {
            text = text.chars().take(MAX_LABEL - 1).collect::<String>().trim_end().to_string() + "…";
        }
        if !text.is_empty() {
            out["label"] = json!(text);
        }
    }
    Some(out)
}

#[cfg(target_os = "macos")]
pub use mac::{apply_prefs, install, diagnostic_status, export_diagnostic, rendered, retire};

#[cfg(target_os = "macos")]
mod mac {
    use super::*;
    use objc2_app_kit::{NSWindow, NSWindowCollectionBehavior, NSWindowSharingType};
    use std::os::unix::fs::PermissionsExt;
    use std::os::unix::net::UnixDatagram;
    use std::sync::{Mutex, Arc};
    use std::collections::HashMap;
    use tauri::webview::PageLoadEvent;
    use std::time::Instant;
    use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder};

    const PREFIX: &str = "agent-cursor-";

    struct State {
        prefs: Prefs,
        task_active: bool,
        custody: FeedCustody,
        last_event: Instant,
        shown: bool,
        monitors: Vec<(i32, i32, u32, u32)>,
        renderers: HashMap<String, readiness::Renderer>,
        renderer_generation: u64,
        sequence: u64,
        admitted: Option<receipt::Delivery>,
        receipts: HashMap<String, receipt::Receipt>,
        documents: HashMap<String, Arc<receipt::Document>>,
    }
    static STATE: Mutex<Option<State>> = Mutex::new(None);
    // Page callbacks can occur while build holds STATE on the main thread.
    // Separate bounded mailbox avoids synchronous re-entry into that mutex.
    static READY: Mutex<Vec<(String, u64, String)>> = Mutex::new(Vec::new());

    fn flush_ready(app: &AppHandle, st: &mut State) {
        let pending = std::mem::take(&mut *READY.lock().unwrap_or_else(|e| e.into_inner()));
        for (label, generation, init) in pending {
            if let Some(renderer) = st.renderers.get_mut(&label) {
                if let Some(window) = app.get_webview_window(&label) {
                    for script in renderer.loaded(generation, init) { let _ = window.eval(script); }
                }
            }
        }
    }

    fn page_url() -> WebviewUrl {
        // Only `tauri dev` needs the external asset server. A packaged debug
        // build must use its bundled page just like a release build.
        if tauri::is_dev() {
            WebviewUrl::External("http://127.0.0.1:3117/agent-cursor/index.html".parse().expect("cursor url"))
        } else {
            WebviewUrl::App("agent-cursor/index.html".into())
        }
    }

    fn windows(app: &AppHandle) -> Vec<tauri::WebviewWindow> {
        app.webview_windows().into_iter().filter(|(l, _)| l.starts_with(PREFIX)).map(|(_, w)| w).collect()
    }

    fn with_ns(window: &tauri::WebviewWindow, f: impl FnOnce(&NSWindow)) {
        if let Ok(ptr) = window.ns_window() {
            f(unsafe { &*(ptr as *const NSWindow) });
        }
    }

    fn harden(window: &tauri::WebviewWindow) {
        with_ns(window, |ns| {
            use NSWindowCollectionBehavior as B;
            ns.setIgnoresMouseEvents(true);
            ns.setSharingType(NSWindowSharingType::None);
            ns.setLevel(crate::mascot::overlay_level());
            ns.setCanHide(false);
            ns.setHidesOnDeactivate(false);
            ns.setCollectionBehavior(
                B::CanJoinAllSpaces | B::CanJoinAllApplications | B::FullScreenAuxiliary
                    | B::IgnoresCycle | B::FullScreenDisallowsTiling | B::Stationary,
            );
        });
    }

    fn order(window: &tauri::WebviewWindow, front: bool) {
        with_ns(window, |ns| if front { ns.orderFrontRegardless() } else { ns.orderOut(None) });
    }

    /// One overlay per display; rebuilt when the display layout changes.
    fn sync_windows(app: &AppHandle, st: &mut State) -> bool {
        let Ok(monitors) = app.available_monitors() else { return false };
        let layout: Vec<_> = monitors
            .iter()
            .map(|m| (m.position().x, m.position().y, m.size().width, m.size().height))
            .collect();
        if layout == st.monitors && !windows(app).is_empty() {
            return false;
        }
        st.renderers.clear();
        st.receipts.clear();
        st.documents.clear();
        st.renderer_generation = st.renderer_generation.wrapping_add(1);
        let generation = st.renderer_generation;
        for old in windows(app) {
            let _ = old.close();
        }
        for (i, m) in monitors.iter().enumerate() {
            let scale = m.scale_factor();
            let (pos, size) = (m.position().to_logical::<f64>(scale), m.size().to_logical::<f64>(scale));
            let label = format!("{PREFIX}{i}");
            st.renderers.insert(label.clone(), readiness::Renderer::new(generation));
            st.receipts.insert(label.clone(), receipt::Receipt::default());
            let document = Arc::new(receipt::Document::default());
            st.documents.insert(label.clone(), document.clone());
            let init = format!("if(window.__agentCursor){{window.__agentCursor.init({0},{1},{2},{3})}}else{{window.__agentCursorInit=[{0},{1},{2},{3}]}}",
                               pos.x, pos.y, size.width, size.height);
            // Runs for every document, before the asynchronous module loads;
            // native Finished also reapplies origin without replaying old hints.
            let document_init = format!("window.__agentCursorDocumentGeneration=1;window.__agentCursorInit=[{},{},{},{}]",
                                        pos.x, pos.y, size.width, size.height);
            let ready_label = label.clone();
            let built = WebviewWindowBuilder::new(app, label.clone(), page_url())
                .initialization_script(&document_init)
                .on_page_load(move |_window, payload| {
                    if payload.event() == PageLoadEvent::Started { document.start(); return; }
                    if payload.event() != PageLoadEvent::Finished { return; }
                    let mut pending = READY.lock().unwrap_or_else(|e| e.into_inner());
                    if pending.len() < 64 {
                        pending.push((ready_label.clone(), generation,
                            format!("window.__agentCursorDocumentGeneration={};{}", document.current(), init)));
                    }
                })
                .title("Supymem cursor")
                .position(pos.x, pos.y)
                .inner_size(size.width, size.height)
                .transparent(SPEC.transparent)
                .decorations(SPEC.decorations)
                .shadow(SPEC.shadow)
                .always_on_top(SPEC.always_on_top)
                .visible_on_all_workspaces(SPEC.visible_on_all_workspaces)
                .skip_taskbar(SPEC.skip_taskbar)
                .resizable(SPEC.resizable)
                .focused(SPEC.focused)
                .visible(SPEC.visible)
                .build();
            let Ok(window) = built else { st.renderers.remove(&label); continue };
            let _ = window.set_ignore_cursor_events(SPEC.ignore_cursor_events);
            harden(&window);
        }
        st.monitors = layout;
        true
    }

    fn set_shown(app: &AppHandle, st: &mut State, shown: bool) {
        if st.shown != shown {
            st.shown = shown;
            for w in windows(app) {
                order(&w, shown);
            }
            crate::agent_stop_pill::set_visible(shown);
        }
    }

    /// Emergency Stop: drop the visual lease and hide the overlay and its Stop pill at once.
    pub fn retire(app: &AppHandle) {
        let handle = app.clone();
        let _ = app.run_on_main_thread(move || {
            let mut guard = STATE.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(st) = guard.as_mut() {
                st.custody = FeedCustody::default();
                st.task_active = false;
                set_shown(&handle, st, false);
            }
            crate::agent_stop_pill::set_task_active(false);
        });
    }

    fn dispatch(app: &AppHandle, event: Value) {
        let kind = event["kind"].as_str().unwrap_or_default().to_string();
        let mut guard = STATE.lock().unwrap_or_else(|e| e.into_inner());
        let Some(st) = guard.as_mut() else { return };
        if !st.custody.admit(&kind, event.get("owner").and_then(Value::as_str)) {
            return;
        }
        st.sequence = st.sequence.saturating_add(1);
        st.admitted = Some(receipt::Delivery { generation: st.renderer_generation, document_generation:0, sequence: st.sequence,
            owner: event["owner"].as_str().map(str::to_owned), kind: kind.clone(), activity: activity(&event).into() });
        for receipt in st.receipts.values_mut() { receipt.rendered = None; }
        st.task_active = st.custody.owned_active() || match kind.as_str() {
            "begin" => true,
            "end" => false,
            _ => st.task_active,
        };
        if !st.prefs.enabled {
            return;
        }
        st.last_event = Instant::now();
        if sync_windows(app, st) {
            st.shown = false; // rebuilt windows start hidden
        }
        set_shown(app, st, true);
        if kind == "begin" { crate::agent_stop_pill::set_task_active(true); }
        if let (Some(x), Some(y)) = (event["x"].as_f64(), event["y"].as_f64()) {
            crate::agent_stop_pill::follow(x, y);
        }
        flush_ready(app, st);
        let mut event = event;
        let delivery = receipt::Delivery { generation: st.renderer_generation, document_generation:0, sequence: st.sequence,
            owner: event["owner"].as_str().map(str::to_owned), kind, activity: activity(&event).into() };
        st.admitted = Some(delivery.clone());
        for w in windows(app) {
            let mut delivery = delivery.clone();
            delivery.document_generation = st.documents.get(w.label()).map(|d| d.current()).unwrap_or(0);
            event["receipt"] = serde_json::to_value(&delivery).unwrap_or(Value::Null);
            let js = format!("if(window.__agentCursor){{window.__agentCursor.event({event})}}else{{const q=window.__agentCursorPending||(window.__agentCursorPending=[]);if(q.length===32)q.shift();q.push({event})}}");
            if let Some(receipt) = st.receipts.get_mut(w.label()) { receipt.deliver(delivery.clone()); }
            if let Some(renderer) = st.renderers.get_mut(w.label()) {
                if let Some(script) = renderer.send(js.clone()) { let _ = w.eval(script); }
            }
        }
    }

    fn idle_check(app: &AppHandle) {
        let mut guard = STATE.lock().unwrap_or_else(|e| e.into_inner());
        let Some(st) = guard.as_mut() else { return };
        flush_ready(app, st);
        if st.shown && should_hide(st.prefs, st.task_active, st.last_event.elapsed()) {
            st.task_active = false;
            set_shown(app, st, false);
        }
    }

    /// Settings toggle / idle timeout changed.
    pub fn apply_prefs(app: &AppHandle, prefs: Prefs) {
        let handle = app.clone();
        let _ = app.run_on_main_thread(move || {
            let mut guard = STATE.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(st) = guard.as_mut() {
                st.prefs = prefs;
                if !prefs.enabled {
                    set_shown(&handle, st, false);
                }
            }
        });
    }

    pub fn install(app: &AppHandle, data_dir: &Path) -> Result<(), String> {
        *STATE.lock().unwrap_or_else(|e| e.into_inner()) = Some(State {
            prefs: load_prefs(data_dir),
            task_active: false,
            custody: FeedCustody::default(),
            last_event: Instant::now(),
            shown: false,
            monitors: Vec::new(),
            renderers: HashMap::new(),
            renderer_generation: 0,
            sequence: 0,
            admitted: None,
            receipts: HashMap::new(),
            documents: HashMap::new(),
        });
        let path = socket_path();
        let _ = std::fs::remove_file(&path);
        let socket = UnixDatagram::bind(&path).map_err(|e| e.to_string())?;
        // Owner-only: other local users cannot paint on this founder's screen.
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).map_err(|e| e.to_string())?;
        socket.set_read_timeout(Some(Duration::from_millis(500))).map_err(|e| e.to_string())?;
        let app = app.clone();
        std::thread::spawn(move || {
            let mut buf = [0u8; 2048];
            loop {
                let event = match socket.recv(&mut buf) {
                    Ok(n) => parse_event(&buf[..n]),
                    Err(_) => None,
                };
                let handle = app.clone();
                let sent = app.run_on_main_thread(move || match event {
                    Some(event) => dispatch(&handle, event),
                    None => idle_check(&handle),
                });
                if sent.is_err() {
                    let _ = std::fs::remove_file(socket_path());
                    break;
                }
            }
        });
        Ok(())
    }

    fn trusted_renderer(window: &tauri::WebviewWindow) -> bool {
        let Ok(url) = window.url() else { return false };
        renderer_origin_allowed(window.label(), &url, tauri::is_dev())
    }

    pub fn rendered(window: &tauri::WebviewWindow, ack: Rendered) -> bool {
        if !trusted_renderer(window) { return false; }
        let mut guard = STATE.lock().unwrap_or_else(|e| e.into_inner());
        let Some(st) = guard.as_mut() else { return false };
        if !st.prefs.enabled || st.sequence != ack.sequence || st.renderer_generation != ack.generation { return false; }
        if st.documents.get(window.label()).map(|d| d.current()) != Some(ack.document_generation) { return false; }
        st.receipts.get_mut(window.label()).map(|r| r.acknowledge(ack)).unwrap_or(false)
    }

    /// Actual NSWindow visibility is sampled on its main thread, independently of page claims.
    pub async fn diagnostic_status(app: &AppHandle) -> Result<Value, String> {
        let (tx, rx) = tokio::sync::oneshot::channel();
        let handle = app.clone();
        app.run_on_main_thread(move || {
            let guard = STATE.lock().unwrap_or_else(|e| e.into_inner());
            let Some(st) = guard.as_ref() else { let _ = tx.send(json!({"installed": false})); return };
            let panels: Vec<Value> = windows(&handle).iter().map(|window| {
                let mut visible = false;
                let mut capture_excluded = false;
                let mut click_through = false;
                let mut key_window = false;
                with_ns(window, |ns| {
                    visible = ns.isVisible();
                    capture_excluded = ns.sharingType() == NSWindowSharingType::None;
                    click_through = ns.ignoresMouseEvents();
                    key_window = ns.isKeyWindow();
                });
                let receipt = st.receipts.get(window.label());
                let document_generation = st.documents.get(window.label()).map(|d| d.current());
                json!({"panel": window.label(), "native_visible": visible,
                    "document_generation": document_generation,
                    "capture_excluded":capture_excluded,"click_through":click_through,"key_window":key_window,
                    "delivery_requested": receipt.and_then(|r| r.delivered.as_ref()),
                    "renderer_committed": receipt.and_then(|r| r.rendered.as_ref()).filter(|r| Some(r.document_generation) == document_generation)})
            }).collect();
            let admitted = st.admitted.as_ref().map(|d| json!({"generation":d.generation,
                "sequence":d.sequence,"owner":d.owner,"kind":d.kind,"activity":d.activity}));
            let _ = tx.send(json!({"schema_version":1,"installed":true,"enabled":st.prefs.enabled,
                "task_active":st.task_active,"last_admitted":admitted,"panels":panels,
                "execution_verified":false,"completion_verified":false,"human_visibility_verified":false}));
        }).map_err(|_| "cursor_diagnostic_unavailable".to_string())?;
        tokio::time::timeout(Duration::from_secs(2), rx).await.map_err(|_| "cursor_diagnostic_unavailable".to_string())?
            .map_err(|_| "cursor_diagnostic_unavailable".to_string())
    }

    pub async fn export_diagnostic(app: &AppHandle) -> Result<(), String> {
        use sha2::{Digest, Sha256};
        let mut value = diagnostic_status(app).await?;
        value["process_id"] = json!(std::process::id());
        value["sampled_at_ms"] = json!(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0));
        value["cursor_asset_sha256"] = json!(format!("{:x}", Sha256::digest(include_bytes!("../splashscreen/agent-cursor/index.html"))));
        receipt::save_private(&value).map(|_| ())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test] fn cursor_receipt_origin_and_acl_are_window_specific_and_local_only() {
        let local=url::Url::parse("tauri://localhost/agent-cursor/index.html").unwrap();
        assert!(renderer_origin_allowed("agent-cursor-0", &local, false));
        assert!(!renderer_origin_allowed("main", &local, false));
        for value in ["https://app.supymem.com/agent-cursor/index.html", "tauri://localhost/index.html", "http://127.0.0.1:3117/agent-cursor/index.html", "http://tauri.localhost:9999/agent-cursor/index.html"] {
            assert!(!renderer_origin_allowed("agent-cursor-0", &url::Url::parse(value).unwrap(), false));
        }
        assert!(renderer_origin_allowed("agent-cursor-0", &url::Url::parse("http://127.0.0.1:3117/agent-cursor/index.html").unwrap(), true));
        let capability:Value=serde_json::from_str(include_str!("../capabilities/cursor-receipt.json")).unwrap();
        assert_eq!(capability["windows"],json!(["agent-cursor-*"]));
        assert_eq!(capability["permissions"],json!(["allow-cursor-receipt"]));
        assert!(capability.get("remote").is_none());
        let acl=include_str!("../permissions/cursor.toml");
        assert!(acl.contains("commands.allow = [\"agent_cursor_rendered\"]"));
        assert!(!include_str!("../permissions/app.toml").contains("\"agent_cursor_rendered\""));
    }

    #[test] fn native_diagnostic_copy_is_closed_even_for_arbitrary_labels() {
        assert_eq!(activity(&json!({"kind":"read","label":"Private goal and password"})),"Reading screen");
        assert_eq!(activity(&json!({"kind":"read","label":"Working on code"})),"Working on code");
        assert_eq!(activity(&json!({"kind":"end","label":"Working on code"})),"");
    }

    #[test]
    fn overlay_never_takes_input_or_focus() {
        assert!(SPEC.ignore_cursor_events, "must be click-through");
        assert!(!SPEC.focused && !SPEC.visible, "must not activate on creation");
        assert!(SPEC.always_on_top && SPEC.visible_on_all_workspaces);
        assert!(SPEC.transparent && !SPEC.decorations && !SPEC.shadow && !SPEC.resizable && SPEC.skip_taskbar);
        assert!(SPEC.excluded_from_capture);
    }

    #[test]
    fn parses_valid_event_and_normalizes_label() {
        let e = parse_event(br#"{"v":1,"kind":"click","x":10.5,"y":20,"label":"  Clicking\nSave "}"#).unwrap();
        assert_eq!(e["kind"], "click");
        assert_eq!(e["x"], 10.5);
        assert_eq!(e["label"], "Clicking Save");
    }

    #[test]
    fn owner_tokens_are_closed_and_preserved_for_receiver_admission() {
        let owner = "0123456789abcdef0123456789abcdef";
        let good = format!(r#"{{"v":1,"kind":"begin","owner":"{owner}"}}"#);
        assert_eq!(parse_event(good.as_bytes()).unwrap()["owner"], owner);
        for bad in ["", "short", "0123456789ABCDEF0123456789ABCDEF", "private body with spaces"] {
            let raw = format!(r#"{{"v":1,"kind":"end","owner":"{bad}"}}"#);
            assert!(parse_event(raw.as_bytes()).is_none());
        }
        assert!(parse_event(br#"{"v":1,"kind":"end","owner":null}"#).is_none());
        assert!(parse_event(br#"{"v":1,"kind":"end"}"#).is_some());
    }

    #[test]
    fn rejects_bad_events() {
        for bad in [
            &br#"{"v":2,"kind":"move","x":1,"y":1}"#[..],
            br#"{"v":1,"kind":"explode","x":1,"y":1}"#,
            br#"{"v":1,"kind":"click"}"#,
            br#"{"v":1,"kind":"move","x":1}"#,
            br#"{"v":1,"kind":"move","x":"1","y":2}"#,
            br#"{"v":1,"kind":"move","x":1e9,"y":2}"#,
            b"not json",
        ] {
            assert!(parse_event(bad).is_none(), "{}", String::from_utf8_lossy(bad));
        }
        assert!(parse_event(br#"{"v":1,"kind":"read"}"#).is_some());
        assert!(parse_event(br#"{"v":1,"kind":"end"}"#).is_some());
    }

    #[test]
    fn long_labels_are_truncated() {
        let long = format!(r#"{{"v":1,"kind":"read","label":"{}"}}"#, "x".repeat(200));
        assert_eq!(parse_event(long.as_bytes()).unwrap()["label"].as_str().unwrap().chars().count(), MAX_LABEL);
    }

    #[test]
    fn idle_hide_follows_prefs() {
        let p = Prefs::default();
        assert!(p.enabled, "default on");
        assert!(!should_hide(p, false, Duration::from_secs(3)));
        assert!(should_hide(p, false, Duration::from_secs(9)));
        assert!(!should_hide(p, true, Duration::from_secs(60)), "task running: stay for slow model turns");
        assert!(should_hide(p, true, Duration::from_secs(121)));
        assert!(should_hide(Prefs::new(false, 8), true, Duration::ZERO));
    }

    #[test]
    fn prefs_round_trip_and_clamp() {
        let dir = std::env::temp_dir().join(format!("agent-cursor-test-{}", std::process::id()));
        assert_eq!(load_prefs(&dir), Prefs::default());
        save_prefs(&dir, Prefs::new(false, 9999)).unwrap();
        assert_eq!(load_prefs(&dir), Prefs { enabled: false, idle_hide_secs: 300 });
        let _ = std::fs::remove_dir_all(&dir);
    }
}
