//! A small clickable "Stop" pill that follows the agent cursor.
//!
//! The cursor overlay itself stays click-through and non-activating. This is a *separate*
//! tiny NSPanel (non-activating, never key, no IPC) so pressing it neither steals focus from
//! the founder's app nor reaches the window underneath. Presses are detected natively, like
//! the menu-bar mascot, and go to the same `emergency_stop::trigger` as every other entry.
//!
//! "For now": the whole pill is behind [`ENABLED`] (and `SUPYMEM_STOP_PILL=0`), so removing it
//! later is deleting this file plus the three `agent_stop_pill::` calls.

use std::sync::atomic::{AtomicBool, AtomicPtr, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use objc2::rc::Retained;
use objc2::MainThreadMarker;
use objc2_app_kit::{
    NSBackingStoreType, NSColor, NSEvent, NSPanel, NSScreen, NSWindow, NSWindowAnimationBehavior,
    NSWindowCollectionBehavior, NSWindowSharingType, NSWindowStyleMask,
};
use objc2_foundation::{NSPoint, NSRect, NSSize};
use tauri::{AppHandle, WebviewUrl, WebviewWindowBuilder};

/// Flag for the "for now" pill. Flip to false (or set SUPYMEM_STOP_PILL=0) to remove it.
pub const ENABLED: bool = true;
const LABEL: &str = "agent-stop";
pub const WIDTH: f64 = 76.0;
pub const HEIGHT: f64 = 28.0;
/// Offset from the pointer tip (top-left space): up and to the right, clear of the activity label below.
const DX: f64 = 22.0;
const DY: f64 = -34.0;

pub fn enabled() -> bool {
    ENABLED && std::env::var("SUPYMEM_STOP_PILL").map_or(true, |v| v != "0")
}

/// Cocoa (bottom-left) frame origin for a pointer at `(x, y)` in screen-global top-left
/// points, kept fully inside `bounds` (Cocoa `x, y, w, h`).
pub fn origin_for(x: f64, y: f64, primary_height: f64, bounds: (f64, f64, f64, f64)) -> (f64, f64) {
    let want_x = x + DX;
    let want_y = primary_height - (y + DY) - HEIGHT;
    let (bx, by, bw, bh) = bounds;
    (want_x.clamp(bx, (bx + bw - WIDTH).max(bx)), want_y.clamp(by, (by + bh - HEIGHT).max(by)))
}

pub fn contains(origin: (f64, f64), p: (f64, f64)) -> bool {
    p.0 >= origin.0 && p.0 <= origin.0 + WIDTH && p.1 >= origin.1 && p.1 <= origin.1 + HEIGHT
}

/// A press is the up-to-down edge inside the pill.
pub fn is_press(was_down: bool, down: bool, inside: bool) -> bool {
    down && !was_down && inside
}

/// Shown only while the cursor overlay is up AND the bridge reports a running task.
struct State {
    overlay: bool,
    task_active: bool,
    shown: bool,
    was_down: bool,
}

static STATE: Mutex<State> = Mutex::new(State { overlay: false, task_active: false, shown: false, was_down: false });
static PANEL: AtomicPtr<NSPanel> = AtomicPtr::new(std::ptr::null_mut());
static FAST: AtomicBool = AtomicBool::new(false);

fn page_url() -> WebviewUrl {
    if tauri::is_dev() {
        WebviewUrl::External("http://127.0.0.1:3117/agent-stop/index.html".parse().expect("stop pill url"))
    } else {
        WebviewUrl::App("agent-stop/index.html".into())
    }
}

fn panel() -> Option<&'static NSWindow> {
    let raw = PANEL.load(Ordering::Acquire);
    (!raw.is_null()).then(|| unsafe { &*(raw as *const NSWindow) })
}

