//! One capture point for native-shell failures: a rotating local log (`~/Library/Logs/Supymem/desktop.log`)
//! plus a closed `error` entry in the shell-events ring, which the signed-in Console drains and sends as
//! `ui.error` (see `apps/console/src/lib/nativeShellEvents.ts`). The ids below are the whole vocabulary; the
//! Console holds the same table. No message text, path, URL or identity is ever recorded.

use serde_json::{json, Value};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use serde::Serialize;

/// (event id, source, intent). `source` is the failing part, `intent` is what the app was trying to do.
pub const EVENTS: &[(&str, &str, &str)] = &[
    ("shell_panic", "desktop_shell", "run_app"),
    ("shell_prev_crash", "desktop_shell", "previous_session"),
    ("shell_console_unreachable", "desktop_webview", "load_console"),
    ("shell_start_failed", "desktop_shell", "start_app"),
    ("shell_sidecar_spawn", "desktop_sidecar", "start_bridge"),
    ("shell_sidecar_exit", "desktop_sidecar", "run_bridge"),
    ("shell_sidecar_health", "desktop_sidecar", "start_bridge"),
    ("shell_updater", "desktop_updater", "update_app"),
];

const LOG_MAX_BYTES: u64 = 1_000_000;
static LOG_LOCK: Mutex<()> = Mutex::new(());
// ponytail: dedupe lasts one app session, persist reason keys if restart storms need suppression.
static SIDECAR_REPORTS: std::sync::LazyLock<Mutex<std::collections::HashSet<(String, String)>>> =
    std::sync::LazyLock::new(|| Mutex::new(std::collections::HashSet::new()));

#[derive(Debug, Serialize)]
pub struct RuntimeDiagnostic {
    phase: &'static str,
    reason: &'static str,
    phase_ms: u64,
    elapsed_ms: u64,
}

impl RuntimeDiagnostic {
    pub fn sidecar(phase: &str, reason: &str, elapsed: Duration) -> Option<Self> {
        Self::sidecar_timed(phase, reason, elapsed, elapsed)
    }

    fn sidecar_timed(phase: &str, reason: &str, elapsed: Duration, phase_elapsed: Duration) -> Option<Self> {
        let (phase, reason) = match (phase, reason) {
            ("sidecar_start", "sidecar_spawn_failed") => ("sidecar_start", "sidecar_spawn_failed"),
            ("sidecar_start", "sidecar_port_conflict") => ("sidecar_start", "sidecar_port_conflict"),
            ("sidecar_start", "sidecar_binary_missing") => ("sidecar_start", "sidecar_binary_missing"),
            ("sidecar_start", "sidecar_permission_denied") => ("sidecar_start", "sidecar_permission_denied"),
            ("sidecar_start", "sidecar_startup_exited") => ("sidecar_start", "sidecar_startup_exited"),
            ("sidecar_health", "sidecar_health_timeout") => ("sidecar_health", "sidecar_health_timeout"),
            ("sidecar_health", "sidecar_health_unavailable") => ("sidecar_health", "sidecar_health_unavailable"),
            ("sidecar_health", "sidecar_protocol_mismatch") => ("sidecar_health", "sidecar_protocol_mismatch"),
            ("sidecar_run", "sidecar_exited") => ("sidecar_run", "sidecar_exited"),
            _ => return None,
        };
        let ms = elapsed.as_millis().min(86_400_000) as u64;
        Some(Self { phase, reason, phase_ms: phase_elapsed.as_millis().min(ms as u128) as u64, elapsed_ms: ms })
    }
}

pub fn log_dir() -> Option<PathBuf> {
    dirs::home_dir().map(|home| home.join("Library").join("Logs").join("Supymem"))
}

/// The standard event shape, one JSON object per line. Unknown ids are reported as `shell_other`.
pub fn format_line(id: &str, app_version: &str, ts: u64) -> Value {
    let (id, source, intent) = EVENTS.iter().find(|e| e.0 == id).copied().unwrap_or(("shell_other", "desktop_shell", "unknown"));
    json!({
        "component": "desktop_shell", "error_event": id, "source": source, "intent": intent,
        "app_version": app_version, "os": format!("{}-{}", std::env::consts::OS, std::env::consts::ARCH), "t": ts,
    })
}

