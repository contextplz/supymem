use base64::{engine::general_purpose::STANDARD, Engine};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Condvar, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

const SERVICE: &str = "Supymem Local";

#[derive(Serialize)]
struct Material {
    key: Option<String>,
    legacy: HashMap<String, String>,
    storage: &'static str,
    needs_reentry: bool,
}

#[derive(Clone)]
pub(crate) struct LocalCredentials(Arc<(Mutex<Option<Vec<u8>>>, Condvar)>);

impl LocalCredentials {
    pub(crate) fn start(root: PathBuf) -> Self {
        static RUNS: OnceLock<Mutex<HashMap<PathBuf, LocalCredentials>>> = OnceLock::new();
        let root = root.canonicalize().unwrap_or(root);
        let mut runs = RUNS.get_or_init(|| Mutex::new(HashMap::new())).lock().unwrap();
        if let Some(state) = runs.get(&root) { return state.clone(); }
        let state = Self(Arc::new((Mutex::new(None), Condvar::new())));
        runs.insert(root.clone(), state.clone());
        let worker = state.clone();
        thread::spawn(move || {
            let mut backend = NativeKeychain;
            let (material, migrated) = resolve(&root, keychain_allowed(), &mut backend);
            let payload = serde_json::to_vec(&material).expect("credential envelope");
            let (lock, ready) = &*worker.0;
            *lock.lock().unwrap() = Some(payload);
            ready.notify_all();
            if !migrated.is_empty() {
                let deadline = Instant::now() + Duration::from_secs(120);
                while Instant::now() < deadline {
                    let completed = read_private(&root.join("local-credentials-migrated.json"))
                        .ok().flatten().and_then(|body| serde_json::from_slice::<Vec<String>>(&body).ok()).unwrap_or_default();
                    if migrated.iter().all(|(_, account)| completed.contains(account)) {
                        remove_migrated(&mut backend, &migrated);
                        break;
                    }
                    thread::sleep(Duration::from_millis(250));
                }
            }
        });
        state
    }

    pub(crate) fn attach(&self, child: &mut Child) {
        let Some(mut input) = child.stdin.take() else { return; };
        let state = self.clone();
        thread::spawn(move || {
            let (lock, ready) = &*state.0;
            let mut material = lock.lock().unwrap();
            while material.is_none() { material = ready.wait(material).unwrap(); }
            let _ = input.write_all(material.as_ref().unwrap());
            let _ = input.write_all(b"\n");
        });
    }
}

trait Keychain {
    fn get(&mut self, service: &str, account: &str) -> Result<Option<String>, ()>;
    fn set(&mut self, service: &str, account: &str, value: &str) -> Result<(), ()>;
    fn delete(&mut self, service: &str, account: &str) -> Result<(), ()>;
}

fn remove_migrated(backend: &mut impl Keychain, migrated: &[(String, String)]) {
    for (service, account) in migrated {
        if backend.delete(service, account).is_err() { break; }
    }
}

fn digest(path: &Path) -> String {
    format!("{:x}", Sha256::digest(key_path(path).as_bytes()))
}

fn key_path(path: &Path) -> String {
    let path = path.canonicalize().unwrap_or_else(|_| path.to_owned());
    let value = path.to_string_lossy().into_owned();
    #[cfg(windows)]
    {
        if let Some(network) = value.strip_prefix("\\\\?\\UNC\\") { return format!("\\\\{network}"); }
        return value.strip_prefix("\\\\?\\").unwrap_or(&value).to_owned();
    }
    #[cfg(not(windows))]
    { value }
}

