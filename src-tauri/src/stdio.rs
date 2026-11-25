//! Pipe-backed coding-agent sessions (ACP / stream-json).
//!
//! Interactive TUIs stay in `pty.rs`. This hub is the Grok-Bot-style path:
//! spawn `cursor-agent acp`, `opencode acp`, `claude -p --output-format stream-json`,
//! or `codex exec --json`, write JSON-RPC / prompts on stdin, stream stdout to the console.

use std::collections::{HashMap, VecDeque};
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, ExitStatus, Stdio};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde::Serialize;
use tauri::{AppHandle, Emitter};
use uuid::Uuid;

use crate::install::hermes_bin_path;
use crate::supervisor::{scrub_env, which_in_path, unix_new_session};

pub struct StdioHub {
    inner: Arc<Mutex<ProcessRegistry>>,
}

#[derive(Default)]
struct ProcessRegistry {
    sessions: HashMap<String, Session>,
    exited: VecDeque<String>,
}

struct Session {
    stdin: ChildStdin,
    child: Child,
    provider: String,
    stopped: bool,
}

#[derive(Clone, Serialize)]
pub struct SessionInfo {
    pub id: String,
    pub provider: String,
}

#[derive(Clone, Serialize)]
pub struct StdioChunk {
    pub id: String,
    pub data: String,
}

#[derive(Clone, Serialize)]
pub struct StdioExit {
    pub id: String,
    pub code: Option<i32>,
}

#[derive(Clone, Serialize)]
pub struct LocalCli {
    pub name: String,
    pub bin: String,
    pub path: String,
}

