//! Detect and install Docker Desktop. Never runs unless the founder clicks Install or Start.

use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use serde::Serialize;

use crate::install::InstallResult;
use crate::supervisor::{conventional_data_dir, which_in_path};

#[derive(Clone, Serialize)]
pub struct DockerStatus {
    pub installed: bool,
    pub engine_running: bool,
    pub skipped: bool,
    pub app_path: Option<String>,
    pub cli_path: Option<String>,
    pub message: String,
}

pub fn docker_app_path() -> Option<PathBuf> {
    let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("/"));
    for p in [
        PathBuf::from("/Applications/Docker.app"),
        home.join("Applications/Docker.app"),
    ] {
        if p.is_dir() {
            return Some(p);
        }
    }
    None
}

fn docker_cli(shell_path: Option<&str>) -> Option<PathBuf> {
    if let Some(p) = which_in_path("docker", shell_path) {
        if p.is_file() {
            return Some(p);
        }
    }
    for p in [
        PathBuf::from("/usr/local/bin/docker"),
        PathBuf::from("/opt/homebrew/bin/docker"),
        dirs::home_dir()
            .unwrap_or_else(|| PathBuf::from("/"))
            .join(".docker/bin/docker"),
    ] {
        if p.is_file() {
            return Some(p);
        }
    }
    None
}

fn run_timed(mut cmd: Command, budget: Duration) -> Option<bool> {
    cmd.stdout(Stdio::null()).stderr(Stdio::null());
    let mut child = cmd.spawn().ok()?;
    let deadline = Instant::now() + budget;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Some(status.success()),
            Ok(None) if Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(40)),
            Err(_) => return None,
        }
    }
}

pub fn engine_is_up() -> bool {
    engine_is_up_with(None)
}

fn engine_is_up_with(shell_path: Option<&str>) -> bool {
    let Some(cli) = docker_cli(shell_path) else {
        return false;
    };
    let mut cmd = Command::new(cli);
    cmd.args(["info"]);
    run_timed(cmd, Duration::from_secs(2)).unwrap_or(false)
}

fn docker_seen_path() -> PathBuf {
    conventional_data_dir().join("docker_setup_seen.json")
}

pub fn docker_prompt_seen() -> bool {
    docker_seen_path().is_file()
}

pub fn mark_docker_seen() -> Result<(), String> {
    let dir = conventional_data_dir();
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    fs::write(docker_seen_path(), "{\"seen\":true}\n").map_err(|e| e.to_string())
}

pub fn status(shell_path: Option<&str>) -> DockerStatus {
    let app = docker_app_path();
    let cli = docker_cli(shell_path);
    let installed = app.is_some() || cli.is_some();
    let engine_running = installed && engine_is_up_with(shell_path);
    if engine_running {
        let _ = mark_docker_seen();
    }
    let skipped = docker_prompt_seen();
    let message = if engine_running {
        "Docker is installed and its engine is running.".into()
    } else if installed {
        "Docker Desktop is installed, but its engine is stopped."
            .into()
    } else {
        "Docker was not detected. It is optional for account access and can be installed later."
            .into()
    };
    DockerStatus {
        installed,
        engine_running,
        skipped,
        app_path: app.map(|p| p.to_string_lossy().into()),
        cli_path: cli.map(|p| p.to_string_lossy().into()),
        message,
    }
}

pub fn start_docker() -> Result<InstallResult, String> {
    let app = docker_app_path().ok_or_else(|| {
        "Docker Desktop is not installed yet. Click Install Docker, then Start.".to_string()
    })?;
    let status = Command::new("open")
        .arg("-a")
        .arg(&app)
        .status()
        .map_err(|e| e.to_string())?;
    if !status.success() {
        return Err("Couldn’t open Docker Desktop.".into());
    }
    let _ = mark_docker_seen();
    Ok(InstallResult {
        ok: true,
        already: true,
        path: app.to_string_lossy().into(),
        message: "Docker Desktop is starting. It can take a minute. Local preview works after the whale icon is steady."
            .into(),
    })
}

fn dmg_url() -> &'static str {
    if cfg!(target_arch = "aarch64") {
        "https://desktop.docker.com/mac/main/arm64/Docker.dmg"
    } else {
        "https://desktop.docker.com/mac/main/amd64/Docker.dmg"
    }
}