fn legacy_items(root: &Path) -> Vec<(String, String)> {
    let providers = root.join("vault/providers");
    let scope = read_private(&providers.join("keychain.scope")).ok().flatten()
        .and_then(|body| String::from_utf8(body).ok()).unwrap_or_else(|| digest(&providers));
    let mut items = Vec::new();
    if scope.len() == 64 && scope.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        if let Some(document) = read_private(&providers.join("settings.json")).ok().flatten()
            .and_then(|body| serde_json::from_slice::<serde_json::Value>(&body).ok()) {
            if let Some(entries) = document.get("providers").and_then(|value| value.as_object()) {
                for entry in entries.values() {
                    if let Some(account) = entry.get("key_ref").and_then(|value| value.as_str()) {
                        if uuid::Uuid::parse_str(account).is_ok() {
                            items.push((format!("com.supymem.local.provider:{scope}"), account.to_string()));
                        }
                    }
                }
            }
        }
    }
    if read_private(&root.join("local-key.keychain")).ok().flatten().is_some_and(|marker| marker != b"shell") {
        items.insert(0, (format!("com.supymem.local.provider:{}", digest(root)), "settings-wrapping-key".into()));
    }
    #[cfg(windows)]
    for (path, account, encrypted) in [
        (root.join("local-key.bin"), "settings-wrapping-key", root.join("local-settings.json")),
        (providers.join("machine.secret"), "machine.secret", providers.join("settings.enc")),
        (root.join("vault/master.key"), "vault-master-key", root.join("vault/secrets.bin")),
    ] {
        let reference = read_private(&path).ok().flatten()
            .or_else(|| (account == "settings-wrapping-key").then(|| read_private(&root.join("local-key.keychain")).ok().flatten()).flatten())
            .and_then(|body| String::from_utf8(body).ok())
            .filter(|value| value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit()));
        if encrypted.exists() && (!path.exists() || reference.is_some()) && !(account == "settings-wrapping-key"
            && read_private(&root.join("local-key.keychain")).ok().flatten().is_some_and(|marker| marker == b"shell")) {
            let identity = key_path(&path).replace('/', "\\").to_lowercase();
            let reference = reference.unwrap_or_else(|| format!("{:x}", Sha256::digest(identity.as_bytes())));
            items.push((format!("com.supymem.local/key/{reference}"), account.into()));
        }
    }
    items
}

fn resolve(root: &Path, allowed: bool, backend: &mut impl Keychain) -> (Material, Vec<(String, String)>) {
    let items = legacy_items(root);
    let mut material = Material { key: None, legacy: HashMap::new(), storage: "keyfile", needs_reentry: !items.is_empty()
        || root.join("local-key.keychain").exists() && !root.join("local-key.bin").exists() };
    if !allowed { return (material, Vec::new()); }
    let account = digest(root);
    let current = match backend.get(SERVICE, &account) {
        Ok(value) => value,
        Err(()) => return (material, Vec::new()),
    };
    let existing = current.is_some();
    let mut completed = Vec::new();
    let mut denied = false;
    for (service, account) in items {
        let mut source = service.clone();
        let value = match backend.get(&service, &account) {
            Ok(Some(value)) => Some(value),
            Ok(None) if service.starts_with("com.supymem.local.provider:") => {
                source = service.replace("com.supymem.local.provider:", "local-model-provider:");
                match backend.get(&source, &account) {
                    Ok(value) => value,
                    Err(()) => { denied = true; break; },
                }
            },
            Ok(None) => None,
            Err(()) => { denied = true; break; },
        };
        if let Some(value) = value {
            material.legacy.insert(account.clone(), value);
            completed.push((source, account));
        }
    }
    let file = match read_private(&root.join("local-key.bin")) {
        Ok(value) => value,
        Err(_) => return (material, Vec::new()),
    };
    #[cfg(windows)]
    let file = file.filter(|value| value.len() != 64 || !value.iter().all(u8::is_ascii_hexdigit));
    let encoded = current.or_else(|| material.legacy.get("settings-wrapping-key").cloned())
        .or_else(|| file.as_ref().map(|value| STANDARD.encode(value)));
    let encoded = match encoded {
        Some(value) if STANDARD.decode(&value).is_ok_and(|key| key.len() == 32) => value,
        Some(_) => return (material, Vec::new()),
        None if denied || root.join("local-key.keychain").exists() => return (material, Vec::new()),
        None => match random_key() { Ok(key) => STANDARD.encode(key), Err(_) => return (material, Vec::new()) },
    };
    if file.as_ref().is_some_and(|key| STANDARD.encode(key) != encoded) { return (material, Vec::new()); }
    material.key = Some(encoded.clone());
    if !denied {
        if existing || backend.set(SERVICE, &account, &encoded).is_ok()
            && backend.get(SERVICE, &account).is_ok_and(|value| value.as_deref() == Some(&encoded)) {
            material.storage = "keychain";
        } else {
            denied = true;
        }
    }
    material.needs_reentry = legacy_items(root).len() != completed.len();
    (material, if denied { Vec::new() } else { completed })
}

