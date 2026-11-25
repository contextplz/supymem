//! "Suggestions" overlay: a collapsed current-venture pill in the top-right corner that shows
//! tasks Supymem can take over. It renders the console route `/suggestions/overlay`, appears on every
//! Space, and never takes keyboard focus from the app the founder is working in (a non-activating
//! panel on macOS). Whether it may appear is decided here from the founder's preferences:
//! on/off, minimum gap between pop-ups, quiet hours and "pause for today".

use std::sync::atomic::{AtomicI64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};
use std::sync::{Mutex, OnceLock, mpsc};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, State};

use crate::AppState;

pub const LABEL: &str = "suggestions";
pub const PATH: &str = "/suggestions/overlay";
const READY_DEADLINE: Duration = Duration::from_secs(15);
struct Presentation {
    id: String, owner: crate::NoticeCustody, deadline: Instant, loaded: bool, shown: bool,
    ready: Option<mpsc::Sender<()>>,
}
#[derive(Default)]
struct OverlayCustody { presentation: Option<Presentation> }
impl OverlayCustody {
    fn begin(&mut self, id: String, owner: crate::NoticeCustody, deadline: Instant, ready: mpsc::Sender<()>) {
        self.presentation = Some(Presentation {id, owner, deadline, loaded: false, shown: false, ready: Some(ready)});
    }
    fn matches(&self, id: &str, owner: &crate::NoticeCustody, now: Instant) -> bool {
        self.presentation.as_ref().is_some_and(|p| p.id == id && &p.owner == owner && (p.shown || now < p.deadline))
    }
    fn retire(&mut self) { self.presentation = None; }
    fn retire_id(&mut self, id: &str) {
        if self.presentation.as_ref().is_some_and(|p| p.id == id) { self.retire(); }
    }
    fn retire_generation(&mut self, generation: u64) {
        if self.presentation.as_ref().is_some_and(|p| p.owner.auth_epoch != generation) { self.retire(); }
    }
    fn queued<R>(&self, id: &str, owner: &crate::NoticeCustody, now: Instant, dispatch: impl FnOnce() -> R) -> Option<R> {
        self.matches(id, owner, now).then(dispatch)
    }
    fn loaded(&mut self, id: &str, owner: &crate::NoticeCustody, now: Instant) {
        if self.matches(id, owner, now) { self.presentation.as_mut().unwrap().loaded = true; }
    }
    fn ready(&mut self, id: &str, owner: &crate::NoticeCustody, now: Instant, show: impl FnOnce(bool) -> bool) -> bool {
        if !self.matches(id, owner, now) { return false; }
        let p = self.presentation.as_mut().unwrap();
        if !p.loaded || !show(p.shown) { return false; }
        p.shown = true;
        if let Some(ready) = p.ready.take() { let _ = ready.send(()); }
        true
    }
}
fn overlay_custody() -> &'static Mutex<OverlayCustody> {
    static CUSTODY: OnceLock<Mutex<OverlayCustody>> = OnceLock::new();
    CUSTODY.get_or_init(|| Mutex::new(OverlayCustody::default()))
}
fn overlay_url_allowed(label: &str, location: &url::Url, console: &str, id: &str) -> bool {
    let Ok(base) = url::Url::parse(console) else { return false; };
    let query: Vec<_> = location.query_pairs().collect();
    label == LABEL && location.origin() == base.origin() && location.path() == PATH
        && location.username().is_empty() && location.password().is_none() && location.fragment().is_none()
        && uuid::Uuid::parse_str(id).is_ok()
        && query.len() == 1 && query[0].0 == "presentation" && query[0].1 == id
}
fn require_overlay(window: &tauri::WebviewWindow, state: &AppState, id: &str) -> Result<(), String> {
    let location = window.url().map_err(|_| "suggestions_origin_denied")?;
    if !overlay_url_allowed(window.label(), &location, &state.supervisor.console_url(), id) {
        return Err("suggestions_origin_denied".into());
    }
    Ok(())
}
pub(crate) fn presentation_current(id: &str, owner: &crate::NoticeCustody) -> bool {
    overlay_custody().lock().is_ok_and(|state| state.matches(id, owner, Instant::now()))
}
pub(crate) fn record_loaded(id: &str, owner: &crate::NoticeCustody) {
    if let Ok(mut state) = overlay_custody().lock() { state.loaded(id, owner, Instant::now()); }
}
#[derive(Serialize)]
pub struct OverlayOwner {
    #[serde(flatten)] owner: crate::NoticeCustody,
    presentation_id: String,
    venture_id: Option<String>,
    viewport_width: f64,
}

/// Everything the window builder and the macOS panel need, in one place so a test can pin it.
pub struct WindowSpec {
    pub width: f64,
    pub start_height: f64,
    pub max_height: f64,
    pub margin: f64,
    pub always_on_top: bool,
    pub decorations: bool,
    pub transparent: bool,
    pub focused: bool,
    pub visible_on_all_workspaces: bool,
    pub accept_first_mouse: bool,
    pub resizable: bool,
    pub skip_taskbar: bool,
    pub shadow: bool,
}

