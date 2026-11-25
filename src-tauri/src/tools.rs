//! Detect IDEs and CLIs the founder already installed. Never read credential files.

use std::fs;
use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;

use serde::{Deserialize, Serialize};

use crate::install::hermes_bin_path;
use crate::supervisor::{conventional_data_dir, which_in_path};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct LocalTool {
    pub name: String,
    pub label: String,
    pub kind: String,
    pub installed: bool,
    pub connected: bool,
    #[serde(default)]
    pub signed_in: Option<bool>,
    #[serde(default)]
    pub coding_available: bool,
    pub app_path: Option<String>,
    pub cli_path: Option<String>,
}

fn home() -> PathBuf {
    dirs::home_dir().unwrap_or_else(|| PathBuf::from("/"))
}

fn app_bundle(name: &str) -> Option<PathBuf> {
    let candidates = [
        PathBuf::from(format!("/Applications/{name}.app")),
        home().join(format!("Applications/{name}.app")),
    ];
    if let Some(p) = candidates.into_iter().find(|p| p.is_dir()) {
        return Some(p);
    }
    spotlight_app(name)
}

fn spotlight_app(name: &str) -> Option<PathBuf> {
    let query = format!("kMDItemFSName == '{name}.app'");
    let mut child = Command::new("mdfind")
        .args(["-onlyin", "/Applications", "-onlyin"])
        .arg(home().join("Applications"))
        .arg(&query)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn()
        .ok()?;
    let deadline = std::time::Instant::now() + Duration::from_millis(400);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if std::time::Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(20)),
            Err(_) => return None,
        }
    }
    let output = child.wait_with_output().ok()?;
    let line = String::from_utf8_lossy(&output.stdout)
        .lines()
        .next()?
        .trim()
        .to_string();
    let path = PathBuf::from(line);
    path.is_dir().then_some(path)
}

fn usable_bin(path: PathBuf) -> Option<PathBuf> {
    // is_file() follows symlinks; Homebrew often leaves a dangling `codex` link.
    path.is_file().then_some(path)
}

fn first_existing(paths: impl IntoIterator<Item = PathBuf>) -> Option<PathBuf> {
    paths.into_iter().find_map(usable_bin)
}

fn first_cli(names: &[&str], shell_path: Option<&str>) -> Option<PathBuf> {
    for name in names {
        if let Some(p) = which_in_path(name, shell_path).and_then(usable_bin) {
            return Some(p);
        }
        let extras = [
            home().join(".local/bin").join(name),
            home().join(".cursor/bin").join(name),
            home().join(".npm-global/bin").join(name),
            home().join(".yarn/bin").join(name),
            home().join("node_modules/.bin").join(name),
            PathBuf::from("/opt/homebrew/bin").join(name),
            PathBuf::from("/usr/local/bin").join(name),
        ];
        if let Some(p) = first_existing(extras) {
            return Some(p);
        }
    }
    None
}

fn cursor_cli(shell_path: Option<&str>) -> Option<PathBuf> {
    first_cli(&["cursor-agent", "agent"], shell_path)
}

fn claude_cli(shell_path: Option<&str>) -> Option<PathBuf> {
    if let Some(p) = first_cli(&["claude"], shell_path) {
        return Some(p);
    }
    first_existing([home().join(".claude/local/claude")])
}

fn codex_app() -> Option<PathBuf> {
    app_bundle("Codex")
}

fn bundled_codex_cli(app: PathBuf) -> Option<PathBuf> {
    first_existing([
        app.join("Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"),
        app.join("Contents/Resources/codex"),
    ])
}

fn codex_cli(shell_path: Option<&str>) -> Option<PathBuf> {
    if let Some(p) = first_cli(&["codex"], shell_path) {
        return Some(p);
    }
    for app in [PathBuf::from("/Applications/ChatGPT.app"),
                PathBuf::from("/Applications/Codex.app"),
                home().join("Applications/ChatGPT.app"),
                home().join("Applications/Codex.app")] {
        if let Some(cli) = bundled_codex_cli(app) {
            return Some(cli);
        }
    }
    first_existing([home().join(".codex/plugins/.plugin-appserver/codex")])
}