fn read_private(path: &Path) -> std::io::Result<Option<Vec<u8>>> {
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    { use std::os::unix::fs::OpenOptionsExt; options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK); }
    #[cfg(windows)]
    { use std::os::windows::fs::OpenOptionsExt; options.custom_flags(0x00200000); }
    let file = match options.open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let metadata = file.metadata()?;
    let mut private = metadata.is_file();
    #[cfg(windows)]
    { private &= private_windows(&file); }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        private &= metadata.uid() == unsafe { libc::geteuid() } && metadata.mode() & 0o077 == 0 && metadata.nlink() == 1;
    }
    if !private { return Err(std::io::Error::new(std::io::ErrorKind::PermissionDenied, "Private credential file required")); }
    let mut body = Vec::new();
    file.take(1_048_577).read_to_end(&mut body)?;
    if body.len() > 1_048_576 { return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "Credential metadata too large")); }
    Ok(Some(body))
}

#[cfg(windows)]
pub(crate) fn private_windows(file: &std::fs::File) -> bool {
    use std::ffi::c_void;
    use std::os::windows::io::AsRawHandle;
    type Handle = *mut c_void;
    #[link(name = "advapi32")]
    extern "system" {
        fn GetSecurityInfo(handle: Handle, kind: u32, information: u32, owner: *mut Handle, group: *mut Handle, acl: *mut Handle, sacl: *mut Handle, descriptor: *mut Handle) -> u32;
        fn OpenProcessToken(process: Handle, access: u32, token: *mut Handle) -> i32;
        fn GetTokenInformation(token: Handle, kind: u32, buffer: Handle, size: u32, needed: *mut u32) -> i32;
        fn EqualSid(first: Handle, second: Handle) -> i32;
        fn GetAce(acl: Handle, index: u32, ace: *mut Handle) -> i32;
        fn GetSecurityDescriptorControl(descriptor: Handle, control: *mut u16, revision: *mut u32) -> i32;
    }
    #[link(name = "kernel32")]
    extern "system" {
        fn GetCurrentProcess() -> Handle;
        fn CloseHandle(handle: Handle) -> i32;
        fn LocalFree(memory: Handle) -> Handle;
        fn GetFileInformationByHandle(handle: Handle, information: *mut u32) -> i32;
    }
    unsafe {
        let mut details = [0u32; 13];
        if GetFileInformationByHandle(file.as_raw_handle(), details.as_mut_ptr()) == 0 || details[0] & 0x400 != 0 || details[10] != 1 { return false; }
        let mut owner = std::ptr::null_mut();
        let mut acl = std::ptr::null_mut();
        let mut descriptor = std::ptr::null_mut();
        if GetSecurityInfo(file.as_raw_handle(), 1, 5, &mut owner, std::ptr::null_mut(), &mut acl, std::ptr::null_mut(), &mut descriptor) != 0 { return false; }
        let mut token = std::ptr::null_mut();
        let private = (|| {
            if OpenProcessToken(GetCurrentProcess(), 8, &mut token) == 0 { return false; }
            let mut needed = 0;
            GetTokenInformation(token, 1, std::ptr::null_mut(), 0, &mut needed);
            if needed == 0 { return false; }
            let mut buffer = vec![0u8; needed as usize];
            if GetTokenInformation(token, 1, buffer.as_mut_ptr().cast(), needed, &mut needed) == 0 { return false; }
            let sid = std::ptr::read_unaligned(buffer.as_ptr().cast::<Handle>());
            let mut control = 0;
            let mut revision = 0;
            if owner.is_null() || acl.is_null() || EqualSid(owner, sid) == 0
                || GetSecurityDescriptorControl(descriptor, &mut control, &mut revision) == 0 || control & 0x1000 == 0
                || std::ptr::read_unaligned(acl.cast::<u8>().add(4).cast::<u16>()) != 1 { return false; }
            let mut ace = std::ptr::null_mut();
            if GetAce(acl, 0, &mut ace) == 0 || ace.is_null() { return false; }
            let header = ace.cast::<u8>();
            *header == 0 && *header.add(1) == 0 && EqualSid(header.add(8).cast_mut().cast(), sid) != 0
        })();
        if !token.is_null() { CloseHandle(token); }
        LocalFree(descriptor);
        private
    }
}

