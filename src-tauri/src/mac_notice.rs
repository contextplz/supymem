//! Native macOS notice delivery via UNUserNotificationCenter.
//!
//! Bypasses notify-rust's `Notification::send_blocking` → `block_on_current`
//! path, which gated delivery on `CFRunLoop::is_waiting()` and rejected every
//! send from the background poll thread while the UI was busy (or during
//! setup). Ack channels for native and in-app are tracked independently.

use std::sync::{Mutex, OnceLock};

use serde::Serialize;

/// Permission preparation is nonsensitive and may wait for the user. Account
/// admission belongs in `dispatch`, after preparation, immediately around the
/// local OS registration. Preparation failures must never invoke dispatch.
pub(crate) fn prepare_then_dispatch<P, R>(prepare: impl FnOnce() -> Result<P, &'static str>,
                                         dispatch: impl FnOnce(P) -> Result<R, &'static str>) -> Result<R, &'static str> {
    let prepared = prepare()?;
    dispatch(prepared)
}

/// Terminal or in-flight delivery status for one notice channel.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ChannelStatus {
    Pending,
    Shown,
    Failed(String),
}

impl ChannelStatus {
    pub fn from_result(result: Result<(), &'static str>) -> Self {
        match result {
            Ok(()) => Self::Shown,
            Err("account_unavailable") => Self::Pending,
            Err(reason) => Self::Failed(reason.to_string()),
        }
    }

    pub fn as_ack_token(&self) -> Option<String> {
        match self {
            Self::Pending => None,
            Self::Shown => Some("native_shown".into()),
            Self::Failed(reason) => Some(format!("native_failed:{reason}")),
        }
    }

    pub fn in_app_token(&self) -> Option<&'static str> {
        match self {
            Self::Pending => None,
            Self::Shown => Some("in_app_shown"),
            Self::Failed(_) => Some("in_app_failed"),
        }
    }

    pub fn is_terminal(&self) -> bool {
        !matches!(self, Self::Pending)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NoticeDelivery {
    pub in_app: ChannelStatus,
    pub native: ChannelStatus,
}

impl Default for NoticeDelivery {
    fn default() -> Self {
        Self {
            in_app: ChannelStatus::Pending,
            native: ChannelStatus::Pending,
        }
    }
}

impl NoticeDelivery {
    /// Transport acceptance alone does not establish a committed banner.
    pub fn record_emitted(&mut self, emitted: Option<bool>) {
        if emitted == Some(false) { self.in_app = ChannelStatus::Failed("emit".into()); }
    }

    /// Record an in-app emit outcome. Does not clear or mask native status.
    pub fn record_in_app(&mut self, ok: bool) {
        self.in_app = if ok {
            ChannelStatus::Shown
        } else {
            ChannelStatus::Failed("emit".into())
        };
    }

    /// Record a native delivery outcome with a stable reason code (no content).
    pub fn record_native(&mut self, result: Result<(), &'static str>) {
        self.native = ChannelStatus::from_result(result);
    }

    /// Fully settled when both channels have a terminal status.
    pub fn settled(&self) -> bool {
        self.in_app.is_terminal() && self.native.is_terminal()
    }

    /// Ack payload tokens for the bridge (only terminal channels).
    pub fn ack_tokens(&self) -> (Option<&'static str>, Option<String>) {
        (self.in_app.in_app_token(), self.native.as_ack_token())
    }
}

/// Last native diagnostic observed by the shell (for Settings).
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct NativeNoticeDiagnostic {
    pub status: String,
    pub reason: Option<String>,
}

impl NativeNoticeDiagnostic {
    pub fn working() -> Self {
        Self {
            status: "working".into(),
            reason: None,
        }
    }

    pub fn blocked() -> Self {
        Self {
            status: "blocked_in_system_settings".into(),
            reason: Some("denied".into()),
        }
    }

    pub fn failed(reason: &str) -> Self {
        Self {
            status: "failed".into(),
            reason: Some(reason.to_string()),
        }
    }

    pub fn line(&self) -> String {
        match self.status.as_str() {
            "working" => "Mac notifications: working".into(),
            "blocked_in_system_settings" => {
                "Mac notifications: blocked in System Settings".into()
            }
            _ => format!(
                "Mac notifications: failed ({})",
                self.reason.as_deref().unwrap_or("unknown")
            ),
        }
    }
}

static LAST_DIAGNOSTIC: OnceLock<Mutex<NativeNoticeDiagnostic>> = OnceLock::new();

fn diagnostic_slot() -> &'static Mutex<NativeNoticeDiagnostic> {
    LAST_DIAGNOSTIC.get_or_init(|| {
        Mutex::new(NativeNoticeDiagnostic {
            status: "unknown".into(),
            reason: None,
        })
    })
}