pub fn list_local_tools(shell_path: Option<&str>) -> Vec<LocalTool> {
    let cursor_app = app_bundle("Cursor");
    let cursor_cli = cursor_cli(shell_path);
    let claude_app = app_bundle("Claude");
    let claude_cli = claude_cli(shell_path);
    let codex_app = codex_app();
    let codex_cli = codex_cli(shell_path);
    let opencode_cli = first_cli(&["opencode"], shell_path)
        .or_else(|| usable_bin(home().join(".opencode/bin/opencode")));
    let hermes = which_in_path("hermes", shell_path).or_else(|| {
        let p = hermes_bin_path();
        p.is_file().then_some(p)
    });

    vec![
        LocalTool {
            name: "hermes".into(),
            label: "Supymem Computer Use".into(),
            kind: "computer_use".into(),
            installed: hermes.is_some(),
            connected: hermes.is_some(),
            signed_in: None,
            coding_available: false,
            app_path: None,
            cli_path: hermes.map(|p| p.to_string_lossy().into()),
        },
        LocalTool {
            name: "cursor".into(),
            label: "Cursor".into(),
            kind: "ide".into(),
            installed: cursor_app.is_some() || cursor_cli.is_some(),
            connected: false,
            signed_in: None,
            coding_available: false,
            app_path: cursor_app.map(|p| p.to_string_lossy().into()),
            cli_path: cursor_cli.map(|p| p.to_string_lossy().into()),
        },
        LocalTool {
            name: "claude".into(),
            label: "Claude Code".into(),
            kind: "ide".into(),
            installed: claude_app.is_some() || claude_cli.is_some(),
            connected: false,
            signed_in: None,
            coding_available: false,
            app_path: claude_app.map(|p| p.to_string_lossy().into()),
            cli_path: claude_cli.map(|p| p.to_string_lossy().into()),
        },
        LocalTool {
            name: "codex".into(),
            label: "Codex".into(),
            kind: "ide".into(),
            installed: codex_app.is_some() || codex_cli.is_some(),
            connected: false,
            signed_in: None,
            coding_available: false,
            app_path: codex_app.map(|p| p.to_string_lossy().into()),
            cli_path: codex_cli.map(|p| p.to_string_lossy().into()),
        },
        LocalTool {
            name: "opencode".into(),
            label: "OpenCode".into(),
            kind: "ide".into(),
            installed: opencode_cli.is_some(),
            connected: false,
            signed_in: None,
            coding_available: false,
            app_path: None,
            cli_path: opencode_cli.map(|p| p.to_string_lossy().into()),
        },
    ]
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CodingAgentStatus {
    pub name: String,
    pub installed: bool,
    pub signed_in: bool,
    pub subscription_login: bool,
    pub available: bool,
}

pub fn apply_coding_statuses(tools: &mut [LocalTool], agents: &[CodingAgentStatus]) {
    for tool in tools.iter_mut().filter(|tool| matches!(tool.name.as_str(), "claude" | "codex" | "cursor" | "opencode")) {
        tool.signed_in = None;
        tool.connected = false;
        tool.coding_available = false;
        if let Some(status) = agents.iter().find(|status| status.name == tool.name) {
            tool.signed_in = Some(status.installed && status.signed_in);
            tool.coding_available = tool.installed && tool.cli_path.is_some() && status.installed
                && status.signed_in && status.available && status.subscription_login;
            tool.connected = tool.coding_available;
        }
    }
}

pub fn connect_local_tool(name: &str, shell_path: Option<&str>) -> Result<LocalTool, String> {
    let name = name.trim().to_lowercase();
    if name == "hermes" {
        return list_local_tools(shell_path)
            .into_iter()
            .find(|t| t.name == "hermes")
            .ok_or_else(|| "Supymem Computer Use is not installed yet.".into());
    }
    let tools = list_local_tools(shell_path);
    let tool = tools
        .iter()
        .find(|t| t.name == name)
        .cloned()
        .ok_or_else(|| "Open Settings > Keys and connections > Coding tools and choose an installed tool.".to_string())?;
    if !tool.installed {
        return Err(format!(
            "{} is not on this computer. Install its CLI, then open Settings > Keys and connections > Coding tools and re-detect.",
            tool.label
        ));
    }
    if matches!(name.as_str(), "claude" | "codex" | "cursor" | "opencode") && tool.cli_path.is_some() {
        return Ok(tool);
    }
    if let Some(app) = &tool.app_path {
        let launched = Command::new("open")
            .arg("-a")
            .arg(app)
            .status()
            .map_err(|_| format!("Open {} yourself, then return to Settings > Keys and connections > Coding tools and re-detect.", tool.label))?;
        if !launched.success() {
            return Err(format!("Open {} yourself, then return to Settings > Keys and connections > Coding tools and re-detect.", tool.label));
        }
    } else {
        return Err(format!("Sign in to {} in Terminal, then open Settings > Keys and connections > Coding tools and re-detect.", tool.label));
    }
    if name == "cursor" {
        std::thread::sleep(Duration::from_millis(400));
        let perm = crate::perms::request_cursor_control();
        if !perm.ready {
            return Err(perm.message);
        }
    }
    // Opening an app and granting Accessibility never proves provider sign-in.
    Ok(list_local_tools(shell_path)
        .into_iter()
        .find(|t| t.name == name)
        .unwrap_or(tool))
}

/// Mirrors console BETA_COMPUTER_USE (apps/console/src/lib/macPermissions.ts). Keep both in step.
pub const BETA_COMPUTER_USE: bool = false;

pub fn setup_needed() -> bool {
    if !BETA_COMPUTER_USE { return false; }
    let stamp = conventional_data_dir().join("mac_setup_seen.json");
    if !stamp.is_file() {
        return true;
    }
    if crate::docker::docker_prompt_seen() {
        return false;
    }
    !crate::docker::engine_is_up()
}

pub fn mark_setup_seen() -> Result<(), String> {
    let dir = conventional_data_dir();
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    fs::write(dir.join("mac_setup_seen.json"), "{\"seen\":true}\n").map_err(|e| e.to_string())?;
    crate::docker::mark_docker_seen()
}

pub fn desktop_page_guards_js() -> &'static str {
    r#"(function(){
      function hideLegacy(){
        if (!document.body) return;
        var workspace = document.querySelector("section.workspace");
        if (workspace && workspace.style.display === "none") workspace.style.display = "";
        var body = document.querySelector(".workspace-body");
        if (body && body.style.display === "none") body.style.display = "";
        var headings = document.body.querySelectorAll("h1,h2,h3,h4");
        for (var i = 0; i < headings.length; i++) {
          var n = headings[i];
          var t = (n.textContent || "").replace(/\s+/g, " ").trim();
          if (!/^Control Cursor\s*\/\s*Claude$/i.test(t)) continue;
          var card = n.closest(".panel-card");
          if (!card || card.classList.contains("workspace") || card.querySelector(".workspace-body, .sidebar")) continue;
          card.style.display = "none";
        }
      }
      hideLegacy();
      if (!window.__supymemPageGuards) {
        window.__supymemPageGuards = true;
        setInterval(hideLegacy, 500);
      }
    })();"#
}

