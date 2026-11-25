//! First-run macOS permission gate: non-prompting status, prompting request,
//! and deep links to the exact System Settings pane. Only the main console
//! window may call these (see `require_native_account_window`).
//!
//! Status is for THIS app process. The computer-use helper (peekaboo) can hold
//! its own TCC entries; `desktop_permission_status` reports those.

use std::collections::BTreeMap;

use tauri::State;

use crate::{require_native_account_window, AppState};

pub const NAMES: [&str; 5] = ["accessibility", "screen-recording", "microphone", "automation", "notifications"];

/// Settings anchor for each permission. `None` = unknown name.
fn pane(name: &str) -> Option<&'static str> {
    Some(match name {
        "accessibility" => "Privacy_Accessibility",
        "screen-recording" => "Privacy_ScreenCapture",
        "microphone" => "Privacy_Microphone",
        "automation" => "Privacy_Automation",
        "notifications" => "notifications",
        _ => return None,
    })
}

#[cfg(target_os = "macos")]
mod mac {
    use std::ffi::c_void;

    use objc2::msg_send;
    use objc2::rc::Retained;
    use objc2::runtime::{AnyClass, Bool};
    use objc2_foundation::NSString;

    #[link(name = "ApplicationServices", kind = "framework")]
    extern "C" {
        fn AXIsProcessTrusted() -> bool;
        fn AXIsProcessTrustedWithOptions(options: *const c_void) -> bool;
        static kAXTrustedCheckOptionPrompt: *const c_void;
        fn CGPreflightScreenCaptureAccess() -> bool;
        fn CGRequestScreenCaptureAccess() -> bool;
        fn AECreateDesc(kind: u32, data: *const c_void, size: isize, out: *mut AEDesc) -> i16;
        fn AEDisposeDesc(desc: *mut AEDesc) -> i16;
        fn AEDeterminePermissionToAutomateTarget(target: *const AEDesc, class: u32, id: u32, ask: bool) -> i32;
    }
    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        static kCFBooleanTrue: *const c_void;
        static kCFTypeDictionaryKeyCallBacks: u8;
        static kCFTypeDictionaryValueCallBacks: u8;
        fn CFDictionaryCreate(alloc: *const c_void, keys: *const *const c_void, values: *const *const c_void,
            n: isize, kcb: *const c_void, vcb: *const c_void) -> *const c_void;
        fn CFRelease(obj: *const c_void);
    }
    #[link(name = "AVFoundation", kind = "framework")]
    extern "C" {}

    #[repr(C)]
    struct AEDesc {
        kind: u32,
        data: *mut c_void,
    }

    // ponytail: AX and Screen Recording have no denied/undetermined split without prompting,
    // so both report "not_determined" = "not yet".
    pub fn accessibility() -> &'static str {
        if unsafe { AXIsProcessTrusted() } { "granted" } else { "not_determined" }
    }

    pub fn request_accessibility() {
        unsafe {
            let keys = [kAXTrustedCheckOptionPrompt];
            let values = [kCFBooleanTrue];
            let dict = CFDictionaryCreate(std::ptr::null(), keys.as_ptr(), values.as_ptr(), 1,
                &kCFTypeDictionaryKeyCallBacks as *const u8 as *const c_void,
                &kCFTypeDictionaryValueCallBacks as *const u8 as *const c_void);
            AXIsProcessTrustedWithOptions(dict);
            if !dict.is_null() { CFRelease(dict); }
        }
    }

    pub fn screen_recording() -> &'static str {
        if unsafe { CGPreflightScreenCaptureAccess() } { "granted" } else { "not_determined" }
    }

    pub fn request_screen_recording() {
        unsafe { CGRequestScreenCaptureAccess() };
    }

    fn audio_type() -> Retained<NSString> {
        NSString::from_str("soun") // AVMediaTypeAudio
    }

    pub fn microphone() -> &'static str {
        let Some(class) = AnyClass::get(c"AVCaptureDevice") else { return "unknown" };
        let media = audio_type();
        let status: isize = unsafe { msg_send![class, authorizationStatusForMediaType: &*media] };
        match status {
            0 => "not_determined",
            1 | 2 => "denied", // 1 = restricted by policy, 2 = denied
            3 => "granted",
            _ => "unknown",
        }
    }

    pub fn request_microphone() {
        let Some(class) = AnyClass::get(c"AVCaptureDevice") else { return };
        let media = audio_type();
        let done = block2::RcBlock::new(|_granted: Bool| {});
        let _: () = unsafe { msg_send![class, requestAccessForMediaType: &*media, completionHandler: &*done] };
    }

    /// System Events is what the Cursor send flow scripts (ide.rs) and is always running.
    fn automation_probe(ask: bool) -> &'static str {
        const BUNDLE_ID: u32 = u32::from_be_bytes(*b"bund");
        const WILDCARD: u32 = u32::from_be_bytes(*b"****");
        let id = b"com.apple.systemevents";
        let mut target = AEDesc { kind: 0, data: std::ptr::null_mut() };
        unsafe {
            if AECreateDesc(BUNDLE_ID, id.as_ptr() as *const c_void, id.len() as isize, &mut target) != 0 {
                return "unknown";
            }
            let code = AEDeterminePermissionToAutomateTarget(&target, WILDCARD, WILDCARD, ask);
            AEDisposeDesc(&mut target);
            match code {
                0 => "granted",
                -1743 => "denied",
                -1744 => "not_determined",
                _ => "unknown", // -600: target not running
            }
        }
    }
    pub fn automation() -> &'static str { automation_probe(false) }
    pub fn request_automation() { automation_probe(true); }
}