impl StdioHub {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(ProcessRegistry::default())),
        }
    }

    pub fn list(&self) -> Vec<SessionInfo> {
        self.inner
            .lock()
            .map(|g| {
                g.sessions.iter()
                    .map(|(id, s)| SessionInfo {
                        id: id.clone(),
                        provider: s.provider.clone(),
                    })
                    .collect()
            })
            .unwrap_or_default()
    }

    pub fn write(&self, id: &str, data: &str) -> Result<(), String> {
        let mut g = self.inner.lock().map_err(|e| e.to_string())?;
        let session = g.sessions.get_mut(id).ok_or_else(|| "session not found".to_string())?;
        session
            .stdin
            .write_all(data.as_bytes())
            .map_err(|e| e.to_string())?;
        session.stdin.flush().ok();
        Ok(())
    }

    pub fn owns_session(&self, id: &str) -> Result<bool, String> {
        let g = self.inner.lock().map_err(|_| "process registry unavailable")?;
        Ok(g.sessions.contains_key(id) || g.exited.iter().any(|known| known == id))
    }

    pub fn kill(&self, id: &str) -> Result<(), String> {
        let mut g = self.inner.lock().map_err(|e| e.to_string())?;
        // A late stop after the observer reaped this exact process is confirmed.
        if g.exited.iter().any(|known| known == id) { return Ok(()); }
        let session = g.sessions.get_mut(id).ok_or("Your coding session could not be found. Open Builds to check its activity, then select Stop again.")?;
        if session.stopped { return Ok(()); }
        stop_coding_tree(session.child.id())?;
        session.child.wait().map_err(|_| "Your coding tool's Stop could not be verified. Select Stop again in Builds before starting another build.")?;
        session.stopped = true;
        Ok(())
    }

    pub fn kill_all(&self) -> Result<(), String> {
        let ids: Vec<String> = self.inner.lock()
            .map_err(|_| "process registry unavailable")?
            .sessions.keys().cloned().collect();
        let mut failures = Vec::new();
        for id in ids {
            if let Err(error) = self.kill(&id) { failures.push(format!("{id}: {error}")); }
        }
        if failures.is_empty() { Ok(()) } else { Err(failures.join("; ")) }
    }

    pub fn spawn(
        &self,
        app: AppHandle,
        provider: String,
        argv: Vec<String>,
        cwd: Option<String>,
        shell_path: Option<String>,
        extra_scrub: &[String],
    ) -> Result<String, String> {
        if argv.is_empty() {
            return Err("empty argv".into());
        }
        let id = Uuid::new_v4().to_string();
        let bin_name = argv[0].clone();
        let resolved = which_in_path(&bin_name, shell_path.as_deref())
            .map(|p| p.to_string_lossy().to_string())
            .unwrap_or(bin_name);
        if which_in_path(&argv[0], shell_path.as_deref()).is_none()
            && !std::path::Path::new(&resolved).is_file()
        {
            return Err(format!("{} is not on PATH", argv[0]));
        }

        let mut cmd = Command::new(&resolved);
        if argv.len() > 1 {
            cmd.args(&argv[1..]);
        }
        if let Some(dir) = cwd {
            cmd.current_dir(dir);
        }

        let mut env_map: HashMap<String, String> = std::env::vars().collect();
        if let Some(path) = &shell_path {
            env_map.insert("PATH".into(), path.clone());
        }
        let mut scrubbed = scrub_env(env_map);
        for k in extra_scrub {
            scrubbed.remove(k);
        }
        for k in [
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "LLM_API_KEY",
            "DEEPSEEK_API_KEY",
        ] {
            scrubbed.remove(k);
        }
        cmd.env_clear();
        for (k, v) in &scrubbed {
            cmd.env(k, v);
        }
        cmd.stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        unix_new_session(&mut cmd);

        let mut child = cmd.spawn().map_err(|e| format!("spawn {}: {e}", argv[0]))?;
        let stdin = child.stdin.take().ok_or("missing stdin")?;
        let stdout = child.stdout.take().ok_or("missing stdout")?;
        let stderr = child.stderr.take().ok_or("missing stderr")?;

        {
            let mut g = self.inner.lock().map_err(|e| e.to_string())?;
            g.sessions.insert(
                id.clone(),
                Session {
                    stdin,
                    child,
                    provider,
                    stopped: false,
                },
            );
        }

        let emit_id = id.clone();
        let app_out = app.clone();
        let (stdout_done, stdout_drained) = mpsc::channel();
        thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines() {
                match line {
                    Ok(data) => {
                        let _ = app_out.emit(
                            "stdio-output",
                            StdioChunk {
                                id: emit_id.clone(),
                                data: format!("{data}\n"),
                            },
                        );
                    }
                    Err(_) => break,
                }
            }
            // EOF is an output-stream event, not proof that the process exited.
            let _ = stdout_done.send(());
        });

        let emit_err = id.clone();
        let app_err = app.clone();
        thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines() {
                if let Ok(data) = line {
                    if data.trim().is_empty() {
                        continue;
                    }
                    let _ = app_err.emit(
                        "stdio-output",
                        StdioChunk {
                            id: emit_err.clone(),
                            data: format!("[stderr] {data}\n"),
                        },
                    );
                } else {
                    break;
                }
            }
        });

        let sessions = self.inner.clone();
        let exit_id = id.clone();
        thread::spawn(move || loop {
            match observe_process_exit(&sessions, &exit_id) {
                Ok(Some(status)) => {
                    // Usually immediate; bound the drain in case a descendant holds
                    // stdout open after the direct child exits.
                    let _ = stdout_drained.recv_timeout(Duration::from_secs(1));
                    let _ = app.emit("stdio-exit", StdioExit { id: exit_id, code: status.code() });
                    break;
                }
                Ok(None) => thread::sleep(Duration::from_millis(100)),
                Err(_) => {
                    let _ = app.emit("stdio-output", StdioChunk { id: exit_id,
                        data: "Your coding tool's Stop could not be verified. Select Stop again in Builds before starting another build.\n".into() });
                    break;
                }
            }
        });

        Ok(id)
    }
}

fn observe_process_exit(registry: &Mutex<ProcessRegistry>, id: &str) -> Result<Option<ExitStatus>, String> {
    let mut registry = registry.lock().map_err(|_| "process registry unavailable")?;
    let session = registry.sessions.get_mut(id).ok_or("process handle unavailable")?;
    #[cfg(unix)]
    if !session.stopped {
        if !coding_process_finished(session.child.id())? { return Ok(None); }
        stop_coding_tree(session.child.id())?;
    }
    let status = session.child.try_wait().map_err(|_| "process observation failed")?;
    if status.is_some() {
        #[cfg(not(unix))]
        if !session.stopped {
            stop_coding_tree(session.child.id())?;
        }
        registry.sessions.remove(id);
        registry.exited.push_back(id.into());
        if registry.exited.len() > 128 { registry.exited.pop_front(); }
    }
    Ok(status)
}

