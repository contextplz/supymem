//! One-press emergency Stop for computer use.
//!
//! The cursor Stop pill, the menu bar item, the global shortcut, the in-app buttons
//! and the chat command all end in [`trigger`], which runs [`run`] once. Order matters:
//! the local halt (kill the CUA driver, retire the cursor) comes first and never waits
//! on the network; only then is the bridge told, which aborts the AI loop and notifies
//! Core in the background (retrying while Core is unreachable). Repeating it is safe.
//! An effect whose outcome is unknown is never replayed: the order ends cancelled and
//! Core keeps that effect held.

use serde_json::{json, Value};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager};

pub const SOURCES: [&str; 5] = ["app", "chat", "cursor", "menu", "shortcut"];
const RECEIPT_FILE: &str = "emergency_stop.json";
const RECEIPT_CAP: usize = 20;
/// Global shortcut: Control + Option + Command + period (not a system or common app binding).
pub const SHORTCUT_LABEL: &str = "⌃⌥⌘.";

/// What a stop touches. Faked in tests; real effects are [`Live`].
pub trait Effects {
    /// Kill the per-task cua-driver so an in-flight native step dies at once.
    fn kill_driver(&self);
    /// Hide the agent cursor and its Stop pill.
    fn retire_cursor(&self);
    /// Local bridge: abort the AI loop and queue the Core notification.
    fn bridge_stop(&self, source: &str) -> Result<Value, String>;
}

pub fn normalized_source(source: &str) -> &'static str {
    SOURCES.iter().copied().find(|known| *known == source).unwrap_or("app")
}

static GUARD: Mutex<()> = Mutex::new(());

/// The single shared stop. `at_ms` is injected so tests are deterministic.
pub fn run(effects: &dyn Effects, dir: &Path, source: &str, at_ms: u64) -> Value {
    let source = normalized_source(source);
    // Concurrent presses (shortcut + pill) queue instead of racing the same kill.
    let _one_at_a_time = GUARD.lock().unwrap_or_else(|e| e.into_inner());
    effects.kill_driver();
    effects.retire_cursor();
    let (bridge, order_id) = match effects.bridge_stop(source) {
        Ok(reply) if reply["stopped"] == true => ("stopped", reply["order_id"].as_str().map(str::to_owned)),
        Ok(_) => ("idle", None),
        // The local halt above already happened; the loop dies with its driver and cursor.
        Err(_) => ("bridge_unreachable", None),
    };
    let receipt = json!({
        "status": "stopped_by_user", "source": source, "at_ms": at_ms,
        "bridge": bridge, "order_id": order_id,
        "core_sync": if bridge == "stopped" { "queued" } else { "not_applicable" },
    });
    save_receipt(dir, &receipt);
    receipt
}

fn save_receipt(dir: &Path, receipt: &Value) {
    let path = dir.join(RECEIPT_FILE);
    let mut all: Vec<Value> = std::fs::read_to_string(&path).ok()
        .and_then(|raw| serde_json::from_str(&raw).ok()).unwrap_or_default();
    all.push(receipt.clone());
    let skip = all.len().saturating_sub(RECEIPT_CAP);
    let _ = std::fs::create_dir_all(dir);
    let _ = std::fs::write(path, Value::Array(all.split_off(skip)).to_string());
}

/// Poll result -> UI state, with change detection so events only fire on a flip.
#[derive(Default)]
pub struct Tracker {
    active: AtomicBool,
}

impl Tracker {
    /// True when the state changed.
    pub fn set(&self, active: bool) -> bool {
        self.active.swap(active, Ordering::AcqRel) != active
    }
    pub fn active(&self) -> bool {
        self.active.load(Ordering::Acquire)
    }
}

static TRACKER: Tracker = Tracker { active: AtomicBool::new(false) };

pub fn is_active() -> bool {
    TRACKER.active()
}

fn now_ms() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0)
}

struct Live<'a>(&'a AppHandle);

impl Effects for Live<'_> {
    fn kill_driver(&self) {
        #[cfg(target_os = "macos")]
        if let Some(state) = self.0.try_state::<crate::AppState>() {
            // Dropping the host kills cua-driver and removes its socket directory.
            let host = state.cua_host.lock().unwrap_or_else(|e| e.into_inner()).take();
            drop(host);
        }
    }
    fn retire_cursor(&self) {
        #[cfg(target_os = "macos")]
        crate::agent_cursor::retire(self.0);
    }
    fn bridge_stop(&self, source: &str) -> Result<Value, String> {
        let state = self.0.try_state::<crate::AppState>().ok_or("app_state_unavailable")?;
        bridge_call(&state.supervisor, true, source)
    }
}