pub fn append_log(dir: &Path, line: &Value) {
    let _guard = LOG_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let _ = fs::create_dir_all(dir);
    let path = dir.join("desktop.log");
    if fs::metadata(&path).map(|m| m.len() >= LOG_MAX_BYTES).unwrap_or(false) {
        let _ = fs::rename(&path, dir.join("desktop.log.1"));
    }
    if let Ok(mut file) = fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(file, "{line}");
    }
}

/// Log locally and queue for the Console to report. Never panics, never blocks on the network.
pub fn report(id: &str) {
    report_reason(id, "unknown");
}

pub fn report_reason(id: &str, reason: &str) {
    let reason = match (id, reason) {
        ("shell_prev_crash", "panic") => "panic",
        ("shell_prev_crash", _) => "unclean_exit",
        ("shell_start_failed", "console_navigation_failed") => "console_navigation_failed",
        ("shell_start_failed", _) => "supervisor_start_failed",
        ("shell_panic", _) => "panic",
        ("shell_console_unreachable", _) => "console_unreachable",
        ("shell_sidecar_spawn", _) => "sidecar_spawn_failed",
        ("shell_sidecar_health", _) => "sidecar_health_unavailable",
        ("shell_sidecar_exit", _) => "sidecar_exited",
        ("shell_updater", _) => "update_failed",
        _ => "unknown",
    };
    report_inner(id, reason, None);
}

pub fn report_runtime(id: &str, phase: &str, reason: &str, elapsed: Duration) {
    report_runtime_timed(id, phase, reason, elapsed, elapsed);
}

pub fn report_runtime_timed(id: &str, phase: &str, reason: &str, elapsed: Duration, phase_elapsed: Duration) {
    let diagnostic = if matches!((id, phase),
        ("shell_sidecar_spawn", "sidecar_start") | ("shell_sidecar_health", "sidecar_health")
        | ("shell_sidecar_exit", "sidecar_run")) {
        RuntimeDiagnostic::sidecar_timed(phase, reason, elapsed, phase_elapsed)
    } else { None };
    if let Some(diagnostic) = diagnostic.as_ref() {
        report_inner(id, diagnostic.reason, Some(diagnostic));
    } else { report(id); }
}

fn sidecar_report_due(reports: &mut std::collections::HashSet<(String, String)>,
                      id: &str, reason: &str) -> bool {
    reports.insert((id.to_string(), reason.to_string()))
}

fn report_inner(id: &str, reason: &str, diagnostic: Option<&RuntimeDiagnostic>) {
    if matches!(id, "shell_sidecar_spawn" | "shell_sidecar_health" | "shell_sidecar_exit")
        && !sidecar_report_due(&mut SIDECAR_REPORTS.lock().unwrap_or_else(|e| e.into_inner()),
            id, reason) { return; }
    let ts = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    let version = std::env::var("COMPANY_OS_APP_VERSION").unwrap_or_else(|_| env!("SUPYMEM_DESKTOP_VERSION").into());
    let mut line = format_line(id, &version, ts);
    line["reason"] = json!(reason);
    if let Some(diagnostic) = diagnostic {
        line["runtime_diagnostic"] = json!(diagnostic);
    }
    if let Some(dir) = log_dir().filter(|_| !cfg!(test)) {
        append_log(&dir, &line);
    }
    let id = line["error_event"].as_str().unwrap_or("shell_other");
    crate::shell_events::record_failure(id, reason, diagnostic);
}

const MARKER: &str = "session_running.marker";

/// Call once at launch. If the previous run never cleared its marker it crashed or was killed.
pub fn begin_session(data_dir: &Path) {
    let marker = data_dir.join(MARKER);
    if marker.exists() {
        let previous = fs::read(&marker).unwrap_or_default();
        report_reason("shell_prev_crash", if previous == b"panic" { "panic" } else { "unclean_exit" });
    }
    let _ = fs::create_dir_all(data_dir);
    let _ = fs::write(marker, b"1");
}

