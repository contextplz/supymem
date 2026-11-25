use std::collections::{HashMap, VecDeque};
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};

use crate::supervisor::{terminate_owned_child, unix_new_session, RoleStatus};

const HOST: &str = "127.0.0.1";
const LAUNCH_LOG_COUNT: usize = 20;
const LAUNCH_LOG_BYTES: u64 = 2 * 1024 * 1024;
const LAUNCH_LOG_TOTAL_BYTES: u64 = LAUNCH_LOG_COUNT as u64 * LAUNCH_LOG_BYTES;
const ROLES: [(&str, u16, &str); 4] = [
    ("core", 3300, "/local-ready"),
    ("worker", 3301, "/local-ready"),
    ("console", 3100, "/api/local-ready"),
    ("bridge", 3500, "/local-ready"),
];

use crate::local_diagnostics::redact_startup_line;

fn prune_launch_logs(current: &Path) -> std::io::Result<()> {
    let directory = current.parent().expect("log directory");
    if !std::fs::symlink_metadata(directory)?.is_dir() { return Ok(()); }
    let mut logs = Vec::new();
    for entry in std::fs::read_dir(directory)?.flatten() {
        let name = entry.file_name();
        if !name.to_str().and_then(|name| name.strip_prefix("desktop-local-"))
            .and_then(|name| name.strip_suffix(".log"))
            .is_some_and(|instance| uuid::Uuid::parse_str(instance).is_ok()) { continue; }
        let path = entry.path();
        if path == current { continue; }
        let Ok(metadata) = path.symlink_metadata() else { continue; };
        if metadata.is_file() { logs.push((metadata.modified()?, path, metadata.len())); }
    }
    logs.sort_unstable_by(|left, right| right.cmp(left));
    let mut kept = 1;
    let mut remaining = LAUNCH_LOG_TOTAL_BYTES - LAUNCH_LOG_BYTES;
    for (_, path, bytes) in logs {
        if kept < LAUNCH_LOG_COUNT && bytes <= remaining {
            kept += 1;
            remaining -= bytes;
        } else if let Err(error) = std::fs::remove_file(path) {
            if error.kind() != std::io::ErrorKind::NotFound { return Err(error); }
        }
    }
    Ok(())
}

fn append_launch_log(file: &mut File, line: &[u8]) {
    // shortcut: keep the active log name stable and stop at its cap, rotate it if a longer on-disk tail is needed.
    if file.metadata().is_ok_and(|metadata| metadata.is_file()
        && metadata.len().saturating_add(line.len() as u64) <= LAUNCH_LOG_BYTES) {
        let _ = file.write_all(line);
    }
}

fn core_restart_delay(now: Instant, attempts: &VecDeque<Instant>) -> Option<Duration> {
    let recent = attempts.iter().rev()
        .take_while(|attempt| now.duration_since(**attempt) < Duration::from_secs(300)).count();
    match recent {
        0 => Some(Duration::ZERO),
        1 => Some(Duration::from_secs(1)),
        2 => Some(Duration::from_secs(2)),
        _ => None,
    }
}

fn remember_stderr(tail: &mut VecDeque<String>, line: &str) {
    if tail.len() == 15 { tail.pop_front(); }
    tail.push_back(redact_startup_line(line));
}

fn startup_fix(reason: &str) -> &'static str {
    let lower = reason.to_ascii_lowercase();
    if ["dyld", "library not loaded", "libnode", "cannot open shared object", "no module named"].iter().any(|marker| lower.contains(marker)) {
        "Replace your app's runtime: rerun ./start from your checkout, or reinstall your local installer, then reopen your app."
    } else if lower.contains("permission denied") {
        "Allow your account to write to your app's data folder, then reopen your app."
    } else if lower.contains("address already in use") {
        "Quit and reopen your app to select another local connection."
    } else {
        "Select Retry to try again. If it fails again, rebuild with ./start or reinstall your local installer."
    }
}

fn maintenance_recovery(line: &str) -> Option<&'static str> {
    if let Some(reason) = line.strip_suffix(" To cancel this replacement, stop your app and worker, run `python -m app.cli_local cancel-restore`, then reopen your app.") {
        if reason.starts_with("Your backup settings could not be unlocked. Your workspace was not replaced.") {
            return Some(concat!(
                "Your backup settings could not be unlocked. To use this backup, unlock your original system keychain or use the matching recovery key in a separate data folder. ",
                "To keep your current workspace, quit your app, run `python -m app.cli_local cancel-restore`, then reopen your app. ",
                "You can import a data-only ZIP from your original app in Keys and connections, under Your data and recovery."
            ));
        }
        return Some(concat!(
            "Your restore did not finish. If your backup drive is disconnected, reconnect it and reopen your app. ",
            "To cancel the restore, quit your app, run `python -m app.cli_local cancel-restore`, then reopen your app."
        ));
    }
    if line.rsplit_once("If your app cannot open, stop your app and worker, then use the recovery command `python -m app.cli_local upgrade --output ")
        .is_some_and(|(_, path)| path.ends_with("` to back up in the default location and update.")) {
        return Some(concat!(
            "Your update needs a writable backup folder. Reconnect your backup drive and reopen your app, or quit your app and run ",
            "`python -m app.cli_local upgrade --output \"FULL_BACKUP_FOLDER_PATH\"`. Replace the example path with the full path to a writable folder outside your data folder. ",
            "After reopening, set Full backup folder path in Keys and connections, under Your data folder and backup."
        ));
    }
    None
}

struct StartupStderr {
    tail: Arc<Mutex<VecDeque<String>>>,
    recovery: Arc<Mutex<Option<&'static str>>>,
    reader: Option<thread::JoinHandle<()>>,
    finished: std::sync::mpsc::Receiver<()>,
}

impl StartupStderr {
    fn finish(&mut self) {
        // shortcut: detached descendants can retain stderr past 250 ms, use nonblocking capture if those descendants must be supervised.
        if self.finished.recv_timeout(Duration::from_millis(250)).is_ok() {
            if let Some(reader) = self.reader.take() { let _ = reader.join(); }
        }
    }
}

pub(crate) fn is_local() -> bool {
    cfg!(feature = "local-stack")
        && std::env::var("SUPYMEM_MODE")
            .is_ok_and(|value| value.trim().eq_ignore_ascii_case("local"))
}

pub(crate) fn data_dir(override_path: Option<PathBuf>) -> PathBuf {
    let path = override_path.filter(|path| !path.as_os_str().is_empty()).unwrap_or_else(|| {
        dirs::data_local_dir()
            .unwrap_or_else(|| PathBuf::from("."))
            .join(env!("SUPYMEM_LOCAL_DATA_NAME"))
    });
    std::env::current_dir().map(|root| root.join(&path)).unwrap_or(path)
}

pub(crate) fn prepare_data_dir(path: &std::path::Path, _resource_dir: &std::path::Path) -> Result<(), String> {
    if path != data_dir(None) { return Ok(()); }
    let legacy = dirs::data_local_dir().unwrap_or_else(|| PathBuf::from("."))
        .join(env!("CARGO_PKG_NAME")).join("local");
    #[cfg(windows)]
    let result = migrate_windows_data_dir(&legacy, path, _resource_dir);
    #[cfg(not(windows))]
    let result = migrate_data_dir(&legacy, path);
    #[cfg(windows)]
    if matches!(&result, Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists) {
        return Err(format!("Your saved work is split between {} and {}. Close both apps and copy the data, backups, and pending restore folders at both locations to a separate folder without combining them. If your original app still opens your workspace, use Settings > Keys and connections > Your data and recovery to create a full backup. Otherwise, contact support before moving either copy.", legacy.display(), path.display()));
    }
    #[cfg(windows)]
    if matches!(&result, Err(error) if error.kind() == std::io::ErrorKind::InvalidData) {
        return Err(format!("Your app could not verify the unfinished move from {} to {}. Close both apps. Put back any folders you moved since the move began, and set aside any new folders without deleting them. Then reopen your app. If you did not move anything, keep both locations and their backups and contact support.", legacy.display(), path.display()));
    }
    result.map_err(|error| format!(
        "Your data folder could not be moved: {error}. Close your app, check the permissions of {} and {}, then reopen it. Keep your backups.",
        legacy.display(), path.display()))
}

#[cfg(windows)]
const WINDOWS_RELOCATION: &str = r#"import sys
from pathlib import Path
root, legacy, target = map(Path, sys.argv[1:])
runtime = Path(sys.executable).parent
sys.path[:0] = [str(root / "services/company_os"), str(root / "packages"), str(runtime / "Lib/site-packages")]
import hashlib
import json
import os
import stat
from contextlib import ExitStack
from app.core.local_process import check_private, file_lock, open_private
from app.core.local_windows import atomic_write, private_directory

def directory_identity(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISDIR(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise OSError("Invalid data folder")
    if next(path.iterdir(), None) is not None:
        return [info.st_dev, info.st_ino]

target.parent.mkdir(parents=True, exist_ok=True)
legacy.parent.mkdir(parents=True, exist_ok=True)
with ExitStack() as locks:
    for directory in (legacy, target):
        path = directory.with_name(f".{directory.name}-process.lock")
        lock = locks.enter_context(os.fdopen(open_private(path, os.O_CREAT | os.O_RDWR), "rb"))
        check_private(lock)
        locks.enter_context(file_lock(lock, exclusive=True, blocking=False))
    pairs = [(legacy, target),
             (legacy.with_name(f"{legacy.name}-backups"), target.with_name(f"{target.name}-backups")),
             (legacy.with_name(f".{legacy.name}-pending-restore"), target.with_name(f".{target.name}-pending-restore"))]
    sources = [directory_identity(source) for source, _ in pairs]
    destinations = [directory_identity(destination) for _, destination in pairs]
    journal = target.with_name(f".{target.name}-relocation.json")
    # Directory IDs survive same-volume renames, unlike paths. Check every pair before moving anything.
    plan = {"roots": [os.path.normcase(str(directory)) for directory in (legacy, target)],
            "directories": [source or destination for source, destination in zip(sources, destinations)]}
    try:
        descriptor = open_private(journal, os.O_RDONLY)
    except FileNotFoundError:
        resuming = False
    else:
        resuming = True
        with os.fdopen(descriptor, "rb") as stream:
            check_private(stream)
            try:
                saved = json.loads(stream.read(1_048_577))
            except ValueError:
                sys.exit(4)
        if saved != plan or any(source and destination for source, destination in zip(sources, destinations)):
            sys.exit(4)
    if not resuming and any(sources) and any(destinations):
        sys.exit(3)
    if resuming or any(sources):
        if any(identity and not identity[1] for identity in plan["directories"]):
            raise OSError("Could not identify a data folder")
    if sources[0]:
        private_directory(legacy.parent)
        private_directory(legacy)
        providers = legacy / "vault/providers"
        scope = providers / "keychain.scope"
        if (providers / "settings.json").exists():
            private_directory(legacy / "vault")
            private_directory(providers)
            if not scope.exists():
                atomic_write(scope, hashlib.sha256(str(providers.resolve()).encode()).hexdigest().encode())
        record = legacy / ".data-directory.json"
        if not record.exists():
            atomic_write(record, json.dumps({"source_roots": [str(legacy.resolve())]}).encode())
    elif target.exists():
        private_directory(target)
    if not resuming and any(sources):
        atomic_write(journal, json.dumps(plan).encode())
    for (source, destination), identity in zip(pairs, sources):
        if not identity:
            continue
        private_directory(source)
        try:
            destination.rmdir()
        except FileNotFoundError:
            pass
        source.rename(destination)
    if resuming or any(sources):
        journal.unlink()
"#;

fn set_openssl_conf(command: &mut Command, resources: &Path) {
    command.env("OPENSSL_CONF", std::env::var_os("OPENSSL_CONF")
        .unwrap_or_else(|| resources.join("local/openssl.cnf").into_os_string()));
}

#[cfg(windows)]
fn migrate_windows_data_dir(legacy: &std::path::Path, target: &std::path::Path, resources: &std::path::Path) -> std::io::Result<()> {
    let runtime = resources.join("local/runtime").join(LocalStack::runtime_target());
    let mut command = Command::new(runtime.join("python.exe"));
    command.env_clear();
    for name in ["SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"] {
        if let Some(value) = std::env::var_os(name) { command.env(name, value); }
    }
    set_openssl_conf(&mut command, resources);
    let output = command.args(["-I", "-c", WINDOWS_RELOCATION]).arg(resources.join("local"))
        .arg(legacy).arg(target).env("SUPYMEM_MODE", "local").env("APP_ENV", "local")
        .current_dir(resources).stdin(Stdio::null()).output()?;
    if output.status.code() == Some(3) {
        return Err(std::io::Error::new(std::io::ErrorKind::AlreadyExists, "Both destinations contain saved work"));
    }
    if output.status.code() == Some(4) {
        return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "Unverified data move"));
    }
    if !output.status.success() {
        for line in String::from_utf8_lossy(&output.stderr).lines() { log::warn!("{}", redact_startup_line(line)); }
        return Err(std::io::Error::other("Your data relocation stopped. Close your other app and local commands, check your data-folder permissions, then reopen your app. Keep your backups."));
    }
    Ok(())
}

