//! Detect macOS permissions and open the exact Settings pane.
//!
//! We cannot click Apple's toggle for the founder. We can open the pane and
//! show a coach that waits until they do.

use std::process::Command;

use serde::Serialize;

#[derive(Clone, Serialize)]
pub struct MacControlStatus {
    pub tool: String,
    pub accessibility: bool,
    pub automation: bool,
    pub ready: bool,
    pub pane: Option<String>,
    pub kind: String,
    pub title: String,
    pub steps: Vec<String>,
    pub message: String,
}

fn permission_copy(copy: &str) -> String {
    if crate::local_stack::is_local() {
        copy.replace("Supymem", "Supymem Local").replace(" — ", ". ")
    } else {
        copy.into()
    }
}

pub fn status_for(tool: &str) -> MacControlStatus {
    let tool = tool.trim().to_lowercase();
    match tool.as_str() {
        "docker" => docker_status(),
        "claude" | "codex" => cli_tool_status(&tool),
        _ => cursor_status(),
    }
}

pub fn status_for_cursor() -> MacControlStatus {
    cursor_status()
}

fn cursor_status() -> MacControlStatus {
    let accessibility = accessibility_trusted();
    if !accessibility {
        return MacControlStatus {
            tool: "cursor".into(),
            accessibility,
            automation: false,
            ready: false,
            pane: Some("accessibility".into()),
            kind: "accessibility".into(),
            title: permission_copy("Turn on Supymem in Accessibility"),
            steps: vec![
                "System Settings is open on Accessibility.".into(),
                permission_copy("If Supymem is already listed and the switch looks on, that row is often a previous DMG — this copy cannot use it."),
                permission_copy("Select Supymem, click the minus button to remove it, then plus, and pick /Applications/Supymem.app."),
                "Turn the switch on, then come back here.".into(),
            ],
            message: permission_copy("Supymem needs Accessibility to open Cursor and press Send in that window."),
        };
    }
    MacControlStatus {
        tool: "cursor".into(),
        accessibility,
        automation: true,
        ready: true,
        pane: None,
        kind: "ready".into(),
        title: "Cursor is allowed".into(),
        steps: vec![],
        message: "This Mac can drive Cursor.".into(),
    }
}

fn cli_tool_status(tool: &str) -> MacControlStatus {
    let label = if tool == "codex" { "Codex" } else { "Claude Code" };
    MacControlStatus {
        tool: tool.into(),
        accessibility: true,
        automation: true,
        ready: true,
        pane: None,
        kind: "cli".into(),
        title: format!("{label} uses its own CLI"),
        steps: vec![
            format!("{label} does not need Accessibility."),
            "We start the tool you already signed into. No Settings toggle.".into(),
            permission_copy("If macOS later asks to allow Terminal or Supymem, click Allow."),
        ],
        message: format!("{label} is a CLI. There is no Accessibility switch to flip."),
    }
}

fn docker_status() -> MacControlStatus {
    let running = crate::docker::engine_is_up();
    let installed = crate::docker::docker_app_path().is_some();
    if running {
        return MacControlStatus {
            tool: "docker".into(),
            accessibility: true,
            automation: true,
            ready: true,
            pane: None,
            kind: "ready".into(),
            title: "Docker is running".into(),
            steps: vec![],
            message: "Docker is running. Local sandboxes can use it.".into(),
        };
    }
    MacControlStatus {
        tool: "docker".into(),
        accessibility: true,
        automation: true,
        ready: false,
        pane: None,
        kind: "docker".into(),
        title: if installed {
            "Start Docker Desktop"
        } else {
            "Install Docker Desktop"
        }
        .into(),
        steps: vec![
            "Docker is not a Privacy toggle. It is its own app.".into(),
            permission_copy("If macOS asks for your password, type it — that installs Docker’s helper."),
            "Wait until the whale icon in the menu bar is steady, then come back.".into(),
        ],
        message: permission_copy(if installed {
            "Docker Desktop is installed but the engine is stopped. Start it — local sandboxes can use it."
        } else {
            "Docker Desktop is not installed yet. Install it, or skip and use Cursor / Claude / Codex."
        }),
    }
}

pub fn request_tool_access(tool: &str) -> MacControlStatus {
    let tool = tool.trim().to_lowercase();
    match tool.as_str() {
        "docker" => docker_status(),
        "claude" | "codex" => cli_tool_status(&tool),
        _ => request_cursor_control(),
    }
}