fn bridge_call(sup: &crate::supervisor::Supervisor, post: bool, source: &str) -> Result<Value, String> {
    let client = reqwest::blocking::Client::builder().user_agent(crate::http::USER_AGENT)
        .timeout(Duration::from_secs(3)).redirect(reqwest::redirect::Policy::none()).no_proxy()
        .build().map_err(|_| "bridge_transport_unavailable".to_string())?;
    let local = crate::local_stack::is_local();
    let owner = if local {
        Some(crate::nav_guard::owner_secret(&sup.data_dir().join("owner-session.secret"))
            .map_err(|_| "Quit and reopen your app, then open Builds and press Stop again.".to_string())?)
    } else { None };
    let (method, path) = if post { (reqwest::Method::POST, "/native/emergency-stop") }
        else { (reqwest::Method::GET, "/native/emergency-stop/status") };
    let request = crate::native_bridge_request(&client, sup, method, path);
    let request = if post { request.json(&json!({ "source": source })) } else { request };
    let reply = request.send()
        .map_err(|_| "bridge_unreachable".to_string())?;
    if !reply.status().is_success() { return Err("bridge_rejected".into()); }
    let result = reply.json::<Value>().map_err(|_| "bridge_reply_invalid".to_string())?;
    if local && crate::nav_guard::owner_secret(&sup.data_dir().join("owner-session.secret")).ok() != owner {
        return Err("Your local owner session changed. Open Builds and press Stop again.".into());
    }
    Ok(result)
}

/// Every entry point calls this. Runs off the caller's thread so a menu click or hotkey
/// handler never blocks; returns immediately.
pub fn trigger(app: &AppHandle, source: &str) {
    let (app, source) = (app.clone(), source.to_string());
    std::thread::spawn(move || {
        let dir = crate::supervisor::conventional_data_dir();
        let receipt = run(&Live(&app), &dir, &source, now_ms());
        let _ = app.emit("computer-use-stopped", receipt);
        refresh(&app);
    });
}

/// Re-read the bridge and publish active/idle to the webview, tray and pill.
fn refresh(app: &AppHandle) {
    let Some(state) = app.try_state::<crate::AppState>() else { return };
    let status = bridge_call(&state.supervisor, false, "app").ok();
    let active = status.as_ref().is_some_and(|s| s["active"] == true && s["stopped_by_user"] != true);
    if TRACKER.set(active) {
        let _ = app.emit("computer-use-active", json!({ "active": active,
            "order_id": status.as_ref().and_then(|s| s["order_id"].as_str()),
            "venture_id": status.as_ref().and_then(|s| s["venture_id"].as_str()) }));
        #[cfg(target_os = "macos")]
        mac::set_active(app, active);
    }
}

/// Keep the UI honest: poll the local bridge (no Core, no account) for a running task.
pub fn install(app: &AppHandle) {
    #[cfg(target_os = "macos")]
    mac::install(app);
    let app = app.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_millis(1500));
        refresh(&app);
    });
}

#[tauri::command]
pub async fn emergency_stop_computer_use(app: AppHandle, source: Option<String>) -> Result<Value, String> {
    let source = normalized_source(source.as_deref().unwrap_or("app"));
    let dir = crate::supervisor::conventional_data_dir();
    let handle = app.clone();
    let receipt = tauri::async_runtime::spawn_blocking(move || run(&Live(&handle), &dir, source, now_ms()))
        .await.map_err(|_| "emergency_stop_failed".to_string())?;
    let _ = app.emit("computer-use-stopped", receipt.clone());
    let again = app.clone();
    std::thread::spawn(move || refresh(&again));
    Ok(receipt)
}

#[tauri::command]
pub fn computer_use_status() -> Value {
    json!({ "active": is_active(), "shortcut": SHORTCUT_LABEL })
}

#[cfg(target_os = "macos")]
mod mac {
    use super::*;
    use std::ffi::c_void;
    use std::sync::OnceLock;
    use tauri::menu::{Menu, MenuItem};
    use tauri::tray::{TrayIcon, TrayIconBuilder};

    static APP: OnceLock<AppHandle> = OnceLock::new();
    static TRAY: OnceLock<(TrayIcon, MenuItem<tauri::Wry>)> = OnceLock::new();

    const STOP_ID: &str = "emergency_stop";
    pub const RUNNING_TITLE: &str = "● Running";