#[cfg(not(windows))]
fn migrate_data_dir(legacy: &std::path::Path, target: &std::path::Path) -> std::io::Result<()> {
    match std::fs::symlink_metadata(target) {
        Ok(metadata) if metadata.is_dir() && !metadata.is_symlink() => {
            #[cfg(unix)]
            private_directory(target)?;
            return if legacy.try_exists()? { Ok(()) } else { migrate_related_dirs(legacy, target) };
        },
        Ok(_) => return Err(std::io::Error::new(std::io::ErrorKind::InvalidInput, "Your data must be a regular folder")),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {},
        Err(error) => return Err(error),
    }
    let metadata = match std::fs::symlink_metadata(legacy) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return migrate_related_dirs(legacy, target),
        Err(error) => return Err(error),
    };
    if !metadata.is_dir() || metadata.is_symlink() {
        return Err(std::io::Error::new(std::io::ErrorKind::InvalidInput, "Your existing data must be a regular folder"));
    }
    #[cfg(unix)]
    {
        private_directory(legacy.parent().expect("legacy parent"))?;
        private_directory(legacy)?;
    }
    #[cfg(unix)]
    let _process_lock = {
        use std::os::fd::AsRawFd;
        use std::os::unix::fs::OpenOptionsExt;
        let name = legacy.file_name().expect("legacy name").to_string_lossy();
        match OpenOptions::new().read(true).write(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(legacy.with_file_name(format!(".{name}-process.lock"))) {
            Ok(file) => {
                if !file.metadata()?.is_file() || unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
                    return Err(std::io::Error::new(std::io::ErrorKind::WouldBlock,
                        "Your existing data is in use. Close other copies of your local app and local commands, then reopen your app"));
                }
                Some(file)
            },
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(error) => return Err(error),
        }
    };
    let providers = legacy.join("vault/providers");
    let scope = providers.join("keychain.scope");
    if providers.join("settings.json").try_exists()? && !scope.try_exists()? {
        use sha2::{Digest, Sha256};
        #[cfg(unix)]
        {
            private_directory(&legacy.join("vault"))?;
            private_directory(&providers)?;
        }
        let digest = format!("{:x}", Sha256::digest(providers.canonicalize()?.to_string_lossy().as_bytes()));
        let temporary = providers.join(format!(".scope-{}", uuid::Uuid::new_v4()));
        let result = (|| {
            let mut options = OpenOptions::new();
            options.write(true).create_new(true);
            #[cfg(unix)]
            { use std::os::unix::fs::OpenOptionsExt; options.mode(0o600); }
            let mut file = options.open(&temporary)?;
            file.write_all(digest.as_bytes())?;
            file.sync_all()?;
            std::fs::rename(&temporary, &scope)
        })();
        if result.is_err() { let _ = std::fs::remove_file(&temporary); }
        result?;
    }
    let record = legacy.join(".data-directory.json");
    if !record.try_exists()? {
        let temporary = legacy.join(format!(".directory-{}", uuid::Uuid::new_v4()));
        let result = (|| {
            let mut options = OpenOptions::new();
            options.write(true).create_new(true);
            #[cfg(unix)]
            { use std::os::unix::fs::OpenOptionsExt; options.mode(0o600); }
            let mut file = options.open(&temporary)?;
            let origin = legacy.canonicalize()?.to_string_lossy().into_owned();
            #[cfg(windows)]
            let origin = match origin.strip_prefix(r"\\?\UNC\") {
                Some(network) => format!(r"\\{network}"),
                None => origin.strip_prefix(r"\\?\").unwrap_or(&origin).to_owned(),
            };
            file.write_all(serde_json::json!({"source_roots": [origin]}).to_string().as_bytes())?;
            file.sync_all()?;
            std::fs::rename(&temporary, &record)
        })();
        if result.is_err() { let _ = std::fs::remove_file(&temporary); }
        result?;
    }
    std::fs::rename(legacy, target)?;
    log::info!("desktop_data_migrated from={} to={}", legacy.display(), target.display());
    migrate_related_dirs(legacy, target)
}

fn migrate_related_dirs(legacy: &std::path::Path, target: &std::path::Path) -> std::io::Result<()> {
    let legacy_name = legacy.file_name().expect("legacy name").to_string_lossy();
    let target_name = target.file_name().expect("target name").to_string_lossy();
    for (prefix, suffix) in [("", "-backups"), (".", "-pending-restore")] {
        let source = legacy.with_file_name(format!("{prefix}{legacy_name}{suffix}"));
        let destination = target.with_file_name(format!("{prefix}{target_name}{suffix}"));
        match std::fs::symlink_metadata(&destination) {
            Ok(metadata) if metadata.is_dir() && !metadata.is_symlink() => continue,
            Ok(_) => return Err(std::io::Error::new(std::io::ErrorKind::InvalidInput, "Your backups and queued restore must be regular folders")),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {},
            Err(error) => return Err(error),
        }
        let metadata = match std::fs::symlink_metadata(&source) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
        };
        if !metadata.is_dir() || metadata.is_symlink() {
            return Err(std::io::Error::new(std::io::ErrorKind::InvalidInput, "Your backups and queued restore must be regular folders"));
        }
        #[cfg(unix)]
        {
            private_directory(source.parent().expect("source parent"))?;
            private_directory(&source)?;
        }
        std::fs::rename(&source, &destination)?;
        log::info!("desktop_data_migrated from={} to={}", source.display(), destination.display());
    }
    Ok(())
}

pub(crate) struct LocalStack {
    resource_dir: PathBuf,
    data_dir: PathBuf,
    pub(crate) log_path: PathBuf,
    log_write: Arc<Mutex<()>>,
    instance: String,
    readiness_keys: [String; ROLES.len()],
    bridge_token: String,
    pub(crate) native_bridge_token: String,
    ports: [u16; ROLES.len()],
    reserved_ports: Mutex<Option<Result<Vec<TcpListener>, String>>>,
    children: Mutex<Vec<(&'static str, Child)>>,
    health_checked: Mutex<Instant>,
    core_restarts: Mutex<VecDeque<Instant>>,
    stderr: Mutex<HashMap<&'static str, StartupStderr>>,
    starting: Mutex<()>,
    healthy: [AtomicBool; ROLES.len()],
    last_error: Mutex<String>,
    maintenance_message: Arc<Mutex<String>>,
    stopped: AtomicBool,
    cancelled: AtomicBool,
    ready: AtomicBool,
    credentials: OnceLock<crate::local_credentials::LocalCredentials>,
}

impl LocalStack {
    pub(crate) fn new(resource_dir: PathBuf, data_dir: PathBuf) -> Self {
        Self::with_ports(resource_dir, data_dir, ROLES.map(|(_, port, _)| port))
    }

    fn with_ports(resource_dir: PathBuf, data_dir: PathBuf, preferred: [u16; ROLES.len()]) -> Self {
        let instance = uuid::Uuid::new_v4().to_string();
        let listeners = preferred.iter().map(|port| {
            TcpListener::bind((HOST, *port)).or_else(|error| {
                if error.kind() == std::io::ErrorKind::AddrInUse { TcpListener::bind((HOST, 0)) } else { Err(error) }
            }).map_err(|error| format!("Your local connection could not reserve a port: {error}. Reopen your app to retry."))
        }).collect::<Result<Vec<_>, _>>();
        let ports = listeners.as_ref().map(|listeners| std::array::from_fn(|index|
            listeners[index].local_addr().expect("bound local address").port())).unwrap_or(preferred);
        log::info!("desktop_startup phase=local_ports core={} worker={} console={} bridge={}", ports[0], ports[1], ports[2], ports[3]);
        Self {
            resource_dir,
            log_path: data_dir.join("logs").join(format!("desktop-local-{instance}.log")),
            log_write: Arc::new(Mutex::new(())),
            data_dir,
            instance,
            readiness_keys: std::array::from_fn(|_| crate::supervisor::random_bridge_secret()),
            bridge_token: crate::supervisor::random_bridge_secret(),
            native_bridge_token: crate::supervisor::random_bridge_secret(),
            ports,
            reserved_ports: Mutex::new(Some(listeners)),
            children: Mutex::new(Vec::new()),
            health_checked: Mutex::new(Instant::now()),
            core_restarts: Mutex::new(VecDeque::new()),
            stderr: Mutex::new(HashMap::new()),
            starting: Mutex::new(()),
            healthy: std::array::from_fn(|_| AtomicBool::new(false)),
            last_error: Mutex::new(String::new()),
            maintenance_message: Arc::new(Mutex::new(String::new())),
            stopped: AtomicBool::new(false),
            cancelled: AtomicBool::new(false),
            ready: AtomicBool::new(false),
            credentials: OnceLock::new(),
        }
    }

    pub(crate) fn url(&self, role: &str) -> String {
        let role = if role == "gateway" { "console" } else { role };
        let index = ROLES.iter().position(|(name, _, _)| *name == role).expect("local role");
        format!("http://{HOST}:{}", self.ports[index])
    }

    pub(crate) fn bridge_token(&self) -> String { self.bridge_token.clone() }

    pub(crate) fn start(&self) -> Result<(), String> {
        self.start_with_console(|| Ok(()))
    }

