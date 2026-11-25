//! Ordered process supervisor: warden → core → gateway → console.
//! Attaches to an already-healthy stack (scripts/dev.py) instead of double-binding.
//! Only processes this supervisor spawned are killed on quit.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;

const OWNED_AUTH: &[&str] = &[
    "LLM_API_KEY",
    "AZURE_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "CURSOR_API_KEY",
    "CODEX_API_KEY",
    "DEEPSEEK_API_KEY",
];

#[derive(Debug, Clone, Serialize)]
pub struct RoleStatus {
    pub role: String,
    pub port: u16,
    pub healthy: bool,
    pub pid: Option<u32>,
    pub owned: bool,
}

pub struct Supervisor {
    inner: Arc<Mutex<Inner>>,
    bridge_check: Mutex<()>,
    local: Option<crate::local_stack::LocalStack>,
}

struct Inner {
    children: HashMap<String, Child>,
    ports: HashMap<String, u16>,
    owned: HashMap<String, bool>,
    data_dir: PathBuf,
    runtime_bin: PathBuf,
    runtime_is_python: bool,
    shell_path: Option<String>,
    dotenv: HashMap<String, String>,
    repo_root: PathBuf,
    resource_dir: PathBuf,
    ready: bool,
    stopped: bool,
    native_bridge_token: String,
    bridge_started_at: Option<Instant>,
    bridge_healthy: bool,
    bridge_session_reset: bool,
    bridge_bind_failed: Arc<std::sync::atomic::AtomicBool>,
    bridge_retry: BridgeRetry,
}

#[derive(Default)]
struct BridgeRetry {
    attempts: u32,
    next: Option<Instant>,
    error: String,
}

const BRIDGE_STARTING: &str = "The local helper is still starting. Cloud work remains available.";

impl BridgeRetry {
    fn failed(&mut self, error: String, now: Instant) {
        let delay = if error == BRIDGE_STARTING { 1 } else {
            self.attempts = self.attempts.saturating_add(1).min(6);
            (1u64 << self.attempts).min(60)
        };
        self.next = Some(now + Duration::from_secs(delay));
        self.error = error;
    }
}

impl Supervisor {
    pub fn new(runtime_bin: PathBuf, data_dir: PathBuf, resource_dir: PathBuf) -> Self {
        let runtime_is_python = runtime_bin
            .file_name()
            .and_then(|s| s.to_str())
            .map(|n| n.starts_with("python"))
            .unwrap_or(false);
        let repo_root = resolve_repo_root();
        let executable = std::env::current_exe().unwrap_or_default();
        let local = crate::local_stack::is_local().then(|| crate::local_stack::LocalStack::new(resource_dir.clone(), data_dir.clone()));
        let native_bridge_token = local.as_ref().map(|stack| stack.native_bridge_token.clone()).unwrap_or_else(random_bridge_secret);
        let dotenv = if let Some(stack) = &local {
            HashMap::from([("SUPYMEM_MODE".into(), "local".into()),
                           ("GATEWAY_URL".into(), stack.url("gateway")),
                           ("CONSOLE_ORIGIN".into(), stack.url("console"))])
        } else {
            initial_dotenv(&repo_root, &executable,
                crate::channel::packaged_local_review(),
                crate::channel::staging() && crate::channel::local_review())
        };
        Self {
            bridge_check: Mutex::new(()),
            local,
            inner: Arc::new(Mutex::new(Inner {
                children: HashMap::new(),
                ports: HashMap::new(),
                owned: HashMap::new(),
                data_dir,
                runtime_bin,
                runtime_is_python,
                shell_path: resolve_shell_path(),
                dotenv,
                repo_root,
                resource_dir,
                ready: false,
                stopped: false,
                native_bridge_token,
                bridge_started_at: None,
                bridge_healthy: false,
                bridge_session_reset: false,
                bridge_bind_failed: Arc::new(std::sync::atomic::AtomicBool::new(false)),
                bridge_retry: BridgeRetry::default(),
            })),
        }
    }

    fn hosted_mode(&self) -> bool {
        let g = match self.inner.lock() {
            Ok(g) => g,
            Err(_) => return !looks_like_repo(&resolve_repo_root()),
        };
        Self::hosted_from(&g)
    }

    fn hosted_from(g: &Inner) -> bool {
        let desktop = g
            .dotenv
            .get("SUPYMEM_DESKTOP_MODE")
            .map(|s| s.as_str())
            .unwrap_or("");
        if desktop.eq_ignore_ascii_case("local") {
            return false;
        }
        let mode = g
            .dotenv
            .get("SUPYMEM_MODE")
            .map(|s| s.as_str())
            .unwrap_or("");
        if mode.eq_ignore_ascii_case("local") {
            return false;
        }
        let hosted = g
            .dotenv
            .get("COMPANY_OS_HOSTED")
            .map(|s| s.as_str())
            .unwrap_or("");
        if mode.eq_ignore_ascii_case("hosted")
            || desktop.eq_ignore_ascii_case("hosted")
            || hosted == "1"
            || hosted.eq_ignore_ascii_case("true")
        {
            return true;
        }
        // Installed .app has no repo next to it — talk to production, not a local stack.
        !looks_like_repo(&g.repo_root)
    }

    pub fn start_all(&self) -> Result<(), String> {
        self.start_all_with_console(|| Ok(()))
    }

    pub(crate) fn start_all_with_console(&self, open_console: impl FnOnce() -> Result<(), String>) -> Result<(), String> {
        if self.inner.lock().map_err(|e| e.to_string())?.stopped {
            return Err("desktop supervisor is shutting down".into());
        }
        if crate::channel::packaged_local_review() && !std::env::current_exe().ok().as_deref()
            .is_some_and(|exe| marked_local_review_app(exe, true)) {
            return Err("local review bundle marker is missing or invalid".into());
        }
        if let Some(local) = &self.local {
            local.start_with_console(open_console)?;
            let mut inner = self.inner.lock().map_err(|error| error.to_string())?;
            if inner.stopped {
                local.stop();
                return Err(local.error("Local startup was cancelled"));
            }
            inner.ready = true;
            return Ok(());
        }
        if self.hosted_mode() {
            return self.start_hosted();
        }
        if crate::channel::packaged_local_review() {
            // The frozen runtime contains the private bridge, not local Core,
            // Gateway or Next. Attach to an explicit local stack; never start
            // an incomplete sidecar and then drift to production on failure.
            for (name, port, path) in [("warden", 3400, "/health"),
                                       ("core", 3300, "/health"),
                                       ("gateway", 3200, "/health"),
                                       ("console", 3100, "/")] {
                if !http_url_ok(&format!("http://127.0.0.1:{port}{path}")) {
                    return Err(format!("local review needs the local {name} service on port {port}; start make company-os-dev"));
                }
            }
        }
        let roles = [
            ("warden", 3400_u16, "/health"),
            ("core", 3300_u16, "/health"),
            ("worker", 3301_u16, "/health"),
            ("gateway", 3200_u16, "/health"),
        ];
        for (role, default_port, health_path) in roles {
            if http_url_ok(&format!("http://127.0.0.1:{default_port}{health_path}")) {
                let mut g = self.inner.lock().map_err(|e| e.to_string())?;
                g.ports.insert(role.to_string(), default_port);
                g.owned.insert(role.to_string(), false);
                continue;
            }
            let port = if port_free(default_port) {
                default_port
            } else {
                pick_port(default_port)
            };
            self.spawn_role(role, port)?;
            if !wait_health(&format!("http://127.0.0.1:{port}{health_path}"), 45.0) {
                return Err(format!("{role} failed health check on port {port}"));
            }
        }
        self.ensure_console()?;
        if crate::channel::local_review() {
            // The local review app uses the same private native bridge as a
            // hosted build, bound to the local Gateway and an isolated token.
            self.inner.lock().map_err(|e| e.to_string())?.dotenv.insert(
                "BRIDGE_TOKEN".into(), format!("br_{}", random_bridge_secret()));
        }
        let mut g = self.inner.lock().map_err(|e| e.to_string())?;
        if g.stopped {
            return Err("desktop supervisor is shutting down".into());
        }
        g.ready = true;
        Ok(())
    }