pub fn set_diagnostic(value: NativeNoticeDiagnostic) {
    if let Ok(mut guard) = diagnostic_slot().lock() {
        *guard = value;
    }
}

pub fn current_diagnostic() -> NativeNoticeDiagnostic {
    diagnostic_slot()
        .lock()
        .map(|g| g.clone())
        .unwrap_or(NativeNoticeDiagnostic {
            status: "unknown".into(),
            reason: None,
        })
}

/// Map an authorization / delivery error into a stable reason code (no content).
pub fn native_fail_reason(kind: &str) -> &'static str {
    match kind {
        "denied" | "blocked" => "denied",
        "no_bundle" => "no_bundle",
        "rejected" => "rejected",
        "main_thread" => "main_thread",
        "unauthorized" => "unauthorized",
        _ => "error",
    }
}

#[cfg(target_os = "macos")]
pub mod macos {
    use super::*;
    use mac_usernotifications::{
        AuthorizationStatus, Notification, check_bundle, install_presenting_delegate,
    };
    use std::thread;
    use tauri::{AppHandle, Manager};

    pub fn warm(app: &AppHandle) {
        // A `tauri dev` executable has no app bundle. Check that before
        // touching UNUserNotificationCenter; macOS throws an Objective-C
        // exception rather than returning an error for this case.
        match check_bundle() {
            Ok(()) => {}
            Err(_) => {
                set_diagnostic(NativeNoticeDiagnostic::failed("no_bundle"));
                return;
            }
        }
        // setup() is on the main thread; install before worker dispatch. Never
        // request notification authorization synchronously here: macOS can
        // wait for a person to answer while the main window has not rendered.
        install_presenting_delegate();
        thread::spawn(|| {
            match mac_usernotifications::blocking::get_notification_settings()
                .map(|settings| settings.authorization_status)
            {
                Ok(AuthorizationStatus::Authorized | AuthorizationStatus::Provisional
                    | AuthorizationStatus::Ephemeral) =>
                    set_diagnostic(NativeNoticeDiagnostic::working()),
                Ok(AuthorizationStatus::Denied) =>
                    set_diagnostic(NativeNoticeDiagnostic::blocked()),
                Ok(AuthorizationStatus::NotDetermined | AuthorizationStatus::Unknown) | Err(_) =>
                    set_diagnostic(NativeNoticeDiagnostic::failed("not_requested")),
            }
        });
        let _ = app; // retained for future main-thread scheduling hooks
    }

    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub enum Permission {
        Granted,
        Denied,
        Unknown,
    }

    pub fn permission_state() -> Permission {
        match mac_usernotifications::blocking::get_notification_settings()
            .map(|settings| settings.authorization_status)
        {
            Ok(AuthorizationStatus::Authorized
            | AuthorizationStatus::Provisional
            | AuthorizationStatus::Ephemeral) => Permission::Granted,
            Ok(AuthorizationStatus::NotDetermined) => {
                match mac_usernotifications::blocking::request_auth() {
                    Ok(true) => Permission::Granted,
                    Ok(false) => Permission::Denied,
                    Err(_) => Permission::Unknown,
                }
            }
            Ok(AuthorizationStatus::Denied | AuthorizationStatus::Unknown) => Permission::Denied,
            Err(_) => Permission::Unknown,
        }
    }

    pub struct PreparedDelivery(());