pub const SPEC: WindowSpec = WindowSpec {
    width: 400.0,
    start_height: 160.0,
    max_height: 640.0,
    margin: 14.0,
    always_on_top: true,
    decorations: false,
    transparent: true,
    focused: false,
    visible_on_all_workspaces: true,
    accept_first_mouse: true,
    resizable: false,
    skip_taskbar: true,
    shadow: false,
};

// ---------------------------------------------------------------- preferences

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct Prefs {
    pub enabled: bool,
    /// Least minutes between two pop-ups.
    pub min_gap_minutes: u32,
    pub quiet_enabled: bool,
    /// Local hours, 0-23. The window wraps midnight when start > end.
    pub quiet_start_hour: u8,
    pub quiet_end_hour: u8,
    /// Unix seconds; no pop-up before this. Set by "Pause for today".
    pub paused_until: i64,
}

impl Default for Prefs {
    fn default() -> Self {
        Self { enabled: true, min_gap_minutes: 60, quiet_enabled: true, quiet_start_hour: 21,
               quiet_end_hour: 8, paused_until: 0 }
    }
}

impl Prefs {
    /// Never trust the page: clamp every field to something the rules below can use.
    pub fn sanitized(mut self) -> Self {
        self.min_gap_minutes = self.min_gap_minutes.clamp(15, 720);
        self.quiet_start_hour = self.quiet_start_hour.min(23);
        self.quiet_end_hour = self.quiet_end_hour.min(23);
        self.paused_until = self.paused_until.max(0);
        self
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    Show,
    Off,
    Paused,
    Quiet,
    TooSoon,
}

pub fn in_quiet_hours(start: u8, end: u8, hour: u8) -> bool {
    match start.cmp(&end) {
        std::cmp::Ordering::Equal => false,
        std::cmp::Ordering::Less => hour >= start && hour < end,
        std::cmp::Ordering::Greater => hour >= start || hour < end,
    }
}

pub fn verdict(p: &Prefs, now: i64, local_hour: u8, last_shown: i64) -> Verdict {
    if !p.enabled {
        Verdict::Off
    } else if now < p.paused_until {
        Verdict::Paused
    } else if p.quiet_enabled && in_quiet_hours(p.quiet_start_hour, p.quiet_end_hour, local_hour) {
        Verdict::Quiet
    } else if last_shown > 0 && now - last_shown < i64::from(p.min_gap_minutes) * 60 {
        Verdict::TooSoon
    } else {
        Verdict::Show
    }
}

fn now_secs() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

/// (local hour, seconds since local midnight).
#[cfg(unix)]
fn local_clock(now: i64) -> (u8, i64) {
    let t = now as libc::time_t;
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    if unsafe { libc::localtime_r(&t, &mut tm) }.is_null() {
        return ((now.rem_euclid(86_400) / 3600) as u8, now.rem_euclid(86_400));
    }
    (tm.tm_hour as u8, i64::from(tm.tm_hour) * 3600 + i64::from(tm.tm_min) * 60 + i64::from(tm.tm_sec))
}

#[cfg(windows)]
fn local_clock(now: i64) -> (u8, i64) {
    #[repr(C)]
    struct SystemTime { year: u16, month: u16, weekday: u16, day: u16, hour: u16, minute: u16, second: u16, milliseconds: u16 }
    #[repr(C)]
    struct FileTime { low: u32, high: u32 }
    #[link(name = "kernel32")]
    extern "system" {
        fn FileTimeToSystemTime(file: *const FileTime, system: *mut SystemTime) -> i32;
        fn SystemTimeToTzSpecificLocalTime(zone: *const std::ffi::c_void, utc: *const SystemTime, local: *mut SystemTime) -> i32;
        fn GetLocalTime(local: *mut SystemTime);
    }
    let mut local: SystemTime = unsafe { std::mem::zeroed() };
    let mut utc: SystemTime = unsafe { std::mem::zeroed() };
    let ticks = now.checked_add(11_644_473_600).and_then(|value| u64::try_from(value).ok())
        .and_then(|value| value.checked_mul(10_000_000));
    let converted = ticks.is_some_and(|value| {
        let file = FileTime { low: value as u32, high: (value >> 32) as u32 };
        unsafe { FileTimeToSystemTime(&file, &mut utc) != 0 && SystemTimeToTzSpecificLocalTime(std::ptr::null(), &utc, &mut local) != 0 }
    });
    if !converted { unsafe { GetLocalTime(&mut local); } }
    (local.hour as u8, i64::from(local.hour) * 3600 + i64::from(local.minute) * 60 + i64::from(local.second))
}

#[cfg(not(any(unix, windows)))]
fn local_clock(now: i64) -> (u8, i64) {
    ((now.rem_euclid(86_400) / 3600) as u8, now.rem_euclid(86_400))
}

/// "Pause for today": until the next local midnight.
pub fn pause_until_midnight(now: i64, secs_since_midnight: i64) -> i64 {
    now - secs_since_midnight + 86_400
}

fn dir() -> std::path::PathBuf {
    crate::supervisor::conventional_data_dir()
}

pub fn load_prefs() -> Prefs {
    std::fs::read_to_string(dir().join("suggestions-prefs.json"))
        .ok()
        .and_then(|raw| serde_json::from_str::<Prefs>(&raw).ok())
        .unwrap_or_default()
        .sanitized()
}

fn save_prefs(p: &Prefs) -> Result<(), String> {
    let raw = serde_json::to_string(p).map_err(|e| e.to_string())?;
    std::fs::write(dir().join("suggestions-prefs.json"), raw).map_err(|e| e.to_string())
}

static LAST_SHOWN: AtomicI64 = AtomicI64::new(-1);

fn last_shown() -> i64 {
    let v = LAST_SHOWN.load(Ordering::Relaxed);
    if v >= 0 {
        return v;
    }
    let read = std::fs::read_to_string(dir().join("suggestions-last.txt")).ok()
        .and_then(|s| s.trim().parse::<i64>().ok()).unwrap_or(0);
    LAST_SHOWN.store(read, Ordering::Relaxed);
    read
}

fn mark_shown(now: i64) {
    LAST_SHOWN.store(now, Ordering::Relaxed);
    let _ = std::fs::write(dir().join("suggestions-last.txt"), now.to_string());
}

// ---------------------------------------------------------------- geometry

/// Top-right corner of a screen area: (x, y_from_top) for a window of `width`.
pub fn top_right(area_x: f64, area_y: f64, area_w: f64, width: f64, margin: f64) -> (f64, f64) {
    (area_x + area_w - width - margin, area_y + margin)
}

// ---------------------------------------------------------------- the window

#[cfg(target_os = "macos")]
mod mac {
    use std::sync::atomic::{AtomicPtr, Ordering};