    fn start_hosted(&self) -> Result<(), String> {
        let token = {
            let mut g = self.inner.lock().map_err(|e| e.to_string())?;
            if g.stopped {
                return Err("desktop supervisor is shutting down".into());
            }
            let token = g
                .dotenv
                .get("BRIDGE_TOKEN")
                .cloned()
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| format!("br_{}", random_bridge_secret()));
            g.dotenv.insert("BRIDGE_TOKEN".into(), token.clone());
            g.dotenv
                .entry("SUPYMEM_MODE".into())
                .or_insert_with(|| "hosted".into());
            token
        };
        let _ = token;
        let mut g = self.inner.lock().map_err(|e| e.to_string())?;
        if g.stopped {
            return Err("desktop supervisor is shutting down".into());
        }
        g.ready = true;
        Ok(())
    }

    /// Block until the hosted bridge answers /health (cold frozen Python can take 20s+).
    pub fn wait_bridge(&self) -> Result<(), String> {
        self.ensure_bridge_with_timeout(BRIDGE_COLD_START).map(|_| ())
    }

    fn ensure_console(&self) -> Result<(), String> {
        const DEFAULT: u16 = 3100;
        if console_healthy(DEFAULT) {
            let mut g = self.inner.lock().map_err(|e| e.to_string())?;
            g.ports.insert("console".into(), DEFAULT);
            g.owned.insert("console".into(), false);
            return Ok(());
        }
        let port = if port_free(DEFAULT) {
            DEFAULT
        } else {
            pick_port(DEFAULT)
        };
        self.spawn_console(port)?;
        if !wait_health(&format!("http://127.0.0.1:{port}/api/health"), 60.0)
            && !wait_health(&format!("http://127.0.0.1:{port}/"), 15.0)
        {
            return Err(format!("console failed health check on port {port}"));
        }
        Ok(())
    }

    pub fn spawn_role(&self, role: &str, port: u16) -> Result<(), String> {
        let mut g = self.inner.lock().map_err(|e| e.to_string())?;
        if g.stopped {
            return Err("desktop supervisor is shutting down".into());
        }
        let port = if role == "bridge" {
            reap_stale_bridges(&g.runtime_bin, &g.data_dir);
            bridge_port(port).map_err(|(reason, error)| {
                crate::error_capture::report_runtime("shell_sidecar_spawn", "sidecar_start",
                    reason, Duration::ZERO);
                error
            })?
        } else { port };
        let mut cmd = Command::new(&g.runtime_bin);
        if g.runtime_is_python {
            cmd.args(["-m", "desktop_runtime"]);
        }
        cmd.arg("--role")
            .arg(role)
            .arg("--port")
            .arg(port.to_string())
            .arg("--data-dir")
            .arg(&g.data_dir)
            .stdout(Stdio::null())
            .stderr(if role == "bridge" { Stdio::piped() } else { Stdio::null() });

        if role == "bridge" && Self::hosted_from(&g) {
            apply_bridge_env(&mut cmd, &g.data_dir, g.shell_path.as_deref(),
                             g.dotenv.get("BRIDGE_TOKEN").map(String::as_str).unwrap_or(""));
        } else {
            apply_child_env(&mut cmd, &g);
        }
        if role == "bridge" {
            apply_review_bridge_env(&mut cmd, crate::channel::packaged_local_review());
            // Local review builds use the same reviewed native owner as hosted
            // builds. Enable execution in both modes; the saved manifest,
            // authenticated device and explicit task approval remain required.
            cmd.env("SUPYMEM_NATIVE_BROWSER_ENABLED", "1");
        }

        let gw = g.ports.get("gateway").copied().unwrap_or(3200);
        if role == "worker" {
            cmd.env("SUPYMEM_MODE", "local")
                .env("APP_ROLE", "worker")
                .env("WORKER_HEALTH_PORT", port.to_string());
        }
        cmd.env("WARDEN_URL", format!("http://127.0.0.1:{}", g.ports.get("warden").copied().unwrap_or(3400)));
        cmd.env("COMPANY_OS_URL", format!("http://127.0.0.1:{}", g.ports.get("core").copied().unwrap_or(3300)));
        if role == "gateway" {
            cmd.env("GATEWAY_URL", format!("http://127.0.0.1:{port}"));
        } else if role == "bridge" {
            cmd.env("BRIDGE_PORT", port.to_string());
            // The sidecar exits on its own if this app dies without cleanup.
            cmd.env("SUPYMEM_PARENT_PID", std::process::id().to_string());
            cmd.env("BRIDGE_AUTH_OWNER", "native");
            cmd.env("BRIDGE_NATIVE_TOKEN", &g.native_bridge_token);
            // Match the native control plane. A packaged hosted bridge must not
            // silently choose staging while the desktop talks to production.
            cmd.env("GATEWAY_URL", if Self::hosted_from(&g) {
                hosted_gateway_from(&g.dotenv)
            } else {
                format!("http://127.0.0.1:{gw}")
            });
        } else {
            cmd.env("GATEWAY_URL", format!("http://127.0.0.1:{gw}"));
        }

        unix_new_session(&mut cmd);

        let started = Instant::now();
        let mut child = cmd
            .spawn()
            .map_err(|e| {
                if role == "bridge" {
                    crate::error_capture::report_runtime("shell_sidecar_spawn", "sidecar_start",
                        spawn_failure_reason(&e), started.elapsed());
                } else {
                    crate::error_capture::report_reason("shell_start_failed", "supervisor_start_failed");
                }
                if role == "bridge" {
                    "The local helper could not be launched. Quit and reopen Supymem. If macOS blocks it or it is missing, reinstall the signed app in Applications. Your saved sign-in and work are retained.".into()
                } else { format!("failed to spawn {role}: {e}") }
            })?;
        if role == "bridge" {
            use std::io::BufRead;
            let bind_failed = Arc::new(std::sync::atomic::AtomicBool::new(false));
            g.bridge_bind_failed = bind_failed.clone();
            if let Some(stderr) = child.stderr.take() {
                thread::spawn(move || {
                    for line in std::io::BufReader::new(stderr).lines().map_while(Result::ok) {
                        if line.to_ascii_lowercase().contains("address already in use") {
                            bind_failed.store(true, std::sync::atomic::Ordering::SeqCst);
                        }
                    }
                });
            }
        }
        g.ports.insert(role.to_string(), port);
        g.owned.insert(role.to_string(), true);
        g.children.insert(role.to_string(), child);
        if role == "bridge" {
            g.bridge_started_at = Some(Instant::now());
            g.bridge_healthy = false;
            g.bridge_session_reset = true;
        }
        Ok(())
    }

    fn spawn_console(&self, port: u16) -> Result<(), String> {
        let mut g = self.inner.lock().map_err(|e| e.to_string())?;
        if g.stopped {
            return Err("desktop supervisor is shutting down".into());
        }
        let console_dir = g.repo_root.join("apps/console");
        let standalone_candidates = vec![
            g.resource_dir.join("console/server.js"),
            g.resource_dir.join("console/apps/console/server.js"),
            console_dir.join(".next/standalone/server.js"),
            console_dir.join(".next/standalone/apps/console/server.js"),
        ];
        let standalone = standalone_candidates.iter().find(|p| p.is_file()).cloned();
        let gw = g.ports.get("gateway").copied().unwrap_or(3200);
        let gateway_url = format!("http://127.0.0.1:{gw}");

        let mut cmd = if let Some(script) = standalone {
            let node = which_in_path("node", g.shell_path.as_deref()).unwrap_or_else(|| "node".into());
            let mut c = Command::new(node);
            c.arg(script);
            c.env("PORT", port.to_string());
            c.env("HOSTNAME", "127.0.0.1");
            c
        } else {
            let pnpm = which_in_path("pnpm", g.shell_path.as_deref()).unwrap_or_else(|| "pnpm".into());
            let mut c = Command::new(pnpm);
            c.args(["--dir", console_dir.to_str().unwrap_or("apps/console"), "dev"]);
            c
        };
        cmd.current_dir(&g.repo_root)
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        apply_child_env(&mut cmd, &g);
        cmd.env("PORT", port.to_string());
        cmd.env("NEXT_PUBLIC_GATEWAY_URL", &gateway_url);
        unix_new_session(&mut cmd);

        let child = cmd
            .spawn()
            .map_err(|e| format!("failed to spawn console: {e}"))?;
        g.ports.insert("console".into(), port);
        g.owned.insert("console".into(), true);
        g.children.insert("console".into(), child);
        Ok(())
    }

    pub fn stop_all(&self) {
        if let Some(local) = &self.local { local.shutdown(); }
        let mut g = match self.inner.lock() {
            Ok(g) => g,
            Err(_) => return,
        };
        g.ready = false;
        g.stopped = true;
        let owned = g.owned.clone();
        let mut children = HashMap::new();
        std::mem::swap(&mut children, &mut g.children);
        drop(g);
        for (role, mut child) in children {
            if owned.get(&role).copied().unwrap_or(false) {
                terminate_owned_child(&mut child);
            }
        }
    }

    pub(crate) fn stop_bridge(&self) {
        let child = self.inner.lock().ok().and_then(|mut g| {
            if g.owned.get("bridge") == Some(&true) {
                g.children.remove("bridge")
            } else { None }
        });
        if let Some(mut child) = child { terminate_owned_child(&mut child); }
    }

    /// Restore the private hosted bridge only if its owned helper exited.
    /// The browser can remain signed in after that process dies, so
    /// treating the hosted Console as healthy is not enough for a reviewed Mac
    /// task. The caller must replay the authenticated bridge session when this
    /// returns `true` because a replacement helper starts empty.
    pub(crate) fn ensure_bridge(&self) -> Result<bool, String> {
        self.ensure_bridge_with_timeout(Duration::ZERO)?;
        let mut g = self.inner.lock().map_err(|e| e.to_string())?;
        Ok(std::mem::take(&mut g.bridge_session_reset))
    }

    fn ensure_bridge_with_timeout(&self, timeout: Duration) -> Result<bool, String> {
        if let Some(local) = &self.local {
            if let Some(error) = local.failure() { return Err(error); }
            return if local.status().iter().any(|role| role.role == "bridge" && role.healthy) {
                Ok(false)
            } else {
                Err("Your coding tools connection is not ready. Select Retry on the startup screen, or quit and reopen your app.".into())
            };
        }
        let _check = if timeout.is_zero() {
            self.bridge_check.try_lock().map_err(|_| BRIDGE_STARTING.to_string())?
        } else {
            self.bridge_check.lock().map_err(|e| e.to_string())?
        };
        {
            let g = self.inner.lock().map_err(|e| e.to_string())?;
            if g.stopped { return Err("desktop supervisor is shutting down".into()); }
            if g.bridge_retry.attempts >= 6 {
                return Err(format!("{} Automatic retries are paused. Cloud work remains available; quit and reopen Supymem to retry Mac tasks.", g.bridge_retry.error));
            }
            if g.bridge_retry.next.is_some_and(|next| next > Instant::now())
                && (timeout.is_zero() || g.bridge_retry.error != BRIDGE_STARTING) {
                return Err(g.bridge_retry.error.clone());
            }
        }
        let result = self.check_bridge(timeout);
        let mut g = self.inner.lock().map_err(|e| e.to_string())?;
        match &result {
            Ok(_) => { g.bridge_retry = BridgeRetry::default(); g.bridge_healthy = true; }
            Err(error) => g.bridge_retry.failed(error.clone(), Instant::now()),
        }
        result
    }

    fn check_bridge(&self, timeout: Duration) -> Result<bool, String> {
        let (mut port, running, existed, since_spawn, was_healthy) = {
            let mut g = self.inner.lock().map_err(|e| e.to_string())?;
            if g.stopped {
                return Err("desktop supervisor is shutting down".into());
            }
            let port = g.ports.get("bridge").copied().unwrap_or(3500);
            let existed = g.children.contains_key("bridge");
            let running = match g.children.get_mut("bridge") {
                Some(child) => child.try_wait().map_err(|e| e.to_string())?.is_none(),
                None => false,
            };
            (port, running, existed, g.bridge_started_at.map(|t| t.elapsed()), g.bridge_healthy)
        };
        if !running {
            if existed { self.report_bridge_exit(was_healthy, since_spawn.unwrap_or_default()); }
            let old = self.inner.lock().map_err(|e| e.to_string())?.children.remove("bridge");
            if let Some(mut child) = old { terminate_owned_child(&mut child); }
            self.spawn_role("bridge", port)?;
            port = self.inner.lock().map_err(|e| e.to_string())?.ports["bridge"];
        }
        let budget = timeout;
        let started = Instant::now();
        loop {
            let (elapsed, exited, was_healthy) = {
                let mut g = self.inner.lock().map_err(|e| e.to_string())?;
                if g.stopped { return Err("desktop supervisor is shutting down".into()); }
                let exited = match g.children.get_mut("bridge") {
                    Some(child) => child.try_wait().map_err(|e| e.to_string())?.is_some(),
                    None => true,
                };
                (g.bridge_started_at.map(|t| t.elapsed()).unwrap_or_default(), exited, g.bridge_healthy)
            };
            if exited {
                if was_healthy { return self.check_bridge(timeout); }
                self.report_bridge_exit(was_healthy, elapsed);
                return Err("The local helper exited before it could connect. Quit and reopen Supymem; if this persists, reinstall the signed app. Your saved sign-in and work are retained.".into());
            }
            // A one-second health probe can time out while the Python helper
            // imports a driver or handles a reviewed action. Killing that
            // still-running process would erase the in-flight worker and leave
            // an indeterminate execution receipt. Only replace an exited
            // helper; a live but temporarily unresponsive one keeps ownership
            // of its tasks and can be diagnosed without replaying an action.
            let probe_timeout = if budget.is_zero() { health_probe_timeout(crate::channel::packaged_local_review()) }
                else { health_probe_timeout(crate::channel::packaged_local_review()).min(budget.saturating_sub(started.elapsed())) };
            let health = if probe_timeout.is_zero() { Err("sidecar_health_timeout") }
                else { bridge_health(port, probe_timeout) };
            if health.is_ok() { return Ok(!running); }
            if !budget.is_zero() && started.elapsed() < budget {
                thread::sleep(Duration::from_millis(300).min(budget.saturating_sub(started.elapsed())));
                continue;
            }
            // Hosted start only spawns the helper; native requests (and the
            // per-second auth retry) reach here while the frozen Python is
            // still unpacking and nothing listens yet. That connection refusal
            // returns in ~0 ms and is a normal cold start, not a timeout.
            if budget.is_zero() && bridge_cold_starting(Some(elapsed)) {
                return Err(BRIDGE_STARTING.into());
            }
            let mut g = self.inner.lock().map_err(|e| e.to_string())?;
            if g.stopped { return Err("desktop supervisor is shutting down".into()); }
            if g.children.get_mut("bridge").map(|child| child.try_wait()).transpose()
                .map_err(|e| e.to_string())?.flatten().is_some() {
                drop(g);
                if was_healthy { return self.check_bridge(timeout); }
                self.report_bridge_exit(was_healthy, elapsed);
                return Err("The local helper stopped during startup. Quit and reopen Supymem; if this persists, reinstall the signed app. Your saved sign-in and work are retained.".into());
            }
            let phase_elapsed = started.elapsed();
            let elapsed = g.bridge_started_at.map(|t| t.elapsed()).unwrap_or(phase_elapsed).max(phase_elapsed);
            drop(g);
            let reason = health.unwrap_err();
            crate::error_capture::report_runtime_timed("shell_sidecar_health", "sidecar_health",
                reason, elapsed, phase_elapsed);
            return Err("The local helper is not responding. Cloud work remains available. Quit and reopen Supymem to restore Mac tasks; if it still fails, reinstall the signed app. Your saved sign-in and work are retained.".into());
        }
    }

    fn report_bridge_exit(&self, was_healthy: bool, elapsed: Duration) {
        let bind_failed = self.inner.lock().map(|g| g.bridge_bind_failed.load(std::sync::atomic::Ordering::SeqCst)).unwrap_or(false);
        if was_healthy {
            crate::error_capture::report_runtime("shell_sidecar_exit", "sidecar_run",
                "sidecar_exited", elapsed);
        } else {
            crate::error_capture::report_runtime("shell_sidecar_spawn", "sidecar_start",
                if bind_failed { "sidecar_port_conflict" } else { "sidecar_startup_exited" }, elapsed);
        }
    }

    pub(crate) fn defer_bridge_sync(&self, error: &str) {
        if let Ok(mut g) = self.inner.lock() {
            g.bridge_retry.failed(error.into(), Instant::now());
        }
    }

    pub(crate) fn local_log_path(&self) -> Option<PathBuf> {
        self.local.as_ref().map(|local| local.log_path.clone())
    }

    pub(crate) fn local_error(&self, reason: &str) -> Option<String> {
        self.local.as_ref().map(|local| {
            let message = local.error(reason);
            local.stop();
            if let Ok(mut inner) = self.inner.lock() { inner.ready = false; }
            message
        })
    }

    pub(crate) fn local_error_message(&self) -> String {
        self.local.as_ref().map(|local| local.last_error()).unwrap_or_default()
    }

    pub(crate) fn local_maintenance_message(&self) -> String {
        self.local.as_ref().map(|local| local.maintenance_message()).unwrap_or_default()
    }

    pub(crate) fn local_failure(&self) -> Option<String> {
        let failure = self.local.as_ref().and_then(|local| local.failure());
        if failure.is_some() {
            if let Ok(mut inner) = self.inner.lock() { inner.ready = false; }
        }
        failure
    }

    pub fn is_ready(&self) -> bool {
        self.inner.lock().map(|g| g.ready).unwrap_or(false)
    }

    pub fn gateway_url(&self) -> String {
        if let Some(local) = &self.local { return local.url("gateway"); }
        let g = self.inner.lock().unwrap();
        if Self::hosted_from(&g) {
            if let Some(url) = g
                .dotenv
                .get("COMPANY_OS_PUBLIC_URL")
                .or_else(|| g.dotenv.get("GATEWAY_URL"))
                .map(|s| s.trim_end_matches('/').to_string())
                .filter(|s| !s.is_empty() && !s.contains("127.0.0.1"))
            {
                return url;
            }
            return "https://gateway.supymem.com".into();
        }
        if let Some(url) = g
            .dotenv
            .get("COMPANY_OS_PUBLIC_URL")
            .map(|s| s.trim_end_matches('/').to_string())
            .filter(|s| !s.is_empty())
        {
            return url;
        }
        if Self::hosted_from(&g) || (g.ports.contains_key("bridge") && !g.ports.contains_key("gateway")) {
            return hosted_gateway_from(&g.dotenv);
        }
        let port = g.ports.get("gateway").copied().unwrap_or(3200);
        format!("http://127.0.0.1:{port}")
    }

    pub fn bridge_url(&self) -> String {
        if let Some(local) = &self.local { return local.url("bridge"); }
        let g = self.inner.lock().unwrap();
        let port = g.ports.get("bridge").copied().unwrap_or(3500);
        format!("http://127.0.0.1:{port}")
    }

    pub fn bridge_token(&self) -> String {
        if let Some(local) = &self.local { return local.bridge_token(); }
        let g = self.inner.lock().unwrap();
        g.dotenv.get("BRIDGE_TOKEN").cloned().unwrap_or_default()
    }

    // Never expose this credential through an IPC command or WebView config.
    pub(crate) fn native_bridge_token(&self) -> String {
        self.inner.lock().unwrap().native_bridge_token.clone()
    }

    pub fn runtime_mode(&self) -> &'static str {
        if self.local.is_some() { return "local"; }
        if self.hosted_mode() || {
            let g = self.inner.lock().ok();
            g.map(|inner| inner.ports.contains_key("bridge") && !inner.ports.contains_key("gateway"))
                .unwrap_or(false)
        } {
            "hosted"
        } else {
            "local"
        }
    }

    pub fn core_url(&self) -> String {
        if let Some(local) = &self.local { return local.url("core"); }
        let g = self.inner.lock().unwrap();
        let port = g.ports.get("core").copied().unwrap_or(3300);
        format!("http://127.0.0.1:{port}")
    }

    pub fn console_url(&self) -> String {
        if let Some(local) = &self.local { return local.url("console"); }
        let g = self.inner.lock().unwrap();
        if Self::hosted_from(&g) {
            if let Some(url) = g
                .dotenv
                .get("PUBLIC_PORTAL_URL")
                .or_else(|| g.dotenv.get("CONSOLE_ORIGIN"))
                .map(|s| s.trim_end_matches('/').to_string())
                .filter(|s| !s.is_empty())
            {
                return url;
            }
            return "https://app.supymem.com".into();
        }
        let port = g.ports.get("console").copied().unwrap_or(3100);
        format!("http://127.0.0.1:{port}")
    }

    pub fn data_dir(&self) -> PathBuf {
        self.inner.lock().unwrap().data_dir.clone()
    }

    pub fn shell_path(&self) -> Option<String> {
        self.inner.lock().ok().and_then(|g| g.shell_path.clone())
    }

    pub fn status(&self) -> Vec<RoleStatus> {
        if let Some(local) = &self.local { return local.status(); }
        let g = self.inner.lock().unwrap();
        g.ports
            .iter()
            .map(|(role, port)| {
                let pid = g.children.get(role).map(|c| c.id());
                let healthy = if role == "console" {
                    console_healthy(*port)
                } else {
                    http_ok(*port)
                };
                RoleStatus {
                    role: role.clone(),
                    port: *port,
                    healthy,
                    pid,
                    owned: g.owned.get(role).copied().unwrap_or(false),
                }
            })
            .collect()
    }
}

