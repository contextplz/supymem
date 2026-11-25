//! Open your editor on verified venture folders, never submit work through GUI focus.
//! Coding runs and their Stop controls belong to the reviewed, owned worker session.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Mutex, OnceLock};
use std::thread;
use std::time::{Duration, SystemTime};

use serde::Serialize;

#[derive(Clone, Serialize)]
pub struct IdeSessionStart {
    pub cwd: String,
    pub provider: String,
    pub opened: bool,
    pub message: String,
    pub order_id: String,
}

#[derive(Clone, Serialize)]
pub struct LandingSnapshot {
    pub html: String,
    pub files: std::collections::BTreeMap<String, String>,
    pub idle_secs: u64,
    pub ready: bool,
    pub cwd: String,
    #[serde(default)]
    pub preview_url: String,
}

pub fn slugify(name: &str) -> String {
    let mut out = String::new();
    let mut dash = false;
    for c in name.chars() {
        if c.is_ascii_alphanumeric() {
            out.push(c.to_ascii_lowercase());
            dash = false;
        } else if !out.is_empty() && !dash {
            out.push('-');
            dash = true;
        }
    }
    out.trim_matches('-').to_string()
}

pub fn projects_root() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from(&home))
        .join("Documents")
        .join("Supymem")
        .join("ventures")
}

fn workspace_record_path(data_dir: &Path, venture_id: &str) -> PathBuf {
    data_dir.join("ventures").join(venture_id).join("workspace.json")
}

fn marker_path(cwd: &Path) -> PathBuf {
    cwd.join(".supymem-venture")
}

fn open_workspace_metadata(path: &Path) -> std::io::Result<std::fs::File> {
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    { use std::os::unix::fs::OpenOptionsExt; options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK); }
    #[cfg(windows)]
    { use std::os::windows::fs::OpenOptionsExt; options.custom_flags(0x00200000); }
    let file = options.open(path)?;
    let metadata = file.metadata()?;
    let mut safe = metadata.is_file();
    #[cfg(unix)]
    { use std::os::unix::fs::MetadataExt; safe &= metadata.nlink() == 1; }
    #[cfg(windows)]
    { safe &= crate::local_credentials::private_windows(&file); }
    if !safe { return Err(std::io::ErrorKind::PermissionDenied.into()); }
    Ok(file)
}

fn read_workspace_metadata(path: &Path) -> std::io::Result<String> {
    let mut text = String::new();
    open_workspace_metadata(path)?.take(16_385).read_to_string(&mut text)?;
    if text.len() > 16_384 { return Err(std::io::ErrorKind::InvalidData.into()); }
    Ok(text)
}

fn replace_workspace_metadata(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let result = (|| -> std::io::Result<()> {
        let parent = path.parent().ok_or(std::io::ErrorKind::InvalidInput)?;
        if parent.canonicalize()? != parent { return Err(std::io::ErrorKind::PermissionDenied.into()); }
        match open_workspace_metadata(path) {
            Ok(_) => {},
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {},
            Err(error) => return Err(error),
        }
        let temporary = parent.join(format!(".supymem-{}", uuid::Uuid::new_v4()));
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        { use std::os::unix::fs::OpenOptionsExt; options.mode(0o600); }
        let mut file = options.open(&temporary)?;
        let written = file.write_all(bytes);
        drop(file);
        let result = written.and_then(|()| std::fs::rename(&temporary, path));
        if result.is_err() { let _ = std::fs::remove_file(&temporary); }
        result
    })();
    result.map_err(|_| "Your project folder record could not be saved safely. Open Builds and select your venture's result folder without links, then retry.".into())
}