    use objc2::rc::Retained;
    use objc2::MainThreadMarker;
    use objc2_app_kit::{
        NSBackingStoreType, NSColor, NSPanel, NSScreen, NSWindow, NSWindowAnimationBehavior,
        NSWindowCollectionBehavior, NSWindowStyleMask,
    };
    use objc2_foundation::{NSPoint, NSRect, NSSize};
    use tauri::{AppHandle, Manager};

    /// NSStatusWindowLevel: above ordinary windows, below the system's own alerts.
    const LEVEL: isize = 25;
    static PANEL: AtomicPtr<NSPanel> = AtomicPtr::new(std::ptr::null_mut());

    /// tao builds a plain NSWindow, which cannot be non-activating, so the first call moves the
    /// webview into an NSPanel that never becomes the key window unless a field needs it.
    pub fn panel(app: &AppHandle) -> Option<&'static NSWindow> {
        let raw = PANEL.load(Ordering::Acquire);
        if !raw.is_null() {
            return Some(unsafe { &*(raw as *const NSWindow) });
        }
        let mtm = MainThreadMarker::new()?;
        let host = app.get_webview_window(super::LABEL)?.ns_window().ok()?;
        let host = unsafe { &*(host as *const NSWindow) };
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
        panel.setLevel(LEVEL);
        panel.setCanHide(false);
        panel.setHidesOnDeactivate(false);
        panel.setMovable(false);
        panel.setAnimationBehavior(NSWindowAnimationBehavior::None);
        panel.setOpaque(false);
        panel.setBackgroundColor(Some(&NSColor::clearColor()));
        panel.setHasShadow(false);
        panel.setCollectionBehavior(
            NSWindowCollectionBehavior::CanJoinAllSpaces
                | NSWindowCollectionBehavior::FullScreenAuxiliary
                | NSWindowCollectionBehavior::IgnoresCycle
                | NSWindowCollectionBehavior::Stationary,
        );
        if let Some(view) = host.contentView() {
            panel.setContentView(Some(&view));
        }
        host.orderOut(None);
        let raw = Retained::into_raw(panel);
        PANEL.store(raw, Ordering::Release);
        Some(unsafe { &*(raw as *const NSWindow) })
    }

    /// Place the panel in the top-right corner of the primary screen's usable area, `height` tall.
    pub fn place(ns: &NSWindow, width: f64, height: f64) {
        let Some(mtm) = MainThreadMarker::new() else { return };
        let Some(screen) = NSScreen::screens(mtm).firstObject() else { return };
        let area = screen.visibleFrame();
        let width = width.min((area.size.width - 2.0 * super::SPEC.margin).max(1.0));
        let height = height.min((area.size.height - 2.0 * super::SPEC.margin).max(1.0));
        let (x, y_from_top) = super::top_right(area.origin.x, 0.0, area.size.width, width, super::SPEC.margin);
        let top = area.origin.y + area.size.height - y_from_top;
        ns.setFrame_display(NSRect::new(NSPoint::new(x, top - height), NSSize::new(width, height)), true);
    }

    /// Show without activating Supymem or stealing focus from the current app.
    pub fn show(ns: &NSWindow) {
        ns.orderFrontRegardless();
    }

    pub fn hide(ns: &NSWindow) {
        ns.orderOut(None);
    }
}