impl Drop for Supervisor {
    fn drop(&mut self) {
        self.stop_all();
    }
}

fn apply_bridge_env(cmd: &mut Command, data_dir: &Path, shell_path: Option<&str>, bridge_token: &str) {
    // The helper uses a gateway bearer, never laptop cloud/provider credentials.
    // Clear inherited environment as well as ignoring repository dotenv values.
    cmd.env_clear();
    for name in ["HOME", "USER", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL"] {
        if let Some(value) = std::env::var_os(name) { cmd.env(name, value); }
    }
    cmd.env("PATH", shell_path.unwrap_or(""));
    cmd.env("COMPANY_OS_DATA_DIR", data_dir);
    cmd.env("SUPYMEM_MODE", "hosted");
    // The native owner may offer the browser capability. Server-side feature
    // gates, saved review, exact scope and fresh claim still control execution.
    cmd.env("SUPYMEM_NATIVE_BROWSER_ENABLED", "1");
    // Explicit source QA gate only. Production remains off unless separately enabled.
    if std::env::var("SUPYMEM_REVIEWED_MODAL_CHILDREN").as_deref() == Ok("1") {
        cmd.env("SUPYMEM_REVIEWED_MODAL_CHILDREN", "1");
    }
    if std::env::var("SUPYMEM_REVIEWED_DESKTOP_V2").as_deref() == Ok("1") {
        cmd.env("SUPYMEM_REVIEWED_DESKTOP_V2", "1");
    }
    if std::env::var("SUPYMEM_AGENT_DESKTOP_ENABLED").as_deref() == Ok("1") {
        cmd.env("SUPYMEM_AGENT_DESKTOP_ENABLED", "1");
    }
    cmd.env("SUPYMEM_VERSION", env!("SUPYMEM_DESKTOP_VERSION"));
    cmd.env("COMPANY_OS_APP_VERSION", env!("SUPYMEM_DESKTOP_VERSION"));
    cmd.env("BRIDGE_TOKEN", bridge_token);
    // Where the runtime posts agent-cursor events (see crate::agent_cursor).
    cmd.env("SUPYMEM_AGENT_CURSOR_SOCK", crate::agent_cursor::socket_path());
}

