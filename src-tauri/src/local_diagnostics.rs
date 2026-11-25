pub(crate) fn redact_startup_line(line: &str) -> String {
    if let Ok(serde_json::Value::Object(record)) = serde_json::from_str(line) {
        let level = record.get("level").and_then(|value| value.as_str()).unwrap_or("INFO");
        let service = record.get("service").and_then(|value| value.as_str()).unwrap_or("");
        let message = record.get("msg").and_then(|value| value.as_str()).unwrap_or("");
        if matches!(message, "gtm_source_read" | "gtm_source_read_failed") {
            let extra = record.get("extra").unwrap_or(&serde_json::Value::Null);
            let host = extra["source_host"].as_str().filter(|host| host.len() <= 253 && host.contains('.')
                && host.split('.').all(|label| !label.is_empty() && label.len() <= 63
                    && label.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')))
                .unwrap_or("[host hidden]");
            if message == "gtm_source_read" { return format!("Customer search fetch: {host} read"); }
            let reason = extra["source_reason"].as_str().filter(|reason| !reason.is_empty() && reason.len() <= 48
                && reason.bytes().all(|byte| byte.is_ascii_alphabetic() || b" -()".contains(&byte))).unwrap_or("unknown");
            let status = extra["diagnostics"]["http_status"].as_u64().filter(|status| (100..=599).contains(status));
            return if let Some(status) = status { format!("Customer search fetch: {host} failed ({status}, {reason})") }
                else { format!("Customer search fetch: {host} failed ({reason})") };
        }
        let mut safe = redact_text(&format!("{level}: {service} {message}"), true);
        if let Some(exception) = record.get("exc").and_then(|value| value.as_str()) {
            for detail in exception.lines() { safe.push_str(&format!("\n{}", redact_text(detail, false))); }
        }
        return safe;
    }
    redact_text(line, false)
}

fn redact_text(line: &str, classified: bool) -> String {
    let lower = line.to_ascii_lowercase();
    if line.trim().is_empty() { return String::new(); }
    let module_name = |module: &str| !module.is_empty() && module.split('.').all(|part| !part.is_empty()
        && part.bytes().all(|character| character.is_ascii_alphanumeric() || character == b'_'));
    let imported_module = line.trim().strip_prefix("from ").and_then(|rest| rest.split_once(" import "))
        .map(|(module, _)| module).or_else(|| line.trim().strip_prefix("import ")?.split_whitespace().next())
        .filter(|module| module_name(module));
    if [
        "Checking your saved data before opening your app.",
        "Saving your automatic backup before updating your data.",
        "Updating your saved data. Your automatic backup is ready.",
        "Your saved data is ready.",
        "Your saved data is ready. Opening your app.",
        "Your automatic backup was restored after an interrupted update.",
        "Update your app before opening this data folder. Your saved work has not been changed.",
        "Your data update could not finish. Your automatic backup was restored. Check free disk space and your data-folder permissions, then reopen your app to retry.",
        "Your data folder is already open. Close your other app window, then reopen this app.",
        "Stop your app and worker, then retry your restore or upgrade.",
        "Your interrupted upgrade could not be restored. Keep your backup folder, close your app, and run the restore command with your latest automatic backup before reopening your app.",
        "Your saved data could not be prepared. Check free disk space and your data-folder permissions, then reopen your app.",
        "Preparing your saved data took too long. Your automatic backup was restored. Check free disk space and your data-folder permissions. Reopen your app to retry.",
        "Preparing your saved data took too long. Your saved work has not been changed. Check free disk space and your data-folder permissions. Reopen your app to retry.",
        "Copying your saved data took too long. Close your other app window, check free disk space, then retry.",
        "Your database could not be verified. Keep your data and choose a healthy backup.",
        "Choose a backup folder outside your data directory, then retry.",
        "Your backup settings could not be opened. Restore your saved settings or choose your backup folder again, then retry.",
        "Your data directory contains a link or special file. Replace it with a regular file, then retry.",
        "Put your SQLite file and saved settings inside your data directory, then retry.",
        "Your data-folder record could not be verified. Stop your app, then restore an intact full backup with python -m app.cli_local restore <backup-file>.",
        "Your saved file location leaves your data folder. Stop your app and worker, then restore an intact full backup with python -m app.cli_local restore <backup-file>.",
    ].contains(&line.trim()) { return line.trim().to_string(); }
    let diagnostic = ["dyld", "library not loaded:", "referenced from:", "reason:", "dlopen(", "image not found", "symbol not found",
        "cannot open shared object file", "no module named", "no such file or directory", "permission denied", "address already in use",
        "traceback (most recent call last):", "file \"", "error:", "exception:", "warning", "info:"];
    if !classified && line.trim() != "ModuleNotFoundError" && imported_module.is_none() && !diagnostic.iter().any(|marker| lower.contains(marker)) {
        return "[Private diagnostic detail hidden.]".into();
    }
    let credential = ["api_key", "api key", "private key", "password", "secret", "token", "authorization", "credential", "cookie", "access_key"]
        .iter().flat_map(|name| lower.match_indices(name).filter_map(|(offset, matched)| {
            let suffix = &lower[offset + matched.len()..];
            let after = suffix.trim_start_matches(['\'', '"', ' ']);
            (after.starts_with(['=', ':']) || suffix.starts_with(char::is_whitespace))
                .then(|| line[..offset].rfind(|character: char| character.is_whitespace() || "([{,".contains(character)).map_or(0, |start| start + 1))
        })).min();
    if let Some(offset) = credential {
        return format!("{}[Private connection detail hidden.]", redact_text(&line[..offset], classified));
    }
    if let Some(module) = imported_module {
        return format!("Python import: {module}");
    }
    let missing_module = line.split_once("No module named '").and_then(|(_, rest)| rest.split_once('\''))
        .map(|(module, _)| module).filter(|module| module_name(module));
    let mut hide_next = false;
    line.split_inclusive(char::is_whitespace).map(|word| {
        let value = word.trim_matches(|character: char| character.is_whitespace() || "\"'(),;".contains(character));
        let lower = value.to_ascii_lowercase();
        let opaque = value.len() >= 20 && value.chars().all(|character| character.is_ascii_alphanumeric() || "_-+/=".contains(character));
        let private = hide_next || opaque && Some(value) != missing_module || value.contains("://") || value.contains('@') && !value.starts_with("@rpath/")
            || value.starts_with("sk-")
            || value.starts_with("/Users/") || value.starts_with("/home/") || value.contains("\\Users\\");
        hide_next = lower == "bearer";
        if private { "[Private connection detail hidden.] ".to_string() } else { word.to_string() }
    }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_exact_maintenance_messages_survive_redaction() {
        let message = "Saving your automatic backup before updating your data.";
        assert_eq!(redact_startup_line(message), message);
        for domain in ["warehouse.example", "hiking.example"] {
            assert_eq!(redact_startup_line(&format!("{message} {domain}")), "[Private diagnostic detail hidden.]");
        }
    }
    #[test]
    fn exceptions_keep_type_module_and_message_without_personal_or_connection_data() {
        for domain in ["inventory.example", "cycling.example"] {
            let safe = redact_startup_line(&format!("RuntimeError: app.core.providers_local could not connect to https://user:secret@{domain} password=hidden /Users/demo-owner/private/file.py"));
            assert!(safe.contains("RuntimeError: app.core.providers_local could not connect"));
            assert!(!safe.contains(domain));
            assert!(!safe.contains("password=hidden"));
            assert!(!safe.contains("/Users/demo-owner"));
        }
    }
    #[test]
    fn structured_python_logs_keep_diagnostics_without_owner_fields() {
        for domain in ["inventory.example", "cycling.example"] {
            let record = serde_json::json!({"level": "WARNING", "service": "app.core.local_secrets", "msg": "Could not open saved settings",
                "subject_hash": domain, "extra": {"password": "test-only-private"}, "exc": "RuntimeError: app.core.providers_local could not decrypt saved settings"});
            let safe = redact_startup_line(&record.to_string());
            assert!(safe.contains("WARNING: app.core.local_secrets Could not open saved settings"));
            assert!(safe.contains("RuntimeError: app.core.providers_local could not decrypt saved settings"));
            assert!(!safe.contains(domain));
            assert!(!safe.contains("test-only-private"));
        }
    }
    #[test]
    fn search_fetch_logs_show_the_public_host_status_and_reason_only() {
        for domain in ["inventory.example", "cycling.example"] {
            let record = serde_json::json!({"level": "WARNING", "msg": "gtm_source_read_failed", "extra": {
                "source_host": domain, "source_reason": "page not found", "source_url": "https://owner@example.invalid/private",
                "diagnostics": {"http_status": 404, "message": "private detail"}}, "exc": "Error: /Users/owner/private"});
            assert_eq!(redact_startup_line(&record.to_string()),
                format!("Customer search fetch: {domain} failed (404, page not found)"));
            let success = serde_json::json!({"level": "INFO", "msg": "gtm_source_read", "extra": {"source_host": domain}});
            assert_eq!(redact_startup_line(&success.to_string()), format!("Customer search fetch: {domain} read"));
            let mut invalid = record;
            invalid["extra"]["source_host"] = serde_json::json!("owner@example.invalid/private");
            invalid["extra"]["source_reason"] = serde_json::json!("owner@example.invalid/private");
            assert_eq!(redact_startup_line(&invalid.to_string()), "Customer search fetch: [host hidden] failed (404, unknown)");
        }
    }
    #[test]
    fn quoted_and_prefixed_credentials_never_leak_after_the_diagnostic() {
        for domain in ["inventory.example", "cycling.example"] {
            for name in ["smtp_password", "secret_access_key", "api key", "authorization"] {
                let safe = redact_startup_line(&format!("RuntimeError: app.core.local_secrets could not connect {name} = '{domain} private credential'"));
                assert!(safe.contains("RuntimeError: app.core.local_secrets could not connect"));
                assert!(!safe.contains(domain));
                assert!(!safe.contains("private credential"));
            }
        }
    }
}