    pub fn install(app: &AppHandle) {
        let _ = APP.set(app.clone());
        if let Err(error) = install_tray(app) {
            log::error!("computer-use stop menu bar item failed: {error}");
        }
        if let Err(code) = register_shortcut() {
            log::warn!("computer-use stop shortcut unavailable ({code})");
        }
    }

    fn install_tray(app: &AppHandle) -> tauri::Result<()> {
        let stop = MenuItem::with_id(app, STOP_ID, format!("Stop computer use    {SHORTCUT_LABEL}"), false, None::<&str>)?;
        let menu = Menu::with_items(app, &[&stop])?;
        let icon = tauri::image::Image::new(include_bytes!("../icons/tray-template@2x.rgba"), 44, 44);
        let tray = TrayIconBuilder::with_id("supymem-stop")
            .icon(icon).icon_as_template(true).menu(&menu)
            .tooltip("Supymem: stop computer use")
            .on_menu_event(|app, event| {
                if event.id.as_ref() == STOP_ID {
                    crate::shell_events::record("activate", "emergency_stop_menu", None);
                    trigger(app, "menu");
                }
            })
            .build(app)?;
        let _ = TRAY.set((tray, stop));
        Ok(())
    }

    /// Menu item enabled only while a task runs; the status item shows a running mark.
    pub fn set_active(app: &AppHandle, active: bool) {
        let _ = app.run_on_main_thread(move || {
            if let Some((tray, item)) = TRAY.get() {
                let _ = item.set_enabled(active);
                let _ = tray.set_title(if active { Some(RUNNING_TITLE) } else { None });
            }
            crate::agent_stop_pill::set_task_active(active);
        });
    }

    // Carbon RegisterEventHotKey: needs no Accessibility or Input Monitoring permission.
    #[repr(C)]
    struct EventTypeSpec { class: u32, kind: u32 }
    #[repr(C)]
    #[derive(Clone, Copy)]
    struct EventHotKeyId { signature: u32, id: u32 }
    type Handler = extern "C" fn(*mut c_void, *mut c_void, *mut c_void) -> i32;
    #[link(name = "Carbon", kind = "framework")]
    extern "C" {
        fn GetApplicationEventTarget() -> *mut c_void;
        fn InstallEventHandler(target: *mut c_void, handler: Handler, count: u32,
            types: *const EventTypeSpec, data: *mut c_void, out: *mut *mut c_void) -> i32;
        fn RegisterEventHotKey(code: u32, modifiers: u32, id: EventHotKeyId, target: *mut c_void,
            options: u32, out: *mut *mut c_void) -> i32;
    }
    const KEYBOARD: u32 = 0x6B65_7962; // 'keyb'
    const HOT_KEY_PRESSED: u32 = 5;
    const SIGNATURE: u32 = 0x5359_4D53; // 'SYMS'
    const KEY_PERIOD: u32 = 0x2F;
    const CONTROL_OPTION_COMMAND: u32 = 0x1000 | 0x0800 | 0x0100;

    extern "C" fn pressed(_call: *mut c_void, _event: *mut c_void, _data: *mut c_void) -> i32 {
        if let Some(app) = APP.get() {
            crate::shell_events::record("activate", "emergency_stop_shortcut", None);
            trigger(app, "shortcut");
        }
        0
    }