fn ensure_window(app: &AppHandle, console: &str, id: &str) -> Result<(), String> {
    let mut url: url::Url = format!("{}{}", console.trim_end_matches('/'), PATH).parse().map_err(|_| "bad_console_url")?;
    url.query_pairs_mut().append_pair("presentation", id);
    if app.get_webview_window(LABEL).is_some() {
        return app.get_webview_window(LABEL).unwrap().navigate(url).map_err(|_| "overlay_navigation_failed".into());
    }
    tauri::WebviewWindowBuilder::new(app, LABEL, tauri::WebviewUrl::External(url))
        .title("Supymem suggestions")
        .inner_size(SPEC.width, SPEC.start_height)
        .decorations(SPEC.decorations)
        .transparent(SPEC.transparent)
        .always_on_top(SPEC.always_on_top)
        .visible_on_all_workspaces(SPEC.visible_on_all_workspaces)
        .skip_taskbar(SPEC.skip_taskbar)
        .resizable(SPEC.resizable)
        .shadow(SPEC.shadow)
        .focused(SPEC.focused)
        .accept_first_mouse(SPEC.accept_first_mouse)
        .visible(false)
        .build()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

/// Show (or refresh) the overlay on the main thread. Never activates the app.
fn present(app: &AppHandle, console: &str, custody: &crate::NoticeCustody) -> Result<(), String> {
    let (tx, rx) = std::sync::mpsc::channel();
    let handle = app.clone();
    let console = console.to_string();
    let custody = custody.clone();
    let id = uuid::Uuid::new_v4().to_string(); let queued_id = id.clone();
    let (ready_tx, ready_rx) = mpsc::channel();
    let deadline = Instant::now() + READY_DEADLINE;
    crate::with_notice_custody(app, &custody, || {
        overlay_custody().lock().map_err(|_| "overlay_custody_unavailable")?
            .begin(id.clone(), custody.clone(), deadline, ready_tx); Ok::<(), String>(())
    }).unwrap_or(Err("account_changed".into()))?;
    let queued = app.run_on_main_thread(move || {
        // The queued callback may run after sign-out or A → B → A. Acquire
        // only here, never while waiting for the main thread.
        let result = crate::with_notice_custody(&handle, &custody, || {
            let current = overlay_custody().lock().map_err(|_| "overlay_custody_unavailable")?;
            current.queued(&queued_id, &custody, Instant::now(), || {
                hide_on_main(&handle); ensure_window(&handle, &console, &queued_id)
            }).unwrap_or(Err("overlay_retired".into()))
        }).unwrap_or(Err("account_changed".into()));
        let _ = tx.send(result);
    })
    .map_err(|_| "main_thread_unavailable".to_string());
    let result = queued.and_then(|()| rx.recv_timeout(Duration::from_secs(5))
        .map_err(|_| "main_thread_timeout".to_string())?).and_then(|()|
            ready_rx.recv_timeout(deadline.saturating_duration_since(Instant::now()))
                .map_err(|_| "overlay_readiness_unconfirmed".to_string()));
    if result.is_ok() { return Ok(()); }
    let retired = overlay_custody().lock().is_ok_and(|state|
        state.presentation.as_ref().is_none_or(|presentation| presentation.id != id));
    if let Ok(mut state) = overlay_custody().lock() {
        state.retire_id(&id);
    }
    hide_if_retired(app);
    if retired { Err("overlay_retired".into()) } else { result }
}

fn hide_on_main(app: &AppHandle) {
    #[cfg(target_os = "macos")]
    if let Some(ns) = mac::panel(app) { mac::hide(ns); }
    #[cfg(not(target_os = "macos"))]
    if let Some(w) = app.get_webview_window(LABEL) { let _ = w.hide(); }
}
fn hide_if_retired(app: &AppHandle) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        if overlay_custody().lock().is_ok_and(|state| state.presentation.as_ref().is_none_or(|p| !p.shown)) { hide_on_main(&handle); }
    });
}
pub(crate) fn retire_for_auth(app: &AppHandle) {
    // The listener runs after a real generation change or a busy-status report.
    // A try-lock custody getter would conflate a concurrent request with sign-out.
    let generation = app.state::<AppState>().auth.notice_generation();
    if let Ok(mut state) = overlay_custody().lock() {
        state.retire_generation(generation);
    }
    hide_if_retired(app);
}
pub fn hide(app: &AppHandle) {
    if let Ok(mut state) = overlay_custody().lock() { state.retire(); }
    hide_if_retired(app);
}