pub fn mark_panic(data_dir: &Path) {
    let _ = fs::write(data_dir.join(MARKER), b"panic");
}

/// Call on a clean exit only.
pub fn end_session(data_dir: &Path) {
    let _ = fs::remove_file(data_dir.join(MARKER));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn runtime_metadata_is_closed_and_bounded() {
        assert!(RuntimeDiagnostic::sidecar("private_prompt", "sidecar_spawn_failed", Duration::ZERO).is_none());
        assert!(RuntimeDiagnostic::sidecar("sidecar_start", "private_screen_text", Duration::ZERO).is_none());
        assert!(RuntimeDiagnostic::sidecar("sidecar_start", "sidecar_exited", Duration::ZERO).is_none());
        let row = json!(RuntimeDiagnostic::sidecar("sidecar_health", "sidecar_health_timeout", Duration::from_secs(999999)).unwrap());
        assert_eq!(row["phase_ms"], 86_400_000);
        assert_eq!(row.as_object().unwrap().len(), 4);
        let row = json!(RuntimeDiagnostic::sidecar_timed("sidecar_health", "sidecar_health_unavailable",
            Duration::from_secs(46), Duration::ZERO).unwrap());
        assert_eq!(row["phase_ms"], 0);
        assert_eq!(row["elapsed_ms"], 46_000);
        assert!(RuntimeDiagnostic::sidecar("sidecar_start", "sidecar_port_conflict", Duration::ZERO).is_some());
        assert!(RuntimeDiagnostic::sidecar("sidecar_health", "sidecar_port_conflict", Duration::ZERO).is_none());
    }

    #[test]
    fn duplicate_sidecar_reports_are_suppressed_across_callers_but_not_reasons() {
        let mut reports = std::collections::HashSet::new();
        assert!(sidecar_report_due(&mut reports, "shell_sidecar_health", "sidecar_health_timeout"));
        for _ in 0..4 {
            assert!(!sidecar_report_due(&mut reports, "shell_sidecar_health", "sidecar_health_timeout"));
        }
        assert!(sidecar_report_due(&mut reports, "shell_sidecar_health", "sidecar_health_unavailable"));
        assert!(sidecar_report_due(&mut reports, "shell_sidecar_spawn", "sidecar_spawn_failed"));
        assert!(!sidecar_report_due(&mut reports, "shell_sidecar_health", "sidecar_health_timeout"));
    }

    #[test]
    fn every_event_has_the_standard_shape() {
        for (id, source, intent) in EVENTS {
            let line = format_line(id, "0.1.2", 7);
            for key in ["component", "error_event", "source", "intent", "app_version", "os", "t"] {
                assert!(line.get(key).is_some(), "{id} missing {key}");
            }
            assert_eq!((line["source"].as_str(), line["intent"].as_str()), (Some(*source), Some(*intent)));
            assert!(id.len() <= 40 && id.bytes().all(|b| b.is_ascii_lowercase() || b == b'_'));
        }
        assert_eq!(format_line("/Users/me/secret", "1", 0)["error_event"], "shell_other");
    }

    #[test]
    fn log_rotates_and_marker_detects_unclean_exit() {
        let dir = std::env::temp_dir().join(format!("cos-errcap-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("desktop.log"), vec![b'x'; LOG_MAX_BYTES as usize]).unwrap();
        append_log(&dir, &format_line("shell_panic", "1", 1));
        assert!(dir.join("desktop.log.1").exists());
        assert!(fs::read_to_string(dir.join("desktop.log")).unwrap().contains("shell_panic"));
        begin_session(&dir);
        assert!(dir.join(MARKER).exists());
        mark_panic(&dir);
        assert_eq!(fs::read(dir.join(MARKER)).unwrap(), b"panic");
        end_session(&dir);
        assert!(!dir.join(MARKER).exists());
        assert!(!dir.join(MARKER).exists());
        let _ = fs::remove_dir_all(&dir);
    }
}