    pub(crate) fn start_with_console(&self, open_console: impl FnOnce() -> Result<(), String>) -> Result<(), String> {
        let _starting = self.starting.try_lock().map_err(|_| "Your app is already starting. Wait for it to finish, then try again.".to_string())?;
        if self.cancelled.load(Ordering::SeqCst) { return Err("Your app is closing. Reopen your app to try again.".into()); }
        if self.ready.load(Ordering::SeqCst) { return open_console().map_err(|reason| self.error(&reason)); }
        self.stopped.store(false, Ordering::SeqCst);
        self.core_restarts.lock().map_err(|error| error.to_string())?.clear();
        self.last_error.lock().map_err(|error| error.to_string())?.clear();
        self.stderr.lock().map_err(|error| error.to_string())?.clear();
        self.start_inner(open_console).map_err(|reason| {
            self.stop();
            self.error(&reason)
        })
    }

    fn start_inner(&self, open_console: impl FnOnce() -> Result<(), String>) -> Result<(), String> {
        let started = Instant::now();
        let deadline = started + Duration::from_secs(240);
        if !self.resource_dir.is_absolute() || !self.data_dir.is_absolute() {
            return Err("The bundled resources or writable data folder could not be resolved".into());
        }
        prepare_data_dir(&self.data_dir, &self.resource_dir)?;
        // shortcut: retries retain native trust and URLs, reopen your app to select new ports after a new conflict.
        let listeners = self.reserved_ports.lock().map_err(|error| error.to_string())?.take().unwrap_or_else(|| {
            self.ports.iter().map(|port| TcpListener::bind((HOST, *port))
                .map_err(|error| format!("Local port {port} is unavailable: {error}"))).collect()
        })?;
        let mut directory = std::fs::DirBuilder::new();
        directory.recursive(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            directory.mode(0o700);
        }
        directory.create(&self.data_dir).map_err(|error| error.to_string())?;
        #[cfg(unix)]
        private_directory(&self.data_dir).map_err(|error| error.to_string())?;
        directory.create(self.log_path.parent().expect("log directory")).map_err(|error| error.to_string())?;
        self.log_file()?;
        if prune_launch_logs(&self.log_path).is_err() {
            log::debug!("desktop_launch_log_retention_failed");
        }
        let mut command = self.command("core", self.ports[0]);
        command.arg("--maintenance");
        log::info!("desktop_startup phase=maintenance_spawn ms={}", started.elapsed().as_millis());
        let mut maintenance = self.spawn_child("core", command)
            .map_err(|error| format!("Your data could not be prepared. {error}"))?;
        self.wait_for_maintenance(&mut maintenance, (started + Duration::from_secs(180)).min(deadline))?;
        log::info!("desktop_startup phase=maintenance_ready ms={}", started.elapsed().as_millis());
        let startup_deadline = (Instant::now() + Duration::from_secs(60)).min(deadline);
        let mut worker_deadline = startup_deadline;
        let client = reqwest::blocking::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(1))
            .build().map_err(|error| error.to_string())?;
        if !self.resource_dir.join("console/apps/console/server.js").is_file() {
            return Err("Bundled console/server.js is missing".into());
        }
        let mut worker_port = None;
        for (index, listener) in listeners.into_iter().enumerate() {
            if ROLES[index].0 == "worker" {
                worker_port = Some(listener);
            } else {
                self.spawn_role(index, listener, started)?;
            }
        }
        let mut open_console = Some(open_console);
        loop {
            self.check_children()?;
            for (index, (role, _, _)) in ROLES.iter().enumerate() {
                if self.healthy[index].load(Ordering::SeqCst) { continue; }
                if *role == "worker" && open_console.is_some() { continue; }
                let role_deadline = if *role == "worker" { worker_deadline } else { startup_deadline };
                if self.probe_ready(index, &client).unwrap_or(false) {
                    self.healthy[index].store(true, Ordering::SeqCst);
                    log::info!("desktop_startup phase=ready role={role} ms={}", started.elapsed().as_millis());
                    if *role == "core" {
                        if let Some(listener) = worker_port.take() {
                            worker_deadline = (Instant::now() + Duration::from_secs(60)).min(deadline);
                            self.spawn_role(1, listener, started)?;
                        }
                    }
                } else if Instant::now() >= role_deadline {
                    return Err(format!("Local {role} did not become healthy"));
                }
            }
            if [0, 2, 3].iter().all(|&index| self.healthy[index].load(Ordering::SeqCst)) {
                if let Some(open) = open_console.take() {
                    open()?;
                    log::info!("desktop_startup phase=console_open ms={}", started.elapsed().as_millis());
                }
            }
            if self.healthy.iter().all(|healthy| healthy.load(Ordering::SeqCst)) { break; }
            thread::sleep(Duration::from_millis(100));
        }
        self.check_children()?;
        self.ready.store(true, Ordering::SeqCst);
        *self.health_checked.lock().map_err(|error| error.to_string())? = Instant::now();
        Ok(())
    }

    fn spawn_role(&self, index: usize, listener: TcpListener, started: Instant) -> Result<(), String> {
        let (role, _, _) = ROLES[index];
        let mut children = self.children.lock().map_err(|error| error.to_string())?;
        if self.stopped.load(Ordering::SeqCst) || self.cancelled.load(Ordering::SeqCst) {
            return Err("Local startup was cancelled".into());
        }
        drop(listener);
        let child = self.spawn_child(role, self.command(role, self.ports[index]))?;
        children.push((role, child));
        log::info!("desktop_startup phase=spawn role={role} ms={}", started.elapsed().as_millis());
        Ok(())
    }

    fn wait_for_maintenance(&self, child: &mut Child, deadline: Instant) -> Result<(), String> {
        loop {
            match child.try_wait() {
                Ok(Some(status)) => {
                    if let Ok(mut captures) = self.stderr.lock() {
                        if let Some(capture) = captures.get_mut("core") { capture.finish(); }
                    }
                    if status.success() {
                        self.maintenance_message.lock().map_err(|error| error.to_string())?.clear();
                        return Ok(());
                    }
                    return Err("Your data could not be prepared or restored. Close other copies of your app, check your data-folder permissions and free disk space, then select Retry. Keep your backups.".into());
                },
                Ok(None) if Instant::now() < deadline && !self.stopped.load(Ordering::SeqCst) && !self.cancelled.load(Ordering::SeqCst) => {},
                _ => {
                    terminate_owned_child(child);
                    return Err("Your data preparation stopped or timed out. Close other copies of your app, then select Retry. Your saved work is retained. Keep your automatic backups. Retry restores an interrupted update before opening your app.".into());
                }
            }
            thread::sleep(Duration::from_millis(100));
        }
    }

    fn spawn_child(&self, role: &'static str, mut command: Command) -> Result<Child, String> {
        let mut log = self.log_file()?;
        let is_maintenance = command.get_args().any(|argument| argument == std::ffi::OsStr::new("--maintenance"));
        let mut captures = self.stderr.lock().map_err(|error| error.to_string())?;
        command.stdout(Stdio::null()).stderr(Stdio::piped());
        unix_new_session(&mut command);
        let mut child = command.spawn().map_err(|error| format!("Cannot start local {role}: {error}"))?;
        if role != "console" { self.credentials().attach(&mut child); }
        let stderr = child.stderr.take().expect("piped child stderr");
        let tail = Arc::new(Mutex::new(VecDeque::new()));
        let captured = Arc::clone(&tail);
        let recovery = Arc::new(Mutex::new(None));
        let captured_recovery = Arc::clone(&recovery);
        let maintenance_message = Arc::clone(&self.maintenance_message);
        let log_write = Arc::clone(&self.log_write);
        let (done, finished) = std::sync::mpsc::channel();
        let reader = thread::spawn(move || {
            let mut input = BufReader::new(stderr);
            loop {
                let mut bytes = Vec::new();
                let count = match input.by_ref().take(8193).read_until(b'\n', &mut bytes) { Ok(count) => count, Err(_) => break };
                if count == 0 { break; }
                let line = if bytes.len() > 8192 {
                    while !bytes.ends_with(b"\n") {
                        bytes.clear();
                        if input.by_ref().take(8193).read_until(b'\n', &mut bytes).unwrap_or(0) == 0 { break; }
                    }
                    "[Long private diagnostic detail hidden.]".to_string()
                } else { String::from_utf8_lossy(&bytes).trim_end_matches(['\r', '\n']).to_string() };
                if is_maintenance {
                    if let Some(action) = maintenance_recovery(&line) {
                        if let Ok(mut recovery) = captured_recovery.lock() { *recovery = Some(action); }
                        continue;
                    }
                }
                let safe = redact_startup_line(&line);
                if is_maintenance && ["Checking your saved data", "Saving your automatic backup", "Updating your saved data", "Your saved data is ready", "Your automatic backup was restored"].iter().any(|prefix| safe.starts_with(prefix)) {
                    if let Ok(mut message) = maintenance_message.lock() { *message = safe.clone(); }
                }
                if let Ok(_guard) = log_write.lock() {
                    append_launch_log(&mut log, format!("{role}: {safe}\n").as_bytes());
                }
                if let Ok(mut tail) = captured.lock() { remember_stderr(&mut tail, &line); }
            }
            let _ = done.send(());
        });
        captures.insert(role, StartupStderr { tail, recovery, reader: Some(reader), finished });
        Ok(child)
    }

    fn log_file(&self) -> Result<File, String> {
        #[cfg(unix)]
        private_directory(self.log_path.parent().expect("log directory")).map_err(|error| error.to_string())?;
        let mut options = OpenOptions::new();
        options.append(true).create(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
            options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
        }
        let log = options.open(&self.log_path).map_err(|error| error.to_string())?;
        if !log.metadata().map_err(|error| error.to_string())?.is_file() {
            return Err("Your startup log must be a private regular file".into());
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if log.metadata().map_err(|error| error.to_string())?.permissions().mode() & 0o077 != 0 {
                return Err("Your startup log permissions must be private".into());
            }
        }
        Ok(log)
    }

