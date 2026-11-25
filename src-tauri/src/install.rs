//! One-click Hermes install. Not Cursor, Claude, Codex, or OpenCode.
//!
//! Creates an isolated venv under Application Support/Supymem and pip-installs
//! `hermes-agent[acp]` so we can spawn `hermes acp`.
//!
//! This module must not run unless the founder clicked Install. It does not
//! write a Hermes config until `write_hermes_config` is called with a
//! platform `sk_app_` key.

use std::fs;

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde::Serialize;

use crate::supervisor::{conventional_data_dir, which_in_path};

#[derive(Clone, Serialize)]
pub struct InstallResult {
    pub ok: bool,
    pub already: bool,
    pub path: String,
    pub message: String,
}

pub fn hermes_venv_dir() -> PathBuf {
    conventional_data_dir().join("hermes-venv")
}

pub fn hermes_bin_path() -> PathBuf {
    hermes_venv_dir().join("bin").join("hermes")
}

pub fn install_local_builder(
    shell_path: Option<&str>,
    mut on_progress: impl FnMut(&str),
) -> Result<InstallResult, String> {
    if let Some(existing) = which_in_path("hermes", shell_path) {
        stamp_consent("already-on-path")?;
        return Ok(InstallResult {
            ok: true,
            already: true,
            path: existing.to_string_lossy().into(),
            message: "Supymem Computer Use is already on this Mac.".into(),
        });
    }

    let python = pick_python(shell_path)?;
    let venv = hermes_venv_dir();
    let dest = hermes_bin_path();

    if dest.is_file() && venv_python_ok(&venv) {
        stamp_consent("already-in-venv")?;
        return Ok(InstallResult {
            ok: true,
            already: true,
            path: dest.to_string_lossy().into(),
            message: "Supymem Computer Use is already on this Mac.".into(),
        });
    }

    if venv.exists() && !venv_python_ok(&venv) {
        on_progress("Replacing an old Python environment (3.11+ is required)…");
        fs::remove_dir_all(&venv).map_err(|e| format!("Could not reset the install folder: {e}"))?;
    }

    fs::create_dir_all(venv.parent().unwrap_or(venv.as_path())).map_err(|e| e.to_string())?;
    if !venv.join("bin").join("python").is_file() {
        on_progress("Creating a local Python environment…");
        let out = command_with_path(&python, shell_path)
            .args(["-m", "venv"])
            .arg(&venv)
            .output()
            .map_err(|e| format!("Could not create a Python environment: {e}"))?;
        if !out.status.success() {
            return Err(format!(
                "Could not create a Python environment for Supymem Computer Use. {}",
                tail_output(&out)
            ));
        }
    }
    let pip = venv.join("bin").join("pip");
    on_progress("Preparing the installer…");
    let pip_ok = command_with_path(&pip, shell_path)
        .args(["install", "-U", "pip", "--disable-pip-version-check"])
        .output()
        .map_err(|e| e.to_string())?;
    if !pip_ok.status.success() {
        return Err(explain_pip_failure("Could not update pip", &pip_ok));
    }
    on_progress("Downloading Supymem Computer Use. This can take a minute — the app stays usable.");
    let install = command_with_path(&pip, shell_path)
        .args(["install", "hermes-agent[acp]", "--disable-pip-version-check"])
        .output()
        .map_err(|e| format!("Could not download Supymem Computer Use: {e}"))?;
    if !install.status.success() {
        return Err(explain_pip_failure(
            "Could not install Supymem Computer Use",
            &install,
        ));
    }
    if !dest.is_file() {
        return Err("Supymem Computer Use installed but its command was not created.".into());
    }
    stamp_consent("installed")?;
    Ok(InstallResult {
        ok: true,
        already: false,
        path: dest.to_string_lossy().into(),
        message: "Supymem Computer Use is installed on this Mac.".into(),
    })
}

pub fn uninstall_local_builder() -> Result<InstallResult, String> {
    let venv = hermes_venv_dir();
    let dest = hermes_bin_path();
    if venv.exists() {
        fs::remove_dir_all(&venv).map_err(|e| format!("Could not remove Supymem Computer Use: {e}"))?;
    }
    let consent = conventional_data_dir().join("local_builder_consent.json");
    let _ = fs::remove_file(consent);
    Ok(InstallResult {
        ok: true,
        already: false,
        path: dest.to_string_lossy().into(),
        message: "Supymem Computer Use was removed from this Mac. Click Install to put it back.".into(),
    })
}