    fn register_shortcut() -> Result<(), i32> {
        unsafe {
            let target = GetApplicationEventTarget();
            let spec = EventTypeSpec { class: KEYBOARD, kind: HOT_KEY_PRESSED };
            let mut handler: *mut c_void = std::ptr::null_mut();
            let status = InstallEventHandler(target, pressed, 1, &spec, std::ptr::null_mut(), &mut handler);
            if status != 0 { return Err(status); }
            let mut key: *mut c_void = std::ptr::null_mut();
            let status = RegisterEventHotKey(KEY_PERIOD, CONTROL_OPTION_COMMAND,
                EventHotKeyId { signature: SIGNATURE, id: 1 }, target, 0, &mut key);
            if status != 0 { return Err(status); }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::sync::atomic::AtomicUsize;

    struct Fake {
        order: Mutex<Vec<&'static str>>,
        bridge: Result<Value, String>,
        kills: AtomicUsize,
    }

    impl Fake {
        fn new(bridge: Result<Value, String>) -> Self {
            Fake { order: Mutex::new(Vec::new()), bridge, kills: AtomicUsize::new(0) }
        }
    }

    impl Effects for Fake {
        fn kill_driver(&self) { self.kills.fetch_add(1, Ordering::SeqCst); self.order.lock().unwrap().push("kill"); }
        fn retire_cursor(&self) { self.order.lock().unwrap().push("cursor"); }
        fn bridge_stop(&self, _source: &str) -> Result<Value, String> {
            self.order.lock().unwrap().push("bridge");
            self.bridge.clone()
        }
    }

    fn dir(name: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!("emergency-stop-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&path);
        path
    }

    #[test]
    fn local_halt_precedes_the_bridge_and_a_receipt_is_saved() {
        let (d, fake) = (dir("order"), Fake::new(Ok(json!({"stopped": true, "order_id": "dwo_1"}))));
        let receipt = run(&fake, &d, "shortcut", 7);
        assert_eq!(*fake.order.lock().unwrap(), ["kill", "cursor", "bridge"]);
        assert_eq!(receipt["status"], "stopped_by_user");
        assert_eq!((receipt["source"].as_str(), receipt["bridge"].as_str(), receipt["order_id"].as_str()),
                   (Some("shortcut"), Some("stopped"), Some("dwo_1")));
        let saved: Vec<Value> = serde_json::from_str(&std::fs::read_to_string(d.join(RECEIPT_FILE)).unwrap()).unwrap();
        assert_eq!(saved, vec![receipt]);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn stop_still_happens_locally_when_the_bridge_or_core_is_unreachable() {
        let (d, fake) = (dir("down"), Fake::new(Err("bridge_unreachable".into())));
        let receipt = run(&fake, &d, "menu", 1);
        assert_eq!(fake.kills.load(Ordering::SeqCst), 1);
        assert_eq!(receipt["bridge"], "bridge_unreachable");
        assert_eq!(receipt["status"], "stopped_by_user");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn nothing_running_is_a_harmless_idle_stop() {
        let (d, fake) = (dir("idle"), Fake::new(Ok(json!({"stopped": false, "active": false}))));
        assert_eq!(run(&fake, &d, "app", 1)["bridge"], "idle");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn repeating_the_stop_is_idempotent_and_keeps_every_press_in_the_log() {
        let (d, fake) = (dir("repeat"), Fake::new(Ok(json!({"stopped": true, "order_id": "dwo_1"}))));
        for at in 0..3 { run(&fake, &d, "cursor", at); }
        assert_eq!(fake.kills.load(Ordering::SeqCst), 3);
        let saved: Vec<Value> = serde_json::from_str(&std::fs::read_to_string(d.join(RECEIPT_FILE)).unwrap()).unwrap();
        assert_eq!(saved.len(), 3);
        assert!(saved.iter().all(|r| r["status"] == "stopped_by_user" && r["order_id"] == "dwo_1"));
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn receipt_log_is_capped_and_unknown_sources_are_normalized() {
        let (d, fake) = (dir("cap"), Fake::new(Ok(json!({"stopped": false}))));
        for at in 0..(RECEIPT_CAP as u64 + 5) { run(&fake, &d, "<script>", at); }
        let saved: Vec<Value> = serde_json::from_str(&std::fs::read_to_string(d.join(RECEIPT_FILE)).unwrap()).unwrap();
        assert_eq!(saved.len(), RECEIPT_CAP);
        assert_eq!(saved[0]["at_ms"], 5);
        assert!(saved.iter().all(|r| r["source"] == "app"));
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn concurrent_presses_serialize_without_losing_the_local_halt() {
        let (d, fake) = (dir("race"), std::sync::Arc::new(Fake::new(Ok(json!({"stopped": true, "order_id": "o"})))));
        let handles: Vec<_> = (0..4).map(|i| {
            let (d, fake) = (d.clone(), fake.clone());
            std::thread::spawn(move || { run(&*fake, &d, "app", i); })
        }).collect();
        for h in handles { h.join().unwrap(); }
        assert_eq!(fake.kills.load(Ordering::SeqCst), 4);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn tracker_reports_only_changes() {
        let t = Tracker::default();
        assert!(!t.set(false));
        assert!(t.set(true) && !t.set(true) && t.active());
        assert!(t.set(false) && !t.active());
    }

    #[test]
    fn every_entry_point_routes_through_the_one_trigger() {
        let lib = include_str!("lib.rs");
        assert!(lib.contains("emergency_stop::emergency_stop_computer_use"));
        let mac = include_str!("emergency_stop.rs");
        for source in ["\"menu\"", "\"shortcut\""] { assert!(mac.contains(&format!("trigger(app, {source})"))); }
        assert!(include_str!("agent_stop_pill.rs").contains("emergency_stop::trigger(app, \"cursor\")"));
        assert_eq!(SHORTCUT_LABEL, "⌃⌥⌘.");
    }
}
