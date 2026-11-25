//! Interactive Claude / Codex / Cursor PTYs using the founder's CLI login.
//! Headless Work Orders stay in Python; this is the TTY the browser cannot host.

use std::collections::{HashMap, VecDeque};
use std::io::{Read, Write};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use portable_pty::{native_pty_system, CommandBuilder, MasterPty, PtySize};
use serde::Serialize;
use tauri::{AppHandle, Emitter};
use uuid::Uuid;

use crate::supervisor::{scrub_env, which_in_path};

pub struct PtyHub {
    inner: Arc<Mutex<ProcessRegistry>>,
}

#[derive(Default)]
struct ProcessRegistry {
    sessions: HashMap<String, Session>,
    exited: VecDeque<String>,
}

struct Session {
    writer: Box<dyn Write + Send>,
    master: Box<dyn MasterPty + Send>,
    child: Box<dyn portable_pty::Child + Send>,
    provider: String,
    stopped: bool,
}

#[derive(Clone, Serialize)]
pub struct SessionInfo {
    pub id: String,
    pub provider: String,
}

#[derive(Clone, Serialize)]
pub struct PtyChunk {
    pub id: String,
    pub data: String,
}

#[derive(Clone, Serialize)]
pub struct PtyExit {
    pub id: String,
    pub code: Option<i32>,
}

impl PtyHub {
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
            .writer
            .write_all(data.as_bytes())
            .map_err(|e| e.to_string())?;
        session.writer.flush().ok();
        Ok(())
    }

    pub fn resize(&self, id: &str, cols: u16, rows: u16) -> Result<(), String> {
        let g = self.inner.lock().map_err(|e| e.to_string())?;
        let session = g.sessions.get(id).ok_or_else(|| "session not found".to_string())?;
        session
            .master
            .resize(PtySize {
                rows,
                cols,
                pixel_width: 0,
                pixel_height: 0,
            })
            .map_err(|e| e.to_string())
    }

    pub fn kill(&self, id: &str) -> Result<(), String> {
        let mut g = self.inner.lock().map_err(|e| e.to_string())?;
        if g.exited.iter().any(|known| known == id) { return Ok(()); }
        let session = g.sessions.get_mut(id).ok_or("Your coding session could not be found. Open Builds to check its activity, then select Stop again.")?;
        if session.stopped { return Ok(()); }
        let pid = session.child.process_id().ok_or("Your coding tool could not be found. Select Stop again in Builds before starting another build.")?;
        crate::stdio::stop_coding_tree(pid)?;
        session.child.wait().map_err(|_| "Your coding tool's Stop could not be verified. Select Stop again in Builds before starting another build.")?;
        session.stopped = true;
        Ok(())
    }

    pub fn kill_all(&self) {
        let ids = match self.inner.lock() {
            Ok(g) => g.sessions.keys().cloned().collect::<Vec<_>>(),
            Err(_) => return,
        };
        for id in ids { let _ = self.kill(&id); }
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
        let pty_system = native_pty_system();
        let pair = pty_system
            .openpty(PtySize {
                rows: 32,
                cols: 100,
                pixel_width: 0,
                pixel_height: 0,
            })
            .map_err(|e| e.to_string())?;

        let bin_name = argv[0].clone();
        let resolved = which_in_path(&bin_name, shell_path.as_deref())
            .map(|p| p.to_string_lossy().to_string())
            .unwrap_or(bin_name);

        let mut cmd = CommandBuilder::new(&resolved);
        if argv.len() > 1 {
            cmd.args(&argv[1..]);
        }
        if let Some(dir) = cwd {
            cmd.cwd(dir);
        }

        let mut env_map: HashMap<String, String> = std::env::vars().collect();
        if let Some(path) = &shell_path {
            env_map.insert("PATH".into(), path.clone());
        }
        let mut scrubbed = scrub_env(env_map);
        for k in extra_scrub {
            scrubbed.remove(k);
        }
        // Interactive Claude/Codex must use the founder's CLI login, never platform keys.
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

        let mut reader = pair.master.try_clone_reader().map_err(|e| e.to_string())?;
        let writer = pair.master.take_writer().map_err(|e| e.to_string())?;
        let child = pair.slave.spawn_command(cmd).map_err(|e| e.to_string())?;

        {
            let mut g = self.inner.lock().map_err(|e| e.to_string())?;
            g.sessions.insert(
                id.clone(),
                Session {
                    writer: Box::new(writer),
                    master: pair.master,
                    child,
                    provider,
                    stopped: false,
                },
            );
        }

        let emit_id = id.clone();
        let app_out = app.clone();
        let (output_done, output_drained) = std::sync::mpsc::channel();
        thread::spawn(move || {
            let mut buf = [0u8; 4096];
            loop {
                match reader.read(&mut buf) {
                    Ok(0) => break,
                    Ok(n) => {
                        let chunk = String::from_utf8_lossy(&buf[..n]).to_string();
                        let _ = app_out.emit(
                            "pty-output",
                            PtyChunk {
                                id: emit_id.clone(),
                                data: chunk,
                            },
                        );
                    }
                    Err(_) => break,
                }
            }
            let _ = output_done.send(());
        });

        let registry = self.inner.clone();
        let exit_id = id.clone();
        thread::spawn(move || loop {
            match observe_process_exit(&registry, &exit_id) {
                Ok(Some(code)) => {
                    // shortcut: output drains for at most one second, extend this bound if full large-output transcripts are required.
                    let _ = output_drained.recv_timeout(Duration::from_secs(1));
                    let _ = app.emit("pty-exit", PtyExit { id: exit_id, code: Some(code) });
                    break;
                }
                Ok(None) => thread::sleep(Duration::from_millis(100)),
                Err(_) => {
                    let _ = app.emit("pty-output", PtyChunk { id: exit_id,
                        data: "Your coding tool's Stop could not be verified. Select Stop again in Builds before starting another build.\n".into() });
                    break;
                }
            }
        });

        Ok(id)
    }
}

