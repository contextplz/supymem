//! Menu-bar mascot: a transparent, always-on-top panel hanging from the top of
//! the screen. Drag it along the bar (edge dead-ends, hop over the notch); a
//! click without movement shows the main window. The page has no IPC access:
//! the pointer is polled here and the page is driven through `eval`.

use std::sync::atomic::{AtomicBool, AtomicPtr, Ordering};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;

use objc2::rc::Retained;
use objc2::MainThreadMarker;
use objc2_app_kit::{
    NSApplication, NSApplicationPresentationOptions, NSBackingStoreType, NSColor, NSEvent,
    NSPanel, NSScreen, NSView, NSWindow, NSWindowAnimationBehavior, NSWindowCollectionBehavior,
    NSWindowLevel, NSWindowSharingType, NSWindowStyleMask,
};
use objc2_foundation::{NSPoint, NSRect, NSSize};
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder};

/// Transparent stage, larger than the 48x62 drawing so lean and swing never clip.
const TILE_W: f64 = 96.0;
const TILE_H: f64 = 88.0;
/// Lift hides the top of the graphite rod above the screen.
const LIFT: f64 = 6.0;
/// Pointer movement (points) below which press+release is a click, not a drag.
const CLICK_SLOP: f64 = 4.0;
/// Clickable area: the character itself, centered at the top of the tile.
const HIT_HALF_W: f64 = 35.0;
const HIT_H: f64 = 66.0;