fn keychain_allowed() -> bool {
    if cfg!(debug_assertions) { return false; }
    #[cfg(target_os = "macos")]
    {
        let Ok(executable) = std::env::current_exe() else { return false; };
        let Ok(signature) = Command::new("/usr/bin/codesign").args(["-dv", "--verbose=4"]).arg(executable).output() else { return false; };
        let detail = String::from_utf8_lossy(&signature.stderr);
        if !developer_id_signature(&detail, signature.status.success()) { return false; }
        unsafe { SecKeychainSetUserInteractionAllowed(0) == 0 }
    }
    #[cfg(not(target_os = "macos"))]
    { true }
}

#[cfg(any(target_os = "macos", test))]
fn developer_id_signature(detail: &str, success: bool) -> bool {
    success && !detail.contains("adhoc") && detail.contains("Authority=Developer ID Application:")
        && detail.lines().any(|line| line.starts_with("TeamIdentifier=") && line != "TeamIdentifier=not set" && line != "TeamIdentifier=")
}

#[cfg(target_os = "macos")]
#[link(name = "Security", kind = "framework")]
extern "C" { fn SecKeychainSetUserInteractionAllowed(allowed: u8) -> i32; }

fn random_key() -> std::io::Result<[u8; 32]> {
    let mut key = [0; 32];
    #[cfg(unix)]
    std::fs::File::open("/dev/urandom")?.read_exact(&mut key)?;
    #[cfg(windows)]
    {
        #[link(name = "bcrypt")]
        extern "system" { fn BCryptGenRandom(algorithm: *mut std::ffi::c_void, buffer: *mut u8, size: u32, flags: u32) -> i32; }
        if unsafe { BCryptGenRandom(std::ptr::null_mut(), key.as_mut_ptr(), 32, 2) } < 0 { return Err(std::io::Error::last_os_error()); }
    }
    Ok(key)
}

struct NativeKeychain;

#[cfg(any(target_os = "macos", target_os = "windows"))]
fn entry(service: &str, account: &str) -> Result<keyring::Entry, ()> {
    #[cfg(windows)]
    {
        if service == SERVICE {
            return keyring::Entry::new_with_target(&format!("{account}@{service}"), service, account).map_err(|_| ());
        }
        if service.starts_with("com.supymem.local/key/") {
            return keyring::Entry::new_with_target(service, service, account).map_err(|_| ());
        }
        if service.starts_with("com.supymem.local.provider:") || service.starts_with("local-model-provider:") {
            return keyring::Entry::new_with_target(&format!("{account}@{service}"), service, account).map_err(|_| ());
        }
    }
    keyring::Entry::new(service, account).map_err(|_| ())
}