fn valid_venture_id(venture_id: &str) -> bool {
    !venture_id.is_empty() && venture_id.len() <= 128 && venture_id.bytes().all(|byte|
        byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
}

fn read_remembered_path(data_dir: &Path, venture_id: &str) -> Option<PathBuf> {
    if !valid_venture_id(venture_id) { return None; }
    let record = workspace_record_path(&data_dir.canonicalize().ok()?, venture_id);
    let parent = record.parent()?;
    if parent.canonicalize().ok()? != parent { return None; }
    let text = read_workspace_metadata(&record).ok()?;
    let v: serde_json::Value = serde_json::from_str(&text).ok()?;
    let p = v.get("path")?.as_str()?;
    Some(PathBuf::from(p))
}

fn folder_belongs_to(path: &Path, venture_id: &str) -> bool {
    if let Ok(text) = read_workspace_metadata(&marker_path(path)) {
        return text.trim() == venture_id;
    }
    mostly_empty_project(path)
}

fn mostly_empty_project(path: &Path) -> bool {
    std::fs::read_dir(path).is_ok_and(|mut entries| entries.next().is_none())
}

fn short_id(venture_id: &str) -> String {
    let raw = venture_id.strip_prefix("ven_").unwrap_or(venture_id);
    raw.chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .rev()
        .take(6)
        .collect::<String>()
        .chars()
        .rev()
        .collect()
}

#[allow(dead_code)]
pub fn resolve_venture_repo(data_dir: &Path, venture_id: &str, folder_name: Option<&str>) -> PathBuf {
    resolve_in(&projects_root(), data_dir, venture_id, folder_name)
}

fn resolve_in(
    root: &Path,
    data_dir: &Path,
    venture_id: &str,
    folder_name: Option<&str>,
) -> PathBuf {
    if let Some(existing) = read_remembered_path(data_dir, venture_id) {
        return existing;
    }
    let slug = folder_name
        .map(slugify)
        .filter(|s| !s.is_empty())
        .or_else(|| {
            let stripped = venture_id.strip_prefix("ven_").unwrap_or(venture_id);
            let s = slugify(stripped);
            (!s.is_empty()).then_some(s)
        })
        .unwrap_or_else(|| "venture".into());
    let candidate = root.join(&slug);
    if !candidate.exists() || folder_belongs_to(&candidate, venture_id) {
        return candidate;
    }
    let suffix = short_id(venture_id);
    if suffix.is_empty() {
        candidate
    } else {
        root.join(format!("{slug}-{suffix}"))
    }
}

pub fn ensure_venture_workspace(
    data_dir: &Path,
    venture_id: &str,
    folder_name: Option<&str>,
) -> Result<PathBuf, String> {
    ensure_in(&projects_root(), data_dir, venture_id, folder_name)
}

fn ensure_in(
    root: &Path,
    data_dir: &Path,
    venture_id: &str,
    folder_name: Option<&str>,
) -> Result<PathBuf, String> {
    if !valid_venture_id(venture_id) {
        return Err("Open your venture in Builds, then retry opening its project folder.".into());
    }
    let cwd = resolve_in(root, data_dir, venture_id, folder_name);
    ensure_at(root, data_dir, venture_id, cwd)
}

pub fn ensure_reviewed_workspace(data_dir: &Path, venture_id: &str, workspace_slug: &str) -> Result<PathBuf, String> {
    if !valid_venture_id(venture_id)
        || slugify(workspace_slug) != workspace_slug || workspace_slug.is_empty() || workspace_slug.len() > 64 {
        return Err("Open Builds and retry loading your reviewed project folder.".into());
    }
    let root = projects_root();
    ensure_at(&root, data_dir, venture_id, root.join(workspace_slug))
}

fn ensure_at(root: &Path, data_dir: &Path, venture_id: &str, cwd: PathBuf) -> Result<PathBuf, String> {
    validate_workspace(root, &cwd, venture_id)?;
    std::fs::create_dir_all(&cwd).map_err(|e| e.to_string())?;
    let marker = marker_path(&cwd);
    if !marker.exists() {
        replace_workspace_metadata(&marker_path(&cwd.canonicalize().map_err(|e| e.to_string())?),
            format!("{venture_id}\n").as_bytes())?;
    }
    remember_workspace(data_dir, venture_id, &cwd)?;
    Ok(cwd)
}

fn validate_workspace(root: &Path, cwd: &Path, venture_id: &str) -> Result<(), String> {
    let next_step = "Your project folder could not be verified. Open Builds, select your venture's result folder, and move it back under Documents/Supymem/ventures without links before retrying.";
    if cwd.parent() != Some(root) { return Err(next_step.into()); }
    for path in cwd.ancestors() {
        match std::fs::symlink_metadata(path) {
            Ok(metadata) if metadata.file_type().is_symlink() => return Err(next_step.into()),
            Ok(_) => {},
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {},
            Err(_) => return Err(next_step.into()),
        }
    }
    let marker = marker_path(cwd);
    if std::fs::symlink_metadata(&marker).is_ok_and(|metadata| metadata.file_type().is_symlink())
        || cwd.exists() && !folder_belongs_to(cwd, venture_id) {
        return Err(next_step.into());
    }
    Ok(())
}

fn remember_workspace(data_dir: &Path, venture_id: &str, cwd: &Path) -> Result<(), String> {
    if !valid_venture_id(venture_id) { return Err("Open Builds and retry loading your venture's folder.".into()); }
    std::fs::create_dir_all(data_dir).map_err(|e| e.to_string())?;
    let data_dir = data_dir.canonicalize().map_err(|_| "Open Settings > Keys and connections > Your data folder and backup, check your data folder, then retry.")?;
    let rec_dir = data_dir.join("ventures").join(venture_id);
    std::fs::create_dir_all(&rec_dir).map_err(|e| e.to_string())?;
    let rec = serde_json::json!({
        "path": cwd.to_string_lossy(),
        "venture_id": venture_id,
    });
    replace_workspace_metadata(&workspace_record_path(&data_dir, venture_id), rec.to_string().as_bytes())?;
    Ok(())
}

fn editor_launch_command(launcher: &Path) -> Command {
    let mut command = Command::new(launcher);
    command.env_clear().env("PATH", "/usr/bin:/bin");
    for name in ["HOME", "USER", "LANG"] {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    command
}

#[cfg(any(target_os = "macos", test))]
fn macos_editor_command(bundle: &Path, cwd: &Path) -> Command {
    let mut command = editor_launch_command(Path::new("/usr/bin/open"));
    command.arg("-a").arg(bundle).arg(cwd);
    command
}

pub fn editor_command(app: &str, cwd: &Path) -> Option<Command> {
    #[cfg(target_os = "macos")]
    {
        let bundle = format!("{app}.app");
        let roots = [Some(PathBuf::from("/Applications")), dirs::home_dir().map(|home| home.join("Applications"))];
        let bundle = roots.into_iter().flatten().map(|root| root.join(&bundle))
            .find(|path| path.is_absolute() && path.is_dir())?;
        Some(macos_editor_command(&bundle, cwd))
    }
    #[cfg(not(target_os = "macos"))]
    {
        #[cfg(windows)]
        let launcher = {
            let (user_install, system_install) = match app {
                "Cursor" => ("Programs/cursor/Cursor.exe", "Cursor/Cursor.exe"),
                "Visual Studio Code" => ("Programs/Microsoft VS Code/Code.exe", "Microsoft VS Code/Code.exe"),
                _ => return None,
            };
            [("LOCALAPPDATA", user_install), ("ProgramFiles", system_install), ("ProgramFiles(x86)", system_install)]
                .into_iter().filter_map(|(name, install)| std::env::var_os(name).map(|root| PathBuf::from(root).join(install)))
                .find(|path| path.is_absolute() && path.is_file())?
        };
        #[cfg(not(windows))]
        let launcher = {
            let bin = match app {
                "Cursor" => "cursor",
                "Visual Studio Code" => "code",
                _ => return None,
            };
            ["/usr/bin", "/usr/local/bin"].into_iter().map(|root| Path::new(root).join(bin))
                .find(|path| path.is_file())?
        };
        let mut command = editor_launch_command(&launcher);
        command.arg(cwd);
        Some(command)
    }
}

pub fn read_venture_landing(
    data_dir: &Path,
    venture_id: &str,
    folder_name: Option<&str>,
) -> Result<LandingSnapshot, String> {
    if !valid_venture_id(venture_id) {
        return Err("Open Builds, select your venture, then retry its local preview.".into());
    }
    let root = projects_root();
    let cwd = resolve_in(&root, data_dir, venture_id, folder_name);
    validate_workspace(&root, &cwd, venture_id)?;
    if !cwd.is_dir() {
        return Err("Your project folder is not ready yet. Open Builds and start your requested build, then retry the preview.".into());
    }
    let mut files = std::collections::BTreeMap::new();
    let html = safe_preview_file(&cwd, "/index.html")
        .and_then(|path| std::fs::read_to_string(path).ok()).unwrap_or_default();
    for name in ["styles.css", "style.css", "main.css"] {
        if let Some(path) = safe_preview_file(&cwd, &format!("/{name}")) {
            if let Ok(body) = std::fs::read_to_string(path) {
                files.insert(name.to_string(), body);
            }
        }
    }
    let newest = ["index.html", "styles.css", "style.css"]
        .iter()
        .filter_map(|n| cwd.join(n).metadata().ok()?.modified().ok())
        .max();
    let idle_secs = newest
        .and_then(|t| SystemTime::now().duration_since(t).ok())
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let ready = landing_idle_ready(html.len() as u64, idle_secs, !html.trim().is_empty());
    let preview_url = if html.len() > 40 {
        start_landing_preview(&cwd).unwrap_or_default()
    } else {
        String::new()
    };
    Ok(LandingSnapshot {
        html,
        files,
        idle_secs,
        ready,
        cwd: cwd.to_string_lossy().into_owned(),
        preview_url,
    })
}

pub fn landing_idle_ready(index_bytes: u64, idle_secs: u64, has_html: bool) -> bool {
    has_html && index_bytes > 200 && idle_secs >= 20
}

fn preview_registry() -> &'static Mutex<HashMap<String, u16>> {
    static REG: OnceLock<Mutex<HashMap<String, u16>>> = OnceLock::new();
    REG.get_or_init(|| Mutex::new(HashMap::new()))
}

pub fn start_landing_preview(cwd: &Path) -> Result<String, String> {
    let key = cwd.to_string_lossy().into_owned();
    if let Ok(reg) = preview_registry().lock() {
        if let Some(port) = reg.get(&key) {
            return Ok(format!("http://127.0.0.1:{port}/"));
        }
    }
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|e| e.to_string())?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    listener.set_nonblocking(false).ok();
    let root = cwd.to_path_buf();
    thread::spawn(move || {
        for incoming in listener.incoming() {
            let Ok(mut stream) = incoming else {
                continue;
            };
            let _ = handle_preview_conn(&root, &mut stream);
        }
    });
    if let Ok(mut reg) = preview_registry().lock() {
        reg.insert(key, port);
    }
    Ok(format!("http://127.0.0.1:{port}/"))
}