pub fn write_hermes_config(base_url: &str, model: &str, api_key: &str) -> Result<(), String> {
    let base = base_url.trim();
    let model = model.trim();
    let key = api_key.trim();
    let parsed = url::Url::parse(base).map_err(|_| "Could not configure Supymem Computer Use: bad server address.".to_string())?;
    let origin_allowed = parsed.username().is_empty() && parsed.password().is_none()
        && parsed.query().is_none() && parsed.fragment().is_none() && parsed.path() == "/openai/v1"
        && ((parsed.scheme() == "https" && parsed.port_or_known_default() == Some(443)
            && parsed.host_str() == Some(if crate::channel::staging() { "gateway.staging.supymem.com" } else { "gateway.supymem.com" }))
            || ((cfg!(debug_assertions) || crate::channel::local_review()) && parsed.scheme() == "http"
                && matches!(parsed.host_str(), Some("127.0.0.1" | "localhost" | "[::1]"))));
    if !origin_allowed { return Err("Could not configure Supymem Computer Use: bad server address.".into()); }
    if !matches!(model, "supymem-flash" | "supymem-pro") {
        return Err("Could not configure Supymem Computer Use: unsupported model.".into());
    }
    if !key.starts_with("sk_app_") || key.len() > 512 || key.len() < 20
        || !key.bytes().all(|b| b.is_ascii_alphanumeric() || b"_-.".contains(&b)) {
        return Err("Could not configure Supymem Computer Use: the platform key was missing.".into());
    }
    let home = dirs::home_dir().ok_or_else(|| "no home directory".to_string())?;
    let hermes_home = home.join(".hermes");
    fs::create_dir_all(&hermes_home).map_err(|e| e.to_string())?;

    let yaml = format!(
        "# Written by Supymem after you clicked Install. Uses workspace credits.\n\
         model:\n\
           provider: custom\n\
           default: {model}\n\
           base_url: {base}\n\
           api_key: {key}\n\
         providers:\n\
           supymem:\n\
             api: {base}\n\
             base_url: {base}\n\
             key_env: SUPYMEM_AI_API_KEY\n\
             api_mode: chat_completions\n\
             transport: chat_completions\n\
             type: openai\n\
             default_model: {model}\n\
             models:\n\
               - supymem-flash\n\
               - supymem-pro\n"
    );
    let env_body = format!(
        "SUPYMEM_AI_API_KEY={key}\nOPENAI_API_KEY={key}\nOPENAI_BASE_URL={base}\n"
    );
    let cfg = hermes_home.join("config.yaml");
    let env_path = hermes_home.join(".env");
    crate::native_security::write_private(&cfg, yaml.as_bytes()).map_err(|e| e.to_string())?;
    crate::native_security::write_private(&env_path, env_body.as_bytes()).map_err(|e| e.to_string())?;
    Ok(())
}

pub fn write_hermes_files(config_yaml: &str, env_file: &str) -> Result<(), String> {
    let yaml = config_yaml.trim();
    let env_body = env_file.trim();
    if yaml.is_empty() || env_body.is_empty() {
        return Err("Could not configure Supymem Computer Use: empty config.".into());
    }
    if !env_body.contains("sk_app_") {
        return Err("Could not configure Supymem Computer Use: the platform key was missing.".into());
    }
    let home = dirs::home_dir().ok_or_else(|| "no home directory".to_string())?;
    let hermes_home = home.join(".hermes");
    fs::create_dir_all(&hermes_home).map_err(|e| e.to_string())?;
    let cfg = hermes_home.join("config.yaml");
    let env_path = hermes_home.join(".env");
    crate::native_security::write_private(&cfg, format!("{yaml}\n").as_bytes()).map_err(|e| e.to_string())?;
    crate::native_security::write_private(&env_path, format!("{env_body}\n").as_bytes()).map_err(|e| e.to_string())?;
    Ok(())
}

fn stamp_consent(how: &str) -> Result<(), String> {
    let dir = conventional_data_dir();
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let body = format!("{{\"adapter\":\"hermes\",\"how\":\"{how}\",\"consented\":true}}\n");
    fs::write(dir.join("local_builder_consent.json"), body).map_err(|e| e.to_string())
}

fn command_with_path(bin: &Path, shell_path: Option<&str>) -> Command {
    let mut cmd = Command::new(bin);
    if let Some(path) = shell_path.filter(|s| !s.is_empty()) {
        cmd.env("PATH", path);
    }
    cmd
}

fn python_candidates(shell_path: Option<&str>) -> Vec<PathBuf> {
    let mut out = Vec::new();
    for name in ["python3.13", "python3.12", "python3.11", "python3"] {
        if let Some(p) = which_in_path(name, shell_path) {
            out.push(p);
        }
    }
    const FIXED: &[&str] = &[
        "/opt/homebrew/bin/python3.13",
        "/opt/homebrew/bin/python3.12",
        "/opt/homebrew/bin/python3.11",
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3.13",
        "/usr/local/bin/python3.12",
        "/usr/local/bin/python3.11",
        "/usr/local/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3",
        "/opt/anaconda3/bin/python3",
        "/opt/anaconda3/bin/python",
    ];
    for p in FIXED {
        let pb = PathBuf::from(p);
        if pb.is_file() {
            out.push(pb);
        }
    }
    out
}