#[cfg(unix)]
pub(crate) fn coding_process_finished(pid: u32) -> Result<bool, String> {
    let mut status: libc::siginfo_t = unsafe { std::mem::zeroed() };
    // Keep the child unreaped until its group stops, so its process ID cannot be reused.
    if unsafe { libc::waitid(libc::P_PID, pid as libc::id_t, &mut status,
        libc::WEXITED | libc::WNOHANG | libc::WNOWAIT) } != 0 {
        return Err("Your coding tool's exit could not be verified. Select Stop again in Builds before starting another build.".into());
    }
    Ok(unsafe { status.si_pid() } != 0)
}

pub(crate) fn stop_coding_tree(pid: u32) -> Result<(), String> {
    let retry = "Your coding tool has not stopped yet. Select Stop again in Builds. If it still runs, quit your coding tool before reopening your app.";
    #[cfg(unix)] {
        if unsafe { libc::kill(-(pid as i32), libc::SIGKILL) } != 0
            && std::io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH) {
            return Err(retry.into());
        }
        for _attempt in 0..100 {
            let output = Command::new("/bin/ps").args(["-axo", "pgid=,stat="])
                .output().map_err(|_| retry.to_string())?;
            if !output.status.success() { return Err(retry.into()); }
            let running = String::from_utf8_lossy(&output.stdout).lines().any(|line| {
                let mut columns = line.split_whitespace();
                columns.next().and_then(|value| value.parse::<u32>().ok()) == Some(pid)
                    && columns.next().is_none_or(|status| !status.starts_with('Z'))
            });
            if !running { return Ok(()); }
            thread::sleep(Duration::from_millis(20));
        }
        Err(retry.into())
    }
    #[cfg(windows)] {
        let status = Command::new("taskkill").args(["/PID", &pid.to_string(), "/T", "/F"])
            .stdout(Stdio::null()).stderr(Stdio::null()).status().map_err(|_| retry.to_string())?;
        if status.success() { Ok(()) } else { Err(retry.into()) }
    }
    #[cfg(not(any(unix, windows)))] { let _ = pid; Err(retry.into()) }
}

pub fn stop_coding_process(stdio: &StdioHub, pty: &crate::pty::PtyHub, id: &str) -> Result<(), String> {
    // Never mask a failed stdio stop with a successful no-op in the PTY hub.
    if stdio.owns_session(id)? { return stdio.kill(id); }
    pty.kill(id)
}

#[cfg(all(test, unix))]
mod process_tests {
    use super::*;
    use std::io::Read;
    use std::time::Instant;

