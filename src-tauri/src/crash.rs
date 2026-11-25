//! Private local panic records. Upload requires an explicit authenticated report.

use serde_json::json;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

pub fn queue_dir(data_dir: &Path) -> PathBuf {
    data_dir.join("crash_queue")
}

pub fn enqueue_panic(data_dir: &Path, message: &str) {
    let dir = queue_dir(data_dir);
    let _ = fs::create_dir_all(&dir);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&dir, fs::Permissions::from_mode(0o700));
    }
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let path = dir.join(format!("crash-{ts}-{}.json", uuid::Uuid::new_v4()));
    let version = std::env::var("COMPANY_OS_APP_VERSION").unwrap_or_else(|_| env!("SUPYMEM_DESKTOP_VERSION").into());
    // The bridge writes this owner-only marker only after a verified native
    // handoff. Unowned historical records are never uploaded automatically.
    let owner = fs::read(data_dir.join("crash_owner.json"))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<serde_json::Value>(&bytes).ok())
        .filter(|value| value.get("account_id").and_then(|v| v.as_str()).is_some_and(|v| v.starts_with("usr_"))
            && value.get("device_id").and_then(|v| v.as_str()).is_some_and(|v| !v.is_empty() && v.len() <= 128));
    let payload = json!({
        "kind": "crash",
        "app_version": version,
        "os": format!("{}-{}", std::env::consts::OS, std::env::consts::ARCH),
        "occurred_at_unix": ts,
        "stack": message,
        "title": "Desktop panic",
        "body": null,
        "logs": null,
        "contact": null,
        "include_account_id": false,
        "anonymous": true,
        "install_id": null,
        "crash_owner": owner,
        "crash_process": "rust",
    });
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    if let Ok(mut file) = options.open(path) {
        let _ = file.write_all(payload.to_string().as_bytes());
    }
    if let Ok(entries) = fs::read_dir(&dir) {
        let mut files: Vec<_> = entries.flatten().filter(|entry| {
            entry.path().extension().is_some_and(|extension| extension == "json")
        }).collect();
        files.sort_by_key(|entry| entry.metadata().and_then(|meta| meta.modified()).ok());
        let excess = files.len().saturating_sub(50);
        for file in files.into_iter().take(excess) {
            let _ = fs::remove_file(file.path());
        }
    }
}

pub fn install_panic_hook(data_dir: PathBuf) {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        // Panic messages can contain user data or secrets. Store source location
        // only; do not copy the payload into a report automatically.
        let msg = info.location().map(|location| location.to_string())
            .unwrap_or_else(|| "native panic".into());
        enqueue_panic(&data_dir, &msg);
        crate::error_capture::mark_panic(&data_dir);
        crate::error_capture::report("shell_panic");
        if !crate::local_stack::is_local() {
            previous(info);
        }
    }));
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::env;

    #[test]
    fn enqueue_panic_writes_json_without_email() {
        let dir = env::temp_dir().join(format!("cos-crash-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        enqueue_panic(&dir, "panic at update.rs:1");
        let queued = queue_dir(&dir);
        let file = fs::read_dir(&queued)
            .unwrap()
            .next()
            .unwrap()
            .unwrap()
            .path();
        let raw = fs::read_to_string(file).unwrap();
        assert!(raw.contains("\"kind\":\"crash\""));
        assert!(!raw.to_lowercase().contains("email"));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn panic_record_keeps_verified_owner_only_in_private_queue() {
        let dir = env::temp_dir().join(format!("cos-owned-crash-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("crash_owner.json"), r#"{"account_id":"usr_test","device_id":"dev_test"}"#).unwrap();
        enqueue_panic(&dir, "safe source location");
        let path = fs::read_dir(queue_dir(&dir)).unwrap().next().unwrap().unwrap().path();
        let raw: serde_json::Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
        assert_eq!(raw["crash_owner"]["account_id"], "usr_test");
        assert_eq!(raw["crash_owner"]["device_id"], "dev_test");
        assert_eq!(raw["crash_process"], "rust");
        let _ = fs::remove_dir_all(&dir);
    }
}