fn apply_review_bridge_env(cmd: &mut Command, packaged_review: bool) {
    // The marked compiled review app alone may hand the local founder session
    // to its native-owned bridge. Public/staging builds clear inherited flags.
    if packaged_review {
        cmd.env("SUPYMEM_LOCAL_REVIEW", "enabled");
    } else {
        cmd.env_remove("SUPYMEM_LOCAL_REVIEW");
    }
}

fn apply_child_env(cmd: &mut Command, g: &Inner) {
    if let Some(path) = &g.shell_path {
        cmd.env("PATH", path);
    }
    cmd.env("COMPANY_OS_DATA_DIR", &g.data_dir);
    cmd.env("COMPANY_OS_REPO_ROOT", &g.repo_root);
    let mut owned: Vec<String> = Vec::new();
    for (k, v) in &g.dotenv {
        cmd.env(k, v);
        if OWNED_AUTH.contains(&k.as_str()) {
            owned.push(k.clone());
        }
    }
    if !owned.is_empty() {
        cmd.env("_COS_OWNED_AUTH", owned.join(","));
    }
}

pub(crate) fn unix_new_session(cmd: &mut Command) {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        unsafe {
            cmd.pre_exec(|| {
                if libc::setsid() < 0 {
                    Err(std::io::Error::last_os_error())
                } else {
                    Ok(())
                }
            });
        }
    }
    let _ = cmd;
}

/// Bridges from earlier runs of this app (same data dir) whose app is gone:
/// session leaders re-parented to launchd. A live app's bridge keeps its app as
/// parent, so a second running instance is never touched.
fn stale_bridge_pids(ps_output: &str, runtime_bin: &Path, data_dir: &Path) -> Vec<i32> {
    let name = runtime_bin.file_name().and_then(|n| n.to_str()).unwrap_or("companyos-runtime");
    let data = data_dir.to_string_lossy();
    ps_output
        .lines()
        .filter_map(|line| {
            let (pid, rest) = line.trim_start().split_once(' ')?;
            let (ppid, cmd) = rest.trim_start().split_once(' ')?;
            let (pid, ppid): (i32, i32) = (pid.parse().ok()?, ppid.parse().ok()?);
            let cmd = cmd.trim_start();
            let first = cmd.split(" --").next().unwrap_or(cmd);
            let args: Vec<_> = cmd.split(" --").skip(1).collect();
            (ppid == 1 && Path::new(first).file_name().and_then(|n| n.to_str()) == Some(name)
                && args.contains(&"role bridge") && args.contains(&format!("data-dir {data}").as_str()))
                .then_some(pid)
        })
        .collect()
}

fn reap_stale_bridges(runtime_bin: &Path, data_dir: &Path) {
    #[cfg(unix)]
    {
        let Ok(out) = Command::new("/bin/ps").args(["-axo", "pid=,ppid=,command="]).output() else { return };
        for pid in stale_bridge_pids(&String::from_utf8_lossy(&out.stdout), runtime_bin, data_dir) {
            // Only our own setsid leaders: the group holds the PyInstaller child too.
            if unsafe { libc::getpgid(pid) } == pid {
                unsafe { libc::kill(-pid, 15); }
                thread::sleep(Duration::from_millis(500));
                unsafe { libc::kill(-pid, 9); }
            }
        }
    }
    let _ = (runtime_bin, data_dir);
}

pub(crate) fn terminate_owned_child(child: &mut Child) {
    #[cfg(unix)]
    {
        let pid = child.id() as i32;
        // Only target a session this supervisor created. This includes the
        // PyInstaller bootloader and its embedded Python child.
        if unsafe { libc::getpgid(pid) } == pid {
            unsafe { libc::kill(-pid, 15); }
            let deadline = Instant::now() + Duration::from_secs(2);
            while matches!(child.try_wait(), Ok(None)) && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(20));
            }
            // Terminate any descendants that ignored the graceful signal.
            unsafe { libc::kill(-pid, 9); }
        }
    }
    #[cfg(windows)]
    {
        let _ = Command::new("taskkill")
            .args(["/PID", &child.id().to_string(), "/T", "/F"])
            .stdout(Stdio::null()).stderr(Stdio::null()).status();
    }
    let _ = child.kill();
    let _ = child.wait();
}

fn hosted_gateway_from(config: &HashMap<String, String>) -> String {
    config.get("COMPANY_OS_PUBLIC_URL")
        .or_else(|| config.get("GATEWAY_URL"))
        .map(|url| url.trim().trim_end_matches('/').to_string())
        .filter(|url| !url.is_empty())
        .unwrap_or_else(|| "https://gateway.supymem.com".into())
}

fn is_packaged_macos_app(executable: &Path) -> bool {
    executable.parent().and_then(Path::parent).and_then(Path::parent)
        .and_then(Path::extension).map(|extension| extension == "app").unwrap_or(false)
}

fn marked_local_review_app(executable: &Path, compiled_for_review: bool) -> bool {
    if !compiled_for_review || !is_packaged_macos_app(executable) {
        return false;
    }
    let Some(contents) = executable.parent().and_then(Path::parent) else { return false; };
    let Ok(info) = plist::Value::from_file(contents.join("Info.plist")) else { return false; };
    info.as_dictionary().and_then(|dict| dict.get("SupymemLocalReviewOnly"))
        .and_then(plist::Value::as_boolean) == Some(true)
}

fn initial_dotenv(repo_root: &Path, executable: &Path,
                  packaged_review_compiled: bool, staging_review: bool) -> HashMap<String, String> {
    if marked_local_review_app(executable, packaged_review_compiled) || staging_review {
        return HashMap::from([("SUPYMEM_MODE".into(), "local".into())]);
    }
    if is_packaged_macos_app(executable) {
        // A normal distributed bundle never reads developer .env or inherits a
        // local endpoint, even when launched from inside a source checkout.
        return HashMap::from([("SUPYMEM_MODE".into(), "hosted".into()),
                              ("GATEWAY_URL".into(), crate::channel::gateway().into()),
                              ("CONSOLE_ORIGIN".into(), crate::channel::console().into())]);
    }
    load_dotenv(&repo_root.join(".env"))
}

pub(crate) fn random_bridge_secret() -> String {
    // Two independently random UUIDs provide 244 random bits. Never use time or
    // a process identifier for the secret protecting native execution.
    format!("{}{}", uuid::Uuid::new_v4().simple(), uuid::Uuid::new_v4().simple())
}

pub fn pick_port(preferred: u16) -> u16 {
    for offset in 0..20 {
        let p = preferred + offset;
        if port_free(p) {
            return p;
        }
    }
    preferred
}

pub fn port_free(port: u16) -> bool {
    std::net::TcpListener::bind(("127.0.0.1", port)).is_ok()
}