/// What the notice poller does with a "suggestions" notice.
#[derive(Debug, PartialEq, Eq)]
pub enum Delivery {
    Shown,
    /// Held back by the founder's preferences. The cards stay in the Suggestions list and badge.
    Held(&'static str),
    /// The overlay could not open; the caller falls back to a native notification.
    Failed,
}

pub(crate) fn handle_notice(app: &AppHandle, console: &str, custody: &crate::NoticeCustody) -> Delivery {
    let state = app.state::<AppState>();
    if state.active_venture.lock().ok().and_then(|value| value.clone()).is_none() {
        return Delivery::Held("no_current_venture");
    }
    if viewport_width(app) <= 600.0 { return Delivery::Held("narrow_window"); }
    let now = now_secs();
    let (hour, _) = local_clock(now);
    match verdict(&load_prefs(), now, hour, last_shown()) {
        Verdict::Show => {}
        Verdict::Off => return Delivery::Held("off"),
        Verdict::Paused => return Delivery::Held("paused"),
        Verdict::Quiet => return Delivery::Held("quiet_hours"),
        Verdict::TooSoon => return Delivery::Held("too_soon"),
    }
    match present(app, console, custody) {
        Ok(()) => {
            mark_shown(now);
            Delivery::Shown
        }
        Err(reason) if reason == "overlay_retired" => Delivery::Held("closed"),
        Err(reason) => {
            log::warn!("suggestions_overlay_failed reason={reason}");
            Delivery::Failed
        }
    }
}

fn viewport_width(app: &AppHandle) -> f64 {
    app.get_webview_window("main").and_then(|window| {
        Some(window.inner_size().ok()?.width as f64 / window.scale_factor().ok()?)
    }).unwrap_or(0.0)
}

// ---------------------------------------------------------------- commands

#[tauri::command]
pub fn suggestions_get_prefs() -> Prefs {
    load_prefs()
}

#[tauri::command]
pub fn suggestions_set_prefs(prefs: Prefs) -> Result<Prefs, String> {
    let current = load_prefs();
    // The pause is set only by "Pause for today"; saving the form never changes it.
    let next = Prefs { paused_until: current.paused_until, ..prefs }.sanitized();
    save_prefs(&next)?;
    Ok(next)
}

#[tauri::command]
pub fn suggestions_pause_today(app: AppHandle) -> Result<Prefs, String> {
    let now = now_secs();
    let mut p = load_prefs();
    p.paused_until = pause_until_midnight(now, local_clock(now).1);
    save_prefs(&p)?;
    hide(&app);
    Ok(p)
}

#[tauri::command]
pub fn suggestions_resume() -> Result<Prefs, String> {
    let mut p = load_prefs();
    p.paused_until = 0;
    save_prefs(&p)?;
    Ok(p)
}

#[tauri::command]
pub fn suggestions_overlay_close(app: AppHandle, window: tauri::WebviewWindow, state: State<'_, AppState>, presentation_id: String) -> Result<(), String> {
    require_overlay(&window, &state, &presentation_id)?;
    if let Ok(mut current) = overlay_custody().lock() {
        if current.presentation.as_ref().is_some_and(|p| p.id == presentation_id) { current.retire(); }
    }
    hide_if_retired(&app); Ok(())
}

/// The overlay reports its content height so the transparent window never covers more than the cards.
#[tauri::command]
pub fn suggestions_overlay_resize(app: AppHandle, window: tauri::WebviewWindow, state: State<'_, AppState>, presentation_id: String, height: f64, width: Option<f64>) -> Result<(), String> {
    require_overlay(&window, &state, &presentation_id)?;
    let height = if height.is_finite() { height.clamp(1.0, SPEC.max_height) } else { return Ok(()); };
    let width = width.unwrap_or(SPEC.width);
    if !width.is_finite() || width <= 0.0 { return Ok(()); }
    let width = width.min(SPEC.width);
    let owner = overlay_custody().lock().map_err(|_| "overlay_custody_unavailable")?.presentation.as_ref()
        .filter(|p| p.id == presentation_id).map(|p| p.owner.clone()).ok_or("overlay_retired")?;
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        let _ = crate::with_notice_custody(&handle, &owner, || {
        if !presentation_current(&presentation_id, &owner) { return; }
        #[cfg(target_os = "macos")]
        if let Some(ns) = mac::panel(&handle) {
            mac::place(ns, width, height);
        }
        #[cfg(not(target_os = "macos"))]
        if let Some(w) = handle.get_webview_window(LABEL) {
            let _ = w.set_size(tauri::LogicalSize::new(width, height));
        }
        });
    });
    Ok(())
}

#[tauri::command]
pub async fn suggestions_overlay_custody(window: tauri::WebviewWindow, state: State<'_, AppState>,
    presentation_id: String) -> Result<Option<OverlayOwner>, String> {
    require_overlay(&window, &state, &presentation_id)?;
    let auth = state.auth.acquire_with_timeout(Duration::from_secs(2)).await?;
    require_overlay(&window, &state, &presentation_id)?;
    let Some((subject, device_id, auth_epoch)) = auth.notice_custody() else { return Ok(None); };
    let owner = crate::NoticeCustody {subject, device_id, auth_epoch};
    let venture_id = state.active_venture.lock().ok().and_then(|value| value.clone());
    let viewport_width = viewport_width(window.app_handle());
    Ok(presentation_current(&presentation_id, &owner).then_some(OverlayOwner {owner, presentation_id, venture_id, viewport_width}))
}