fn observe_process_exit(registry: &Mutex<ProcessRegistry>, id: &str) -> Result<Option<i32>, String> {
    let mut registry = registry.lock().map_err(|_| "process registry unavailable")?;
    let session = registry.sessions.get_mut(id).ok_or("process handle unavailable")?;
    #[cfg(unix)]
    if !session.stopped {
        let pid = session.child.process_id().ok_or("process identity unavailable")?;
        if !crate::stdio::coding_process_finished(pid)? { return Ok(None); }
        crate::stdio::stop_coding_tree(pid)?;
    }
    let status = session.child.try_wait().map_err(|_| "process observation failed")?;
    if let Some(status) = status {
        #[cfg(not(unix))]
        if !session.stopped {
            let pid = session.child.process_id().ok_or("process identity unavailable")?;
            crate::stdio::stop_coding_tree(pid)?;
        }
        registry.sessions.remove(id);
        registry.exited.push_back(id.into());
        if registry.exited.len() > 128 { registry.exited.pop_front(); }
        return Ok(Some(status.exit_code() as i32));
    }
    Ok(None)
}

impl Default for PtyHub {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::io::BufRead;

    #[test]
    fn build_native_pty_eof_retains_custody_until_the_whole_tool_stops() {
        for name in ["supplier-audits", "garden-plans"] {
            let cwd = std::env::temp_dir().join(format!("native-pty-{}-{name}", Uuid::new_v4()));
            std::fs::create_dir_all(&cwd).unwrap();
            let hub = PtyHub::new();
            let pair = native_pty_system().openpty(PtySize { rows: 24, cols: 80, pixel_width: 0, pixel_height: 0 }).unwrap();
            let reader = pair.master.try_clone_reader().unwrap();
            let writer = pair.master.take_writer().unwrap();
            let mut command = CommandBuilder::new("/bin/sh");
            command.args(["-c", "sleep 60 >/dev/null 2>&1 & echo $!; exec 0</dev/null; exec 1>&-; exec 2>&-; sleep 60"]);
            command.cwd(&cwd);
            let child = pair.slave.spawn_command(command).unwrap();
            drop(pair.slave);
            let mut reader = std::io::BufReader::new(reader);
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            let descendant: i32 = line.trim().parse().unwrap();
            hub.inner.lock().unwrap().sessions.insert("test".into(), Session {
                writer, master: pair.master, child, provider: "test".into(), stopped: false });
            let (done, drained) = std::sync::mpsc::channel();
            thread::spawn(move || { let _ = reader.read_to_end(&mut Vec::new()); let _ = done.send(()); });
            let eof = drained.recv_timeout(Duration::from_secs(2));
            let observed = observe_process_exit(&hub.inner, "test");
            let stopped = hub.kill("test");
            let output = std::process::Command::new("/bin/ps")
                .args(["-p", &descendant.to_string(), "-o", "stat="]).output().unwrap();
            let alive = output.status.success() && !String::from_utf8_lossy(&output.stdout).trim().starts_with('Z');
            unsafe { libc::kill(descendant, libc::SIGKILL); }
            std::fs::remove_dir_all(cwd).unwrap();
            eof.unwrap();
            assert!(observed.unwrap().is_none());
            stopped.unwrap();
            assert!(!alive, "Stop left a PTY tool running for {name}");
            assert!(observe_process_exit(&hub.inner, "test").unwrap().is_some());
            assert!(hub.list().is_empty());
            crate::stdio::stop_coding_process(&crate::stdio::StdioHub::new(), &hub, "test").unwrap();
        }
    }
}
