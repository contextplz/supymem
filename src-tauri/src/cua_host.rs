//! A per-review CUA daemon owned directly by the signed Supymem app.
//!
//! The bounded manifest narrows the upstream driver's own authority to one
//! already-reviewed PID and window. The bridge still has to admit each effect
//! through the durable work-order gate; possession of this socket is not an
//! effect grant.

use std::fs;
use std::os::unix::fs::FileTypeExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use uuid::Uuid;

pub struct CuaTaskHost {
    child: Child,
    directory: PathBuf,
    socket: PathBuf,
}

fn manifest(pid: u32, window_id: u32) -> Result<String, String> {
    manifest_surfaces(&[(pid, window_id)])
}

fn manifest_surfaces(surfaces: &[(u32, u32)]) -> Result<String, String> {
    if !(1..=8).contains(&surfaces.len()) || surfaces.iter().any(|(pid,wid)| *pid == 0 || *wid == 0)
        || surfaces.iter().collect::<std::collections::HashSet<_>>().len() != surfaces.len() {
        return Err("desktop_cua_scope_invalid".into());
    }
    let mut pids = Vec::new();
    for (pid,_) in surfaces { if !pids.contains(pid) { pids.push(*pid); } }
    let applications = pids.iter().map(u32::to_string).collect::<Vec<_>>().join(", ");
    let windows = surfaces.iter().map(|(pid,wid)| format!("      - pid: {pid}\n        window_id: {wid}\n"))
        .collect::<String>();
    Ok(format!("version: 3\nexpires_after: 20m\nidle_timeout: 3m\nresources:\n  desktop:\n    applications: [{applications}]\n    windows:\n{windows}allow:\n  tools:\n    - start_session\n    - get_session_state\n    - end_session\n    - health_report\n    - check_permissions\n    - get_window_state\n    - list_windows\n    - list_apps\n    - click\n    - scroll\n    - type_text\n    - set_value\n    - press_key\n    - hotkey\n    - move_cursor\n"))
}

fn manifest_reviewed_modals(surfaces: &[(u32,u32)], parents: &[(u32,u32,String)]) -> Result<String,String> {
    manifest_reviewed_modals_with_gate(surfaces,parents,
        std::env::var("SUPYMEM_REVIEWED_MODAL_CHILDREN").as_deref() == Ok("1"))
}

fn manifest_reviewed_modals_with_gate(surfaces: &[(u32,u32)], parents: &[(u32,u32,String)], enabled: bool) -> Result<String,String> {
    let policy = manifest_surfaces(surfaces)?;
    if parents.is_empty() { return Ok(policy); }
    if !enabled
        || parents.len() > 8 || parents.iter().any(|(pid,wid,start)|
            !surfaces.contains(&(*pid,*wid)) || !(1..=24).contains(&start.len())
                || !start.bytes().all(|b| b.is_ascii_digit()))
        || parents.iter().map(|(pid,wid,_)|(*pid,*wid)).collect::<std::collections::HashSet<_>>().len() != parents.len() {
        return Err("desktop_modal_policy_unsupported".into());
    }
    let grants = parents.iter().map(|(pid,wid,start)|format!(
        "      - pid: {pid}\n        window_id: {wid}\n        process_start_identity: '{start}'\n")).collect::<String>();
    Ok(policy.replacen("allow:\n", &format!("    modal_parents:\n{grants}allow:\n"),1))
}

/// Read-only lifecycle groundwork; no menu/new-window authority.
pub const OBSERVATION_PROTOCOL: &str = "exact_window_observation_v1";

const LOG_CAP: u64 = 1_000_000;

/// Driver stderr only (no tool arguments or screen text). Rotated at each start:
/// cua-driver.log is moved to .1 once it passes 1 MB, so at most ~2 MB plus one task.
// ponytail: size is checked per task start, not mid-run; a 20m-capped daemon is quiet.
fn open_driver_log() -> Stdio {
    let Some(home) = dirs::home_dir() else { return Stdio::null() };
    let dir = home.join("Library/Logs/Supymem");
    let path = dir.join("cua-driver.log");
    if fs::create_dir_all(&dir).is_err() { return Stdio::null(); }
    if fs::metadata(&path).is_ok_and(|m| m.len() > LOG_CAP) {
        let _ = fs::rename(&path, dir.join("cua-driver.log.1"));
    }
    fs::OpenOptions::new().create(true).append(true).open(&path)
        .map(Stdio::from).unwrap_or_else(|_| Stdio::null())
}

fn socket_ready(socket: &Path) -> bool {
    // A connect-and-drop probe consumes a real daemon connection before the
    // bridge's MCP proxy can initialize. The proxy performs the handshake.
    fs::metadata(socket).is_ok_and(|metadata| metadata.file_type().is_socket())
}

impl CuaTaskHost {
    pub fn start(binary: &Path, pid: u32, window_id: u32) -> Result<Self, String> {
        Self::start_surfaces(binary, &[(pid, window_id)])
    }

    pub fn start_surfaces(binary: &Path, surfaces: &[(u32,u32)]) -> Result<Self, String> {
        Self::start_reviewed_modals(binary,surfaces,&[])
    }