#[cfg(target_os = "macos")]
fn notifications_status() -> &'static str {
    use mac_usernotifications::AuthorizationStatus as S;
    // A dev executable has no bundle and UNUserNotificationCenter would throw.
    if mac_usernotifications::check_bundle().is_err() { return "unknown"; }
    match mac_usernotifications::blocking::get_notification_settings().map(|s| s.authorization_status) {
        Ok(S::Authorized | S::Provisional | S::Ephemeral) => "granted",
        Ok(S::Denied) => "denied",
        Ok(S::NotDetermined) => "not_determined",
        _ => "unknown",
    }
}

fn status_of(name: &str) -> &'static str {
    #[cfg(target_os = "macos")]
    match name {
        "accessibility" => return mac::accessibility(),
        "screen-recording" => return mac::screen_recording(),
        "microphone" => return mac::microphone(),
        "automation" => return mac::automation(),
        "notifications" => return notifications_status(),
        _ => {}
    }
    let _ = name;
    "unknown"
}

fn snapshot() -> BTreeMap<&'static str, &'static str> {
    NAMES.iter().map(|n| (*n, status_of(n))).collect()
}

static SEEN: std::sync::Mutex<BTreeMap<&'static str, &'static str>> = std::sync::Mutex::new(BTreeMap::new());

/// A permission the founder turned on since it was last seen off (this session) counts as a grant.
fn note_grants(now: &BTreeMap<&'static str, &'static str>) {
    let mut seen = SEEN.lock().unwrap_or_else(|e| e.into_inner());
    for (name, status) in now {
        if *status == "granted" && seen.get(name).is_some_and(|before| *before != "granted") {
            crate::shell_events::record("activate", "permissions_granted", None);
        }
        seen.insert(*name, *status);
    }
}

#[tauri::command]
pub async fn permissions_status(window: tauri::WebviewWindow, state: State<'_, AppState>)
    -> Result<BTreeMap<&'static str, &'static str>, String> {
    require_native_account_window(&window, &state)?;
    let now = tauri::async_runtime::spawn_blocking(snapshot).await.map_err(|_| "permissions_unavailable".to_string())?;
    note_grants(&now);
    Ok(now)
}

/// Shows the system prompt where macOS allows one and returns the fresh snapshot.
/// Once the user has answered, macOS never re-prompts; the UI then opens Settings.
#[tauri::command]
pub async fn permissions_request(window: tauri::WebviewWindow, state: State<'_, AppState>, name: String)
    -> Result<BTreeMap<&'static str, &'static str>, String> {
    require_native_account_window(&window, &state)?;
    if pane(&name).is_none() { return Err("permission_name_invalid".into()); }
    let now = tauri::async_runtime::spawn_blocking(move || {
        #[cfg(target_os = "macos")]
        match name.as_str() {
            "accessibility" => mac::request_accessibility(),
            "screen-recording" => mac::request_screen_recording(),
            "microphone" => mac::request_microphone(),
            "automation" => mac::request_automation(),
            "notifications" if notifications_status() == "not_determined" => {
                let _ = mac_usernotifications::blocking::request_auth();
            }
            _ => {}
        }
        #[cfg(not(target_os = "macos"))]
        let _ = name;
        snapshot()
    }).await.map_err(|_| "permissions_unavailable".to_string())?;
    note_grants(&now);
    Ok(now)
}

#[tauri::command]
pub fn permissions_open_settings(window: tauri::WebviewWindow, state: State<'_, AppState>, name: String)
    -> Result<(), String> {
    require_native_account_window(&window, &state)?;
    match pane(&name).ok_or_else(|| "permission_name_invalid".to_string())? {
        "notifications" => { crate::perms::open_url("x-apple.systempreferences:com.apple.Notifications-Settings.extension"); }
        anchor => crate::perms::open_privacy_pane(anchor),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    #[test]
    fn every_permission_has_a_pane_and_a_status() {
        for n in super::NAMES {
            assert!(super::pane(n).is_some());
            assert!(["granted", "denied", "not_determined", "unknown"].contains(&super::status_of(n)));
        }
        assert!(super::pane("camera").is_none());
    }
}