fn bridge_port(preferred: u16) -> Result<u16, (&'static str, String)> {
    for port in preferred..=preferred.saturating_add(19) {
        match std::net::TcpListener::bind(("127.0.0.1", port)) {
            Ok(_) => return Ok(port),
            Err(error) if error.kind() == std::io::ErrorKind::AddrInUse => continue,
            Err(_) => return Err(("sidecar_spawn_failed",
                "The local helper cannot open a local port. Check macOS security settings, then quit and reopen Supymem. Your saved sign-in and work are retained.".into())),
        }
    }
    std::net::TcpListener::bind(("127.0.0.1", 0))
        .and_then(|listener| listener.local_addr()).map(|address| address.port())
        .map_err(|_| ("sidecar_port_conflict",
            "The local helper cannot find a free port. Cloud work remains available. Quit other Supymem instances and reopen this app. Your saved sign-in and work are retained.".into()))
}

fn spawn_failure_reason(error: &std::io::Error) -> &'static str {
    match error.kind() {
        std::io::ErrorKind::NotFound => "sidecar_binary_missing",
        std::io::ErrorKind::PermissionDenied => "sidecar_permission_denied",
        _ => "sidecar_spawn_failed",
    }
}

fn bridge_health(port: u16, timeout: Duration) -> Result<(), &'static str> {
    let client = reqwest::blocking::Client::builder().user_agent(crate::http::USER_AGENT)
        .timeout(timeout).redirect(reqwest::redirect::Policy::none()).no_proxy()
        .build().map_err(|_| "sidecar_health_unavailable")?;
    let response = client.get(format!("http://127.0.0.1:{port}/health")).send()
        .map_err(|error| if error.is_timeout() { "sidecar_health_timeout" } else { "sidecar_health_unavailable" })?;
    if !response.status().is_success() { return Err("sidecar_protocol_mismatch"); }
    let body: serde_json::Value = response.json()
        .map_err(|error| if error.is_timeout() { "sidecar_health_timeout" } else { "sidecar_protocol_mismatch" })?;
    if body["service"] == "bridge" && body["status"] == "ok" { Ok(()) }
    else { Err("sidecar_protocol_mismatch") }
}

/// Cold-start budget for the frozen bridge, matching the local-review wait.
const BRIDGE_COLD_START: Duration = Duration::from_secs(45);

fn bridge_cold_starting(since_spawn: Option<Duration>) -> bool {
    since_spawn.is_some_and(|d| d < BRIDGE_COLD_START)
}

fn wait_health(url: &str, timeout_s: f64) -> bool {
    let deadline = Instant::now() + Duration::from_secs_f64(timeout_s);
    while Instant::now() < deadline {
        if http_url_ok(url) {
            return true;
        }
        thread::sleep(Duration::from_millis(300));
    }
    false
}

fn http_ok(port: u16) -> bool {
    http_url_ok(&format!("http://127.0.0.1:{port}/health"))
}

fn console_healthy(port: u16) -> bool {
    http_url_ok(&format!("http://127.0.0.1:{port}/api/health"))
        || http_url_ok(&format!("http://127.0.0.1:{port}/"))
}

pub fn http_url_ok(url: &str) -> bool {
    http_url_ok_with_timeout(url, health_probe_timeout(crate::channel::packaged_local_review()))
}

fn health_probe_timeout(packaged_review: bool) -> Duration {
    // Next dev compilation and local Python services can take several seconds while cold.
    Duration::from_secs(if packaged_review { 10 } else { 1 })
}

fn http_url_ok_with_timeout(url: &str, timeout: Duration) -> bool {
    let client = match reqwest::blocking::Client::builder().user_agent(crate::http::USER_AGENT)
        .timeout(timeout)
        .build()
    {
        Ok(c) => c,
        Err(_) => return false,
    };
    match client.get(url).send() {
        Ok(r) => r.status().is_success(),
        Err(_) => false,
    }
}

/// Fold founder CLI dirs into PATH without duplicates (login-shell order first).
pub fn merge_founder_path(base: &str, extras: &[&str]) -> String {
    let mut parts: Vec<String> = extras
        .iter()
        .filter(|p| !p.is_empty())
        .map(|p| (*p).to_string())
        .collect();
    for part in std::env::split_paths(std::ffi::OsStr::new(base)) {
        let s = part.to_string_lossy().to_string();
        if !s.is_empty() && !parts.iter().any(|p| p == &s) {
            parts.push(s);
        }
    }
    std::env::join_paths(&parts).map(|path| path.to_string_lossy().into_owned()).unwrap_or_else(|_| base.to_string())
}

fn founder_path_extras() -> Vec<String> {
    let mut extras = vec![
        "/Applications/ChatGPT.app/Contents/Resources".into(),
        "/Applications/Codex.app/Contents/Resources".into(),
        "/opt/homebrew/bin".into(),
        "/usr/local/bin".into(),
    ];
    if let Some(home) = dirs::home_dir() {
        extras.insert(0, home.join("Applications/Codex.app/Contents/Resources").display().to_string());
        extras.insert(0, home.join("Applications/ChatGPT.app/Contents/Resources").display().to_string());
        extras.insert(0, home.join(".local/bin").display().to_string());
        extras.insert(0, home.join(".npm-global/bin").display().to_string());
        extras.insert(
            0,
            home.join("Applications/Cursor.app/Contents/Resources/app/bin")
                .display()
                .to_string(),
        );
    }
    extras.insert(
        0,
        "/Applications/Cursor.app/Contents/Resources/app/bin".into(),
    );
    extras.insert(0, conventional_data_dir().join("hermes-venv").join("bin").display().to_string());
    extras
}

fn login_shell_path(timeout: Duration) -> Option<String> {
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".into());
    // Non-interactive login (`-lc`). `-lic` can hang on founders' interactive zshrc.
    let mut child = Command::new(shell)
        .args(["-lc", "printf %s \"$PATH\""])
        .env("TERM", "dumb")
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
            Ok(None) => thread::sleep(Duration::from_millis(40)),
            Err(_) => return None,
        }
    }
    let output = child.wait_with_output().ok()?;
    if !output.status.success() {
        return None;
    }
    let path = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if path.is_empty() {
        None
    } else {
        Some(path)
    }
}

/// Resolve the founder's login-shell PATH (macOS GUI apps otherwise see a bare PATH).
pub fn resolve_shell_path() -> Option<String> {
    let extras = founder_path_extras();
    let extra_refs: Vec<&str> = extras.iter().map(String::as_str).collect();
    #[cfg(target_os = "macos")]
    {
        let from_shell = login_shell_path(Duration::from_secs(2));
        let base = from_shell.or_else(|| std::env::var("PATH").ok()).unwrap_or_default();
        let merged = merge_founder_path(&base, &extra_refs);
        if merged.is_empty() {
            None
        } else {
            Some(merged)
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        let base = std::env::var("PATH").unwrap_or_default();
        let merged = merge_founder_path(&base, &extra_refs);
        if merged.is_empty() {
            None
        } else {
            Some(merged)
        }
    }
}

fn prefer_data_dir(new: PathBuf, legacy: PathBuf) -> PathBuf {
    if new.exists() {
        return new;
    }
    if legacy.exists() {
        match std::fs::rename(&legacy, &new) {
            Ok(()) => new,
            Err(_) => legacy,
        }
    } else {
        new
    }
}

pub fn conventional_data_dir() -> PathBuf {
    if crate::local_stack::is_local() {
        return crate::local_stack::data_dir(std::env::var_os("COMPANY_OS_DATA_DIR").map(PathBuf::from));
    }
    if let Ok(v) = std::env::var("COMPANY_OS_DATA_DIR") {
        return PathBuf::from(v);
    }
    #[cfg(target_os = "macos")]
    {
        let support = dirs::home_dir()
            .unwrap_or_else(|| PathBuf::from("."))
            .join("Library/Application Support");
        if crate::channel::packaged_local_review() { support.join("Supymem Local Review") }
        else if crate::channel::local_review() { support.join("Supymem Staging Local Review") }
        else if crate::channel::staging() { support.join(crate::channel::data_name()) }
        else { prefer_data_dir(support.join("Supymem"), support.join("CompanyOS")) }
    }
    #[cfg(target_os = "windows")]
    {
        let base = dirs::data_dir().unwrap_or_else(|| PathBuf::from("."));
        if crate::channel::staging() { base.join(crate::channel::data_name()) }
        else { prefer_data_dir(base.join("Supymem"), base.join("CompanyOS")) }
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let base = dirs::data_dir().unwrap_or_else(|| PathBuf::from("."));
        if crate::channel::staging() { base.join("supymem-staging") }
        else { prefer_data_dir(base.join("supymem"), base.join("companyos")) }
    }
}

fn sidecar_triple() -> &'static str {
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    {
        return "aarch64-apple-darwin";
    }
    #[cfg(all(target_os = "macos", target_arch = "x86_64"))]
    {
        return "x86_64-apple-darwin";
    }
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    {
        return "x86_64-unknown-linux-gnu";
    }
    #[cfg(all(target_os = "windows", target_arch = "x86_64"))]
    {
        return "x86_64-pc-windows-msvc";
    }
    #[cfg(not(any(
        all(target_os = "macos", target_arch = "aarch64"),
        all(target_os = "macos", target_arch = "x86_64"),
        all(target_os = "linux", target_arch = "x86_64"),
        all(target_os = "windows", target_arch = "x86_64")
    )))]
    {
        "unknown"
    }
}

fn looks_like_script(path: &Path) -> bool {
    let Ok(bytes) = std::fs::read(path) else {
        return false;
    };
    bytes.starts_with(b"#!")
}