fn mascot_page_url() -> WebviewUrl {
    if crate::local_stack::is_local() {
        return WebviewUrl::External("tauri://localhost/swing/index.html".parse().expect("mascot url"));
    }
    // `tauri dev` resolves App URLs against the console devUrl (Next), which
    // does not serve /swing. `make desktop-dev` serves splashscreen/ on :3117.
    if cfg!(debug_assertions) {
        WebviewUrl::External("http://127.0.0.1:3117/swing/index.html".parse().expect("mascot url"))
    } else {
        WebviewUrl::App("swing/index.html".into())
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Band {
    pub x0: f64,
    pub x1: f64,
}

fn band_max(band: &Band, tile: f64) -> f64 {
    (band.x1 - tile).max(band.x0)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NotchSide {
    Left,
    Right,
}

/// Keep a tile inside the menu-bar bands. A point in the notch gap snaps to
/// the nearer side when there is no travel direction.
pub fn clamp_to_bands(x: f64, bands: &[Band], tile: f64) -> f64 {
    clamp_across_notch(x, bands, tile, None, None).0
}

/// Follow the cursor in a band. Outer screen ends are hard dead-ends.
/// Reaching a notch lip hops to the other side so the tile never sits in
/// the notch hole, and both left and right menu-bar bands stay reachable.
pub fn clamp_across_notch(
    x: f64,
    bands: &[Band],
    tile: f64,
    from: Option<f64>,
    latched: Option<NotchSide>,
) -> (f64, Option<NotchSide>) {
    if bands.is_empty() {
        return (x, None);
    }
    if bands.len() < 2 {
        let band = &bands[0];
        return (x.clamp(band.x0, band_max(band, tile)), None);
    }
    let left = bands.iter().min_by(|a, b| a.x0.total_cmp(&b.x0)).unwrap();
    let right = bands.iter().max_by(|a, b| a.x0.total_cmp(&b.x0)).unwrap();
    let left_max = band_max(left, tile);
    let right_min = right.x0;
    let right_max = band_max(right, tile);
    if right_min <= left_max {
        return (x.clamp(left.x0, right_max), None);
    }

    let heading = from.map(|prev| x - prev).unwrap_or(0.0);
    // Strict interiors: sitting exactly on a lip is treated as "crossing" so
    // we hop instead of parking against the notch hole.
    let in_left = x >= left.x0 && x < left_max;
    let in_right = x > right_min && x <= right_max;
    // After a hop the cursor is still on the old lip. A 1pt jitter must not
    // yank the tile back; wait until the cursor is clearly in that band.
    const UNLATCH: f64 = 16.0;
    let commit_left = in_left && (latched != Some(NotchSide::Right) || x < left_max - UNLATCH);
    let commit_right = in_right && (latched != Some(NotchSide::Left) || x > right_min + UNLATCH);

    if commit_left {
        // Dead-end at the left screen edge.
        return (x.clamp(left.x0, left_max), Some(NotchSide::Left));
    }
    if commit_right {
        // Dead-end at the right screen edge.
        return (x.clamp(right_min, right_max), Some(NotchSide::Right));
    }
    // Past the outer ends (cursor overshot the band): hard stop, no hop.
    if x < left.x0 {
        return (left.x0, Some(NotchSide::Left));
    }
    if x > right_max {
        return (right_max, Some(NotchSide::Right));
    }

    // At a lip or in the gap: hop to the other side. Never rest in the notch.
    let from_left = from
        .map(|prev| prev <= left_max)
        .unwrap_or(latched == Some(NotchSide::Left));
    let from_right = from
        .map(|prev| prev >= right_min)
        .unwrap_or(latched == Some(NotchSide::Right));
    let side = if from_left && heading >= 0.0 {
        NotchSide::Right
    } else if from_right && heading <= 0.0 {
        NotchSide::Left
    } else if heading > 1.0 {
        NotchSide::Right
    } else if heading < -1.0 {
        NotchSide::Left
    } else {
        latched.unwrap_or_else(|| {
            if (x - left_max).abs() <= (right_min - x).abs() {
                NotchSide::Left
            } else {
                NotchSide::Right
            }
        })
    };
    let pin = match side {
        NotchSide::Left => left_max,
        NotchSide::Right => right_min,
    };
    (pin, Some(side))
}

/// Two bands: hang just left of the notch. One band: center on the bar.
pub fn default_x(bands: &[Band], tile: f64) -> f64 {
    if bands.len() >= 2 {
        let left = bands.iter().min_by(|a, b| a.x0.total_cmp(&b.x0)).unwrap();
        return (left.x1 - tile - 6.0).max(left.x0);
    }
    match bands.first() {
        Some(band) => band.x0 + ((band.x1 - band.x0 - tile).max(0.0) / 2.0),
        None => 80.0,
    }
}

struct Geo {
    left: f64,
    top: f64,
    bands: Vec<Band>,
}

/// Menu-bar bands of the primary screen (two around a notch, else one).
fn geometry() -> Option<Geo> {
    let mtm = MainThreadMarker::new()?;
    let screen = NSScreen::screens(mtm).firstObject()?;
    let frame = screen.frame();
    let mut bands = Vec::new();
    for rect in [screen.auxiliaryTopLeftArea(), screen.auxiliaryTopRightArea()] {
        if rect.size.width > 40.0 && rect.size.height > 8.0 {
            bands.push(Band {
                x0: rect.origin.x - frame.origin.x,
                x1: rect.origin.x + rect.size.width - frame.origin.x,
            });
        }
    }
    bands.sort_by(|a, b| a.x0.total_cmp(&b.x0));
    if bands.len() >= 2 {
        // Keep clear of the rounded notch corners.
        bands[0].x1 = (bands[0].x1 - 4.0).max(bands[0].x0);
        bands[1].x0 += 4.0;
    } else {
        bands = vec![Band { x0: 8.0, x1: (frame.size.width - 8.0).max(32.0) }];
    }
    Some(Geo { left: frame.origin.x, top: frame.origin.y + frame.size.height, bands })
}

fn fullscreen_presentation() -> bool {
    let Some(mtm) = MainThreadMarker::new() else {
        return false;
    };
    let opts = NSApplication::sharedApplication(mtm).currentSystemPresentationOptions();
    if opts.contains(NSApplicationPresentationOptions::FullScreen)
        || opts.contains(NSApplicationPresentationOptions::AutoHideMenuBar)
        || opts.contains(NSApplicationPresentationOptions::HideMenuBar)
    {
        return true;
    }
    // Another app's native fullscreen hides the menu bar: visibleFrame then
    // reaches the top of the display.
    NSScreen::screens(mtm).firstObject().is_some_and(|screen| {
        let (f, v) = (screen.frame(), screen.visibleFrame());
        v.origin.y + v.size.height >= f.origin.y + f.size.height - 1.0
    })
}

pub(crate) fn overlay_level() -> NSWindowLevel {
    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGShieldingWindowLevel() -> i32;
    }
    unsafe { CGShieldingWindowLevel() as NSWindowLevel + 1 }
}

static OVERLAY: AtomicPtr<NSPanel> = AtomicPtr::new(std::ptr::null_mut());
static HOST: AtomicPtr<NSWindow> = AtomicPtr::new(std::ptr::null_mut());

pub(crate) fn move_content_to_panel(host: &NSWindow, panel: &NSPanel, mtm: MainThreadMarker) -> Option<()> {
    let view = host.contentView()?;
    let placeholder = NSView::initWithFrame(mtm.alloc(), view.frame());
    host.setContentView(Some(&placeholder));
    panel.setContentView(Some(&view));
    Some(())
}

/// The mascot's NSPanel. tao only builds a plain NSWindow, which cannot be
/// non-activating, so the first call moves its content view into a panel.
fn panel(app: &AppHandle) -> Option<&'static NSWindow> {
    let mtm = MainThreadMarker::new()?;
    let raw = OVERLAY.load(Ordering::Acquire);
    if !raw.is_null() {
        let host = unsafe { &*HOST.load(Ordering::Acquire) };
        host.setIgnoresMouseEvents(true);
        host.orderOut(None);
        return Some(unsafe { &*(raw as *const NSWindow) });
    }
    let host = app.get_webview_window("mascot")?.ns_window().ok()?;
    let host = unsafe { Retained::retain(host as *mut NSWindow) }?;
    host.setIgnoresMouseEvents(true);
    host.orderOut(None);
    let panel = NSPanel::initWithContentRect_styleMask_backing_defer(
        mtm.alloc::<NSPanel>(),
        host.frame(),
        NSWindowStyleMask::Borderless | NSWindowStyleMask::NonactivatingPanel,
        NSBackingStoreType::Buffered,
        false,
    );
    unsafe { panel.setReleasedWhenClosed(false) };
    panel.setFloatingPanel(true);
    panel.setBecomesKeyOnlyIfNeeded(true);
    panel.setWorksWhenModal(true);
    panel.setLevel(overlay_level());
    panel.setCanHide(false);
    panel.setHidesOnDeactivate(false);
    panel.setMovable(false);
    panel.setAnimationBehavior(NSWindowAnimationBehavior::None);
    // Keep the tile out of screenshots and recordings.
    panel.setSharingType(NSWindowSharingType::None);
    panel.setOpaque(false);
    panel.setBackgroundColor(Some(&NSColor::clearColor()));
    panel.setHasShadow(false);
    // Never swallow clicks: hover, drag, click and right-click are all polled
    // from the global pointer in `tick`, so the panel needs no mouse events.
    panel.setIgnoresMouseEvents(true);
    move_content_to_panel(&host, &panel, mtm)?;
    HOST.store(Retained::into_raw(host), Ordering::Release);
    let raw = Retained::into_raw(panel);
    OVERLAY.store(raw, Ordering::Release);
    Some(unsafe { &*(raw as *const NSWindow) })
}

fn set_frame(ns: &NSWindow, geo: &Geo, x: f64) {
    let rect = NSRect::new(
        NSPoint::new(geo.left + x, geo.top - TILE_H + LIFT),
        NSSize::new(TILE_W, TILE_H),
    );
    ns.setFrame_display(rect, true);
}

/// Follow the active Space, including other apps' fullscreen Spaces.
fn set_spaces(ns: &NSWindow, fullscreen: bool) {
    use NSWindowCollectionBehavior as B;
    let space = if fullscreen { B::MoveToActiveSpace } else { B::CanJoinAllSpaces };
    ns.setCollectionBehavior(
        space
            | B::CanJoinAllApplications
            | B::FullScreenAuxiliary
            | B::IgnoresCycle
            | B::FullScreenDisallowsTiling
            | B::Stationary,
    );
    ns.orderFrontRegardless();
}

fn slot_path() -> std::path::PathBuf {
    crate::supervisor::conventional_data_dir().join("mascot-slot.json")
}

fn load_x() -> Option<f64> {
    let raw = std::fs::read_to_string(slot_path()).ok()?;
    serde_json::from_str::<serde_json::Value>(&raw).ok()?["x"].as_f64()
}

fn save_x(x: f64) {
    let _ = std::fs::write(slot_path(), serde_json::json!({ "x": x }).to_string());
}

struct Drag {
    grab_dx: f64,
    press: (f64, f64),
    /// Farthest the pointer got from the press point.
    moved: f64,
    last_want_x: f64,
    last_placed_x: f64,
    notch_side: Option<NotchSide>,
}

struct State {
    was_down: bool,
    was_right: bool,
    hover: bool,
    heading: i8,
    fullscreen: Option<bool>,
    drag: Option<Drag>,
}

static STATE: Mutex<State> = Mutex::new(State {
    was_down: false,
    was_right: false,
    hover: false,
    heading: 0,
    fullscreen: None,
    drag: None,
});
/// Poll fast only while the pointer is on the mascot.
static ACTIVE: AtomicBool = AtomicBool::new(false);
static VISIBLE: AtomicBool = AtomicBool::new(true);

pub fn is_visible() -> bool { VISIBLE.load(Ordering::Acquire) }

pub fn set_visible(app: &AppHandle, visible: bool) -> Result<(), String> {
    // Persist first: a failed disk write must not pretend this survives restart.
    crate::charlie_prefs::save(&crate::supervisor::conventional_data_dir(), visible)
        .map_err(|e| e.to_string())?;
    VISIBLE.store(visible, Ordering::Release);
    ACTIVE.store(false, Ordering::Relaxed);
    if let Ok(mut st) = STATE.lock() {
        st.drag = None;
        st.hover = false;
        st.was_down = NSEvent::pressedMouseButtons() & 1 != 0;
        st.was_right = NSEvent::pressedMouseButtons() & 2 != 0;
        st.fullscreen = None;
    }
    if let Some(ns) = panel(app) {
        if visible { ns.orderFrontRegardless(); } else { ns.orderOut(None); }
    }
    script(app, format!("window.__mascotVisible={visible}"));
    if let Some(menu) = app.menu() {
        for entry in menu.items().unwrap_or_default() {
            if let tauri::menu::MenuItemKind::Submenu(submenu) = entry {
                if let Some(tauri::menu::MenuItemKind::MenuItem(item)) = submenu.get("charlie_visibility") {
                    let _ = item.set_text(if visible { "Hide Charlie" } else { "Show Charlie" });
                }
            }
        }
    }
    Ok(())
}

fn script(app: &AppHandle, js: String) {
    if let Some(window) = app.get_webview_window("mascot") {
        let _ = window.eval(js);
    }
}

pub fn set_state(app: &AppHandle, state: &str, level: Option<f32>) {
    let level = level.map_or_else(|| "null".to_string(), |value| value.to_string());
    script(app, format!("window.charlieMascot&&window.charlieMascot.set(\"{state}\",{level})"));
}

fn set_heading(app: &AppHandle, st: &mut State, heading: i8) {
    if st.heading != heading {
        st.heading = heading;
        script(app, format!("window.__mascotLean&&window.__mascotLean({})", f64::from(heading) * 0.45));
    }
}

fn hit(frame: NSRect, p: NSPoint) -> bool {
    let top = frame.origin.y + frame.size.height;
    (p.x - (frame.origin.x + frame.size.width / 2.0)).abs() <= HIT_HALF_W
        && p.y <= top
        && p.y >= top - HIT_H
}

fn tick(app: &AppHandle) {
    if !is_visible() { return; }
    let (Some(ns), Some(geo)) = (panel(app), geometry()) else {
        return;
    };
    let mouse = NSEvent::mouseLocation();
    let down = NSEvent::pressedMouseButtons() & 1 != 0;
    let frame = ns.frame();
    let right = NSEvent::pressedMouseButtons() & 2 != 0;
    let inside = hit(frame, mouse);
    let mut click = false;
    let mut st = STATE.lock().unwrap_or_else(|e| e.into_inner());

    let hover = inside || st.drag.is_some();
    if hover != st.hover {
        st.hover = hover;
        script(app, format!("window.__mascotHover&&window.__mascotHover({hover})"));
    }
    ACTIVE.store(hover, Ordering::Relaxed);

    if down && !st.was_down && inside && st.drag.is_none() {
        let start_x = frame.origin.x - geo.left;
        let (_, side) = clamp_across_notch(start_x, &geo.bands, TILE_W, Some(start_x), None);
        st.drag = Some(Drag {
            grab_dx: mouse.x - frame.origin.x,
            press: (mouse.x, mouse.y),
            moved: 0.0,
            last_want_x: start_x,
            last_placed_x: start_x,
            notch_side: side,
        });
    }
    st.was_down = down;
    let context = right && !st.was_right && inside && st.drag.is_none();
    st.was_right = right;

    if let Some(mut drag) = st.drag.take() {
        drag.moved = drag.moved.max((mouse.x - drag.press.0).hypot(mouse.y - drag.press.1));
        if !down {
            // Released: a press that never left the slop is a click.
            click = drag.moved <= CLICK_SLOP;
            if !click {
                save_x(drag.last_placed_x);
            }
            set_heading(app, &mut st, 0);
        } else {
            if drag.moved > CLICK_SLOP {
                let want = mouse.x - drag.grab_dx - geo.left;
                let (x, side) =
                    clamp_across_notch(want, &geo.bands, TILE_W, Some(drag.last_want_x), drag.notch_side);
                set_frame(ns, &geo, x);
                let heading = if (x - drag.last_placed_x).abs() > 0.5 {
                    if x > drag.last_placed_x { 1 } else { -1 }
                } else if (want - drag.last_want_x).abs() > 0.5 {
                    if want > drag.last_want_x { 1 } else { -1 }
                } else {
                    st.heading
                };
                set_heading(app, &mut st, heading);
                drag.last_want_x = want;
                drag.last_placed_x = x;
                drag.notch_side = side;
            }
            st.drag = Some(drag);
        }
    } else {
        // Idle: follow display / menu-bar changes.
        let x = clamp_to_bands(frame.origin.x - geo.left, &geo.bands, TILE_W);
        let moved = (frame.origin.x - geo.left - x).abs() > 0.5
            || (frame.origin.y - (geo.top - TILE_H + LIFT)).abs() > 0.5
            || frame.size.width != TILE_W;
        let fullscreen = fullscreen_presentation() || !ns.isOnActiveSpace();
        if moved {
            set_frame(ns, &geo, x);
        }
        if moved || st.fullscreen != Some(fullscreen) {
            st.fullscreen = Some(fullscreen);
            set_spaces(ns, fullscreen);
        }
    }
    drop(st);
    if context {
        crate::shell_events::record("activate", "mascot_menu_action", None);
        if let Err(error) = context_menu(app) {
            log::warn!("mascot context menu failed: {error}");
        }
    }
    if click {
        crate::shell_events::record("activate", "mascot_show", None);
        crate::bring_main_forward(app);
    }
}

/// Right-click menu; items reuse the shared handler ids. `popup` runs a nested
/// menu-tracking loop on the main thread (tick is already there; `busy` in the
/// poll thread prevents re-entry).
fn context_menu(app: &AppHandle) -> Result<(), String> {
    use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
    let e = |e: tauri::Error| e.to_string();
    let item = |id: &str, label: &str| MenuItem::with_id(app, id, label, true, None::<&str>).map_err(e);
    let menu = Menu::with_items(
        app,
        &[
            &item("show", "Show Supymem")?,
            &item("charlie_visibility", "Hide Charlie")?,
            &item("new_venture", "New venture")?,
            &PredefinedMenuItem::separator(app).map_err(e)?,
            &item("pause", "Pause the current venture")?,
            &item("resume", "Resume the current venture")?,
            &PredefinedMenuItem::separator(app).map_err(e)?,
            &item("updates", "Check for updates")?,
            &item("quit", "Quit Supymem")?,
        ],
    )
    .map_err(e)?;
    let window = app.get_webview_window("mascot").ok_or("mascot window missing")?;
    tauri::menu::ContextMenu::popup(&menu, window.as_ref().window().clone()).map_err(e)
}

pub fn install(app: &AppHandle) -> Result<(), String> {
    let visible = crate::charlie_prefs::load(&crate::supervisor::conventional_data_dir());
    VISIBLE.store(visible, Ordering::Release);
    if let Some(old) = app.get_webview_window("mascot") {
        let _ = old.close();
    }
    WebviewWindowBuilder::new(app, "mascot", mascot_page_url())
        .initialization_script(format!("window.__mascotVisible={visible}"))
        .title("Supymem")
        .theme(Some(tauri::Theme::Light))
        .inner_size(TILE_W, TILE_H)
        .decorations(false)
        .transparent(true)
        .shadow(false)
        .skip_taskbar(true)
        .resizable(false)
        .focused(false)
        .visible(false)
        .build()
        .map_err(|e| e.to_string())?;
    let ns = panel(app).ok_or("mascot panel missing")?;
    if let Some(geo) = geometry() {
        let x = load_x().map_or_else(
            || default_x(&geo.bands, TILE_W),
            |x| clamp_to_bands(x, &geo.bands, TILE_W),
        );
        set_frame(ns, &geo, x);
        if visible { set_spaces(ns, false); } else { ns.orderOut(None); }
    }
    let app = app.clone();
    let busy = std::sync::Arc::new(AtomicBool::new(false));
    thread::spawn(move || loop {
        let dragging = STATE.lock().map(|s| s.drag.is_some()).unwrap_or(false);
        thread::sleep(Duration::from_millis(if dragging {
            8
        } else if ACTIVE.load(Ordering::Relaxed) {
            16
        } else {
            60
        }));
        if busy.swap(true, Ordering::AcqRel) {
            continue; // main thread still on the previous tick
        }
        let (handle, flag) = (app.clone(), busy.clone());
        if app
            .run_on_main_thread(move || {
                tick(&handle);
                flag.store(false, Ordering::Release);
            })
            .is_err()
        {
            break;
        }
    });
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{clamp_across_notch, clamp_to_bands, default_x, hit, Band, NotchSide};
    use objc2_foundation::{NSPoint, NSRect, NSSize};

    #[test]
    fn panel_handoffs_preserve_the_tao_host_content_view() {
        let source = include_str!("mascot.rs").split("#[cfg(test)]").next().unwrap();
        let handoff = source.split("pub(crate) fn move_content_to_panel(").nth(1).unwrap()
            .split("\n}").next().unwrap();
        assert!(handoff.contains("let view = host.contentView()?"));
        assert!(handoff.contains("NSView::initWithFrame(mtm.alloc(), view.frame())"));
        assert!(handoff.find("host.setContentView(Some(&placeholder))").unwrap()
            < handoff.find("panel.setContentView(Some(&view))").unwrap());
        let panel = source.split("fn panel(").nth(1).unwrap().split("fn set_frame(").next().unwrap();
        assert!(panel.contains("move_content_to_panel(&host, &panel, mtm)?"));
        assert!(!panel.contains("setContentView"));
        let pill = include_str!("agent_stop_pill.rs");
        assert!(pill.contains("crate::mascot::move_content_to_panel(host, &panel, mtm).ok_or("));
        assert!(!pill.contains("setContentView"));
    }

    #[test]
    fn host_stays_hidden_before_the_panel_is_ordered_front() {
        let source = include_str!("mascot.rs").split("#[cfg(test)]").next().unwrap();
        let install = source.split("pub fn install(").nth(1).unwrap();
        let panel = source.split("fn panel(").nth(1).unwrap().split("fn set_frame(").next().unwrap();
        assert!(install.contains(".visible(false)"));
        assert!(install.find("panel(app)").unwrap() < install.find("set_spaces(ns, false)").unwrap());
        assert!(panel.contains("host.setIgnoresMouseEvents(true)"));
        assert!(panel.find("HOST.load").unwrap() < panel.find(".ns_window()").unwrap());
        assert!(panel.find("HOST.store").unwrap() < panel.find("OVERLAY.store").unwrap());
        assert!(panel.find("host.orderOut(None)").unwrap() < panel.find("return Some").unwrap());
        let visibility = source.split("pub fn set_visible(").nth(1).unwrap().split("fn script(").next().unwrap();
        assert!(visibility.contains("panel(app)"));
        assert!(visibility.contains("ns.orderFrontRegardless()"));
        assert!(visibility.contains("ns.orderOut(None)"));
        assert!(!source.contains(".show("));
        assert!(!source.contains(".visible(true)"));
        assert!(!source.contains("host.orderFront"));
        assert!(!source.contains("host.makeKey"));
    }

    #[test]
    fn hit_is_only_the_character_not_the_whole_tile() {
        let frame = NSRect::new(NSPoint::new(100.0, 0.0), NSSize::new(96.0, 88.0));
        assert!(hit(frame, NSPoint::new(148.0, 80.0)));
        assert!(hit(frame, NSPoint::new(182.0, 50.0)));
        assert!(!hit(frame, NSPoint::new(184.0, 50.0)));
        assert!(!hit(frame, NSPoint::new(104.0, 80.0))); // transparent side margin
        assert!(!hit(frame, NSPoint::new(148.0, 5.0))); // transparent bottom
    }

    #[test]
    fn clamp_stays_inside_a_band() {
        let bands = [Band { x0: 10.0, x1: 100.0 }];
        assert_eq!(clamp_to_bands(40.0, &bands, 22.0), 40.0);
        assert_eq!(clamp_to_bands(90.0, &bands, 22.0), 78.0);
        assert_eq!(clamp_to_bands(0.0, &bands, 22.0), 10.0);
    }

    #[test]
    fn clamp_dead_ends_at_screen_edges() {
        let bands = [Band { x0: 0.0, x1: 200.0 }, Band { x0: 360.0, x1: 800.0 }];
        assert_eq!(
            clamp_across_notch(-20.0, &bands, 22.0, Some(10.0), Some(NotchSide::Left)).0,
            0.0
        );
        assert_eq!(
            clamp_across_notch(900.0, &bands, 22.0, Some(700.0), Some(NotchSide::Right)).0,
            778.0
        );
    }

    #[test]
    fn clamp_hops_across_the_notch_but_never_sits_in_it() {
        let bands = [Band { x0: 0.0, x1: 200.0 }, Band { x0: 360.0, x1: 800.0 }];
        assert_eq!(clamp_to_bands(170.0, &bands, 22.0), 170.0);
        // Dragging into the gap from the left lands on the right lip.
        assert_eq!(
            clamp_across_notch(210.0, &bands, 22.0, Some(170.0), Some(NotchSide::Left)).0,
            360.0
        );
        // Dragging into the gap from the right lands on the left lip.
        assert_eq!(
            clamp_across_notch(350.0, &bands, 22.0, Some(400.0), Some(NotchSide::Right)).0,
            178.0
        );
        // Hitting the lip itself is enough to hop.
        assert_eq!(
            clamp_across_notch(178.0, &bands, 22.0, Some(170.0), Some(NotchSide::Left)).0,
            360.0
        );
    }

    #[test]
    fn clamp_stays_on_the_hopped_side_while_cursor_clears() {
        let bands = [Band { x0: 0.0, x1: 200.0 }, Band { x0: 360.0, x1: 800.0 }];
        assert_eq!(
            clamp_across_notch(210.0, &bands, 22.0, Some(210.0), Some(NotchSide::Right)).0,
            360.0
        );
        // Still on the old lip right after the hop must not bounce back.
        assert_eq!(
            clamp_across_notch(177.0, &bands, 22.0, Some(178.0), Some(NotchSide::Right)).0,
            360.0
        );
    }

    #[test]
    fn default_hangs_from_the_notch() {
        let bands = [Band { x0: 0.0, x1: 220.0 }, Band { x0: 400.0, x1: 900.0 }];
        assert_eq!(default_x(&bands, 22.0), 192.0);
        assert!(default_x(&bands, 22.0) + 22.0 <= 220.0);
    }

    #[test]
    fn default_centers_when_there_is_no_notch() {
        let bands = [Band { x0: 0.0, x1: 1000.0 }];
        assert_eq!(default_x(&bands, 22.0), 489.0);
    }
}