fn python_minor(bin: &Path) -> Option<(u32, u32)> {
    let out = Command::new(bin)
        .args([
            "-c",
            "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
        ])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    parse_minor(&String::from_utf8_lossy(&out.stdout))
}

fn parse_minor(raw: &str) -> Option<(u32, u32)> {
    let line = raw.trim().lines().next()?.trim();
    let mut parts = line.split('.');
    let major = parts.next()?.parse().ok()?;
    let minor = parts.next()?.parse().ok()?;
    Some((major, minor))
}

fn python_ok(maj: u32, min: u32) -> bool {
    maj == 3 && (11..14).contains(&min)
}

fn pick_python(shell_path: Option<&str>) -> Result<PathBuf, String> {
    for p in python_candidates(shell_path) {
        if let Some((maj, min)) = python_minor(&p) {
            if python_ok(maj, min) {
                return Ok(p);
            }
        }
    }
    Err(
        "Supymem Computer Use needs Python 3.11, 3.12, or 3.13. This Mac’s system Python is too old, \
         and the app could not see a newer one. Install Python 3.12 from python.org or run \
         `brew install python@3.12`, then click Install again."
            .into(),
    )
}

fn venv_python_ok(venv: &Path) -> bool {
    python_minor(&venv.join("bin").join("python"))
        .map(|(maj, min)| python_ok(maj, min))
        .unwrap_or(false)
}

fn tail_output(out: &Output) -> String {
    let mut text = String::new();
    text.push_str(&String::from_utf8_lossy(&out.stderr));
    if text.trim().is_empty() {
        text.push_str(&String::from_utf8_lossy(&out.stdout));
    }
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return "No error text from the installer.".into();
    }
    let last: String = trimmed
        .lines()
        .rev()
        .take(8)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect::<Vec<_>>()
        .join(" ");
    if last.chars().count() > 280 {
        last.chars().skip(last.chars().count() - 280).collect()
    } else {
        last
    }
}

fn explain_pip_failure(prefix: &str, out: &Output) -> String {
    let blob = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stderr),
        String::from_utf8_lossy(&out.stdout)
    );
    if blob.contains("No matching distribution")
        || blob.contains("Requires-Python")
        || blob.contains("Ignored the following versions that require a different python")
    {
        return "Supymem Computer Use needs Python 3.11, 3.12, or 3.13. The installer was using an older Python. Click Install again — this build looks for Homebrew Python first.".into();
    }
    if blob.contains("Could not find a version") {
        return format!("{prefix}. The package index had no matching build. {tail}", tail = tail_output(out));
    }
    let lower = blob.to_lowercase();
    if lower.contains("network")
        || lower.contains("connection")
        || lower.contains("timed out")
        || lower.contains("temporary failure in name resolution")
        || lower.contains("failed to establish")
        || lower.contains("max retries exceeded")
    {
        return format!("{prefix}. Check the internet and try again.");
    }
    format!("{prefix}. {}", tail_output(out))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_minor_reads_major_minor() {
        assert_eq!(parse_minor("3.13\n"), Some((3, 13)));
        assert_eq!(parse_minor("3.9"), Some((3, 9)));
        assert_eq!(parse_minor("garbage"), None);
    }

    #[test]
    fn python_ok_matches_hermes_range() {
        assert!(!python_ok(3, 9));
        assert!(!python_ok(3, 10));
        assert!(python_ok(3, 11));
        assert!(python_ok(3, 13));
        assert!(!python_ok(3, 14));
    }

    #[test]
    fn pip_failure_does_not_blame_internet_for_python_version() {
        #[cfg(unix)]
        let status = std::os::unix::process::ExitStatusExt::from_raw(1 << 8);
        #[cfg(windows)]
        let status = std::os::windows::process::ExitStatusExt::from_raw(1);
        let out = Output {
            status,
            stdout: Vec::new(),
            stderr: b"ERROR: Ignored the following versions that require a different python version: 0.19.0 Requires-Python >=3.11\nERROR: No matching distribution found for hermes-agent[acp]\n".to_vec(),
        };
        let msg = explain_pip_failure("Could not install", &out);
        assert!(!msg.to_lowercase().contains("internet"), "{msg}");
        assert!(msg.contains("3.11"), "{msg}");
    }
}