fn sidecar_candidates(resource_dir: &Path) -> Vec<PathBuf> {
    let triple = sidecar_triple();
    #[cfg(target_os = "windows")]
    let names = vec![
        format!("companyos-runtime-{triple}"),
        "companyos-runtime".into(),
        format!("companyos-runtime-{triple}.exe"),
        "companyos-runtime.exe".into(),
    ];
    #[cfg(not(target_os = "windows"))]
    let names = vec![
        format!("companyos-runtime-{triple}"),
        "companyos-runtime".into(),
    ];
    let mut dirs = vec![resource_dir.to_path_buf(), resource_dir.join("binaries")];
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            dirs.push(parent.to_path_buf());
        }
    }
    let mut out = Vec::new();
    for dir in dirs {
        for name in &names {
            out.push(dir.join(name));
        }
    }
    out
}

pub fn resolve_runtime_bin(resource_dir: &Path) -> PathBuf {
    let root = resolve_repo_root();
    let py = root.join(".venv/bin/python");
    let packaged = std::env::current_exe().ok().as_deref().map(is_packaged_macos_app).unwrap_or(false);
    // The distributed sidecar intentionally contains only the hosted bridge.
    // Source development still needs the source warden/core/gateway roles.
    if !packaged && looks_like_repo(&root) && py.is_file() {
        return py;
    }
    let candidates = sidecar_candidates(resource_dir);
    let existing: Vec<PathBuf> = candidates.iter().cloned()
        .filter(|p| p.is_file())
        .collect();
    if let Some(bin) = existing.iter().find(|p| !looks_like_script(p)) {
        return bin.clone();
    }
    if packaged { return candidates[0].clone(); }
    if py.exists() {
        return py;
    }
    if let Some(stub) = existing.first() {
        return stub.clone();
    }
    PathBuf::from("python3")
}

pub fn looks_like_repo(root: &Path) -> bool {
    root.join("packages/desktop_runtime").is_dir() && root.join("apps/console").is_dir()
}

pub fn find_repo_root_from(start: &Path) -> Option<PathBuf> {
    let mut dir = if start.is_file() {
        start.parent()?.to_path_buf()
    } else {
        start.to_path_buf()
    };
    for _ in 0..16 {
        if dir.join("packages/desktop_runtime").exists() {
            return Some(dir);
        }
        if !dir.pop() {
            break;
        }
    }
    None
}

pub fn resolve_repo_root() -> PathBuf {
    if let Ok(root) = std::env::var("COMPANY_OS_REPO_ROOT") {
        let p = PathBuf::from(root);
        if p.join("packages/desktop_runtime").exists() {
            return p;
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(found) = find_repo_root_from(&exe) {
            return found;
        }
    }
    // Do not walk cwd: a GUI `open` from ~/Documents trips macOS TCC before attach.
    PathBuf::from(".")
}

pub fn load_dotenv(path: &Path) -> HashMap<String, String> {
    let mut out = HashMap::new();
    let Ok(text) = std::fs::read_to_string(path) else {
        return out;
    };
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') || !line.contains('=') {
            continue;
        }
        let (k, v) = line.split_once('=').unwrap();
        let key = k.trim();
        if key.is_empty() {
            continue;
        }
        let val = v.trim().trim_matches('"').trim_matches('\'').to_string();
        out.insert(key.to_string(), val);
    }
    out
}

pub fn which_in_path(bin: &str, path: Option<&str>) -> Option<PathBuf> {
    let search = path
        .map(ToString::to_string)
        .or_else(|| std::env::var("PATH").ok())?;
    for dir in std::env::split_paths(std::ffi::OsStr::new(&search)) {
        let candidate = dir.join(bin);
        if candidate.is_file() {
            return Some(candidate);
        }
        #[cfg(windows)]
        {
            let exe = dir.join(format!("{bin}.exe"));
            if exe.is_file() {
                return Some(exe);
            }
            let cmd = dir.join(format!("{bin}.cmd"));
            if cmd.is_file() {
                return Some(cmd);
            }
        }
    }
    None
}

pub fn scrub_env(mut env: HashMap<String, String>) -> HashMap<String, String> {
    let mut owned: Vec<String> = OWNED_AUTH.iter().map(|s| (*s).to_string()).collect();
    if let Some(marker) = env.get("_COS_OWNED_AUTH") {
        for part in marker.split(',') {
            if !part.is_empty() {
                owned.push(part.to_string());
            }
        }
    }
    for k in owned {
        env.remove(&k);
    }
    env
}

#[cfg(unix)]
mod libc {
    extern "C" {
        pub fn setsid() -> i32;
        pub fn getpgid(pid: i32) -> i32;
        pub fn kill(pid: i32, signal: i32) -> i32;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bridge_retry_backs_off_and_caps_without_delaying_cold_start() {
        let now = Instant::now();
        let mut retry = BridgeRetry::default();
        retry.failed(BRIDGE_STARTING.into(), now);
        assert_eq!(retry.next, Some(now + Duration::from_secs(1)));
        assert_eq!(retry.attempts, 0);
        for seconds in [2, 4, 8, 16, 32, 60] {
            retry.failed("failed".into(), now);
            assert_eq!(retry.next, Some(now + Duration::from_secs(seconds)));
        }
        assert_eq!(retry.attempts, 6);
    }

    #[test]
    fn bridge_health_refusal_non_bridge_and_real_timeout_are_distinct() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        assert_eq!(bridge_health(port, Duration::from_secs(1)), Err("sidecar_health_unavailable"));
        for (delay, status, expected) in [
            (Duration::ZERO, 404, "sidecar_protocol_mismatch"),
            (Duration::ZERO, 200, "sidecar_protocol_mismatch"),
            (Duration::from_millis(200), 200, "sidecar_health_timeout"),
        ] {
            let (url, server) = delayed_health_response(delay, status);
            let port = url::Url::parse(&url).unwrap().port().unwrap();
            let started = Instant::now();
            assert_eq!(bridge_health(port, Duration::from_millis(100)), Err(expected));
            if expected == "sidecar_health_timeout" { assert!(started.elapsed() >= Duration::from_millis(90)); }
            server.join().unwrap();
        }
    }