fn handle_preview_conn(root: &Path, stream: &mut std::net::TcpStream) -> std::io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_secs(2)))?;
    stream.set_write_timeout(Some(Duration::from_secs(2)))?;
    let mut buf = [0u8; 4096];
    let n = stream.read(&mut buf)?;
    let req = String::from_utf8_lossy(&buf[..n]);
    let path = req
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .unwrap_or("/");
    let Some(file) = safe_preview_file(root, path) else {
        let body = b"not found";
        stream.write_all(
            format!(
                "HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            )
            .as_bytes(),
        )?;
        stream.write_all(body)?;
        return Ok(());
    };
    let body = std::fs::read(&file).unwrap_or_default();
    let mime = match file.extension().and_then(|s| s.to_str()).unwrap_or("") {
        "css" => "text/css; charset=utf-8",
        "js" | "mjs" => "text/javascript; charset=utf-8",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "webp" => "image/webp",
        "gif" => "image/gif",
        "html" | "htm" => "text/html; charset=utf-8",
        _ => "application/octet-stream",
    };
    stream.write_all(
        format!(
            "HTTP/1.1 200 OK\r\nContent-Type: {mime}\r\nContent-Length: {}\r\nConnection: close\r\nX-Content-Type-Options: nosniff\r\nCache-Control: no-store\r\n\r\n",
            body.len()
        )
        .as_bytes(),
    )?;
    stream.write_all(&body)?;
    Ok(())
}

