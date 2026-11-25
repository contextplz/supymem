use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::Path;
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const LOG_CAP: u64 = 1_000_000;
static RECENT: Mutex<VecDeque<Instant>> = Mutex::new(VecDeque::new());

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
enum Kind {
    RuntimeError,
    PromiseError,
    RenderError,
    BlankRender,
}

#[derive(Deserialize, Serialize)]
struct Source {
    chunk: String,
    line: u32,
    column: u32,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct WebviewRecord {
    kind: Kind,
    route: String,
    error_type: Option<String>,
    source: Option<Source>,
    #[serde(default)]
    pending: Vec<String>,
}

#[tauri::command]
pub(crate) fn append_webview_error(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, super::AppState>,
    record: WebviewRecord,
) -> Result<(), &'static str> {
    let location = window.url().map_err(|_| "webview_log_origin_denied")?;
    if !super::local_stack::is_local() || location.scheme() != "http" ||
        !super::auth::trusted_account_window(window.label(), &location, &state.supervisor.console_url()) {
        return Err("webview_log_origin_denied");
    }
    let mut recent = RECENT.lock().map_err(|_| "webview_log_unavailable")?;
    let now = Instant::now();
    while recent.front().is_some_and(|time| now.duration_since(*time) >= Duration::from_secs(60)) {
        recent.pop_front();
    }
    if recent.len() >= 20 { return Ok(()); }
    recent.push_back(now);
    append(&state.supervisor.data_dir(), &record.closed()).map_err(|_| "webview_log_unavailable")
}

impl WebviewRecord {
    fn closed(self) -> Value {
        let text = match self.kind {
            Kind::RuntimeError => "Runtime failure",
            Kind::PromiseError => "Promise failure",
            Kind::RenderError => "Render failure",
            Kind::BlankRender => "Blank render",
        };
        let mut entry = json!({"kind": self.kind, "route": closed_route(&self.route), "text": text,
            "time_ms": SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64});
        if matches!(self.kind, Kind::BlankRender) {
            entry["pending"] = json!(self.pending.iter().filter(|gate| matches!(gate.as_str(),
                "local_capabilities" | "local_provider" | "venture_load" | "conversation_load" | "workspace_session" | "route_load"
            )).take(6).collect::<Vec<_>>());
        } else {
            entry["error_type"] = json!(self.error_type.as_deref().filter(|value| matches!(*value,
                "Error" | "TypeError" | "ReferenceError" | "SyntaxError" | "RangeError" | "URIError" | "EvalError" | "AggregateError" | "ChunkLoadError"
            )).unwrap_or("Unknown"));
            if let Some(source) = self.source.filter(|source| source.chunk.len() == 16
                && source.chunk.bytes().all(|byte| byte.is_ascii_hexdigit())) {
                entry["source"] = json!(source);
            }
        }
        entry
    }
}

fn closed_route(route: &str) -> &str {
    match route {
        "/" | "/new" | "/ventures" | "/welcome" | "/setup" | "/setup/permissions" | "/account" | "/auth"
        | "/settings" | "/settings/local" | "/settings/local/capabilities" | "/guide" | "/download"
        | "/connections" | "/operator" | "/suggestions/overlay" | "/pagebuilder-lab" | "/lab" | "/marketing"
        | "/gtm" | "/gtm/lab" | "/content" | "/community" | "/media" | "/ads" | "/ops" | "/ops/agents"
        | "/ops/tasks" | "/ops/settings" | "/ops/ads" | "/ops/ai-gtm" | "/ops/outreach" | "/ops/social"
        | "/ops/finance" | "/ops/memory" | "/v/{id}" | "/v/{id}/idea" | "/v/{id}/context"
        | "/v/{id}/research" | "/v/{id}/market" | "/v/{id}/plan" | "/v/{id}/execution" | "/v/{id}/builds"
        | "/v/{id}/build" | "/v/{id}/website" | "/v/{id}/site" | "/v/{id}/analytics" | "/v/{id}/charter"
        | "/v/{id}/tasks" | "/v/{id}/org" | "/v/{id}/people" | "/v/{id}/connections" | "/v/{id}/connect"
        | "/v/{id}/evolution" | "/v/{id}/simulate" | "/v/{id}/go-to-market" | "/v/{id}/gtm" | "/v/{id}/content"
        | "/v/{id}/community" | "/v/{id}/settings" | "/v/{id}/team" | "/v/{id}/feedback" | "/v/{id}/import"
        | "/v/{id}/work" | "/v/{id}/now" | "/v/{id}/leads" | "/v/{id}/linkedin" | "/v/{id}/device-work-orders"
        | "/v/{id}/experiments" | "/v/{id}/suggestions" | "/v/{id}/guide" | "/v/{id}/market-map" => route,
        _ => "/other",
    }
}