    fn child(script: &str) -> (StdioHub, Option<std::process::ChildStdout>) {
        let hub = StdioHub::new();
        let mut command = Command::new("/bin/sh");
        command.args(["-c", script]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
        unix_new_session(&mut command);
        let mut child = command.spawn().unwrap();
        let stdin = child.stdin.take().unwrap();
        let stdout = child.stdout.take();
        hub.inner.lock().unwrap().sessions.insert("test".into(), Session { stdin, child, provider: "test".into(), stopped: false });
        (hub, stdout)
    }

    #[test]
    fn closed_stdout_does_not_claim_exit_and_stop_waits_for_actual_child() {
        let (hub, stdout) = child("exec 1>&-; read value");
        stdout.unwrap().read_to_end(&mut Vec::new()).unwrap();
        assert!(observe_process_exit(&hub.inner, "test").unwrap().is_none());
        assert_eq!(hub.list().len(), 1);
        hub.kill("test").unwrap();
        assert!(observe_process_exit(&hub.inner, "test").unwrap().is_some());
        assert!(hub.list().is_empty());
        assert!(observe_process_exit(&hub.inner, "test").is_err());
        assert!(hub.owns_session("test").unwrap());
        // Exact verified exit survives a late Stop click through the real command route.
        stop_coding_process(&hub, &crate::pty::PtyHub::new(), "test").unwrap();
    }

    #[test]
    fn observation_reports_real_nonzero_exit_once() {
        let (hub, _stdout) = child("exit 7");
        let pid = hub.inner.lock().unwrap().sessions["test"].child.id();
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            if coding_process_finished(pid).unwrap() {
                assert!(coding_process_finished(pid).unwrap());
                let status = observe_process_exit(&hub.inner, "test").unwrap().unwrap();
                assert_eq!(status.code(), Some(7));
                break;
            }
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(10));
        }
        assert!(hub.list().is_empty());
    }

    #[test]
    fn shutdown_stops_a_live_pipe_child_and_is_repeatable() {
        let (hub, _stdout) = child("read value");
        hub.kill_all().unwrap();
        assert!(observe_process_exit(&hub.inner, "test").unwrap().is_some());
        hub.kill_all().unwrap();
        assert!(hub.list().is_empty());
    }

    #[test]
    fn missing_handle_cannot_confirm_stop() {
        assert!(StdioHub::new().kill("unknown").is_err());
        assert!(stop_coding_process(&StdioHub::new(), &crate::pty::PtyHub::new(), "unknown").is_err());
    }

    #[test]
    fn build_native_stop_kills_tool_descendants_for_unrelated_workspaces() {
        for name in ["supplier-audits", "garden-plans"] {
            let hub = StdioHub::new();
            let cwd = std::env::temp_dir().join(format!("native-stop-{}-{name}", Uuid::new_v4()));
            std::fs::create_dir_all(&cwd).unwrap();
            let mut command = Command::new("/bin/sh");
            command.args(["-c", "sleep 60 & echo $!; read value"]).current_dir(&cwd)
                .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
            crate::supervisor::unix_new_session(&mut command);
            let mut child = command.spawn().unwrap();
            let stdin = child.stdin.take().unwrap();
            let mut line = String::new();
            BufReader::new(child.stdout.take().unwrap()).read_line(&mut line).unwrap();
            let descendant: i32 = line.trim().parse().unwrap();
            hub.inner.lock().unwrap().sessions.insert("test".into(), Session { stdin, child, provider: "test".into(), stopped: false });
            let stopped = hub.kill("test");
            let status = Command::new("/bin/ps").args(["-p", &descendant.to_string(), "-o", "stat="]).output().unwrap();
            let alive = status.status.success() && !String::from_utf8_lossy(&status.stdout).trim().starts_with('Z');
            unsafe { libc::kill(descendant, libc::SIGKILL); }
            std::fs::remove_dir_all(cwd).unwrap();
            stopped.unwrap();
            assert!(!alive, "Stop left a tool running for {name}");
        }
    }
}

impl Default for StdioHub {
    fn default() -> Self {
        Self::new()
    }
}

pub fn list_local_clis(shell_path: Option<String>) -> Vec<LocalCli> {
    let mut out = Vec::new();
    let hermes = which_in_path("hermes", shell_path.as_deref()).or_else(|| {
        let bundled = hermes_bin_path();
        bundled.is_file().then_some(bundled)
    });
    if let Some(path) = hermes {
        out.push(LocalCli {
            name: "hermes".into(),
            bin: "hermes".into(),
            path: path.to_string_lossy().into(),
        });
    }
    for tool in crate::tools::list_local_tools(shell_path.as_deref()) {
        if tool.name == "hermes" {
            continue;
        }
        let Some(path) = tool.cli_path else {
            continue;
        };
        if !tool.installed {
            continue;
        }
        let bin = std::path::Path::new(&path)
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or(tool.name.as_str())
            .to_string();
        out.push(LocalCli {
            name: tool.name,
            bin,
            path,
        });
    }
    out
}