#[cfg(any(target_os = "macos", target_os = "windows"))]
impl Keychain for NativeKeychain {
    fn get(&mut self, service: &str, account: &str) -> Result<Option<String>, ()> {
        let entry = entry(service, account)?;
        #[cfg(windows)]
        if service.starts_with("com.supymem.local/key/") || service.starts_with("com.supymem.local.provider:") || service.starts_with("local-model-provider:") {
            return match entry.get_secret() {
                Ok(value) if service.starts_with("com.supymem.local/key/") => Ok(Some(STANDARD.encode(value))),
                Ok(value) if value.len() % 2 == 0 => String::from_utf16(&value.chunks_exact(2).map(|bytes| u16::from_le_bytes([bytes[0], bytes[1]])).collect::<Vec<_>>()).map(Some).map_err(|_| ()),
                Ok(_) => Err(()),
                Err(keyring::Error::NoEntry) => Ok(None),
                Err(_) => Err(()),
            };
        }
        match entry.get_password() {
            Ok(value) => Ok(Some(value)),
            Err(keyring::Error::NoEntry) => {
                #[cfg(windows)]
                if service == SERVICE {
                    return match keyring::Entry::new(service, account).map_err(|_| ())?.get_password() {
                        Ok(value) => Ok(Some(value)),
                        Err(keyring::Error::NoEntry) => Ok(None),
                        Err(_) => Err(()),
                    };
                }
                Ok(None)
            },
            Err(_) => Err(()),
        }
    }
    fn set(&mut self, service: &str, account: &str, value: &str) -> Result<(), ()> {
        entry(service, account)?.set_password(value).map_err(|_| ())
    }
    fn delete(&mut self, service: &str, account: &str) -> Result<(), ()> {
        entry(service, account)?.delete_credential().map_err(|_| ())
    }
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
impl Keychain for NativeKeychain {
    fn get(&mut self, service: &str, account: &str) -> Result<Option<String>, ()> {
        let value = secret_tool(&["lookup", "service", service, "username", account], None)?;
        Ok(if value.is_empty() { None } else { Some(value) })
    }
    fn set(&mut self, service: &str, account: &str, value: &str) -> Result<(), ()> {
        secret_tool(&["store", "--label=Supymem Local", "service", service, "username", account], Some(value)).map(|_| ())
    }
    fn delete(&mut self, service: &str, account: &str) -> Result<(), ()> {
        secret_tool(&["clear", "service", service, "username", account], None).map(|_| ())
    }
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
fn secret_tool(arguments: &[&str], input: Option<&str>) -> Result<String, ()> {
    // shortcut: Linux keychain access uses the installed Secret Service helper, keep the key-file fallback on machines without it.
    let mut child = Command::new("secret-tool").args(arguments).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().map_err(|_| ())?;
    if let Some(mut pipe) = child.stdin.take() {
        if let Some(value) = input { pipe.write_all(value.as_bytes()).map_err(|_| ())?; }
    }
    let deadline = Instant::now() + Duration::from_secs(2);
    while child.try_wait().map_err(|_| ())?.is_none() {
        if Instant::now() >= deadline { let _ = child.kill(); let _ = child.wait(); return Err(()); }
        thread::sleep(Duration::from_millis(10));
    }
    let output = child.wait_with_output().map_err(|_| ())?;
    if output.status.success() { String::from_utf8(output.stdout).map(|value| value.trim_end_matches('\n').to_string()).map_err(|_| ()) }
    else if arguments[0] == "lookup" && output.status.code() == Some(1) && output.stderr.is_empty() { Ok(String::new()) }
    else { Err(()) }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[derive(Default)]
    struct FakeKeychain { values: HashMap<(String, String), String>, reads: usize, deny_after: Option<usize>, deny_writes: bool }
    impl Keychain for FakeKeychain {
        fn get(&mut self, service: &str, account: &str) -> Result<Option<String>, ()> {
            self.reads += 1;
            if self.deny_after.is_some_and(|limit| self.reads > limit) { return Err(()); }
            Ok(self.values.get(&(service.into(), account.into())).cloned())
        }
        fn set(&mut self, service: &str, account: &str, value: &str) -> Result<(), ()> {
            if self.deny_writes { return Err(()); }
            self.values.insert((service.into(), account.into()), value.into()); Ok(())
        }
        fn delete(&mut self, service: &str, account: &str) -> Result<(), ()> { self.values.remove(&(service.into(), account.into())); Ok(()) }
    }
    #[test]
    fn migration_and_denial_read_each_legacy_item_once_and_never_retry() {
        for domain in ["inventory.example", "cycling.example"] {
            for count in [0, 1, 3] {
                let root = std::env::temp_dir().join(format!("{domain}-{}", uuid::Uuid::new_v4()));
                let providers = root.join("vault/providers");
                std::fs::create_dir_all(&providers).unwrap();
                let mut backend = FakeKeychain::default();
                let mut entries = serde_json::Map::new();
                for index in 0..count {
                    let account = uuid::Uuid::new_v4().to_string();
                    entries.insert(index.to_string(), serde_json::json!({"key_ref": account}));
                    backend.values.insert((format!("com.supymem.local.provider:{}", digest(&providers)), account), format!("{domain}-{index}"));
                }
                crate::native_security::write_private(&providers.join("settings.json"), &serde_json::to_vec(&serde_json::json!({"providers": entries})).unwrap()).unwrap();
                let (material, completed) = resolve(&root, true, &mut backend);
                assert_eq!(material.legacy.len(), count);
                assert_eq!(completed.len(), count);
                assert_eq!(backend.reads, count + 2);
                remove_migrated(&mut backend, &completed);
                assert_eq!(backend.values.len(), 1);
                backend.reads = 0;
                backend.deny_after = Some(0);
                let (fallback, completed) = resolve(&root, true, &mut backend);
                assert_eq!(fallback.storage, "keyfile");
                assert_eq!(backend.reads, 1);
                assert!(completed.is_empty());
                backend.reads = 0;
                let (fallback, _) = resolve(&root, false, &mut backend);
                assert_eq!(fallback.storage, "keyfile");
                assert_eq!(backend.reads, 0);
                if count > 0 {
                    backend.reads = 0;
                    backend.deny_after = Some(1);
                    let (fallback, completed) = resolve(&root, true, &mut backend);
                    assert_eq!(backend.reads, 2);
                    assert_eq!(fallback.storage, "keyfile");
                    assert!(completed.is_empty());
                }
                std::fs::remove_dir_all(root).unwrap();
            }
        }
    }
    #[test]
    fn unavailable_keychain_keeps_private_file_fallback_without_retrying() {
        for domain in ["inventory.example", "cycling.example"] {
            for existing in [false, true] {
                let root = std::env::temp_dir().join(format!("{domain}-{}", uuid::Uuid::new_v4()));
                std::fs::create_dir(&root).unwrap();
                let path = root.join("local-key.bin");
                let key = random_key().unwrap();
                if existing { crate::native_security::write_private(&path, &key).unwrap(); }
                let mut backend = FakeKeychain { deny_after: Some(0), ..Default::default() };
                let (material, completed) = resolve(&root, true, &mut backend);
                assert_eq!(material.storage, "keyfile");
                assert!(material.key.is_none());
                assert!(!material.needs_reentry);
                assert!(completed.is_empty());
                assert_eq!(backend.reads, 1);
                assert!(backend.values.is_empty());
                assert_eq!(read_private(&path).unwrap(), existing.then(|| key.to_vec()));
                std::fs::remove_dir_all(root).unwrap();
            }
        }
    }
    #[test]
    fn all_children_share_one_resolution_even_when_it_falls_back() {
        for domain in ["inventory.example", "cycling.example"] {
            let root = std::env::temp_dir().join(format!("{domain}-{}", uuid::Uuid::new_v4()));
            std::fs::create_dir(&root).unwrap();
            let first = LocalCredentials::start(root.clone());
            let second = LocalCredentials::start(root.clone());
            assert!(Arc::ptr_eq(&first.0, &second.0));
            std::fs::remove_dir(root).unwrap();
        }
    }
    #[test]
    fn store_denial_keeps_copied_values_without_another_keychain_operation() {
        for domain in ["inventory.example", "cycling.example"] {
            let root = std::env::temp_dir().join(format!("{domain}-{}", uuid::Uuid::new_v4()));
            let providers = root.join("vault/providers");
            std::fs::create_dir_all(&providers).unwrap();
            let account = uuid::Uuid::new_v4().to_string();
            let mut backend = FakeKeychain { deny_writes: true, ..Default::default() };
            backend.values.insert((format!("com.supymem.local.provider:{}", digest(&providers)), account.clone()), domain.into());
            crate::native_security::write_private(&providers.join("settings.json"), &serde_json::to_vec(&serde_json::json!({"providers": {"provider": {"key_ref": account}}})).unwrap()).unwrap();
            let (material, completed) = resolve(&root, true, &mut backend);
            assert_eq!(material.storage, "keyfile");
            assert_eq!(material.legacy.get(&account).unwrap(), domain);
            assert!(!material.needs_reentry);
            assert_eq!(backend.reads, 2);
            assert!(completed.is_empty());
            assert_eq!(backend.values.len(), 1);
            std::fs::remove_dir_all(root).unwrap();
        }
    }
    #[test]
    fn debug_adhoc_and_unsigned_builds_never_attempt_keychain_access() {
        assert!(!keychain_allowed());
        for domain in ["inventory.example", "cycling.example"] {
            for detail in [String::new(), "flags=0x2(adhoc)\nTeamIdentifier=not set".into(), format!("Authority=Developer ID Application: {domain}\nTeamIdentifier=not set")] {
                assert!(!developer_id_signature(&detail, true));
            }
            assert!(developer_id_signature(&format!("Authority=Developer ID Application: {domain}\nTeamIdentifier=fixture"), true));
        }
    }
    #[test]
    fn signed_restarts_read_only_the_shell_key_without_a_false_migration_notice() {
        for domain in ["inventory.example", "cycling.example"] {
            let root = std::env::temp_dir().join(format!("{domain}-{}", uuid::Uuid::new_v4()));
            std::fs::create_dir(&root).unwrap();
            crate::native_security::write_private(&root.join("local-key.keychain"), b"shell").unwrap();
            crate::native_security::write_private(&root.join("local-key.bin"), &[7; 32]).unwrap();
            let mut backend = FakeKeychain::default();
            backend.values.insert((SERVICE.into(), digest(&root)), STANDARD.encode([7; 32]));
            let (material, completed) = resolve(&root, true, &mut backend);
            assert_eq!(material.storage, "keychain");
            assert!(!material.needs_reentry);
            assert!(completed.is_empty());
            assert_eq!(backend.reads, 1);
            assert_eq!(read_private(&root.join("local-key.bin")).unwrap().unwrap(), [7; 32]);
            std::fs::remove_file(root.join("local-key.bin")).unwrap();
            backend.reads = 0;
            backend.deny_after = Some(0);
            let (fallback, completed) = resolve(&root, true, &mut backend);
            assert_eq!(fallback.storage, "keyfile");
            assert!(fallback.needs_reentry);
            assert!(completed.is_empty());
            assert_eq!(backend.reads, 1);
            assert_eq!(backend.values.len(), 1);
            std::fs::remove_dir_all(root).unwrap();
        }
    }
}