/// Build the hidden host window, then move its web view into a non-activating panel.
pub fn install(app: &AppHandle) -> Result<(), String> {
    if !enabled() { return Ok(()); }
    let mtm = MainThreadMarker::new().ok_or("stop pill must be installed on the main thread")?;
    WebviewWindowBuilder::new(app, LABEL, page_url())
        .title("Supymem stop").inner_size(WIDTH, HEIGHT)
        .decorations(false).transparent(true).shadow(false).always_on_top(true)
        .visible_on_all_workspaces(true).skip_taskbar(true).resizable(false)
        .focused(false).visible(false)
        .build().map_err(|e| e.to_string())?;
    let host = tauri::Manager::get_webview_window(app, LABEL).ok_or("stop pill window missing")?;
    let host = unsafe { &*(host.ns_window().map_err(|e| e.to_string())? as *const NSWindow) };
    let panel = NSPanel::initWithContentRect_styleMask_backing_defer(
        mtm.alloc::<NSPanel>(), host.frame(),
        NSWindowStyleMask::Borderless | NSWindowStyleMask::NonactivatingPanel,
        NSBackingStoreType::Buffered, false);
    unsafe { panel.setReleasedWhenClosed(false) };
    panel.setFloatingPanel(true);
    // Never key: pressing it must not pull keyboard focus away from the founder's app.
    panel.setBecomesKeyOnlyIfNeeded(true);
    panel.setWorksWhenModal(true);
    panel.setLevel(crate::mascot::overlay_level());
    panel.setCanHide(false);
    panel.setHidesOnDeactivate(false);
    panel.setMovable(false);
    panel.setAnimationBehavior(NSWindowAnimationBehavior::None);
    // Keep the pill out of the agent's own screenshots, like the cursor.
    panel.setSharingType(NSWindowSharingType::None);
    panel.setOpaque(false);
    panel.setBackgroundColor(Some(&NSColor::clearColor()));
    panel.setHasShadow(false);
    panel.setIgnoresMouseEvents(false);
    use NSWindowCollectionBehavior as B;
    panel.setCollectionBehavior(B::CanJoinAllSpaces | B::CanJoinAllApplications | B::FullScreenAuxiliary
        | B::IgnoresCycle | B::FullScreenDisallowsTiling | B::Stationary);
    crate::mascot::move_content_to_panel(host, &panel, mtm).ok_or("stop pill content view missing")?;
    host.orderOut(None);
    PANEL.store(Retained::into_raw(panel), Ordering::Release);

    let app = app.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_millis(if FAST.load(Ordering::Relaxed) { 16 } else { 80 }));
        let handle = app.clone();
        if app.run_on_main_thread(move || tick(&handle)).is_err() { break; }
    });
    Ok(())
}

fn screen_geometry(mtm: MainThreadMarker, x: f64, y: f64) -> (f64, (f64, f64, f64, f64)) {
    let screens = NSScreen::screens(mtm);
    let primary = screens.firstObject().map(|s| s.frame().size.height).unwrap_or(0.0);
    let cocoa_y = primary - y;
    let frame = screens.iter().map(|s| s.frame())
        .find(|f| x >= f.origin.x && x <= f.origin.x + f.size.width && cocoa_y >= f.origin.y && cocoa_y <= f.origin.y + f.size.height)
        .or_else(|| screens.firstObject().map(|s| s.frame()))
        .unwrap_or(NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(1440.0, 900.0)));
    (primary, (frame.origin.x, frame.origin.y, frame.size.width, frame.size.height))
}

/// Cursor event with a screen point: glide the pill next to it. Main thread.
pub fn follow(x: f64, y: f64) {
    let (Some(ns), Some(mtm)) = (panel(), MainThreadMarker::new()) else { return };
    let (primary, bounds) = screen_geometry(mtm, x, y);
    let (ox, oy) = origin_for(x, y, primary, bounds);
    let frame = NSRect::new(NSPoint::new(ox, oy), NSSize::new(WIDTH, HEIGHT));
    ns.setFrame_display_animate(frame, true, ns.isVisible());
}