    fn runtime_target() -> &'static str {
        if cfg!(all(target_os = "macos", target_arch = "aarch64")) {
            "aarch64-apple-darwin"
        } else if cfg!(all(target_os = "macos", target_arch = "x86_64")) {
            "x86_64-apple-darwin"
        } else {
            env!("TAURI_ENV_TARGET_TRIPLE")
        }
    }

    fn command(&self, role: &str, port: u16) -> Command {
        let runtime = self.resource_dir.join("local/runtime").join(LocalStack::runtime_target());
        let mut command = Command::new(runtime.join(if role == "console" {
            if cfg!(windows) { "node.exe" } else { "bin/node" }
        } else if cfg!(windows) { "python.exe" } else { "bin/python3" }));
        command.env_clear();
        for name in ["PATH", "HOME", "USERNAME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "LLM_OFFLINE", "LLM_CASSETTE_MODE", "GUARDRAILS_MODE", "EMAIL_PROVIDER"] {
            if let Some(value) = std::env::var_os(name) { command.env(name, value); }
        }
        set_openssl_conf(&mut command, &self.resource_dir);
        if role == "console" {
            command.stdin(Stdio::null());
            command.arg(self.resource_dir.join("local/console.cjs"))
                .arg(self.resource_dir.join("console/apps/console/server.js"))
                .env("PORT", port.to_string()).env("HOSTNAME", HOST);
        } else {
            command.stdin(Stdio::piped()).env("SUPYMEM_LOCAL_KEY_PIPE", "1");
            command.args(["-I", "-c", r#"import sys
from pathlib import Path
root = Path(sys.argv.pop(1))
runtime = Path(sys.executable).parent
site = runtime / "Lib/site-packages" if sys.platform == "win32" else runtime.parent / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
sys.path[:0] = [str(root / "services/company_os"), str(root / "packages"), str(site)]
from desktop_runtime.parent_watch import exit_when_parent_dies
exit_when_parent_dies()
import runpy
runpy.run_path(str(root / "launcher.py"), run_name="__main__")
"#]).arg(self.resource_dir.join("local"))
                .args(["--role", role, "--host", HOST, "--port", &port.to_string(), "--data-dir"])
                .arg(&self.data_dir)
                .env("APP_ROLE", if role == "worker" { "worker" } else { "api" });
        }
        #[cfg(target_os = "linux")]
        command.env("LD_LIBRARY_PATH", runtime.join("lib"));
        if role == "bridge" {
            command.env("BRIDGE_AUTH_OWNER", "native")
                .env("BRIDGE_TOKEN", &self.bridge_token)
                .env("BRIDGE_NATIVE_TOKEN", &self.native_bridge_token);
        }
        let index = ROLES.iter().position(|(name, _, _)| *name == role).expect("local role");
        command.current_dir(self.data_dir.parent().unwrap_or(&self.data_dir))
            .env("SUPYMEM_MODE", "local")
            .env("SUPYMEM_LOCAL_READINESS_KEY", &self.readiness_keys[index])
            .env("SUPYMEM_APP_VERSION", env!("SUPYMEM_DESKTOP_VERSION"))
            .env("SUPYMEM_DESKTOP_INSTANCE_ID", &self.instance)
            .env("SUPYMEM_PARENT_PID", std::process::id().to_string())
            .env("COMPANY_OS_DATA_DIR", &self.data_dir)
            .env("APP_ENV", "local")
            .env("NEXT_TELEMETRY_DISABLED", "1")
            .env("WARDEN_URL", format!("http://{HOST}:3400"))
            .env("WORKER_HEALTH_PORT", self.ports[1].to_string())
            .env("CORE_PORT", self.ports[0].to_string())
            .env("WORKER_PORT", self.ports[1].to_string())
            .env("CONSOLE_PORT", self.ports[2].to_string())
            .env("BRIDGE_PORT", self.ports[3].to_string())
            .env("CORE_URL", self.url("core"))
            .env("COMPANY_OS_URL", self.url("core"))
            .env("GATEWAY_URL", self.url("gateway"))
            .env("COMPANY_OS_PUBLIC_URL", self.url("gateway"))
            .env("NEXT_PUBLIC_GATEWAY_URL", self.url("gateway"))
            .env("CONSOLE_ORIGIN", self.url("console"))
            .env("CONSOLE_URL", self.url("console"))
            .env("PUBLIC_PORTAL_URL", self.url("console"));
        command
    }

    fn credentials(&self) -> &crate::local_credentials::LocalCredentials {
        self.credentials.get_or_init(|| crate::local_credentials::LocalCredentials::start(self.data_dir.clone()))
    }

    fn check_children(&self) -> Result<(), String> {
        let mut children = self.children.lock().map_err(|error| error.to_string())?;
        if self.stopped.load(Ordering::SeqCst) || self.cancelled.load(Ordering::SeqCst) {
            return Err("Local startup was cancelled".into());
        }
        for position in 0..children.len() {
            let (role, child) = &mut children[position];
            if !child_finished(child).map_err(|error| error.to_string())? { continue; }
            let index = match *role {
                "core" if self.ready.load(Ordering::SeqCst) => 0,
                "worker" if self.ready.load(Ordering::SeqCst) => 1,
                _ => return Err(format!("Local {role} stopped")),
            };
            self.restart_role(index, &mut children, position)?;
        }
        Ok(())
    }

    fn stop_role(&self, role: &str, child: &mut Child) {
        terminate_owned_child(child);
        if let Ok(mut captures) = self.stderr.lock() {
            if let Some(capture) = captures.get_mut(role) {
                capture.finish();
            }
        }
    }

    fn restart_role(&self, index: usize, children: &mut Vec<(&'static str, Child)>, position: usize) -> Result<(), String> {
        let (role, _, _) = ROLES[index];
        self.healthy[index].store(false, Ordering::SeqCst);
        let (_, mut child) = children.remove(position);
        self.stop_role(role, &mut child);
        let client = reqwest::blocking::Client::builder().no_proxy()
            .redirect(reqwest::redirect::Policy::none()).timeout(Duration::from_secs(1))
            .build().map_err(|error| error.to_string())?;
        let mut last_error = "Local core stopped responding after repeated restarts".to_string();
        loop {
            if role == "core" {
                let delay = {
                    let mut attempts = self.core_restarts.lock().map_err(|error| error.to_string())?;
                    let now = Instant::now();
                    let Some(delay) = core_restart_delay(now, &attempts) else { return Err(last_error); };
                    if attempts.len() == 3 { attempts.pop_front(); }
                    attempts.push_back(now + delay);
                    delay
                };
                thread::sleep(delay);
            }
            if self.stopped.load(Ordering::SeqCst) || self.cancelled.load(Ordering::SeqCst) {
                return Err("Local startup was cancelled".into());
            }
            let error = match self.spawn_child(role, self.command(role, self.ports[index])) {
                Ok(mut replacement) => match self.wait_for_replacement(index, &mut replacement, &client) {
                    Ok(()) => {
                        children.insert(position, (role, replacement));
                        self.healthy[index].store(true, Ordering::SeqCst);
                        log::info!("desktop_startup phase=restarted role={role}");
                        return Ok(());
                    }
                    Err(error) => {
                        self.stop_role(role, &mut replacement);
                        error
                    }
                },
                Err(error) => format!("Local {role} did not restart: {error}"),
            };
            if role != "core" { return Err(error); }
            last_error = format!("Local core stopped responding after repeated restarts: {error}");
        }
    }

    fn wait_for_replacement(&self, index: usize, child: &mut Child, client: &reqwest::blocking::Client) -> Result<(), String> {
        let (role, _, _) = ROLES[index];
        let deadline = Instant::now() + Duration::from_secs(60);
        loop {
            if self.stopped.load(Ordering::SeqCst) || self.cancelled.load(Ordering::SeqCst) {
                return Err("Local startup was cancelled".into());
            }
            if child_finished(child).map_err(|error| error.to_string())? || Instant::now() >= deadline {
                return Err(format!("Local {role} did not recover"));
            }
            if self.probe_ready(index, client).unwrap_or(false) { return Ok(()); }
            thread::sleep(Duration::from_millis(100));
        }
    }

    fn probe_ready(&self, index: usize, client: &reqwest::blocking::Client) -> Result<bool, reqwest::Error> {
        let (role, _, path) = ROLES[index];
        let nonce = uuid::Uuid::new_v4().simple().to_string();
        let response = client.get(format!("{}{path}", self.url(role)))
            .header("X-Local-Ready-Nonce", &nonce).send()?;
        Ok(readiness_proven(response.status(), response.headers(), &self.readiness_keys[index], &nonce))
    }

    pub(crate) fn failure(&self) -> Option<String> {
        if self.stopped.load(Ordering::SeqCst) {
            return None;
        }
        self.check_children().and_then(|_| self.check_health()).err().map(|reason| {
            self.stop();
            self.error(&reason)
        })
    }

    fn check_health(&self) -> Result<(), String> {
        if !self.ready.load(Ordering::SeqCst) { return Ok(()); }
        let mut checked = self.health_checked.lock().map_err(|error| error.to_string())?;
        if checked.elapsed() < Duration::from_secs(5) { return Ok(()); }
        let client = reqwest::blocking::Client::builder().no_proxy()
            .redirect(reqwest::redirect::Policy::none()).timeout(Duration::from_secs(1))
            .build().map_err(|error| error.to_string())?;
        for index in [0, 1] {
            let (role, _, _) = ROLES[index];
            let ready = self.probe_ready(index, &client);
            if ready.as_ref().is_ok_and(|healthy| *healthy) { continue; }
            if role == "core" && ready.as_ref().is_err_and(|error| error.is_timeout())
                && checked.elapsed() < Duration::from_secs(60) { return Ok(()); }
            let mut children = self.children.lock().map_err(|error| error.to_string())?;
            let position = children.iter().position(|(name, _)| *name == role)
                .ok_or_else(|| format!("Local {role} is unavailable"))?;
            self.restart_role(index, &mut children, position)?;
        }
        *checked = Instant::now();
        Ok(())
    }

    pub(crate) fn error(&self, reason: &str) -> String {
        let role = ROLES.iter().find(|(role, _, _)| reason.to_ascii_lowercase().contains(*role)).map(|(role, _, _)| *role)
            .or_else(|| (reason.contains("data could not be prepared") || reason.contains("data preparation")).then_some("core"));
        let (details, recovery) = self.stderr.lock().ok().and_then(|captures| {
            let capture = captures.get(role?)?;
            let details = capture.tail.lock().ok().map(|tail| tail.iter().cloned().collect::<Vec<_>>().join("\n"));
            let recovery = capture.recovery.lock().ok().and_then(|recovery| *recovery);
            Some((details.unwrap_or_default(), recovery))
        }).unwrap_or_default();
        log::info!("desktop_startup phase=local_failure reason={reason}");
        let mut options = OpenOptions::new();
        options.append(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
        }
        if let (Ok(_guard), Ok(mut log)) = (self.log_write.lock(), options.open(&self.log_path)) {
            append_launch_log(&mut log, format!("{reason}\n").as_bytes());
        }
        let reason_lower = reason.to_ascii_lowercase();
        let occupied = self.ports.iter().find(|port| reason_lower.contains(&format!("port {port} is unavailable")));
        let part = if reason_lower.contains("console") { "Your app screen" }
            else if reason_lower.contains("worker") { "Your background work" }
            else if reason_lower.contains("core") { "Your saved work" }
            else if reason_lower.contains("port") { "Your app's local connection" }
            else { "Your local app" };
        let action = recovery.map(str::to_owned)
            .or_else(|| occupied.map(|port| format!("Another app is using local port {port}. Quit and reopen your app to select another port.")))
            .unwrap_or_else(|| format!("{part} is unavailable. {}", startup_fix(&format!("{reason}\n{details}"))));
        let mut message = format!("{action} Your saved work is retained.");
        if recovery.is_none() {
            message.push('\n');
            message.push_str(if details.is_empty() { reason } else { &details });
        }
        if let Ok(mut error) = self.last_error.lock() { *error = message.clone(); }
        message
    }

    pub(crate) fn last_error(&self) -> String {
        self.last_error.lock().map(|error| error.clone()).unwrap_or_default()
    }

    pub(crate) fn maintenance_message(&self) -> String {
        self.maintenance_message.lock().map(|message| message.clone()).unwrap_or_default()
    }

    pub(crate) fn stop(&self) {
        self.stopped.store(true, Ordering::SeqCst);
        self.ready.store(false, Ordering::SeqCst);
        if let Ok(mut ports) = self.reserved_ports.lock() { *ports = None; }
        for healthy in &self.healthy { healthy.store(false, Ordering::SeqCst); }
        if let Ok(mut children) = self.children.lock() {
            for (_, mut child) in children.drain(..).rev() {
                terminate_owned_child(&mut child);
            }
        }
        if let Ok(mut captures) = self.stderr.lock() {
            for capture in captures.values_mut() {
                capture.finish();
            }
        }
    }

    pub(crate) fn shutdown(&self) {
        self.cancelled.store(true, Ordering::SeqCst);
        self.stop();
    }

    pub(crate) fn status(&self) -> Vec<RoleStatus> {
        let children = self.children.lock().unwrap();
        ROLES.iter().enumerate().map(|(index, (role, _, _))| {
            let pid = children.iter().find(|(name, _)| name == role).map(|(_, child)| child.id());
            RoleStatus { role: (*role).into(), port: self.ports[index], healthy: pid.is_some() && self.healthy[index].load(Ordering::SeqCst), pid, owned: pid.is_some() }
        }).collect()
    }
}

impl Drop for LocalStack {
    fn drop(&mut self) {
        self.shutdown();
    }
}

#[cfg(unix)]
fn private_directory(path: &std::path::Path) -> std::io::Result<()> {
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
    let directory = OpenOptions::new().read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_DIRECTORY).open(path)?;
    let metadata = directory.metadata()?;
    if metadata.uid() != unsafe { libc::geteuid() } {
        return Err(std::io::Error::new(std::io::ErrorKind::PermissionDenied,
            "Your data or log folder belongs to another account. Choose a folder you own, then restart your app."));
    }
    directory.set_permissions(std::fs::Permissions::from_mode(0o700))
}

fn child_finished(child: &mut Child) -> std::io::Result<bool> {
    #[cfg(unix)]
    {
        let mut info = unsafe { std::mem::zeroed::<libc::siginfo_t>() };
        let result = unsafe {
            libc::waitid(libc::P_PID, child.id() as libc::id_t, &mut info,
                libc::WEXITED | libc::WNOHANG | libc::WNOWAIT)
        };
        if result < 0 { return Err(std::io::Error::last_os_error()); }
        // Keep the session leader unreaped until its owned descendants are stopped.
        Ok(info.si_signo != 0)
    }
    #[cfg(not(unix))]
    { child.try_wait().map(|status| status.is_some()) }
}

fn readiness_proof(key: &str, nonce: &str) -> String {
    debug_assert_eq!(key.len(), 64);
    let mut inner_pad = [0x36; 64];
    let mut outer_pad = [0x5c; 64];
    for ((inner, outer), byte) in inner_pad.iter_mut().zip(outer_pad.iter_mut()).zip(key.bytes()) {
        *inner ^= byte;
        *outer ^= byte;
    }
    let mut inner = Sha256::new();
    inner.update(inner_pad);
    inner.update(nonce);
    let mut outer = Sha256::new();
    outer.update(outer_pad);
    outer.update(inner.finalize());
    format!("{:x}", outer.finalize())
}

fn readiness_proven(status: reqwest::StatusCode, headers: &reqwest::header::HeaderMap, key: &str, nonce: &str) -> bool {
    status == reqwest::StatusCode::NO_CONTENT && headers.get("x-local-ready-proof")
        .is_some_and(|proof| proof.as_bytes() == readiness_proof(key, nonce).as_bytes())
}

#[cfg(feature = "local-stack")]
pub fn smoke(resource_dir: PathBuf, data_dir: PathBuf) -> Result<(), String> {
    let stack = LocalStack::new(resource_dir, data_dir);
    stack.start()?;
    let client = reqwest::blocking::Client::builder().no_proxy()
        .redirect(reqwest::redirect::Policy::none()).timeout(Duration::from_secs(5))
        .build().map_err(|error| stack.error(&error.to_string()))?;
    let setup = client.get(format!("{}/setup", stack.url("console"))).send()
        .map_err(|error| stack.error(&error.to_string()))?;
    if !setup.status().is_success() || !setup.text().unwrap_or_default().contains("Checking your AI settings") {
        return Err(stack.error("Your first-run setup page did not load"));
    }
    if !stack.probe_ready(2, &client).unwrap_or(false) {
        return Err(stack.error("Your console could not be verified. Reopen your app to retry."));
    }
    let provider = client.get(format!("{}/api/local/providers", stack.url("console")))
        .header("X-Local-Proxy-Secret", crate::nav_guard::owner_secret(&stack.data_dir.join("owner-session.secret"))
            .map_err(|error| stack.error(&error.to_string()))?).send()
        .map_err(|error| stack.error(&error.to_string()))?;
    if !provider.status().is_success() || provider.json::<serde_json::Value>()
        .map_err(|error| stack.error(&error.to_string()))?["configured"] != false {
        return Err(stack.error("Your fresh installation did not request your own AI settings"));
    }
    if !stack.probe_ready(3, &client).unwrap_or(false) {
        return Err(stack.error("Your coding tools connection could not be verified. Reopen your app to retry."));
    }
    let coding = client.get(format!("{}/native/coding-agents", stack.url("bridge")))
        .header("x-native-token", &stack.native_bridge_token).send()
        .map_err(|error| stack.error(&error.to_string()))?;
    if !coding.status().is_success() || !coding.json::<serde_json::Value>()
        .map_err(|error| stack.error(&error.to_string()))?["agents"].is_array() {
        return Err(stack.error("Your coding tools could not be checked. Open Settings > Keys and connections > Coding tools and select Re-detect coding tools."));
    }
    println!("PASS: core, worker, bridge coding status, /api/local-ready, /setup and first-run provider configuration");
    stack.stop();
    for port in stack.ports {
        TcpListener::bind((HOST, port)).map_err(|error| stack.error(&error.to_string()))?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn launch_logs_keep_the_newest_within_the_count_and_byte_budget() {
        for domain in ["inventory.example", "cycling.example"] {
            let root = std::env::temp_dir().join(format!("launch-logs-{}-{domain}", uuid::Uuid::new_v4()));
            let logs = root.join("logs");
            std::fs::create_dir_all(&logs).unwrap();
            let current = logs.join(format!("desktop-local-{}.log", uuid::Uuid::new_v4()));
            let mut log = OpenOptions::new().append(true).create_new(true).open(&current).unwrap();
            let start = std::time::SystemTime::UNIX_EPOCH + Duration::from_secs(60);
            let past: Vec<_> = (0..22).map(|index| {
                let path = logs.join(format!("desktop-local-{}.log", uuid::Uuid::new_v4()));
                let older = File::create(&path).unwrap();
                older.set_len(1).unwrap();
                older.set_modified(start + Duration::from_secs(index)).unwrap();
                path
            }).collect();
            let backup = root.join("backup-newest.tar.gz");
            let database = logs.join("workspace.sqlite3");
            let unrelated = logs.join("desktop-local-founder.log");
            for saved in [&backup, &database, &unrelated] { std::fs::write(saved, domain).unwrap(); }

            prune_launch_logs(&current).unwrap();
            assert_eq!(past.iter().filter(|path| path.exists()).count(), 19);
            assert!(!past[2].exists() && past[3].exists());
            let newest = past.last().unwrap();
            OpenOptions::new().write(true).open(newest).unwrap().set_len(LAUNCH_LOG_TOTAL_BYTES - LAUNCH_LOG_BYTES).unwrap();
            prune_launch_logs(&current).unwrap();
            assert!(current.exists() && newest.exists() && !past[20].exists());
            log.set_len(LAUNCH_LOG_BYTES - 1).unwrap();
            append_launch_log(&mut log, b"too long");
            assert_eq!(log.metadata().unwrap().len(), LAUNCH_LOG_BYTES - 1);
            append_launch_log(&mut log, b"\n");
            assert_eq!(log.metadata().unwrap().len(), LAUNCH_LOG_BYTES);
            drop(log);
            for saved in [&backup, &database, &unrelated] { assert_eq!(std::fs::read_to_string(saved).unwrap(), domain); }
            std::fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    fn local_maintenance_runs_outside_the_replaceable_data_directory() {
        for domain in ["warehouse.example", "hiking.example"] {
            let parent = std::env::temp_dir().join(format!("local-maintenance-{domain}"));
            let stack = LocalStack::new(parent.join("resources"), parent.join("data"));
            let command = stack.command("core", 3300);
            assert_eq!(command.get_current_dir(), Some(parent.as_path()));
            assert!(command.get_args().any(|argument| argument == stack.data_dir.as_os_str()));
        }
    }

    #[test]
    fn startup_stderr_keeps_the_last_fifteen_lines_and_hides_unknown_values() {
        for domain in ["inventory.example", "cycling.example"] {
            let mut tail = VecDeque::new();
            for index in 0..22 { remember_stderr(&mut tail, &format!("dyld: {domain} diagnostic {index}")); }
            assert_eq!(tail.len(), 15);
            assert_eq!(tail.front().unwrap(), &format!("dyld: {domain} diagnostic 7"));
            remember_stderr(&mut tail, "password: short-private-value");
            remember_stderr(&mut tail, "unlabelled-private-value");
            assert_eq!(tail.len(), 15);
            assert!(!tail.iter().any(|line| line.contains("private-value")));
        }
    }

    #[test]
    fn startup_diagnostics_keep_loader_reasons_but_not_connection_secrets() {
        for domain in ["inventory.example", "cycling.example"] {
            let loader = "Library not loaded: @rpath/libnode.141.dylib";
            assert_eq!(redact_startup_line(loader), loader);
            for line in [format!("Reason: https://user:short-private-value@{domain}"),
                format!("dyld: sk-{}", "abcdefghijklmnopqrstuvwxyz123456"),
                "Authorization: Bearer short-private-value".into(),
                ["-----BEGIN", "PRIVATE KEY-----"].join(" "),
                "unlabelled-private-value".into()] {
                let safe = redact_startup_line(&line);
                assert!(!safe.contains("short-private-value"));
                assert!(!safe.contains("abcdefghijklmnopqrstuvwxyz123456"));
                assert!(!safe.contains("PRIVATE KEY"));
                assert!(!safe.contains("unlabelled-private-value"));
            }
        }
    }

    #[test]
    fn startup_loader_failure_names_the_runtime_rebuild_step() {
        for domain in ["inventory.example", "cycling.example"] {
            assert!(startup_fix(&format!("dyld: {domain}: Library not loaded: @rpath/libnode.141.dylib")).contains("./start"));
            assert!(startup_fix("Permission denied").contains("data folder"));
            assert!(startup_fix("Address already in use").contains("reopen"));
        }
    }

    #[cfg(unix)]
    #[test]
    fn failed_child_stderr_reaches_the_page_and_log_only_after_redaction() {
        for domain in ["inventory.example", "cycling.example"] {
            for role in ["console", "core"] {
                let root = std::env::temp_dir().join(format!("startup-stderr-{}", uuid::Uuid::new_v4()));
                let data = root.join(domain);
                std::fs::create_dir_all(data.join("logs")).unwrap();
                let stack = LocalStack::with_ports(root.clone(), data, [0; ROLES.len()]);
                let mut command = Command::new("/bin/sh");
                command.args(["-c", "printf '%s\\n' 'password: short-private-value' 'Library not loaded: @rpath/libnode.141.dylib' >&2; exit 1"]);
                let mut child = stack.spawn_child(role, command).unwrap();
                let reason = if role == "core" {
                    stack.wait_for_maintenance(&mut child, Instant::now() + Duration::from_secs(2)).unwrap_err()
                } else {
                    assert!(!child.wait().unwrap().success());
                    "Local console stopped".to_string()
                };
                stack.stop();
                let message = stack.error(&reason);
                assert!(message.contains("./start"));
                assert!(message.contains("Library not loaded: @rpath/libnode.141.dylib"));
                assert!(!message.contains("short-private-value"));
                assert!(!std::fs::read_to_string(&stack.log_path).unwrap().contains("short-private-value"));
                drop(stack);
                std::fs::remove_dir_all(root).unwrap();
            }
        }
    }

    #[cfg(unix)]
    #[test]
    fn failed_maintenance_shows_the_specific_recovery_step() {
        let cancel = "To cancel this replacement, stop your app and worker, run `python -m app.cli_local cancel-restore`, then reopen your app.";
        let cases = [
            ("inventory.example", "Choose a folder you can write to.", cancel, "python -m app.cli_local cancel-restore"),
            ("cycling.example", "Choose a folder you can write to.", "If your app cannot open, stop your app and worker, then use the recovery command `python -m app.cli_local upgrade --output /private-path` to back up in the default location and update.", "python -m app.cli_local upgrade --output"),
            ("cycling.example", "Your backup settings could not be unlocked. Your workspace was not replaced.", cancel, "matching recovery key"),
        ];
        for (domain, error, instruction, expected) in cases {
            let root = std::env::temp_dir().join(format!("maintenance-recovery-{}", uuid::Uuid::new_v4()));
            let data = root.join(domain);
            std::fs::create_dir_all(data.join("logs")).unwrap();
            let saved = data.join("workspace.sqlite3");
            std::fs::write(&saved, domain).unwrap();
            let stack = LocalStack::with_ports(root.clone(), data, [0; ROLES.len()]);
            let mut command = Command::new("/bin/sh");
            command.args(["-c", "printf '%s\\n' \"$1 $2\" 'password: short-private-value' >&2; exit 1",
                "--maintenance", error, instruction]);
            let mut child = stack.spawn_child("core", command).unwrap();
            let reason = stack.wait_for_maintenance(&mut child, Instant::now() + Duration::from_secs(2)).unwrap_err();
            let message = stack.error(&reason);
            assert!(message.contains(expected));
            assert_eq!(stack.last_error(), message);
            assert!(!message.to_ascii_lowercase().contains("select retry"));
            assert!(!message.contains("/private-path"));
            assert!(!message.contains("short-private-value"));
            let log = std::fs::read_to_string(&stack.log_path).unwrap();
            assert!(!log.contains("/private-path"));
            assert!(!log.contains("short-private-value"));
            assert_eq!(std::fs::read_to_string(saved).unwrap(), domain);
            drop(stack);
            std::fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    #[cfg(not(windows))]
    fn local_data_directory_uses_packaged_name_and_preserves_saved_work() {
        let config: serde_json::Value = serde_json::from_str(include_str!("../tauri.local.json")).unwrap();
        #[cfg(all(target_os = "linux", feature = "local-stack"))]
        {
            let context: tauri::Context<tauri::Wry> = tauri::generate_context!();
            assert_eq!(context.config().identifier, config["identifier"].as_str().unwrap());
            assert_eq!(context.package_info().name, config["productName"].as_str().unwrap());
        }
        assert_eq!(data_dir(None).file_name().unwrap(), config["productName"].as_str().unwrap());
        for domain in ["procurement.example", "gardening.example"] {
            let root = std::env::temp_dir().join(format!("local-data-{}", uuid::Uuid::new_v4()));
            let legacy = root.join(env!("CARGO_PKG_NAME")).join("local");
            let target = root.join(config["productName"].as_str().unwrap());
            let hosted_config: serde_json::Value = serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
            let hosted = root.join(hosted_config["productName"].as_str().unwrap());
            assert_ne!(target, hosted);
            std::fs::create_dir_all(legacy.join("backups")).unwrap();
            let providers = legacy.join("vault/providers");
            std::fs::create_dir_all(&providers).unwrap();
            std::fs::write(providers.join("settings.json"), "{}").unwrap();
            use sha2::{Digest, Sha256};
            let scope = format!("{:x}", Sha256::digest(providers.canonicalize().unwrap().to_string_lossy().as_bytes()));
            std::fs::create_dir_all(&hosted).unwrap();
            std::fs::write(hosted.join("saved.txt"), domain).unwrap();
            std::fs::write(legacy.join("database.sqlite3"), domain).unwrap();
            std::fs::write(legacy.join("backups/database.sqlite3"), domain).unwrap();
            let backups = legacy.with_file_name("local-backups");
            let pending = legacy.with_file_name(".local-pending-restore");
            std::fs::create_dir_all(&backups).unwrap();
            std::fs::create_dir_all(&pending).unwrap();
            std::fs::write(backups.join("backup.tar.gz"), domain).unwrap();
            std::fs::write(pending.join("request.json"), domain).unwrap();
            #[cfg(unix)]
            {
                use std::os::fd::AsRawFd;
                let lock = OpenOptions::new().read(true).write(true).create_new(true)
                    .open(legacy.with_file_name(".local-process.lock")).unwrap();
                assert_eq!(unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_SH) }, 0);
                assert!(migrate_data_dir(&legacy, &target).is_err());
                assert_eq!(std::fs::read_to_string(legacy.join("database.sqlite3")).unwrap(), domain);
                assert!(!target.exists());
            }
            migrate_data_dir(&legacy, &target).unwrap();
            assert!(!legacy.exists());
            migrate_data_dir(&legacy, &target).unwrap();
            assert_eq!(std::fs::read_to_string(target.join("database.sqlite3")).unwrap(), domain);
            assert_eq!(std::fs::read_to_string(target.join("backups/database.sqlite3")).unwrap(), domain);
            assert_eq!(std::fs::read_to_string(target.join("vault/providers/keychain.scope")).unwrap(), scope);
            let target_name = target.file_name().unwrap().to_str().unwrap();
            assert_eq!(std::fs::read_to_string(target.with_file_name(format!("{target_name}-backups")).join("backup.tar.gz")).unwrap(), domain);
            assert_eq!(std::fs::read_to_string(target.with_file_name(format!(".{target_name}-pending-restore")).join("request.json")).unwrap(), domain);
            assert_eq!(std::fs::read_to_string(hosted.join("saved.txt")).unwrap(), domain);
            std::fs::create_dir_all(&legacy).unwrap();
            std::fs::write(legacy.join("saved.txt"), domain).unwrap();
            migrate_data_dir(&legacy, &target).unwrap();
            assert_eq!(std::fs::read_to_string(legacy.join("saved.txt")).unwrap(), domain);
            let missing_parent = root.join("missing/target");
            assert!(migrate_data_dir(&legacy, &missing_parent).is_err());
            assert_eq!(std::fs::read_to_string(legacy.join("saved.txt")).unwrap(), domain);
            #[cfg(unix)]
            {
                let linked = root.join("linked");
                std::os::unix::fs::symlink(&legacy, &linked).unwrap();
                assert!(migrate_data_dir(&linked, &root.join("new")).is_err());
                assert!(!root.join("new").exists());
            }
            std::fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    fn local_children_keep_release_version_and_session_bus_without_secrets() {
        for domain in ["procurement.example", "gardening.example"] {
            let root = std::env::temp_dir().join(domain);
            let stack = LocalStack::new(root.clone(), root.join("data"));
            let openssl_conf = std::env::var_os("OPENSSL_CONF")
                .unwrap_or_else(|| root.join("local/openssl.cnf").into_os_string());
            for role in ["core", "worker", "console", "bridge"] {
                let command = stack.command(role, 3300);
                for (name, expected) in [
                    ("SUPYMEM_APP_VERSION", Some(std::ffi::OsString::from(env!("SUPYMEM_DESKTOP_VERSION")))),
                    ("DBUS_SESSION_BUS_ADDRESS", std::env::var_os("DBUS_SESSION_BUS_ADDRESS")),
                    ("XDG_RUNTIME_DIR", std::env::var_os("XDG_RUNTIME_DIR")),
                    ("OPENSSL_CONF", Some(openssl_conf.clone())),
                ] {
                    assert_eq!(command.get_envs().find(|(key, _)| *key == name)
                        .and_then(|(_, value)| value), expected.as_deref());
                }
                assert!(command.get_envs().all(|(name, _)| name != "OPENAI_API_KEY"));
            }
        }
    }

    #[test]
    #[cfg(unix)]
    fn bundled_backend_entrypoint_handles_maintenance_core_and_worker() {
        use std::os::unix::fs::symlink;
        let python = Command::new("python3").args(["-c", "import sys; print(sys.executable)"]).output().unwrap();
        assert!(python.status.success());
        for domain in ["procurement.example", "gardening.example"] {
            let root = std::env::temp_dir().join(format!("local-entrypoint-{}", uuid::Uuid::new_v4())).join(domain);
            let binaries = root.join("local/runtime").join(LocalStack::runtime_target()).join("bin");
            let package = root.join("local/packages/desktop_runtime");
            std::fs::create_dir_all(&binaries).unwrap();
            std::fs::create_dir_all(&package).unwrap();
            let settings = root.join("local/services/company_os/app/core");
            std::fs::create_dir_all(&settings).unwrap();
            std::fs::write(settings.join("local_env_import.py"), "import os\ndef import_local_env():\n    assert os.environ['CRASH_HOOKS_INSTALLED'] == '1'\n").unwrap();
            std::fs::create_dir_all(root.join("data")).unwrap();
            symlink(String::from_utf8(python.stdout.clone()).unwrap().trim(), binaries.join("python3")).unwrap();
            std::fs::write(root.join("local/launcher.py"), include_str!("../../scripts/desktop/local_core.py")).unwrap();
            std::fs::write(package.join("__init__.py"), "").unwrap();
            std::fs::write(package.join("parent_watch.py"), "import os\ndef exit_when_parent_dies():\n    os.environ['PARENT_WATCH_STARTED'] = '1'\n").unwrap();
            std::fs::write(package.join("crash_report.py"), "import os\ndef install_exception_hooks():\n    os.environ['CRASH_HOOKS_INSTALLED'] = '1'\n").unwrap();
            std::fs::write(package.join("__main__.py"), r#"import argparse, os
from pathlib import Path
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--role', choices=['core', 'worker'], required=True)
    parser.add_argument('--host', required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--maintenance', action='store_true')
    args = parser.parse_args()
    assert args.host == '127.0.0.1'
    assert args.data_dir.is_absolute()
    assert os.environ['PARENT_WATCH_STARTED'] == '1'
    assert os.environ['APP_ROLE'] == ('worker' if args.role == 'worker' else 'api')
    assert os.environ['SUPYMEM_MODE'] == 'local'
    assert 'SUPYMEM_SINGLE_PROCESS' not in os.environ
    (args.data_dir / 'entrypoint').write_text('maintenance' if args.maintenance else args.role)
"#).unwrap();
            let stack = LocalStack::new(root.clone(), root.join("data"));
            for role in ["maintenance", "core", "worker"] {
                let mut command = stack.command(if role == "worker" { "worker" } else { "core" }, 3300);
                if role == "maintenance" { command.arg("--maintenance"); }
                assert!(command.status().unwrap().success());
                assert_eq!(std::fs::read_to_string(root.join("data/entrypoint")).unwrap(), role);
            }
            std::fs::remove_dir_all(root.parent().unwrap()).unwrap();
        }
    }

    #[test]
    #[cfg(unix)]
    fn local_directories_preserve_both_domains_and_reject_symlinks() {
        use std::os::unix::fs::{PermissionsExt, symlink};
        for domain in ["procurement.example", "gardening.example"] {
            let root = std::env::temp_dir().join(format!("local-private-{}", uuid::Uuid::new_v4()));
            let data = root.join(domain);
            std::fs::create_dir_all(&data).unwrap();
            std::fs::set_permissions(&data, std::fs::Permissions::from_mode(0o777)).unwrap();
            let saved = data.join("notes.txt");
            std::fs::write(&saved, domain).unwrap();
            private_directory(&data).unwrap();
            assert_eq!(std::fs::metadata(&data).unwrap().permissions().mode() & 0o777, 0o700);
            let alias = root.join("alias");
            symlink(&data, &alias).unwrap();
            assert!(private_directory(&alias).is_err());
            assert_eq!(std::fs::read_to_string(&saved).unwrap(), domain);
            std::fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    #[cfg(unix)]
    fn local_maintenance_timeout_preserves_saved_work() {
        for domain in ["procurement.example", "gardening.example"] {
            let root = std::env::temp_dir().join(format!("maintenance-{}", uuid::Uuid::new_v4())).join(domain);
            std::fs::create_dir_all(&root).unwrap();
            let saved = root.join("notes.txt");
            std::fs::write(&saved, domain).unwrap();
            let stack = LocalStack::new(root.clone(), root.clone());
            let mut command = Command::new("python3");
            command.args(["-c", "import time; time.sleep(300)"]);
            unix_new_session(&mut command);
            let mut child = command.spawn().unwrap();
            let error = stack.wait_for_maintenance(&mut child, Instant::now() + Duration::from_millis(50)).unwrap_err();
            assert!(error.contains("Retry"));
            assert!(child.try_wait().unwrap().is_some());
            assert_eq!(std::fs::read_to_string(saved).unwrap(), domain);
            std::fs::remove_dir_all(root.parent().unwrap()).unwrap();
        }
    }

    fn assert_port_released(port: u16) {
        let deadline = Instant::now() + Duration::from_secs(5);
        while TcpListener::bind((HOST, port)).is_err() {
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(20));
        }
    }

    #[test]
    fn core_restart_backoff_and_rolling_limit() {
        let start = Instant::now();
        let mut attempts = VecDeque::new();
        for delay in [0, 1, 2] {
            let now = start + Duration::from_secs(delay);
            assert_eq!(core_restart_delay(now, &attempts), Some(Duration::from_secs(delay)));
            attempts.push_back(now);
        }
        assert_eq!(core_restart_delay(start + Duration::from_secs(299), &attempts), None);
        assert_eq!(core_restart_delay(start + Duration::from_secs(300), &attempts), Some(Duration::from_secs(2)));
        assert_eq!(core_restart_delay(start + Duration::from_secs(302), &attempts), Some(Duration::ZERO));
    }

    #[test]
    #[cfg(unix)]
    fn local_stack_readiness_failure_and_owned_shutdown() {
        use std::os::unix::fs::{PermissionsExt, symlink};
        struct StartupLog(Mutex<Vec<(log::Level, String)>>);
        impl log::Log for StartupLog {
            fn enabled(&self, _: &log::Metadata) -> bool { true }
            fn log(&self, record: &log::Record) {
                self.0.lock().unwrap().push((record.level(), record.args().to_string()));
            }
            fn flush(&self) {}
        }
        static LOG: StartupLog = StartupLog(Mutex::new(Vec::new()));
        log::set_logger(&LOG).unwrap();
        log::set_max_level(log::LevelFilter::Info);
        for domain in ["procurement.example", "gardening.example"] {
            assert_eq!(data_dir(Some(domain.into())), std::env::current_dir().unwrap().join(domain));
            assert_eq!(data_dir(Some(PathBuf::new())), data_dir(None));
            let root = std::env::temp_dir().join(format!("local-stack-{}", uuid::Uuid::new_v4())).join(domain);
            let binaries = root.join("local/runtime").join(LocalStack::runtime_target()).join("bin");
            std::fs::create_dir_all(&binaries).unwrap();
            std::fs::create_dir_all(root.join("console/apps/console")).unwrap();
            std::fs::write(root.join("console/apps/console/server.js"), "").unwrap();
            let sidecar = binaries.join("python3");
            std::fs::write(&sidecar, r#"#!/usr/bin/env python3
import argparse, hmac, os, subprocess, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
parser = argparse.ArgumentParser()
parser.add_argument('--role')
parser.add_argument('--host')
parser.add_argument('--port', type=int)
parser.add_argument('--data-dir')
parser.add_argument('--maintenance', action='store_true')
args = parser.parse_args(sys.argv[sys.argv.index('--role'):]) if '--role' in sys.argv else None
assert 'SUPYMEM_STARTUP_PRIVATE_FIXTURE' not in os.environ
assert 'PYTHONPATH' not in os.environ and 'PYTHONHOME' not in os.environ
assert 'NODE_OPTIONS' not in os.environ and 'NODE_PATH' not in os.environ
assert 'SUPYMEM_SINGLE_PROCESS' not in os.environ
assert os.environ['SUPYMEM_MODE'] == 'local'
if args and args.maintenance:
    if os.path.exists(os.path.join(args.data_dir, 'fail-maintenance')):
        sys.exit(9)
    with open(os.path.join(args.data_dir, 'maintenance'), 'w') as handle:
        handle.write(os.environ['SUPYMEM_DESKTOP_INSTANCE_ID'])
    owner_path = os.path.join(args.data_dir, 'owner-session.secret')
    with open(owner_path, 'w') as handle:
        handle.write('a' * 64)
    os.chmod(owner_path, 0o600)
    sys.exit(0)
port = args.port if args else int(os.environ['PORT'])
role = args.role if args else 'console'
for name, url in [('CORE', 'CORE_URL'), ('CONSOLE', 'CONSOLE_ORIGIN')]:
    assert os.environ[url] == 'http://127.0.0.1:' + os.environ[name + '_PORT']
assert os.environ['GATEWAY_URL'] == os.environ['CONSOLE_ORIGIN'] == os.environ['CONSOLE_URL']
assert os.environ['WORKER_HEALTH_PORT'] == os.environ['WORKER_PORT']
assert port == int(os.environ[role.upper() + '_PORT'])
if role == 'console':
    assert 'SUPYMEM_STARTUP_PRIVATE_FIXTURE' not in os.environ
else:
    assert os.environ['APP_ROLE'] == ('worker' if role == 'worker' else 'api')
    assert os.environ['WARDEN_URL'].startswith('http://127.0.0.1:')
    assert os.path.isfile(os.path.join(args.data_dir, 'maintenance'))
time.sleep(0.8 if role == 'console' else 0.5)
if role == 'worker':
    data = os.environ['COMPANY_OS_DATA_DIR']
    assert os.path.isfile(os.path.join(data, 'core-ready'))
    while os.path.exists(os.path.join(data, 'hold-worker')):
        time.sleep(0.01)
if os.path.exists(os.path.join(os.environ['COMPANY_OS_DATA_DIR'], 'fail-' + role)):
    sys.exit(9)
if role == 'console':
    descendant = subprocess.Popen([sys.executable, '-c', "import socket, time; listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen(); print(listener.getsockname()[1], flush=True); time.sleep(300)"], stdout=subprocess.PIPE, text=True)
    with open(os.path.join(os.environ['COMPANY_OS_DATA_DIR'], 'descendant.port'), 'w') as handle:
        handle.write(descendant.stdout.readline())
print('ready', flush=True)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if 'X-Local-Proxy-Secret' in self.headers or not self.path.endswith('/local-ready'):
            self.send_response(403)
            self.end_headers()
            return
        nonce = self.headers['X-Local-Ready-Nonce']
        assert len(nonce) == 32
        slow_probe = os.path.join(os.environ['COMPANY_OS_DATA_DIR'], 'slow-probe-' + role)
        if os.path.exists(slow_probe):
            os.unlink(slow_probe)
            time.sleep(2)
        stalled = os.path.join(os.environ['COMPANY_OS_DATA_DIR'], 'stalled-' + role)
        if os.path.exists(stalled):
            os.unlink(stalled)
            time.sleep(300)
        not_ready = os.path.join(os.environ['COMPANY_OS_DATA_DIR'], 'not-ready-' + role)
        if os.path.exists(not_ready):
            os.unlink(not_ready)
            self.send_response(503)
            self.end_headers()
            return
        if role == 'core':
            with open(os.path.join(os.environ['COMPANY_OS_DATA_DIR'], 'core-ready'), 'w'):
                pass
        proof = hmac.digest(os.environ['SUPYMEM_LOCAL_READINESS_KEY'].encode(), nonce.encode(), 'sha256').hex()
        self.send_response(204)
        self.send_header('X-Local-Ready-Proof', proof)
        self.end_headers()
    def log_message(self, *args): pass
HTTPServer(('127.0.0.1', port), Handler).serve_forever()
"#).unwrap();
            std::fs::set_permissions(&sidecar, std::fs::Permissions::from_mode(0o700)).unwrap();
            symlink(&sidecar, binaries.join("node")).unwrap();
            let listeners = (0..ROLES.len()).map(|_| TcpListener::bind((HOST, 0)).unwrap()).collect::<Vec<_>>();
            let preferred = std::array::from_fn(|index| listeners[index].local_addr().unwrap().port());
            drop(listeners);
            let stack = LocalStack::with_ports(root.clone(), root.join("data"), preferred);
            assert_eq!(stack.ports, preferred);
            std::env::set_var("SUPYMEM_STARTUP_PRIVATE_FIXTURE", domain);
            let started = Instant::now();
            std::fs::create_dir_all(&stack.data_dir).unwrap();
            let hold_worker = stack.data_dir.join("hold-worker");
            std::fs::write(&hold_worker, "").unwrap();
            thread::scope(|scope| {
                let boot = scope.spawn(|| stack.start_with_console(|| {
                    let status = stack.status();
                    assert!(status[0].healthy && status[2].healthy && status[3].healthy);
                    assert!(status[1].owned && !status[1].healthy);
                    std::fs::remove_file(&hold_worker).unwrap();
                    Ok(())
                }));
                while !stack.status()[0].healthy {
                    assert!(started.elapsed() < Duration::from_secs(5));
                    thread::sleep(Duration::from_millis(10));
                }
                assert!(!stack.status()[2].healthy);
                assert!(stack.start().unwrap_err().contains("already starting"));
                boot.join().unwrap().unwrap();
            });
            println!("cold_start_fixture domain={domain} ms={}", started.elapsed().as_millis());
            assert!(started.elapsed() < Duration::from_millis(1500), "independent processes must overlap");
            assert_eq!(stack.status().len(), 4, "desktop owns core, worker, console and bridge");
            let bridge = stack.command("bridge", stack.ports[3]);
            let bridge_env = bridge.get_envs().collect::<HashMap<_, _>>();
            assert_eq!(bridge_env[std::ffi::OsStr::new("BRIDGE_NATIVE_TOKEN")], Some(std::ffi::OsStr::new(&stack.native_bridge_token)));
            assert_eq!(bridge_env[std::ffi::OsStr::new("BRIDGE_TOKEN")], Some(std::ffi::OsStr::new(&stack.bridge_token)));
            assert_ne!(stack.native_bridge_token, stack.bridge_token);
            assert_eq!(std::fs::read_to_string(root.join("data/maintenance")).unwrap(), stack.instance);
            assert_eq!(stack.url("gateway"), stack.url("console"));
            assert!(stack.status().iter().all(|role| role.owned && role.healthy));
            assert_eq!(std::fs::metadata(&stack.log_path).unwrap().permissions().mode() & 0o777, 0o600);
            {
                let before_probe = stack.status();
                std::fs::write(root.join("data/slow-probe-core"), "").unwrap();
                *stack.health_checked.lock().unwrap() = Instant::now() - Duration::from_secs(5);
                assert!(stack.failure().is_none(), "A temporary probe timeout must not stop your healthy app");
                thread::sleep(Duration::from_secs(2));
                assert!(stack.failure().is_none());
                assert_eq!(stack.status()[0].pid, before_probe[0].pid);
                assert!(stack.status().iter().all(|role| role.owned && role.healthy));
            }
            let descendant_port = std::fs::read_to_string(root.join("data/descendant.port")).unwrap().trim().parse().unwrap();
            let original = stack.status();
            let worker_pid = original[1].pid;
            {
                let mut children = stack.children.lock().unwrap();
                children.iter_mut().find(|(role, _)| *role == "worker").unwrap().1.kill().unwrap();
            }
            let deadline = Instant::now() + Duration::from_secs(5);
            while stack.status()[1].pid == worker_pid {
                assert!(stack.failure().is_none(), "Your worker should restart without stopping your app");
                assert!(Instant::now() < deadline);
                thread::sleep(Duration::from_millis(20));
            }
            let restarted = stack.status();
            assert!(restarted.iter().all(|role| role.owned && role.healthy));
            assert_eq!(restarted[0].pid, original[0].pid);
            assert_eq!(restarted[2].pid, original[2].pid);
            std::fs::write(root.join("data/stalled-worker"), "").unwrap();
            *stack.health_checked.lock().unwrap() = Instant::now() - Duration::from_secs(5);
            assert!(stack.failure().is_none());
            assert_ne!(stack.status()[1].pid, restarted[1].pid);
            assert!(TcpListener::bind((HOST, descendant_port)).is_err());
            stack.stop();
            assert_port_released(descendant_port);
            assert!(stack.status().iter().all(|role| !role.owned));
            assert!(stack.ports.iter().all(|port| TcpListener::bind((HOST, *port)).is_ok()));
            let crashed = LocalStack::with_ports(root.clone(), root.join("data"), stack.ports);
            crashed.start().unwrap();
            let descendant_port = std::fs::read_to_string(root.join("data/descendant.port")).unwrap().trim().parse().unwrap();
            crashed.children.lock().unwrap().iter_mut().find(|(role, _)| *role == "bridge").unwrap().1.kill().unwrap();
            let deadline = Instant::now() + Duration::from_secs(5);
            while crashed.failure().is_none() {
                assert!(Instant::now() < deadline);
                thread::sleep(Duration::from_millis(20));
            }
            assert!(crashed.status().iter().all(|role| !role.owned));
            assert_port_released(descendant_port);
            assert!(crashed.ports.iter().all(|port| TcpListener::bind((HOST, *port)).is_ok()));
            assert!(std::fs::read_to_string(&crashed.log_path).unwrap().contains("stopped"));
            let saved = root.join("data/saved.txt");
            std::fs::write(&saved, domain).unwrap();
            let recovering = LocalStack::with_ports(root.clone(), root.join("data"), stack.ports);
            recovering.start().unwrap();
            for (marker, elapsed) in [("stalled-core", 60), ("not-ready-core", 5)] {
                let original = recovering.status();
                std::fs::write(root.join("data").join(marker), "").unwrap();
                *recovering.health_checked.lock().unwrap() = Instant::now() - Duration::from_secs(elapsed);
                assert!(recovering.failure().is_none(), "Your saved work should recover without closing your app");
                let restarted = recovering.status();
                assert!(restarted.iter().all(|role| role.owned && role.healthy));
                assert_ne!(restarted[0].pid, original[0].pid);
                assert!(restarted[1..].iter().zip(&original[1..]).all(|(current, before)| current.pid == before.pid));
            }
            recovering.stop();
            assert_eq!(std::fs::read_to_string(&saved).unwrap(), domain);
            let rejected = LocalStack::with_ports(root.clone(), root.join("data"), stack.ports);
            rejected.start().unwrap();
            for marker in ["not-ready-core", "fail-core"] {
                std::fs::write(root.join("data").join(marker), "").unwrap();
            }
            *rejected.health_checked.lock().unwrap() = Instant::now() - Duration::from_secs(5);
            assert!(rejected.failure().unwrap().contains("Retry"));
            assert_eq!(rejected.core_restarts.lock().unwrap().len(), 3);
            assert!(rejected.status().iter().all(|role| !role.owned));
            std::fs::remove_file(root.join("data/fail-core")).unwrap();
            let invalid_log = LocalStack::new(root.clone(), root.join("data"));
            symlink(&saved, &invalid_log.log_path).unwrap();
            assert!(invalid_log.start().unwrap_err().contains("Retry"));
            assert_eq!(std::fs::read_to_string(&saved).unwrap(), domain);
            let missing = LocalStack::with_ports(root.join("missing"), root.join("data"), stack.ports);
            let message = missing.start().unwrap_err();
            assert!(message.contains("Retry"));
            assert_eq!(message, missing.last_error());
            assert!(missing.log_path.is_file());
            assert!(LOG.0.lock().unwrap().iter().any(|(level, message)|
                *level == log::Level::Info && message.contains("phase=local_failure reason=")
                    && message.contains("Your data could not be prepared")));
            assert!(missing.status().iter().all(|role| !role.owned));
            std::fs::write(root.join("data/fail-core"), "").unwrap();
            let partial = LocalStack::with_ports(root.clone(), root.join("data"), stack.ports);
            let message = partial.start().unwrap_err();
            assert!(message.contains("Retry"));
            assert_eq!(message, partial.last_error());
            assert!(partial.log_path.is_file());
            assert!(partial.status().iter().all(|role| !role.owned));
            assert!(partial.ports.iter().all(|port| TcpListener::bind((HOST, *port)).is_ok()));
            assert_eq!(std::fs::read_to_string(&saved).unwrap(), domain);
            std::fs::remove_file(root.join("data/fail-core")).unwrap();
            std::fs::write(root.join("data/fail-maintenance"), "").unwrap();
            assert!(partial.start().unwrap_err().contains("Retry"));
            assert!(partial.status().iter().all(|role| !role.owned));
            assert!(partial.ports.iter().all(|port| TcpListener::bind((HOST, *port)).is_ok()));
            assert_eq!(std::fs::read_to_string(&saved).unwrap(), domain);
            std::fs::remove_file(root.join("data/fail-maintenance")).unwrap();
            partial.start().unwrap();
            assert!(partial.status().iter().all(|role| role.healthy));
            partial.shutdown();
            assert!(partial.start().unwrap_err().contains("closing"));
            assert_eq!(std::fs::read_to_string(&saved).unwrap(), domain);
            for (role, _, _) in ROLES {
                let marker = root.join(format!("data/fail-{role}"));
                std::fs::write(&marker, "").unwrap();
                let failed = LocalStack::with_ports(root.clone(), root.join("data"), stack.ports);
                assert!(failed.start().unwrap_err().contains("Retry"));
                assert!(failed.status().iter().all(|role| !role.owned));
                assert!(failed.ports.iter().all(|port| TcpListener::bind((HOST, *port)).is_ok()));
                assert_eq!(std::fs::read_to_string(&saved).unwrap(), domain);
                std::fs::remove_file(marker).unwrap();
            }
            let other_apps = stack.ports.map(|port| TcpListener::bind((HOST, port)).unwrap());
            let occupied = LocalStack::with_ports(root.clone(), root.join("data"), stack.ports);
            for (index, listener) in other_apps.iter().enumerate() {
                assert_ne!(occupied.ports[index], listener.local_addr().unwrap().port());
            }
            occupied.start().unwrap();
            assert!(occupied.status().iter().all(|role| role.owned && role.healthy));
            assert_eq!(occupied.url("gateway"), occupied.url("console"));
            assert!(other_apps.iter().all(|listener| listener.local_addr().is_ok()));
            occupied.stop();
            let retry = occupied.error(&format!("Local port {} is unavailable: Address already in use", occupied.ports[2]));
            assert!(retry.contains("Quit and reopen your app to select another port"));
            drop(other_apps);
            let cancelled = LocalStack::with_ports(root.clone(), root.join("data"), stack.ports);
            thread::scope(|scope| {
                let boot = scope.spawn(|| cancelled.start());
                let deadline = Instant::now() + Duration::from_secs(5);
                while cancelled.children.lock().unwrap().is_empty() {
                    assert!(Instant::now() < deadline);
                    thread::sleep(Duration::from_millis(10));
                }
                cancelled.shutdown();
                assert!(boot.join().unwrap().is_err());
            });
            assert!(cancelled.ports.iter().all(|port| TcpListener::bind((HOST, *port)).is_ok()));
            assert_eq!(std::fs::read_to_string(&saved).unwrap(), domain);
            std::env::remove_var("SUPYMEM_STARTUP_PRIVATE_FIXTURE");
            std::fs::remove_dir_all(root.parent().unwrap()).unwrap();
        }
    }

    #[test]
    fn readiness_requires_a_proof_for_the_current_challenge() {
        use reqwest::{StatusCode, header::HeaderMap};

        for domain in ["cold-chain.test", "weekend-pottery.test"] {
            let key = crate::supervisor::random_bridge_secret();
            let nonce = uuid::Uuid::new_v4().simple().to_string();
            let mut headers = HeaderMap::new();
            assert!(!readiness_proven(StatusCode::NO_CONTENT, &headers, &key, &nonce));
            headers.insert("x-local-ready-proof", readiness_proof(&key, &nonce).parse().unwrap());
            assert!(readiness_proven(StatusCode::NO_CONTENT, &headers, &key, &nonce), "{domain}");
            assert!(!readiness_proven(StatusCode::OK, &headers, &key, &nonce));
            let other_nonce = uuid::Uuid::new_v4().simple().to_string();
            let other_key = crate::supervisor::random_bridge_secret();
            assert!(!readiness_proven(StatusCode::NO_CONTENT, &headers, &key, &other_nonce));
            assert!(!readiness_proven(StatusCode::NO_CONTENT, &headers, &other_key, &nonce));
        }
    }
}