pub fn control_argv(
    provider: &str,
    mode: &str,
    auto_approve: bool,
    prompt: Option<&str>,
    resume: Option<&str>,
    workspace: Option<&str>,
) -> Result<Vec<String>, String> {
    let name = provider.trim().to_lowercase();
    let kind = mode.trim().to_lowercase();
    match name.as_str() {
        "cursor" => {
            if kind == "interactive" {
                let mut argv = vec!["cursor-agent".into()];
                if auto_approve {
                    argv.push("--force".into());
                }
                Ok(argv)
            } else {
                let mut argv = vec!["cursor-agent".into()];
                if let Some(ws) = workspace {
                    if !ws.is_empty() {
                        argv.extend(["--workspace".into(), ws.into()]);
                    }
                }
                argv.push("acp".into());
                Ok(argv)
            }
        }
        "claude" => {
            if kind == "interactive" {
                let mut argv = vec!["claude".into(), "--ide".into()];
                if auto_approve {
                    argv.extend(["--permission-mode".into(), "bypassPermissions".into()]);
                }
                Ok(argv)
            } else {
                let mut argv = vec![
                    "claude".into(),
                    "-p".into(),
                    "--output-format".into(),
                    "stream-json".into(),
                    "--verbose".into(),
                    "--ide".into(),
                ];
                if auto_approve {
                    argv.extend(["--permission-mode".into(), "bypassPermissions".into()]);
                }
                if let Some(r) = resume {
                    if !r.is_empty() {
                        argv.extend(["--resume".into(), r.into()]);
                    }
                }
                if let Some(p) = prompt {
                    if !p.is_empty() {
                        argv.push(p.into());
                    }
                }
                Ok(argv)
            }
        }
        "codex" => {
            if kind == "interactive" {
                Ok(vec!["codex".into(), "--sandbox".into(), "workspace-write".into()])
            } else {
                let mut argv = vec!["codex".into(), "exec".into(), "--skip-git-repo-check".into()];
                if auto_approve {
                    argv.push("--dangerously-bypass-approvals-and-sandbox".into());
                } else {
                    argv.extend(["--sandbox".into(), "workspace-write".into()]);
                }
                if let Some(r) = resume {
                    if !r.is_empty() {
                        argv.extend(["resume".into(), r.into()]);
                    }
                }
                argv.push("--json".into());
                if let Some(p) = prompt {
                    if !p.is_empty() {
                        argv.push(p.into());
                    }
                }
                Ok(argv)
            }
        }
        "opencode" => {
            if kind != "acp" {
                return Err("OpenCode control uses its connected ACP session. Open Keys and connections, verify OpenCode, then retry from Builds.".into());
            }
            let mut argv = vec!["opencode".into(), "acp".into()];
            if let Some(cwd) = workspace { argv.extend(["--cwd".into(), cwd.into()]); }
            Ok(argv)
        }
        "hermes" => {
            let bundled = hermes_bin_path();
            let bin = if bundled.is_file() {
                bundled.to_string_lossy().into_owned()
            } else {
                "hermes".into()
            };
            Ok(vec![bin, "acp".into()])
        }
        other => Err(format!("unsupported provider: {other}")),
    }
}

pub fn open_coding_ide(provider: &str, cwd: &std::path::Path) -> Result<String, String> {
    let name = provider.trim().to_lowercase();
    let apps: &[&str] = match name.as_str() {
        "hermes" => return Ok("Use this pane to watch your work. You can select Open folder in Builds to see your files.".into()),
        "cursor" => &["Cursor"],
        "claude" | "codex" => &["Cursor", "Visual Studio Code"],
        "opencode" => &["Visual Studio Code", "Cursor"],
        _ => return Err("Open Keys and connections, select a coding tool, then retry opening your editor.".into()),
    };
    for app in apps {
        if crate::ide::editor_command(app, cwd).is_some_and(|mut command| command.status().is_ok_and(|status| status.success())) {
            return Ok("Your editor is open. Open Builds to watch or stop your build.".into());
        }
    }
    Err("Your editor could not open. Open Builds and select Open folder, or open that folder in your installed editor.".into())
}

#[cfg(test)]
mod argv_tests {
    use super::*;

    #[test]
    fn build_native_opencode_uses_workspace_acp_not_an_untracked_web_server() {
        for workspace in ["/tmp/supplier-audits", "/tmp/garden-plans"] {
            assert_eq!(control_argv("opencode", "acp", false, None, None, Some(workspace)).unwrap(),
                ["opencode", "acp", "--cwd", workspace]);
            assert!(control_argv("opencode", "print", false, None, None, Some(workspace)).is_err());
        }
    }
}