pub fn safe_preview_file(root: &Path, req_path: &str) -> Option<std::path::PathBuf> {
    let trimmed = req_path.split('?').next().unwrap_or("/");
    let rel = trimmed.trim_start_matches('/');
    let candidate = if rel.is_empty() {
        root.join("index.html")
    } else {
        if rel.contains("..") || rel.starts_with('/') {
            return None;
        }
        root.join(rel)
    };
    let relative = candidate.strip_prefix(root).ok()?;
    let mut path = root.to_path_buf();
    if std::fs::symlink_metadata(&path).ok()?.file_type().is_symlink() { return None; }
    for component in relative.components() {
        let std::path::Component::Normal(name) = component else { return None; };
        if name.to_str()?.starts_with('.') { return None; }
        path.push(name);
        if std::fs::symlink_metadata(&path).ok()?.file_type().is_symlink() { return None; }
    }
    if !matches!(candidate.extension()?.to_str()?, "html" | "htm" | "css" | "js" | "mjs"
        | "svg" | "png" | "jpg" | "jpeg" | "webp" | "gif" | "ico" | "woff" | "woff2" | "ttf") {
        return None;
    }
    let metadata = candidate.metadata().ok()?;
    if !metadata.is_file() || metadata.len() > 4 * 1024 * 1024 { return None; }
    let canon_root = root.canonicalize().ok()?;
    let canon = candidate.canonicalize().ok()?;
    canon.starts_with(&canon_root).then_some(canon)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    #[test]
    fn editor_launch_uses_system_open_and_drops_inherited_credentials() {
        if std::env::var_os("SUPYMEM_EDITOR_TEST_MARKER").is_none() {
            let status = Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "ide::tests::editor_launch_uses_system_open_and_drops_inherited_credentials"])
                .env("SUPYMEM_EDITOR_TEST_MARKER", "synthetic")
                .status().unwrap();
            assert!(status.success());
            return;
        }
        for domain in ["supplier-audits.example", "garden-plans.example"] {
            let cwd = Path::new("/tmp").join(format!("folder {domain}"));
            let bundle = Path::new("/Applications/Cursor.app");
            let command = macos_editor_command(bundle, &cwd);
            assert_eq!(command.get_program(), "/usr/bin/open");
            assert!(command.get_args().eq([Path::new("-a"), bundle, &cwd].map(Path::as_os_str)));
            let mut expected = vec!["PATH"];
            expected.extend(["HOME", "LANG", "USER"].into_iter().filter(|name| std::env::var_os(name).is_some()));
            expected.sort_unstable();
            assert!(command.get_envs().map(|(name, _)| name).eq(expected));
            assert!(command.get_envs().any(|(name, value)| name == "PATH" && value.is_some_and(|value| value == "/usr/bin:/bin")));

            assert!(editor_launch_command(Path::new("/bin/sh"))
                .args(["-c", "test \"${SUPYMEM_EDITOR_TEST_MARKER-unset}\" = unset"])
                .status().unwrap().success());
        }
    }
    use std::sync::atomic::{AtomicU64, Ordering};

    static SEQ: AtomicU64 = AtomicU64::new(0);

    fn scratch() -> PathBuf {
        let n = SEQ.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("supymem-ide-test-{}-{n}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir.canonicalize().unwrap()
    }

    #[test]
    fn slugify_name() {
        assert_eq!(slugify("Repairqueue"), "repairqueue");
        assert_eq!(slugify("Bike Shop!!"), "bike-shop");
        assert_eq!(slugify("  "), "");
    }

    #[test]
    fn landing_idle_ready_needs_html_and_quiet() {
        assert!(!landing_idle_ready(50, 30, true));
        assert!(!landing_idle_ready(500, 5, true));
        assert!(!landing_idle_ready(500, 12, true));
        assert!(landing_idle_ready(500, 20, true));
        assert!(!landing_idle_ready(500, 12, false));
    }

    #[test]
    fn safe_preview_file_blocks_traversal() {
        let tmp = scratch();
        std::fs::write(tmp.join("index.html"), "<html>ok</html>").unwrap();
        std::fs::write(tmp.join("styles.css"), "body{}").unwrap();
        assert!(safe_preview_file(&tmp, "/").unwrap().ends_with("index.html"));
        assert!(safe_preview_file(&tmp, "/styles.css").unwrap().ends_with("styles.css"));
        assert!(safe_preview_file(&tmp, "/../index.html").is_none());
    }

    #[test]
    fn remembers_documents_path() {
        let tmp = scratch();
        let data = tmp.join("data");
        let projects = tmp.join("projects");
        let cwd = ensure_in(&projects, &data, "ven_rq", Some("Repairqueue")).unwrap();
        assert_eq!(cwd, projects.join("repairqueue"));
        assert!(cwd.join(".supymem-venture").exists());
        let again = resolve_in(&projects, &data, "ven_rq", Some("Other"));
        assert_eq!(again, cwd);
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn collision_gets_suffix() {
        let tmp = scratch();
        let data = tmp.join("data");
        let projects = tmp.join("projects");
        std::fs::create_dir_all(projects.join("repairqueue")).unwrap();
        std::fs::write(projects.join("repairqueue").join("index.html"), "nope").unwrap();
        let cwd = resolve_in(&projects, &data, "ven_abcdef", Some("Repairqueue"));
        assert_eq!(cwd, projects.join("repairqueue-abcdef"));
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn build_native_remembered_workspace_cannot_escape_or_adopt_another_venture() {
        for (venture, name) in [("ven_operations", "Supplier audits"), ("ven_hobby", "Garden plans")] {
            let tmp = scratch();
            let data = tmp.join("data");
            let projects = tmp.join("projects");
            let outside = tmp.join("outside");
            std::fs::create_dir_all(&outside).unwrap();
            std::fs::write(outside.join("keep.txt"), "private").unwrap();
            assert!(remember_workspace(&data, "../other", &outside).is_err());
            assert!(read_remembered_path(&data, "../other").is_none());
            remember_workspace(&data, venture, &outside).unwrap();
            assert!(ensure_in(&projects, &data, venture, Some(name)).is_err());
            assert!(!outside.join(".supymem-venture").exists());
            let owned = projects.join(slugify(name));
            std::fs::create_dir_all(&owned).unwrap();
            std::fs::write(marker_path(&owned), format!("{venture}-other\n")).unwrap();
            remember_workspace(&data, venture, &owned).unwrap();
            assert!(ensure_in(&projects, &data, venture, Some(name)).is_err());
            #[cfg(unix)] {
                std::fs::remove_dir_all(&owned).unwrap();
                std::os::unix::fs::symlink(&outside, &owned).unwrap();
                assert!(ensure_in(&projects, &data, venture, Some(name)).is_err());
            }
            std::fs::remove_dir_all(tmp).unwrap();
        }
    }

    #[cfg(unix)]
    #[test]
    fn workspace_record_rejects_linked_files() {
        for venture in ["ven_supplier_audits", "ven_garden_plans"] {
            let tmp = scratch();
            let data = tmp.join("data");
            remember_workspace(&data, venture, &tmp).unwrap();
            let record = workspace_record_path(&data, venture);
            let outside = tmp.join("unrelated.txt");
            std::fs::write(&outside, venture).unwrap();
            std::fs::remove_file(&record).unwrap();
            std::fs::hard_link(&outside, record).unwrap();
            assert!(remember_workspace(&data, venture, &tmp).is_err());
            assert_eq!(std::fs::read_to_string(&outside).unwrap(), venture);
            std::fs::remove_dir_all(tmp).unwrap();
        }
    }

    #[test]
    fn build_native_preview_serves_public_assets_not_secrets_or_symlinks() {
        for name in ["supplier-audits", "garden-plans"] {
            let tmp = scratch().join(name);
            std::fs::create_dir_all(tmp.join(".git")).unwrap();
            for (file, body) in [("index.html", "<html>ok</html>"), ("app.js", "let ready = true;"),
                (".env", "private"), ("SUPYMEM_TASK.md", "private"), (".git/config", "private"),
                ("auth.json", "private"), ("private.html", "private")] {
                std::fs::write(tmp.join(file), body).unwrap();
            }
            assert!(safe_preview_file(&tmp, "/").is_some());
            assert!(safe_preview_file(&tmp, "/app.js?v=1").is_some());
            for request in ["/.env", "/SUPYMEM_TASK.md", "/.git/config", "/auth.json", "/../index.html"] {
                assert!(safe_preview_file(&tmp, request).is_none(), "{request}");
            }
            #[cfg(unix)] {
                std::os::unix::fs::symlink(tmp.join("private.html"), tmp.join("linked.html")).unwrap();
                assert!(safe_preview_file(&tmp, "/linked.html").is_none());
            }
            std::fs::remove_dir_all(tmp.parent().unwrap()).unwrap();
        }
    }

    #[cfg(unix)]
    #[test]
    fn build_native_projects_root_matches_the_worker_not_xdg_documents() {
        if let Some(home) = std::env::var_os("BUILD_NATIVE_TEST_HOME") {
            assert_eq!(projects_root(), PathBuf::from(home).join("Documents/Supymem/ventures"));
            return;
        }
        for name in ["supplier-audits", "garden-plans"] {
            let root = std::env::temp_dir().join(format!("native-documents-{}-{name}", uuid::Uuid::new_v4()));
            let config = root.join("config");
            std::fs::create_dir_all(&config).unwrap();
            std::fs::create_dir_all(root.join("Elsewhere")).unwrap();
            std::fs::write(config.join("user-dirs.dirs"), "XDG_DOCUMENTS_DIR=\"$HOME/Elsewhere\"\n").unwrap();
            let output = Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "ide::tests::build_native_projects_root_matches_the_worker_not_xdg_documents", "--test-threads=1"])
                .env("HOME", &root).env("XDG_CONFIG_HOME", &config).env("BUILD_NATIVE_TEST_HOME", &root)
                .output().unwrap();
            std::fs::remove_dir_all(root).unwrap();
            assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stdout));
        }
    }
}