fn append(data_dir: &Path, entry: &Value) -> io::Result<()> {
    let line = format!("{entry}\n");
    if line.len() as u64 > LOG_CAP { return Err(io::ErrorKind::InvalidInput.into()); }
    let dir = data_dir.join("logs");
    let mut builder = fs::DirBuilder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    if let Err(error) = builder.create(&dir) {
        if error.kind() != io::ErrorKind::AlreadyExists { return Err(error); }
    }
    let metadata = fs::symlink_metadata(&dir)?;
    if !metadata.is_dir() || !private(&metadata) { return Err(io::ErrorKind::PermissionDenied.into()); }
    let path = dir.join("webview-errors.jsonl");
    let mut file = open_log(&path)?;
    if file.metadata()?.len().saturating_add(line.len() as u64) > LOG_CAP {
        drop(file);
        fs::rename(&path, dir.join("webview-errors.jsonl.1"))?;
        file = open_log(&path)?;
    }
    file.write_all(line.as_bytes())
}

fn open_log(path: &Path) -> io::Result<File> {
    let mut options = OpenOptions::new();
    options.append(true).create(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.custom_flags(0x00200000);
    }
    let file = options.open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || !private(&metadata) { return Err(io::ErrorKind::PermissionDenied.into()); }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if metadata.nlink() != 1 { return Err(io::ErrorKind::PermissionDenied.into()); }
    }
    Ok(file)
}

fn private(_metadata: &fs::Metadata) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        _metadata.uid() == unsafe { libc::geteuid() } && _metadata.mode() & 0o077 == 0
    }
    #[cfg(not(unix))]
    { true }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn appends_json_lines_rotates_before_the_cap_and_never_follows_a_log_link() {
        let root = std::env::temp_dir().join(format!("supymem-webview-errors-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&root).unwrap();
        let logs = root.join("logs");
        let path = logs.join("webview-errors.jsonl");
        for (route, expected) in [("/v/{id}/builds", "/v/{id}/builds"), ("/v/{id}/private-gardening", "/other")] {
            let entry = serde_json::json!({"kind": "blank_render", "route": closed_route(route)});
            assert_eq!(entry["route"], expected);
            append(&root, &entry).unwrap();
        }
        let saved = fs::read_to_string(&path).unwrap();
        assert_eq!(saved.lines().count(), 2);
        assert!(saved.lines().all(|line| serde_json::from_str::<serde_json::Value>(line).is_ok()));

        fs::OpenOptions::new().write(true).open(&path).unwrap().set_len(LOG_CAP - 1).unwrap();
        let entry = serde_json::json!({"kind": "render_error", "route": "/new"});
        append(&root, &entry).unwrap();
        assert_eq!(fs::metadata(logs.join("webview-errors.jsonl.1")).unwrap().len(), LOG_CAP - 1);
        assert_eq!(fs::read_to_string(&path).unwrap(), format!("{entry}\n"));

        #[cfg(unix)]
        {
            let other = root.join("unchanged");
            fs::write(&other, "not a diagnostic log").unwrap();
            fs::remove_file(&path).unwrap();
            std::os::unix::fs::symlink(&other, &path).unwrap();
            assert!(append(&root, &entry).is_err());
            assert_eq!(fs::read_to_string(&other).unwrap(), "not a diagnostic log");
        }
        fs::remove_dir_all(root).unwrap();
    }
}