fn download_dmg(url: &str, dest: &Path, mut on_progress: impl FnMut(&str)) -> Result<(), String> {
    on_progress("Downloading Docker Desktop…");
    if let Some(parent) = dest.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let client = reqwest::blocking::Client::builder().user_agent(crate::http::USER_AGENT)
        .timeout(Duration::from_secs(600))
        .build()
        .map_err(|e| e.to_string())?;
    let mut res = client
        .get(url)
        .send()
        .map_err(|e| format!("Could not download Docker Desktop: {e}"))?;
    if !res.status().is_success() {
        return Err(format!("Docker download failed ({}).", res.status()));
    }
    let mut file = fs::File::create(dest).map_err(|e| e.to_string())?;
    let mut copied = 0u64;
    let mut buf = [0u8; 64 * 1024];
    loop {
        let n = res.read(&mut buf).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        file.write_all(&buf[..n]).map_err(|e| e.to_string())?;
        copied += n as u64;
        if copied % (50 * 1024 * 1024) < 64 * 1024 {
            on_progress(&format!(
                "Downloading Docker Desktop… {} MB",
                copied / (1024 * 1024)
            ));
        }
    }
    if copied < 1_000_000 {
        return Err("Docker download was too small. Check the internet and try again.".into());
    }
    Ok(())
}

fn copy_app_from_dmg(dmg: &Path, mut on_progress: impl FnMut(&str)) -> Result<PathBuf, String> {
    on_progress("Opening the Docker installer…");
    let mount = conventional_data_dir().join("installers/docker-mount");
    let _ = Command::new("hdiutil")
        .args(["detach"])
        .arg(&mount)
        .status();
    let _ = fs::remove_dir_all(&mount);
    fs::create_dir_all(&mount).map_err(|e| e.to_string())?;
    let attach = Command::new("hdiutil")
        .args(["attach", "-nobrowse", "-readonly"])
        .arg("-mountpoint")
        .arg(&mount)
        .arg(dmg)
        .status()
        .map_err(|e| e.to_string())?;
    if !attach.success() {
        let _ = Command::new("open").arg(dmg).status();
        return Err(
            "Downloaded Docker Desktop. Open the DMG, drag Docker to Applications, then click Start Docker."
                .into(),
        );
    }
    let src = mount.join("Docker.app");
    if !src.is_dir() {
        let _ = Command::new("hdiutil").args(["detach"]).arg(&mount).status();
        let _ = Command::new("open").arg(dmg).status();
        return Err(
            "The Docker installer did not contain Docker.app. Open the DMG and drag it to Applications."
                .into(),
        );
    }
    let dest = PathBuf::from("/Applications/Docker.app");
    on_progress("Installing Docker Desktop into Applications…");
    if dest.exists() {
        let _ = fs::remove_dir_all(&dest);
    }
    let copied = Command::new("cp")
        .args(["-R"])
        .arg(&src)
        .arg(&dest)
        .status()
        .map_err(|e| e.to_string())?;
    let _ = Command::new("hdiutil").args(["detach"]).arg(&mount).status();
    if !copied.success() || !dest.is_dir() {
        let _ = Command::new("open").arg(dmg).status();
        return Err(
            "Couldn’t copy Docker to Applications. The DMG is open — drag Docker there, then click Start Docker."
                .into(),
        );
    }
    Ok(dest)
}

pub fn install_docker(
    shell_path: Option<&str>,
    mut on_progress: impl FnMut(&str),
) -> Result<InstallResult, String> {
    if docker_app_path().is_some() {
        let started = start_docker()?;
        return Ok(InstallResult {
            ok: true,
            already: true,
            path: started.path,
            message: "Docker Desktop is already installed. Starting it now.".into(),
        });
    }
    let dest = conventional_data_dir().join("installers/Docker.dmg");
    if let Err(e) = download_dmg(dmg_url(), &dest, &mut on_progress) {
        let _ = Command::new("open")
            .arg("https://www.docker.com/products/docker-desktop/")
            .status();
        return Err(format!(
            "{e} Opened the Docker download page. Install Docker Desktop, then come back and click Start Docker."
        ));
    }
    let app = copy_app_from_dmg(&dest, &mut on_progress)?;
    on_progress("Starting Docker Desktop…");
    let _ = Command::new("open").arg("-a").arg(&app).status();
    let _ = mark_docker_seen();
    let _ = status(shell_path);
    Ok(InstallResult {
        ok: true,
        already: false,
        path: app.to_string_lossy().into(),
        message: "Docker Desktop was installed and a start request was sent. Check its engine status before using it."
            .into(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_does_not_panic() {
        let s = status(None);
        assert!(!s.message.is_empty());
        if docker_app_path().is_none() && docker_cli(None).is_none() {
            assert!(!s.installed);
            assert!(!s.engine_running);
        }
    }
}