    pub fn start_reviewed_modals(binary: &Path, surfaces: &[(u32,u32)], parents: &[(u32,u32,String)]) -> Result<Self,String> {
        if !binary.is_file() {
            return Err("desktop_cua_not_bundled".into());
        }
        let policy = manifest_reviewed_modals(surfaces,parents)?;
        let directory = PathBuf::from("/private/tmp").join(format!("supymem-cua-{}", Uuid::new_v4()));
        fs::create_dir(&directory).map_err(|_| "desktop_cua_private_dir_unavailable")?;
        let cleanup = |code: &str| {
            let _ = fs::remove_dir_all(&directory);
            code.to_string()
        };
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))
            .map_err(|_| cleanup("desktop_cua_private_dir_unavailable"))?;
        let policy_path = directory.join("manifest.yaml");
        fs::write(&policy_path, policy).map_err(|_| cleanup("desktop_cua_manifest_unavailable"))?;
        fs::set_permissions(&policy_path, fs::Permissions::from_mode(0o600))
            .map_err(|_| cleanup("desktop_cua_manifest_unavailable"))?;
        let socket = directory.join("driver.sock");
        let child = Command::new(binary)
            .args(["serve", "--embedded", "--socket"])
            .arg(&socket)
            .args(["--permission-mode", "bounded", "--capability-manifest"])
            .arg(&policy_path)
            .arg("--approve-capability-manifest")
            .env("CUA_DRIVER_EMBEDDED", "1")
            .env("CUA_DRIVER_HOST_BUNDLE_ID", "com.companyos.app")
            .env("CUA_DRIVER_RS_TELEMETRY_ENABLED", "0")
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(open_driver_log())
            .spawn().map_err(|_| cleanup("desktop_cua_launch_failed"))?;
        let mut host = Self { child, directory, socket };
        let deadline = Instant::now() + Duration::from_secs(8);
        loop {
            if host.child.try_wait().map_err(|_| "desktop_cua_start_unconfirmed")?
                .is_some() {
                return Err("desktop_cua_start_unconfirmed".into());
            }
            if socket_ready(&host.socket) {
                return Ok(host);
            }
            if Instant::now() >= deadline {
                return Err("desktop_cua_start_unconfirmed".into());
            }
            thread::sleep(Duration::from_millis(50));
        }
    }

    pub fn socket(&self) -> &Path { &self.socket }
}

impl Drop for CuaTaskHost {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = fs::remove_dir_all(&self.directory);
    }
}

#[cfg(test)]
mod tests {
    use super::{manifest, manifest_surfaces, manifest_reviewed_modals_with_gate, socket_ready};
    use std::fs;
    use std::io::ErrorKind;
    use std::os::unix::net::{UnixListener, UnixStream};

    #[test]
    fn explicit_modal_manifest_preserves_static_windows_and_never_grants_siblings() {
        let pairs=[(42,7),(43,8)];
        let parents=[(42,7,"123456".to_string())];
        assert!(manifest_reviewed_modals_with_gate(&pairs,&parents,false).is_err());
        let text=manifest_reviewed_modals_with_gate(&pairs,&parents,true).unwrap();
        assert!(text.contains("    windows:\n"));
        assert!(text.contains("    modal_parents:\n      - pid: 42\n        window_id: 7\n        process_start_identity: '123456'\n"));
        assert!(!text.contains("windows: all"));
        assert!(manifest_reviewed_modals_with_gate(&pairs,&[(42,9,"123456".into())],true).is_err());
        assert!(manifest_reviewed_modals_with_gate(&pairs,&[(42,7,"not-kernel-identity".into())],true).is_err());
        assert!(manifest_reviewed_modals_with_gate(&pairs,&[(42,7,"123".into()),(42,7,"123".into())],true).is_err());
        assert_eq!(manifest_reviewed_modals_with_gate(&pairs,&[],false).unwrap(),manifest_surfaces(&pairs).unwrap());
    }

    #[test]
    fn readiness_does_not_consume_the_proxy_connection() {
        let directory = std::path::Path::new("/tmp")
            .join(format!("supymem-cua-ready-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&directory).unwrap();
        let socket = directory.join("driver.sock");
        assert!(!socket_ready(&socket));
        fs::write(&socket, b"not a socket").unwrap();
        assert!(!socket_ready(&socket));
        fs::remove_file(&socket).unwrap();
        let listener = UnixListener::bind(&socket).unwrap();
        listener.set_nonblocking(true).unwrap();
        assert!(socket_ready(&socket));
        assert_eq!(listener.accept().unwrap_err().kind(), ErrorKind::WouldBlock);
        let _proxy = UnixStream::connect(&socket).unwrap();
        assert!(listener.accept().is_ok());
        drop(listener);
        fs::remove_dir_all(&directory).unwrap();
    }

    #[test]
    fn manifest_binds_exact_window_and_has_no_unrestricted_tools() {
        let text = manifest(42, 7).unwrap();
        assert!(text.contains("applications: [42]"));
        assert!(text.contains("window_id: 7"));
        assert!(!text.contains("browser_"));
        assert!(text.contains("    - scroll\n"));
        assert!(text.contains("    - list_windows\n"));
        assert!(!text.contains("    - invoke_menu\n"));
        assert!(text.contains("    - hotkey\n"));
        assert!(!text.contains("unrestricted"));
        assert!(manifest(0, 7).is_err());
    }
    #[test]
    fn multi_surface_manifest_contains_only_explicit_pairs_and_unique_pids() {
        let text = manifest_surfaces(&[(42,7),(43,8),(42,9)]).unwrap();
        assert!(text.contains("applications: [42, 43]"));
        for (pid,wid) in [(42,7),(43,8),(42,9)] {
            assert!(text.contains(&format!("- pid: {pid}\n        window_id: {wid}")));
        }
        assert_eq!(text.matches("window_id:").count(),3);
        assert!(!text.contains("launch_app"));
        assert!(!text.contains("invoke_menu"));
        assert!(manifest_surfaces(&[]).is_err());
        assert!(manifest_surfaces(&[(42,7),(42,7)]).is_err());
        assert!(manifest_surfaces(&[(42,0)]).is_err());
    }

}