#[tauri::command]
pub async fn suggestions_overlay_ready(app: AppHandle, window: tauri::WebviewWindow, state: State<'_, AppState>,
    presentation_id: String, subject: String, device_id: String, auth_epoch: u64, height: f64, width: Option<f64>) -> Result<bool, String> {
    require_overlay(&window, &state, &presentation_id)?;
    if !height.is_finite() || height <= 0.0 { return Ok(false); }
    let height = height.clamp(1.0, SPEC.max_height);
    let width = width.unwrap_or(SPEC.width);
    if !width.is_finite() || width <= 0.0 { return Ok(false); }
    let width = width.min(SPEC.width);
    let owner = crate::NoticeCustody {subject, device_id, auth_epoch};
    let (tx, rx) = tokio::sync::oneshot::channel(); let handle = app.clone();
    app.run_on_main_thread(move || {
        let shown = crate::with_notice_custody(&handle, &owner, || {
            if viewport_width(&handle) <= 600.0 { return false; }
            if require_overlay(&window, &handle.state::<AppState>(), &presentation_id).is_err() { return false; }
            let Ok(mut current) = overlay_custody().lock() else { return false; };
            current.ready(&presentation_id, &owner, Instant::now(), |already_shown| {
            #[cfg(target_os = "macos")]
            { let Some(ns) = mac::panel(&handle) else { return false; }; mac::place(ns, width, height); if !already_shown { mac::show(ns); } }
            #[cfg(not(target_os = "macos"))]
            { let Some(w) = handle.get_webview_window(LABEL) else { return false; };
              let _ = w.set_size(tauri::LogicalSize::new(width, height)); if !already_shown && w.show().is_err() { return false; } }
            true
            })
        }).unwrap_or(false);
        let _ = tx.send(shown);
    }).map_err(|_| "overlay_dispatch_unavailable")?;
    tokio::time::timeout(Duration::from_secs(5), rx).await.map_err(|_| "overlay_readiness_unconfirmed")?
        .map_err(|_| "overlay_readiness_unconfirmed".into())
}

/// The only API calls the overlay may make: list, mark shown, and decide on one card.
pub fn api_path_allowed(method: &str, path: &str) -> bool {
    let id_ok = |id: &str| id.starts_with("sug_") && id.len() <= 64
        && id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-');
    if method == "GET" {
        if let Some(id) = path.strip_prefix("/api/suggestions?venture_id=") {
            return id.starts_with("ven_") && id.len() <= 64
                && id.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-');
        }
    }
    match (method, path.split('/').collect::<Vec<_>>().as_slice()) {
        ("GET", ["", "api", "suggestions"]) => true,
        ("GET", ["", "api", "suggestions?status=history"]) => true,
        ("POST", ["", "api", "suggestions", "shown"]) => true,
        ("POST", ["", "api", "suggestions", id, "decision"]) => id_ok(id),
        _ => false,
    }
}

/// `desktop_api_request` for the overlay window: same signed-in transport, but only the calls above.
#[tauri::command]
pub async fn suggestions_api_request(window: tauri::WebviewWindow, state: State<'_, AppState>, method: String,
                                     path: String, body: Option<serde_json::Value>)
                                     -> Result<crate::auth::Reply, String> {
    let _ = (window, state, method, path, body);
    Err("suggestions_owner_required".into())
}

#[tauri::command]
pub async fn suggestions_overlay_request(window: tauri::WebviewWindow, state: State<'_, AppState>,
    presentation_id: String, subject: String, device_id: String, auth_epoch: u64,
    method: String, path: String, body: Option<serde_json::Value>) -> Result<crate::auth::Reply, String> {
    require_overlay(&window, &state, &presentation_id)?;
    if !api_path_allowed(&method, &path) { return Err("suggestions_request_scope_denied".into()); }
    let owner = crate::NoticeCustody {subject, device_id, auth_epoch};
    crate::native_api_request_scoped(&state, method, path, body, None, Some((owner, presentation_id))).await
}