    #[test]
    #[cfg(unix)]
    fn missing_blocked_and_exited_runtime_fail_startup_without_health_timeout() {
        use std::os::unix::fs::PermissionsExt;
        let available = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        drop(available);
        let dir = std::env::temp_dir().join(format!("supymem-spawn-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let runtime = dir.join("fixture-runtime");
        let supervisor = Supervisor::new(runtime.clone(), dir.clone(), dir.clone());
        {
            let mut inner = supervisor.inner.lock().unwrap();
            inner.dotenv.clear();
            inner.dotenv.insert("SUPYMEM_MODE".into(), "hosted".into());
        }
        assert!(supervisor.start_all().is_ok(), "the cloud Console must remain usable");
        let error = supervisor.wait_bridge().unwrap_err();
        assert!(error.contains("could not be launched"));
        assert!(supervisor.inner.lock().unwrap().children.is_empty());
        assert_eq!(supervisor.ensure_bridge().unwrap_err(), error);
        std::fs::write(&runtime, "#!/bin/sh\nexit 1\n").unwrap();
        assert!(supervisor.spawn_role("bridge", 3500).unwrap_err().contains("could not be launched"));
        std::fs::set_permissions(&runtime, std::fs::Permissions::from_mode(0o700)).unwrap();
        supervisor.inner.lock().unwrap().bridge_retry = BridgeRetry::default();
        let started = Instant::now();
        assert!(supervisor.wait_bridge().unwrap_err().contains("exited before"));
        assert!(started.elapsed() < Duration::from_secs(5));
        supervisor.stop_all();
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    #[cfg(unix)]
    fn occupied_bridge_ports_fall_back_to_an_ephemeral_port() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let replacement = bridge_port(port).unwrap();
        assert_ne!(replacement, port);
        let dir = std::env::temp_dir().join(format!("supymem-port-{}", uuid::Uuid::new_v4()));
        let supervisor = Supervisor::new(PathBuf::from("/bin/sleep"), dir.clone(), dir.clone());
        supervisor.spawn_role("bridge", port).unwrap();
        assert_ne!(supervisor.inner.lock().unwrap().ports["bridge"], port);
        supervisor.stop_all();
        let mut occupied = vec![listener];
        for candidate in port + 1..=port + 19 {
            if let Ok(listener) = std::net::TcpListener::bind(("127.0.0.1", candidate)) { occupied.push(listener); }
        }
        let replacement = bridge_port(port).unwrap();
        assert!(!occupied.iter().any(|listener| listener.local_addr().unwrap().port() == replacement));
        let blocked = Supervisor::new(PathBuf::from("/bin/sleep"), dir.clone(), dir.clone());
        blocked.spawn_role("bridge", port).unwrap();
        blocked.stop_all();
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn bridge_retry_cap_blocks_even_expired_timers_and_keeps_cloud_ready() {
        let supervisor = Supervisor::new("missing-runtime".into(), PathBuf::new(), PathBuf::new());
        {
            let mut inner = supervisor.inner.lock().unwrap();
            inner.dotenv = HashMap::from([("SUPYMEM_MODE".into(), "hosted".into())]);
            inner.ready = true;
            inner.bridge_retry.attempts = 6;
            inner.bridge_retry.error = "helper unavailable".into();
            inner.bridge_retry.next = Some(Instant::now() - Duration::from_secs(1));
        }
        assert!(supervisor.ensure_bridge().unwrap_err().contains("retries are paused"));
        assert!(supervisor.wait_bridge().unwrap_err().contains("Cloud work remains available"));
        assert!(supervisor.is_ready());
        assert!(supervisor.inner.lock().unwrap().children.is_empty());
        assert_eq!(spawn_failure_reason(&std::io::ErrorKind::NotFound.into()), "sidecar_binary_missing");
        assert_eq!(spawn_failure_reason(&std::io::ErrorKind::PermissionDenied.into()), "sidecar_permission_denied");
    }

    fn delayed_health_response(delay: Duration, status: u16) -> (String, thread::JoinHandle<()>) {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/health", listener.local_addr().unwrap());
        listener.set_nonblocking(true).unwrap();
        let server = thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(5);
            let mut stream = loop {
                match listener.accept() {
                    Ok((stream, _)) => break stream,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock && Instant::now() < deadline => {
                        thread::sleep(Duration::from_millis(10));
                    }
                    Err(error) => panic!("health fixture did not receive its request: {error}"),
                }
            };
            stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
            let mut request = [0; 1024];
            assert!(stream.read(&mut request).unwrap() > 0);
            thread::sleep(delay);
            // The ordinary one-second probe closes before the delayed response arrives.
            let _ = stream.write_all(format!("HTTP/1.1 {status} Test\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok").as_bytes());
        });
        (url, server)
    }

    #[test]
    fn packaged_review_accepts_slow_health_without_relaxing_the_public_timeout() {
        assert_eq!(health_probe_timeout(false), Duration::from_secs(1));
        assert!(health_probe_timeout(true) <= Duration::from_secs(10));
        for (review, expected) in [(false, false), (true, true)] {
            let (url, server) = delayed_health_response(Duration::from_millis(1300), 200);
            let healthy = http_url_ok_with_timeout(&url, health_probe_timeout(review));
            server.join().unwrap();
            assert_eq!(healthy, expected, "review={review}");
        }
    }

    #[test]
    fn review_and_public_health_still_refuse_non_success_responses() {
        for review in [false, true] {
            let (url, server) = delayed_health_response(Duration::ZERO, 503);
            let healthy = http_url_ok_with_timeout(&url, health_probe_timeout(review));
            server.join().unwrap();
            assert!(!healthy, "review={review}");
        }
    }

    #[test]
    fn shared_health_probe_uses_only_the_compiled_review_budget() {
        let (url, server) = delayed_health_response(Duration::from_millis(1300), 200);
        let healthy = http_url_ok(&url);
        server.join().unwrap();
        assert_eq!(healthy, crate::channel::packaged_local_review());
    }

    #[test]
    fn packaged_review_requires_both_compiled_flag_and_bundle_marker() {
        let root = std::env::temp_dir().join(format!("supymem-review-gate-{}", uuid::Uuid::new_v4()));
        let contents = root.join("Supymem.app/Contents");
        let macos = contents.join("MacOS");
        std::fs::create_dir_all(&macos).unwrap();
        let exe = macos.join("company-os-desktop");
        std::fs::write(root.join(".env"), "SUPYMEM_MODE=local\nGATEWAY_URL=http://127.0.0.1:3200\n").unwrap();
        let info = contents.join("Info.plist");
        std::fs::write(&info, r#"<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>SupymemLocalReviewOnly</key><true/></dict></plist>"#).unwrap();

        assert!(marked_local_review_app(&exe, true));
        assert!(!marked_local_review_app(&exe, false));
        assert_eq!(initial_dotenv(&root, &exe, true, false).get("SUPYMEM_MODE").map(String::as_str), Some("local"));
        let ordinary = initial_dotenv(&root, &exe, false, false);
        assert_eq!(ordinary.get("SUPYMEM_MODE").map(String::as_str), Some("hosted"));
        assert_eq!(ordinary.get("GATEWAY_URL").map(String::as_str), Some(crate::channel::gateway()));

        std::fs::write(&info, r#"<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>SupymemLocalReviewOnly</key><false/></dict></plist>"#).unwrap();
        assert!(!marked_local_review_app(&exe, true));
        assert_eq!(initial_dotenv(&root, &exe, true, false).get("SUPYMEM_MODE").map(String::as_str), Some("hosted"));
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    #[cfg(unix)]
    fn hosted_bridge_keeps_running_helper_after_transient_health_miss() {
        let dir = std::env::temp_dir().join(format!("supymem-bridge-health-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let supervisor = Supervisor::new(PathBuf::from("/bin/sleep"), dir.clone(), dir.clone());
        let child = Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let pid = child.id();
        {
            let mut inner = supervisor.inner.lock().unwrap();
            inner.dotenv.remove("SUPYMEM_DESKTOP_MODE");
            inner.dotenv.insert("SUPYMEM_MODE".into(), "hosted".into());
            inner.ports.insert("bridge".into(), pick_port(3500));
            inner.children.insert("bridge".into(), child);
            inner.owned.insert("bridge".into(), true);
            inner.bridge_started_at = Some(Instant::now() - BRIDGE_COLD_START);
        }
        assert!(supervisor.ensure_bridge().unwrap_err().contains("not responding"));
        // A freshly spawned helper that is not listening yet is starting, not timed out.
        {
            let mut inner = supervisor.inner.lock().unwrap();
            inner.bridge_started_at = Some(Instant::now());
            inner.bridge_retry = BridgeRetry::default();
        }
        assert!(supervisor.ensure_bridge().unwrap_err().contains("still starting"));
        let mut inner = supervisor.inner.lock().unwrap();
        let still_running = inner.children.get_mut("bridge").unwrap().try_wait().unwrap().is_none();
        assert!(still_running);
        assert_eq!(inner.children.get("bridge").unwrap().id(), pid);
        drop(inner);
        supervisor.stop_all();
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn bridge_health_miss_is_reported_only_after_cold_start() {
        assert!(bridge_cold_starting(Some(Duration::ZERO)));
        assert!(bridge_cold_starting(Some(Duration::from_secs(44))));
        assert!(!bridge_cold_starting(Some(BRIDGE_COLD_START)));
        assert!(!bridge_cold_starting(None));
    }

    #[test]
    fn console_startup_never_waits_for_local_helper_health() {
        let source = include_str!("supervisor.rs");
        let startup = source.split("pub fn start_all(&self)").nth(1).unwrap()
            .split("/// Block until the hosted bridge").next().unwrap();
        assert!(!startup.contains("ensure_bridge_with_timeout("));
        assert!(!startup.contains("wait_bridge("));
        assert!(!startup.contains("spawn_role(\"bridge\""));
    }

    #[test]
    #[cfg(unix)]
    fn hosted_bridge_restarts_after_owned_helper_exits() {
        use std::os::unix::fs::PermissionsExt;

        let dir = std::env::temp_dir().join(format!("supymem-bridge-restart-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let runtime = dir.join("fixture-runtime");
        std::fs::write(&runtime, r#"#!/bin/sh
port=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--port" ]; then shift; port="$1"; fi
  shift
done
exec python3 -c 'import http.server,sys
class H(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200 if self.path == "/health" else 404); self.end_headers(); self.wfile.write(b"{\"service\":\"bridge\",\"status\":\"ok\"}")
 def log_message(self, *args): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()' "$port"
"#).unwrap();
        let mut permissions = std::fs::metadata(&runtime).unwrap().permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(&runtime, permissions).unwrap();

        let supervisor = Supervisor::new(runtime, dir.clone(), dir.clone());
        {
            let mut inner = supervisor.inner.lock().unwrap();
            // The developer checkout may set the desktop mode to local. This
            // fixture exercises hosted bridge recovery regardless of .env.
            inner.dotenv.remove("SUPYMEM_DESKTOP_MODE");
            inner.dotenv.insert("SUPYMEM_MODE".into(), "hosted".into());
        }
        supervisor.start_hosted().unwrap();
        supervisor.wait_bridge().unwrap();
        assert!(supervisor.inner.lock().unwrap().bridge_healthy);
        let first_pid = supervisor.inner.lock().unwrap().children.get("bridge").unwrap().id();
        {
            let mut inner = supervisor.inner.lock().unwrap();
            let child = inner.children.get_mut("bridge").unwrap();
            child.kill().unwrap();
            child.wait().unwrap();
        }

        supervisor.wait_bridge().unwrap();
        assert!(supervisor.ensure_bridge().unwrap());
        assert!(supervisor.inner.lock().unwrap().bridge_retry.next.is_none());
        assert!(!supervisor.ensure_bridge().unwrap(), "session replay is requested only once");
        let second_pid = supervisor.inner.lock().unwrap().children.get("bridge").unwrap().id();
        assert_ne!(first_pid, second_pid);
        assert!(!supervisor.ensure_bridge().unwrap());
        supervisor.stop_all();
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn no_late_runtime_spawn_after_shutdown_begins() {
        let dir = std::env::temp_dir().join(format!("supymem-stop-test-{}", uuid::Uuid::new_v4()));
        let supervisor = Supervisor::new("nonexistent-test-runtime".into(), dir.clone(), dir);
        supervisor.stop_all();
        assert!(supervisor.start_all().unwrap_err().contains("shutting down"));
        assert!(supervisor.spawn_role("bridge", 3500).unwrap_err().contains("shutting down"));
        assert!(supervisor.spawn_console(3100).unwrap_err().contains("shutting down"));
    }

    #[test]
    #[cfg(unix)]
    fn owned_shutdown_terminates_descendants_and_preserves_unrelated_process() {
        use std::io::{BufRead, BufReader};

        let mut unrelated = Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let mut cmd = Command::new("/bin/sh");
        cmd.args(["-c", "trap 'wait; exit 0' TERM; sleep 30 & echo $!; wait"])
            .stdout(Stdio::piped()).stderr(Stdio::null());
        unix_new_session(&mut cmd);
        let mut child = cmd.spawn().unwrap();
        let mut pid_text = String::new();
        BufReader::new(child.stdout.take().unwrap()).read_line(&mut pid_text).unwrap();
        let descendant: i32 = pid_text.trim().parse().unwrap();
        terminate_owned_child(&mut child);
        assert!(child.try_wait().unwrap().is_some());
        // SIGKILL delivery and launchd's orphan reaping are asynchronous. Observe
        // eventual exit with a deadline instead of treating a transient zombie
        // immediately after the signal as a still-running helper.
        let deadline = Instant::now() + Duration::from_secs(1);
        while unsafe { libc::kill(descendant, 0) } == 0 && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        assert_ne!(unsafe { libc::kill(descendant, 0) }, 0, "runtime descendant survived shutdown");
        assert!(unrelated.try_wait().unwrap().is_none(), "unrelated process was terminated");
        let _ = unrelated.kill();
        let _ = unrelated.wait();
    }

    #[test]
    fn stale_bridge_detection_only_matches_orphans_of_this_data_dir() {
        let bin = Path::new("/Applications/Supymem.app/Contents/MacOS/companyos-runtime");
        let dir = Path::new("/Users/demo-owner/Library/Application Support/Supymem");
        let ps = "  100     1 /Applications/Supymem.app/Contents/MacOS/companyos-runtime --role bridge --port 3500 --data-dir /Users/demo-owner/Library/Application Support/Supymem\n\
  101   100 /Applications/Supymem.app/Contents/MacOS/companyos-runtime --role bridge --port 3500 --data-dir /Users/demo-owner/Library/Application Support/Supymem\n\
  102   555 /Applications/Supymem.app/Contents/MacOS/companyos-runtime --role bridge --port 3501 --data-dir /Users/demo-owner/Library/Application Support/Supymem\n\
  103     1 /Applications/Supymem.app/Contents/MacOS/companyos-runtime --role bridge --port 3502 --data-dir /tmp/other\n\
  104     1 /usr/bin/vim companyos-runtime --role bridge\n";
        assert_eq!(stale_bridge_pids(ps, bin, dir), vec![100]);
        for command in [
            "/Applications/Supymem.app/Contents/MacOS/companyos-runtime-other --role bridge --port 3500 --data-dir /Users/demo-owner/Library/Application Support/Supymem",
            "/Applications/Supymem.app/Contents/MacOS/companyos-runtime --role bridge --port 3500 --data-dir /Users/demo-owner/Library/Application Support/Supymem Other",
            "/Applications/Supymem.app/Contents/MacOS/companyos-runtime --role bridge-other --port 3500 --data-dir /Users/demo-owner/Library/Application Support/Supymem",
        ] {
            assert!(stale_bridge_pids(&format!("100 1 {command}"), bin, dir).is_empty());
        }
    }

    #[test]
    #[cfg(unix)]
    fn hosted_bridge_does_not_inherit_secrets_or_repo_pythonpath() {
        let mut command = Command::new("/usr/bin/env");
        command.env("LLM_API_KEY", "fixture-secret");
        command.env("AWS_SECRET_ACCESS_KEY", "fixture-cloud-secret");
        command.env("PYTHONPATH", "/fixture/repository");
        apply_bridge_env(&mut command, Path::new("/tmp/supymem-fixture"), Some("/usr/bin:/bin"), "fixture-bridge");
        let output = command.output().unwrap();
        assert!(output.status.success());
        let values = String::from_utf8(output.stdout).unwrap();
        assert!(!values.contains("fixture-secret"));
        assert!(!values.contains("fixture-cloud-secret"));
        assert!(!values.contains("PYTHONPATH="));
        assert!(values.contains("BRIDGE_TOKEN=fixture-bridge"));
        assert!(values.contains("SUPYMEM_MODE=hosted"));
        assert!(values.contains(&format!("COMPANY_OS_APP_VERSION={}", env!("SUPYMEM_DESKTOP_VERSION"))));
    }

    #[test]
    #[cfg(unix)]
    fn local_founder_bridge_gate_is_compiled_review_only() {
        for review in [false, true] {
            let mut command = Command::new("/usr/bin/env");
            command.env_clear().env("SUPYMEM_LOCAL_REVIEW", "inherited");
            apply_review_bridge_env(&mut command, review);
            let output = command.output().unwrap();
            assert!(output.status.success());
            let values = String::from_utf8(output.stdout).unwrap();
            assert_eq!(values.contains("SUPYMEM_LOCAL_REVIEW=enabled"), review);
            assert!(!values.contains("inherited"));
            if !review { assert!(!values.contains("SUPYMEM_LOCAL_REVIEW=")); }
        }
    }

    #[test]
    fn bridge_secrets_are_independently_random() {
        let a = random_bridge_secret();
        let b = random_bridge_secret();
        assert_eq!(a.len(), 64);
        assert!(a.chars().all(|ch| ch.is_ascii_hexdigit()));
        assert_ne!(a, b);
    }

    #[test]
    #[cfg(feature = "local-stack")]
    fn local_supervisor_uses_the_stack_bridge_port_and_native_token() {
        let previous = std::env::var_os("SUPYMEM_MODE");
        let _occupied = std::net::TcpListener::bind(("127.0.0.1", 3500)).ok();
        std::env::set_var("SUPYMEM_MODE", "local");
        for domain in ["warehouse.example", "hiking.example"] {
            let root = std::env::temp_dir().join(format!("bridge-{domain}-{}", uuid::Uuid::new_v4()));
            let supervisor = Supervisor::new(root.join("python3"), root.join("data"), root.join("resources"));
            let stack = supervisor.local.as_ref().unwrap();
            let bridge = stack.status().into_iter().find(|role| role.role == "bridge").unwrap();
            assert_ne!(bridge.port, 3500);
            assert_eq!(supervisor.bridge_url(), format!("http://127.0.0.1:{}", bridge.port));
            assert_eq!(supervisor.native_bridge_token(), stack.native_bridge_token);
            assert_eq!(supervisor.bridge_token(), stack.bridge_token());
            assert_ne!(supervisor.bridge_token(), supervisor.native_bridge_token());
        }
        if let Some(previous) = previous { std::env::set_var("SUPYMEM_MODE", previous); }
        else { std::env::remove_var("SUPYMEM_MODE"); }
    }

    #[test]
    fn packaged_bridge_uses_same_hosted_gateway_as_desktop() {
        let mut config = HashMap::new();
        assert_eq!(hosted_gateway_from(&config), "https://gateway.supymem.com");
        config.insert("GATEWAY_URL".into(), "https://gateway.staging.supymem.com/".into());
        assert_eq!(hosted_gateway_from(&config), "https://gateway.staging.supymem.com");
        config.insert("COMPANY_OS_PUBLIC_URL".into(), "https://gateway.supymem.com".into());
        assert_eq!(hosted_gateway_from(&config), "https://gateway.supymem.com");
    }

    #[test]
    fn packaged_app_is_identified_inside_a_checkout_too() {
        assert!(is_packaged_macos_app(Path::new("/repo/dist/Supymem.app/Contents/MacOS/company-os-desktop")));
        assert!(!is_packaged_macos_app(Path::new("/repo/src-tauri/target/debug/company-os-desktop")));
    }

    #[test]
    fn dotenv_parses_keys_without_export() {
        let dir = std::env::temp_dir().join(format!("cos-dotenv-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let p = dir.join(".env");
        std::fs::write(&p, "LLM_API_KEY=secret\n# c\nPATH=/opt/bin\n").unwrap();
        let map = load_dotenv(&p);
        assert_eq!(map.get("LLM_API_KEY").unwrap(), "secret");
        assert_eq!(map.get("PATH").unwrap(), "/opt/bin");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn scrub_drops_platform_keys() {
        let mut env = HashMap::new();
        env.insert("ANTHROPIC_API_KEY".into(), "platform".into());
        env.insert("PATH".into(), "/usr/bin".into());
        env.insert("_COS_OWNED_AUTH".into(), "ANTHROPIC_API_KEY".into());
        let out = scrub_env(env);
        assert!(!out.contains_key("ANTHROPIC_API_KEY"));
        assert_eq!(out.get("PATH").unwrap(), "/usr/bin");
    }

    #[test]
    fn prefer_data_dir_migrates_legacy_folder() {
        let dir = std::env::temp_dir().join(format!("cos-data-{}", std::process::id()));
        let old = dir.join("CompanyOS");
        let new = dir.join("Supymem");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&old).unwrap();
        std::fs::write(old.join("keep.txt"), "ok").unwrap();
        let got = prefer_data_dir(new.clone(), old.clone());
        assert_eq!(got, new);
        assert!(new.join("keep.txt").is_file());
        assert!(!old.exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn find_repo_root_walks_up_from_nested_path() {
        let dir = std::env::temp_dir().join(format!("cos-repo-{}", std::process::id()));
        let nested = dir.join("src-tauri/target/release/bundle/macos");
        let _ = std::fs::create_dir_all(dir.join("packages/desktop_runtime"));
        let _ = std::fs::create_dir_all(&nested);
        let found = find_repo_root_from(&nested.join("Supymem.app"));
        assert_eq!(found.as_deref(), Some(dir.as_path()));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn installed_app_dir_is_not_a_repo() {
        let dir = std::env::temp_dir().join(format!("cos-app-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        assert!(!looks_like_repo(&dir));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn merge_founder_path_puts_cli_dirs_first() {
        for domain in ["warehouse.example", "hiking.example"] {
            let paths = [format!("tools {domain}"), "node".into()];
            let base = std::env::join_paths(&paths).unwrap().to_string_lossy().into_owned();
            let merged = merge_founder_path(&base, &[&paths[0], "python"]);
            assert_eq!(std::env::split_paths(&merged).collect::<Vec<_>>(),
                vec![PathBuf::from(&paths[0]), PathBuf::from("python"), PathBuf::from("node")]);
        }
    }
}
