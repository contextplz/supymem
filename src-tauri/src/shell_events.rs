//! Bounded on-disk ring of closed native usage events (tray, mascot, menu, updater, permissions, Mac task failures).
//!
//! The native shell keeps running while the Console is signed out, offline or not loaded yet, so events are
//! appended here and the signed-in Console drains them through `drain_native_events`, then sends them on the
//! normal telemetry path (`desktop_api_request` -> `/api/telemetry`). Only a closed control or command id, a
//! kind, optional outcome and closed runtime phase/reason/timings are stored:
//! no subject, account, venture, path, URL or text.

use serde_json::{json, Value};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

/// Oldest entries are dropped first once the ring is full.
pub const MAX_EVENTS: usize = 200;
const FILE: &str = "native_usage_ring.jsonl";

static DIR: OnceLock<PathBuf> = OnceLock::new();
// One writer at a time; the tray, mascot, updater and permission code run on different threads.
static LOCK: Mutex<()> = Mutex::new(());

pub fn init(data_dir: PathBuf) {
    let _ = DIR.set(data_dir);
}

/// Record one event. `kind` is `activate`, `outcome` (with `ok`/`failed`) or `error` (a failed native command).
pub fn record(kind: &str, id: &str, outcome: Option<&str>) {
    if let Some(dir) = DIR.get() {
        append(dir, kind, id, outcome);
    }
}

pub fn record_runtime(id: &str, diagnostic: &crate::error_capture::RuntimeDiagnostic) {
    if let Some(dir) = DIR.get() {
        append_inner(dir, "error", id, None, None, Some(diagnostic));
    }
}

pub fn record_failure(id: &str, reason: &str, diagnostic: Option<&crate::error_capture::RuntimeDiagnostic>) {
    if let Some(dir) = DIR.get() {
        append_inner(dir, "error", id, None, Some(reason), diagnostic);
    }
}

fn closed(value: &str) -> bool {
    !value.is_empty() && value.len() <= 40 && value.starts_with(|c: char| c.is_ascii_lowercase())
        && value.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
}

pub fn append(dir: &Path, kind: &str, id: &str, outcome: Option<&str>) {
    append_inner(dir, kind, id, outcome, None, None);
}

fn append_inner(dir: &Path, kind: &str, id: &str, outcome: Option<&str>,
                reason: Option<&str>, diagnostic: Option<&crate::error_capture::RuntimeDiagnostic>) {
    if !matches!(kind, "activate" | "outcome" | "error") || !closed(id)
        || outcome.is_some_and(|o| !matches!(o, "ok" | "failed")) {
        return;
    }
    let _guard = LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let _ = fs::create_dir_all(dir);
    let path = dir.join(FILE);
    let ts = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    let mut row = json!({ "kind": kind, "id": id, "outcome": outcome, "t": ts });
    if let Some(reason) = reason.filter(|reason| closed(reason)) { row["reason"] = json!(reason); }
    if let Some(diagnostic) = diagnostic {
        row["runtime_diagnostic"] = json!(diagnostic);
    }
    let line = row.to_string();
    let mut lines: Vec<String> = fs::read_to_string(&path).unwrap_or_default().lines().map(str::to_owned).collect();
    lines.push(line);
    let excess = lines.len().saturating_sub(MAX_EVENTS);
    let mut body = lines.split_off(excess).join("\n");
    body.push('\n');
    let mut options = fs::OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    if let Ok(mut file) = options.open(path) {
        let _ = file.write_all(body.as_bytes());
    }
}

/// Take every stored event and empty the ring. A crash between taking and sending loses those events, which is
/// acceptable for usage counts (an error is never lost twice: its source still logs).
pub fn drain(dir: &Path) -> Vec<Value> {
    let _guard = LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let path = dir.join(FILE);
    let text = fs::read_to_string(&path).unwrap_or_default();
    if !text.is_empty() {
        let _ = fs::remove_file(&path);
    }
    text.lines().filter_map(|line| serde_json::from_str(line).ok()).collect()
}

pub fn drain_default() -> Vec<Value> {
    DIR.get().map(|dir| drain(dir)).unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn runtime_failure_round_trips_once_alongside_legacy_events() {
        let dir = temp();
        let diagnostic = crate::error_capture::RuntimeDiagnostic::sidecar("sidecar_health",
            "sidecar_health_timeout", std::time::Duration::from_millis(25)).unwrap();
        append_inner(&dir, "error", "shell_sidecar_health", None, Some("sidecar_health_timeout"), Some(&diagnostic));
        append(&dir, "error", "shell_sidecar_spawn", None);
        let events = drain(&dir);
        assert_eq!(events[0]["runtime_diagnostic"]["phase_ms"], 25);
        assert_eq!(events[0]["reason"], "sidecar_health_timeout");
        assert!(events[1].get("runtime_diagnostic").is_none());
        assert!(drain(&dir).is_empty());
        let _ = fs::remove_dir_all(dir);
    }

    fn temp() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("cos-ring-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn ring_keeps_only_the_newest_events_and_drains_once() {
        let dir = temp();
        for i in 0..(MAX_EVENTS + 25) {
            append(&dir, "activate", &format!("tray_{i}"), None);
        }
        let events = drain(&dir);
        assert_eq!(events.len(), MAX_EVENTS);
        assert_eq!(events[0]["id"], "tray_25");
        assert_eq!(events[MAX_EVENTS - 1]["id"], format!("tray_{}", MAX_EVENTS + 24));
        assert!(drain(&dir).is_empty());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn only_closed_ids_and_kinds_are_stored() {
        let dir = temp();
        append(&dir, "activate", "tray_show", None);
        append(&dir, "outcome", "updater_check", Some("failed"));
        append(&dir, "activate", "Has Spaces", None);
        append(&dir, "activate", "/Users/someone/file", None);
        append(&dir, "click", "tray_show", None);
        append(&dir, "outcome", "tray_pause", Some("maybe"));
        let events = drain(&dir);
        assert_eq!(events.len(), 2);
        assert_eq!(events[1]["outcome"], "failed");
        assert!(events.iter().all(|e| e.as_object().unwrap().keys().all(|k| ["kind", "id", "outcome", "t"].contains(&k.as_str()))));
        let _ = fs::remove_dir_all(&dir);
    }
}