/// "See details": bring the main window to the venture's Suggestions page.
#[tauri::command]
pub fn suggestions_open_venture(app: AppHandle, window: tauri::WebviewWindow, state: State<'_, AppState>,
    presentation_id: String, venture_id: String) -> Result<(), String> {
    require_overlay(&window, &state, &presentation_id)?;
    let owner = overlay_custody().lock().map_err(|_| "overlay_custody_unavailable")?.presentation.as_ref()
        .filter(|p| p.id == presentation_id).map(|p| p.owner.clone()).ok_or("overlay_retired")?;
    let target = crate::trusted_notice_target(&state.supervisor.console_url(), &format!("/v/{venture_id}/suggestions"))
        .ok_or("invalid_venture")?;
    let handle = app.clone();
    app.run_on_main_thread(move || {
        let _ = crate::with_notice_custody(&handle, &owner, || {
            if !presentation_current(&presentation_id, &owner) { return; }
            if let Some(main) = handle.get_webview_window("main") {
                let _ = main.navigate(target); crate::bring_window_forward(&main);
            }
        });
    }).map_err(|_| "overlay_dispatch_unavailable")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn owner(epoch: u64) -> crate::NoticeCustody {
        crate::NoticeCustody {subject: "usr_a".into(), device_id: "device_a".into(), auth_epoch: epoch}
    }
    #[test]
    fn queued_timeout_and_expired_attempt_never_create_a_late_window() {
        let mut state = OverlayCustody::default(); let actor = owner(1); let now = Instant::now();
        let (tx, _) = mpsc::channel(); state.begin("old".into(), actor.clone(), now + READY_DEADLINE, tx);
        state.retire_id("old"); let mut effects = 0;
        assert!(state.queued("old", &actor, now, || effects += 1).is_none());
        let (tx, _) = mpsc::channel(); state.begin("expired".into(), actor.clone(), now, tx);
        assert!(state.queued("expired", &actor, now, || effects += 1).is_none());
        assert_eq!(effects, 0);
    }
    #[test]
    fn fresh_api_readback_then_ready_qualifies_only_current_owner_and_is_idempotent() {
        let mut state = OverlayCustody::default(); let actor = owner(1); let now = Instant::now();
        let (tx, rx) = mpsc::channel(); state.begin("current".into(), actor.clone(), now + READY_DEADLINE, tx);
        assert!(!state.ready("current", &actor, now, |_| panic!("shown before fresh API readback")));
        state.loaded("old", &actor, now); state.loaded("current", &owner(9), now);
        assert!(!state.ready("current", &actor, now, |_| panic!("foreign readback admitted")));
        state.loaded("current", &actor, now);
        assert!(state.ready("current", &actor, now, |already| {assert!(!already); true}));
        assert!(rx.try_recv().is_ok());
        assert!(state.ready("current", &actor, now + READY_DEADLINE * 2, |already| {assert!(already); true}));
        state.retire();
        assert!(!state.ready("current", &actor, now, |_| panic!("closed window reopened")));
    }
    #[test]
    fn a_b_a_and_new_nonce_retire_cached_dom_and_stale_source_callbacks() {
        let mut state = OverlayCustody::default(); let now = Instant::now(); let old = owner(1);
        let (tx, _) = mpsc::channel(); state.begin("old_page".into(), old.clone(), now + READY_DEADLINE, tx);
        state.loaded("old_page", &old, now); assert!(state.ready("old_page", &old, now, |_| true));
        state.retire(); let current = owner(3);
        let (tx, _) = mpsc::channel(); state.begin("fresh_page".into(), current.clone(), now + READY_DEADLINE, tx);
        let mut effects = 0;
        assert!(state.queued("old_page", &old, now, || effects += 1).is_none());
        assert!(!state.ready("fresh_page", &old, now, |_| {effects += 1; true}));
        assert!(!state.ready("fresh_page", &current, now, |_| {effects += 1; true}));
        assert_eq!(effects, 0); assert!(!state.presentation.as_ref().unwrap().shown);
        state.retire_id("old_page"); assert!(state.matches("fresh_page", &current, now));
    }
    #[test]
    fn retirement_preserves_the_same_generation_but_blocks_old_owner_callbacks() {
        let mut state = OverlayCustody::default(); let now = Instant::now(); let actor = owner(7);
        let (tx, _) = mpsc::channel(); state.begin("shown".into(), actor.clone(), now + READY_DEADLINE, tx);
        state.loaded("shown", &actor, now); assert!(state.ready("shown", &actor, now, |_| true));
        state.retire_generation(7); assert!(state.matches("shown", &actor, now));
        state.retire_generation(8);
        assert!(state.queued("shown", &actor, now, || panic!("retired owner dispatched")).is_none());
        assert!(!state.ready("shown", &actor, now, |_| panic!("retired DOM shown")));
    }
    #[test]
    fn exact_overlay_document_and_fresh_nonce_are_required() {
        let id = "f594fd8c-ad05-45a9-9783-94a61c6d9d38";
        let valid = format!("https://app.supymem.com{PATH}?presentation={id}");
        assert!(overlay_url_allowed(LABEL, &valid.parse().unwrap(), "https://app.supymem.com", id));
        for invalid in [valid.replace("app.supymem.com", "evil.test"), valid.replace(PATH, "/suggestions"),
            format!("{valid}&presentation={id}"), format!("{valid}#old"), valid.replace(id, "old-page")] {
            assert!(!overlay_url_allowed(LABEL, &invalid.parse().unwrap(), "https://app.supymem.com", id));
        }
        assert!(!overlay_url_allowed("main", &valid.parse().unwrap(), "https://app.supymem.com", id));
    }

    fn at(hour: u8, last: i64, p: &Prefs) -> Verdict {
        verdict(p, 1_000_000, hour, last)
    }

    #[test]
    fn overlay_window_never_takes_focus_and_floats_on_every_space() {
        assert!(SPEC.always_on_top && SPEC.transparent && SPEC.visible_on_all_workspaces);
        assert!(!SPEC.decorations && !SPEC.focused && !SPEC.resizable && SPEC.skip_taskbar && !SPEC.shadow);
        assert!(SPEC.accept_first_mouse, "buttons must work on the first click without activating the app");
        assert!(SPEC.start_height < SPEC.max_height && SPEC.width >= 320.0);
        assert_eq!(LABEL, "suggestions");
        assert!(PATH.starts_with("/suggestions/"));
    }

    #[test]
    fn overlay_window_has_ipc_and_commands_are_allowed() {
        let caps: serde_json::Value = serde_json::from_str(include_str!("../capabilities/default.json")).unwrap();
        assert!(caps["windows"].as_array().unwrap().iter().any(|w| w == LABEL));
        let perms = include_str!("../permissions/app.toml");
        for cmd in ["suggestions_get_prefs", "suggestions_set_prefs", "suggestions_pause_today", "suggestions_resume",
                    "suggestions_overlay_close", "suggestions_overlay_resize", "suggestions_open_venture",
                    "suggestions_overlay_custody", "suggestions_overlay_ready", "suggestions_overlay_request"] {
            assert!(perms.contains(&format!("\"{cmd}\"")), "{cmd}");
        }
    }

    #[test]
    fn overlay_may_only_list_mark_shown_and_decide() {
        assert!(api_path_allowed("GET", "/api/suggestions"));
        assert!(api_path_allowed("GET", "/api/suggestions?venture_id=ven_a"));
        for path in ["/api/suggestions?venture_id=ven_a&status=history", "/api/suggestions?venture_id=ven_a/../x",
                     "/api/suggestions?venture_id=usr_a", "/api/suggestions?venture_id=ven_a%26x"] {
            assert!(!api_path_allowed("GET", path));
        }
        assert!(api_path_allowed("POST", "/api/suggestions/shown"));
        assert!(api_path_allowed("POST", "/api/suggestions/sug_abc123/decision"));
        for (m, p) in [("PUT", "/api/suggestions"), ("GET", "/api/ventures"), ("POST", "/api/suggestions/x/decision"),
                       ("POST", "/api/suggestions/sug_a/../decision"), ("POST", "/api/suggestions/sug_a/decision/x"),
                       ("GET", "/api/suggestions/sug_a"), ("POST", "/api/ventures/ven_1/suggestions/refresh"),
                       ("GET", "https://evil.test/api/suggestions")] {
            assert!(!api_path_allowed(m, p), "{m} {p}");
        }
    }

    #[test]
    fn card_stack_sits_in_the_top_right_corner() {
        assert_eq!(top_right(0.0, 0.0, 1440.0, 400.0, 14.0), (1026.0, 14.0));
        assert_eq!(top_right(-1920.0, 30.0, 1920.0, 400.0, 14.0), (-414.0, 44.0));
    }

    #[test]
    fn quiet_hours_wrap_midnight() {
        assert!(in_quiet_hours(21, 8, 23) && in_quiet_hours(21, 8, 3) && !in_quiet_hours(21, 8, 12));
        assert!(in_quiet_hours(1, 5, 2) && !in_quiet_hours(1, 5, 5));
        assert!(!in_quiet_hours(8, 8, 8), "equal start and end means no quiet window");
    }

    #[test]
    fn frequency_limit_pause_quiet_and_off_hold_the_overlay_back() {
        let p = Prefs::default();
        assert_eq!(at(12, 0, &p), Verdict::Show);
        assert_eq!(at(12, 1_000_000 - 30 * 60, &p), Verdict::TooSoon);
        assert_eq!(at(12, 1_000_000 - 61 * 60, &p), Verdict::Show);
        assert_eq!(at(23, 0, &p), Verdict::Quiet);
        assert_eq!(at(23, 0, &Prefs { quiet_enabled: false, ..p.clone() }), Verdict::Show);
        assert_eq!(at(12, 0, &Prefs { enabled: false, ..p.clone() }), Verdict::Off);
        assert_eq!(at(12, 0, &Prefs { paused_until: 1_000_001, ..p.clone() }), Verdict::Paused);
        assert_eq!(at(12, 0, &Prefs { paused_until: 1_000_000, ..p }), Verdict::Show);
    }

    #[test]
    fn pause_for_today_ends_at_the_next_local_midnight() {
        // 10:00 local: 14 hours remain.
        assert_eq!(pause_until_midnight(1_000_000, 10 * 3600), 1_000_000 + 14 * 3600);
    }

    #[test]
    fn page_supplied_preferences_are_clamped() {
        let p = Prefs { min_gap_minutes: 0, quiet_start_hour: 99, quiet_end_hour: 255, paused_until: -5, ..Prefs::default() }.sanitized();
        assert_eq!((p.min_gap_minutes, p.quiet_start_hour, p.quiet_end_hour, p.paused_until), (15, 23, 23, 0));
        assert_eq!(Prefs { min_gap_minutes: 99_999, ..Prefs::default() }.sanitized().min_gap_minutes, 720);
    }

    #[test]
    fn missing_fields_fall_back_to_defaults() {
        let p: Prefs = serde_json::from_str(r#"{"enabled": false}"#).unwrap();
        assert!(!p.enabled && p.min_gap_minutes == 60 && p.quiet_enabled);
    }
}