    /// Nonsensitive permission preparation must not hold an account lease.
    pub fn prepare_delivery() -> Result<PreparedDelivery, &'static str> {
        check_bundle().map_err(|_| native_fail_reason("no_bundle"))?;
        match permission_state() {
            Permission::Granted => {}
            Permission::Denied => {
                set_diagnostic(NativeNoticeDiagnostic::blocked());
                return Err(native_fail_reason("denied"));
            }
            Permission::Unknown => {
                set_diagnostic(NativeNoticeDiagnostic::failed("unauthorized"));
                return Err(native_fail_reason("unauthorized"));
            }
        }
        // Ensure delegate is present even if warm raced.
        install_presenting_delegate();
        Ok(PreparedDelivery(()))
    }

    /// Only OS registration happens here. The caller holds the exact account
    /// lease for private notices; permission dialogs have already completed.
    pub fn deliver_prepared(app: &AppHandle, id: &str, title: &str, body: &str,
                            target: Option<url::Url>, custody: Option<crate::NoticeCustody>,
                            _prepared: PreparedDelivery) -> Result<(), &'static str> {

        let notification = Notification::new()
            .title(title)
            .message(body)
            .id(id)
            .sound(mac_usernotifications::sound::DEFAULT);

        let handle = notification
            .send_blocking()
            .map_err(|err| {
                let reason = match &err {
                    mac_usernotifications::Error::NoBundleIdentifier => "no_bundle",
                    mac_usernotifications::Error::NotificationRejected => "rejected",
                    mac_usernotifications::Error::MainThreadNotRunning => "main_thread",
                    _ => "error",
                };
                log::warn!("native_notice_delivery_failed reason={reason}");
                set_diagnostic(NativeNoticeDiagnostic::failed(reason));
                native_fail_reason(reason)
            })?;

        log::info!("native_notice_delivery_ok reason=accepted");
        set_diagnostic(NativeNoticeDiagnostic::working());

        if let Some((target, custody)) = target.zip(custody) {
            let app = app.clone();
            thread::spawn(move || {
                // Response callbacks arrive on the main run loop; Tauri pumps it.
                let response = mac_usernotifications::block_on_current(handle.response());
                if let Ok(Ok(resp)) = response {
                    if resp.is_default_action() {
                        // Queueing is not admission: the user can sign out
                        // before this main-thread callback actually runs.
                        let handle = app.clone();
                        let _ = app.run_on_main_thread(move || {
                            let _ = crate::with_notice_custody(&handle, &custody, || {
                                if let Some(window) = handle.get_webview_window("main") {
                                    let _ = window.navigate(target);
                                    let _ = window.unminimize();
                                    let _ = window.show();
                                    let _ = window.set_focus();
                                }
                            });
                        });
                    }
                }
            });
        }
        Ok(())
    }

    /// Generic app/test notices contain no account-owned navigation target.
    pub fn deliver(app: &AppHandle, id: &str, title: &str, body: &str,
                   target: Option<url::Url>) -> Result<(), &'static str> {
        let prepared = prepare_delivery()?;
        deliver_prepared(app, id, title, body, target, None, prepared)
    }

    pub fn deliver_test(app: &AppHandle) -> Result<(), String> {
        deliver(
            app,
            &format!("not_test_{}", uuid::Uuid::new_v4().as_simple()),
            "Supymem test notification",
            "If you see this banner and a Notification Center entry, Mac notices are working.",
            None,
        )
        .map_err(|reason| reason.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn in_app_success_does_not_settle_without_native() {
        let mut state = NoticeDelivery::default();
        state.record_in_app(true);
        assert!(!state.settled());
        assert_eq!(state.ack_tokens().0, Some("in_app_shown"));
        assert!(state.ack_tokens().1.is_none());
    }

    #[test]
    fn native_failure_is_recorded_independently() {
        let mut state = NoticeDelivery::default();
        state.record_in_app(true);
        state.record_native(Err("main_thread"));
        assert!(state.settled());
        assert_eq!(
            state.ack_tokens().1.as_deref(),
            Some("native_failed:main_thread")
        );
    }

    #[test]
    fn native_success_acks_shown() {
        let mut state = NoticeDelivery::default();
        state.record_native(Ok(()));
        state.record_in_app(true);
        assert!(state.settled());
        assert_eq!(state.ack_tokens().1.as_deref(), Some("native_shown"));
    }

    #[test]
    fn diagnostic_lines_are_founder_facing() {
        assert_eq!(
            NativeNoticeDiagnostic::working().line(),
            "Mac notifications: working"
        );
        assert_eq!(
            NativeNoticeDiagnostic::blocked().line(),
            "Mac notifications: blocked in System Settings"
        );
        assert_eq!(
            NativeNoticeDiagnostic::failed("main_thread").line(),
            "Mac notifications: failed (main_thread)"
        );
    }

    #[test]
    fn or_ack_masking_is_rejected_by_settled_rule() {
        // Historical bug: shell acked when in_app OR native succeeded.
        // Settled requires both channels terminal; in_app alone must not clear.
        let mut state = NoticeDelivery::default();
        state.record_in_app(true);
        let legacy_would_ack = matches!(state.in_app, ChannelStatus::Shown)
            || matches!(state.native, ChannelStatus::Shown);
        assert!(legacy_would_ack);
        assert!(!state.settled());
    }
}