#[cfg(test)]
mod tests {
    use super::{bundled_codex_cli, first_existing};
    use std::path::PathBuf;

    #[test]
    fn first_existing_skips_missing() {
        assert!(first_existing([PathBuf::from("/no/such/supymem-tool")]).is_none());
    }

    #[test]
    fn bundled_codex_cli_finds_nested_chatgpt_binary() {
        let root = std::env::temp_dir().join(format!("supymem-codex-cli-{}", std::process::id()));
        let app = root.join("ChatGPT.app");
        let binary = app.join("Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex");
        std::fs::create_dir_all(binary.parent().unwrap()).unwrap();
        std::fs::write(&binary, b"test").unwrap();
        assert_eq!(bundled_codex_cli(app), Some(binary));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn codex_detection_does_not_substitute_another_app() {
        let tools = super::list_local_tools(None);
        let codex = tools.iter().find(|t| t.name == "codex").expect("codex row");
        if let Some(path) = &codex.app_path {
            assert_eq!(PathBuf::from(path).file_name().unwrap(), "Codex.app");
        }
    }

    #[test]
    fn detection_does_not_claim_external_provider_authentication() {
        let tools = super::list_local_tools(None);
        for tool in tools.iter().filter(|tool| tool.name != "hermes") {
            assert!(
                !tool.connected,
                "app detection is not account authentication"
            );
        }
    }

    #[test]
    fn coding_status_is_boolean_only_and_requires_subscription_login() {
        let mut tools=vec![super::LocalTool {name:"claude".into(),label:"Claude Code".into(),kind:"ide".into(),
            installed:true,connected:false,signed_in:None,coding_available:false,app_path:None,cli_path:Some("/bin/claude".into())},
            super::LocalTool {name:"cursor".into(),label:"Cursor".into(),kind:"ide".into(),installed:true,
                connected:false,signed_in:None,coding_available:false,app_path:None,cli_path:Some("/bin/cursor".into())}];
        let statuses=vec![super::CodingAgentStatus {name:"claude".into(),installed:true,signed_in:true,
            subscription_login:false,available:true}, super::CodingAgentStatus {name:"cursor".into(),installed:true,
                signed_in:true,subscription_login:true,available:true}];
        super::apply_coding_statuses(&mut tools,&statuses);
        assert_eq!(tools[0].signed_in,Some(true)); assert!(!tools[0].connected);
        assert!(!tools[0].coding_available); assert!(tools[1].connected);
        assert!(tools[1].coding_available);
    }

    #[test]
    fn redetection_clears_stale_readiness_and_requires_a_cli() {
        for (provider, venture) in [("codex", "stockroom-scheduler"), ("cursor", "recipe-journal"), ("opencode", "stockroom-scheduler")] {
            let mut tools = vec![super::LocalTool { name: provider.into(), label: provider.into(), kind: "ide".into(),
                installed: true, connected: true, signed_in: Some(true), coding_available: true,
                app_path: None, cli_path: Some(format!("/{venture}/{provider}")) }];
            super::apply_coding_statuses(&mut tools, &[]);
            assert!(!tools[0].connected && !tools[0].coding_available);
            assert_eq!(tools[0].signed_in, None);
            let status = super::CodingAgentStatus { name: provider.into(), installed: true,
                signed_in: true, subscription_login: true, available: true };
            super::apply_coding_statuses(&mut tools, &[status]);
            assert!(tools[0].connected && tools[0].coding_available);
            tools[0].cli_path = None;
            let status = super::CodingAgentStatus { name: provider.into(), installed: true,
                signed_in: true, subscription_login: true, available: true };
            super::apply_coding_statuses(&mut tools, &[status]);
            assert!(!tools[0].connected && !tools[0].coding_available);
        }
    }

    #[test]
    fn opencode_can_be_signed_in_without_supporting_builds() {
        for venture in ["stockroom-scheduler", "recipe-journal"] {
            let mut tools = [super::LocalTool { name: "opencode".into(), label: "OpenCode".into(), kind: "ide".into(),
                installed: true, connected: true, signed_in: None, coding_available: true,
                app_path: None, cli_path: Some(format!("/{venture}/opencode")) }];
            let status = super::CodingAgentStatus { name: "opencode".into(), installed: true,
                signed_in: true, subscription_login: true, available: false };
            super::apply_coding_statuses(&mut tools, &[status]);
            assert_eq!(tools[0].signed_in, Some(true));
            assert!(!tools[0].connected && !tools[0].coding_available);
        }
    }

    #[test]
    fn cli_only_connection_does_not_launch_or_claim_authentication() {
        let root = std::env::temp_dir().join(format!("supymem-connect-{}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        for (provider, binary) in [("claude", "claude"), ("codex", "codex"), ("cursor", "cursor-agent"), ("opencode", "opencode")] {
            std::fs::write(root.join(binary), b"not an executable").unwrap();
            let tool = super::connect_local_tool(provider, root.to_str()).unwrap();
            assert!(tool.installed && tool.cli_path.is_some());
            assert!(!tool.connected && !tool.coding_available);
            assert_eq!(tool.signed_in, None);
        }
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn page_guards_never_hide_the_workspace_section() {
        let js = super::desktop_page_guards_js();
        assert!(!js.contains(r#"closest("section"#));
        assert!(js.contains("section.workspace"));
        assert!(js.contains(r#"workspace.style.display = """#));
        assert!(js.contains("closest(\".panel-card\")"));
        assert!(!js.contains("supymem-mac-setup-card"));
        assert!(!js.contains("Connect tools on this Mac"));
    }
}