fn apply(st: &mut State) {
    let Some(ns) = panel() else { return };
    let show = st.overlay && st.task_active;
    if show == st.shown { return; }
    st.shown = show;
    FAST.store(show, Ordering::Relaxed);
    if show { ns.orderFrontRegardless() } else { ns.orderOut(None) }
}

/// Cursor overlay shown/hidden. Main thread.
pub fn set_visible(visible: bool) {
    let mut st = STATE.lock().unwrap_or_else(|e| e.into_inner());
    st.overlay = visible;
    apply(&mut st);
}

/// Bridge says a task is (not) running. A finished task retires the pill at once. Main thread.
pub fn set_task_active(active: bool) {
    let mut st = STATE.lock().unwrap_or_else(|e| e.into_inner());
    st.task_active = active;
    apply(&mut st);
}

fn tick(app: &AppHandle) {
    let Some(ns) = panel() else { return };
    let mut st = STATE.lock().unwrap_or_else(|e| e.into_inner());
    let down = NSEvent::pressedMouseButtons() & 1 != 0;
    let frame = ns.frame();
    let mouse = NSEvent::mouseLocation();
    let inside = st.shown && contains((frame.origin.x, frame.origin.y), (mouse.x, mouse.y));
    let press = is_press(st.was_down, down, inside);
    st.was_down = down;
    drop(st);
    if press {
        crate::shell_events::record("activate", "emergency_stop_cursor", None);
        crate::emergency_stop::trigger(app, "cursor");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SCREEN: (f64, f64, f64, f64) = (0.0, 0.0, 1440.0, 900.0);

    #[test]
    fn pill_sits_up_and_right_of_the_pointer_in_cocoa_coordinates() {
        // Pointer at top-left (500, 400) on a 900pt tall display.
        let (x, y) = origin_for(500.0, 400.0, 900.0, SCREEN);
        assert_eq!(x, 522.0);
        assert_eq!(y, 900.0 - (400.0 - 34.0) - HEIGHT);
    }

    #[test]
    fn pill_never_leaves_the_display() {
        let (x, y) = origin_for(1435.0, 2.0, 900.0, SCREEN);
        assert_eq!(x, 1440.0 - WIDTH);
        assert_eq!(y, 900.0 - HEIGHT);
        let (x, y) = origin_for(-50.0, 5000.0, 900.0, SCREEN);
        assert_eq!((x, y), (0.0, 0.0));
    }

    #[test]
    fn pill_follows_onto_a_secondary_display() {
        let second = (1440.0, -100.0, 1920.0, 1080.0);
        let (x, _) = origin_for(1500.0, 300.0, 900.0, second);
        assert_eq!(x, 1522.0);
    }

    #[test]
    fn only_a_fresh_press_inside_the_pill_counts() {
        assert!(is_press(false, true, true));
        assert!(!is_press(true, true, true), "held button is not a new press");
        assert!(!is_press(false, true, false), "a click elsewhere is not a stop");
        assert!(!is_press(false, false, true));
        assert!(contains((10.0, 10.0), (10.0 + WIDTH, 10.0 + HEIGHT)));
        assert!(!contains((10.0, 10.0), (9.0, 15.0)));
    }

    #[test]
    fn pill_window_has_no_ipc_capability_and_is_flag_gated() {
        for capability in [include_str!("../capabilities/default.json"), include_str!("../capabilities/cursor-receipt.json"),
                           include_str!("../capabilities/updater.json")] {
            assert!(!capability.contains("agent-stop"), "the pill page must stay IPC-free");
        }
        assert!(ENABLED, "the pill ships on for now; flip ENABLED to remove it");
        let page = include_str!("../splashscreen/agent-stop/index.html");
        assert!(!page.contains("invoke") && !page.contains("__TAURI"));
    }
}