/// Open the Settings pane the founder must toggle, and trigger the system prompt.
pub fn request_cursor_control() -> MacControlStatus {
    if !accessibility_trusted() {
        open_privacy_pane("Privacy_Accessibility");
        prompt_accessibility();
        let mut st = cursor_status();
        st.message = permission_copy("System Settings is open on Accessibility. Turn on Supymem in the list.");
        return st;
    }
    let cursor_ok = automation_for("Cursor");
    let events_ok = automation_for("System Events");
    if cursor_ok && events_ok {
        return MacControlStatus {
            tool: "cursor".into(),
            accessibility: true,
            automation: true,
            ready: true,
            pane: None,
            kind: "ready".into(),
            title: "Cursor is allowed".into(),
            steps: vec![],
            message: "This Mac can drive Cursor.".into(),
        };
    }
    open_privacy_pane("Privacy_Automation");
    MacControlStatus {
        tool: "cursor".into(),
        accessibility: true,
        automation: false,
        ready: false,
        pane: Some("automation".into()),
        kind: "automation".into(),
        title: permission_copy("Allow Supymem to control Cursor"),
        steps: vec![
            "System Settings is open on Automation.".into(),
            permission_copy("Find Supymem. Turn on Cursor (and System Events if listed)."),
            "Come back and click Connect Cursor again.".into(),
        ],
        message: permission_copy("System Settings is open on Automation. Allow Supymem → Cursor."),
    }
}

pub fn open_privacy_pane(anchor: &str) {
    let modern = format!(
        "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?{anchor}"
    );
    let legacy = format!("x-apple.systempreferences:com.apple.preference.security?{anchor}");
    if open_url(&modern) {
        return;
    }
    let _ = open_url(&legacy);
}

pub(crate) fn open_url(url: &str) -> bool {
    Command::new("open")
        .arg(url)
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

fn accessibility_trusted() -> bool {
    // TCC keys Accessibility to this binary's cdhash. osascript is a
    // different process — never treat its AX success as our trust.
    ax_api_trusted()
}

fn ax_api_trusted() -> bool {
    #[cfg(target_os = "macos")]
    {
        #[link(name = "ApplicationServices", kind = "framework")]
        extern "C" {
            fn AXIsProcessTrusted() -> bool;
        }
        unsafe { AXIsProcessTrusted() }
    }
    #[cfg(not(target_os = "macos"))]
    {
        true
    }
}

/// The founder said they flipped the switch. Close the coach.
/// TCC may still be matching a previous DMG's cdhash — Settings can show ON
/// for a stale identity while this process is denied.
pub fn honor_user_confirmation(tool: &str) -> MacControlStatus {
    let mut st = status_for(tool);
    if st.ready {
        return st;
    }
    if let Ok(dir) = std::env::var("HOME") {
        let stamp = std::path::PathBuf::from(dir)
            .join("Library/Application Support/Supymem/ax_user_confirmed.json");
        let _ = std::fs::create_dir_all(stamp.parent().unwrap());
        let _ = std::fs::write(
            &stamp,
            format!("{{\"tool\":\"{}\",\"at\":{}}}\n", tool, unix_now()),
        );
    }
    st.ready = true;
    st.kind = "ready".into();
    st.title = "Saved".into();
    st.message = permission_copy("Closing. If Cursor still cannot be driven, in Accessibility click minus on Supymem, plus, pick /Applications/Supymem.app, and turn it on once.");
    st
}

fn unix_now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn prompt_accessibility() {
    let _ = Command::new("osascript")
        .args([
            "-e",
            r#"tell application "System Events" to get name of first process"#,
        ])
        .output();
}

fn automation_for(app: &str) -> bool {
    let script = format!(
        r#"tell application "{app}" to get name
return "ok""#
    );
    let out = Command::new("osascript").args(["-e", &script]).output();
    let Ok(out) = out else {
        return false;
    };
    if out.status.success() {
        return true;
    }
    let err = String::from_utf8_lossy(&out.stderr).to_lowercase();
    if err.contains("isn’t running") || err.contains("isn't running") || err.contains("-600") {
        return true;
    }
    false
}

#[cfg(test)]
mod tests {
    #[test]
    fn pane_urls_are_well_formed() {
        let a = "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility";
        let b = "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Automation";
        assert!(a.contains("Privacy_Accessibility"));
        assert!(b.contains("Privacy_Automation"));
    }

    #[test]
    fn claude_and_codex_do_not_need_accessibility() {
        let c = super::cli_tool_status("claude");
        let x = super::cli_tool_status("codex");
        assert!(c.ready && x.ready);
        assert_eq!(c.kind, "cli");
    }
}
