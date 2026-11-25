//! Trust boundaries shared by native IPC and navigation. Tested without a GUI.
use url::Url;

pub(crate) fn console_origin_allowed(url: &Url, development: bool, staging: bool) -> bool {
    if !url.username().is_empty() || url.password().is_some() { return false; }
    (url.scheme() == "https" && url.port_or_known_default() == Some(443)
        && url.host_str() == Some(if staging { "app.staging.example.invalid" } else { "app.example.invalid" }))
        || (development && url.scheme() == "http"
            && matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "[::1]")))
}

pub(crate) fn bundled(url: &Url) -> bool {
    url.username().is_empty() && url.password().is_none() && url.port().is_none()
        && ((url.scheme() == "tauri" && url.host_str() == Some("localhost"))
            || (!cfg!(target_os = "macos") && url.scheme() == "http" && url.host_str() == Some("tauri.localhost")))
}

pub(crate) fn trusted_window(label: &str, location: &Url, console: &str,
                            development: bool, staging: bool, auxiliary: bool) -> bool {
    if !location.username().is_empty() || location.password().is_some() { return false; }
    if label == "main" {
        return bundled(location) || (matches!(location.scheme(), "http" | "https") && Url::parse(console).is_ok_and(|expected|
            console_origin_allowed(&expected, development, staging) && location.origin() == expected.origin()));
    }
    auxiliary && bundled(location) && matches!((label, location.path()),
        ("setup", "/setup.html") | ("coach", "/coach.html"))
}

#[cfg(unix)]
pub(crate) fn write_private(path: &std::path::Path, body: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let temp = path.with_file_name(format!(".supymem-{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| {
        let mut file = std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(&temp)?;
        file.write_all(body)?;
        file.sync_all()?;
        std::fs::rename(&temp, path)
    })();
    if result.is_err() { let _ = std::fs::remove_file(&temp); }
    result
}

#[cfg(windows)]
pub(crate) fn write_private(path: &std::path::Path, body: &[u8]) -> std::io::Result<()> {
    use std::ffi::c_void;
    use std::io::Write;
    use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
    use std::os::windows::ffi::OsStrExt;
    use std::os::windows::io::FromRawHandle;
    type Handle = *mut c_void;
    #[repr(C)]
    struct SecurityAttributes { size: u32, descriptor: Handle, inherit: i32 }
    #[link(name = "advapi32")]
    extern "system" {
        fn OpenProcessToken(process: Handle, access: u32, token: *mut Handle) -> i32;
        fn GetTokenInformation(token: Handle, kind: u32, buffer: Handle, size: u32, needed: *mut u32) -> i32;
        fn ConvertSidToStringSidW(sid: Handle, text: *mut *mut u16) -> i32;
        fn ConvertStringSecurityDescriptorToSecurityDescriptorW(text: *const u16, revision: u32, descriptor: *mut Handle, size: *mut u32) -> i32;
    }
    #[link(name = "kernel32")]
    extern "system" {
        fn GetCurrentProcess() -> Handle;
        fn CloseHandle(handle: Handle) -> i32;
        fn LocalFree(memory: Handle) -> Handle;
        fn CreateFileW(path: *const u16, access: u32, share: u32, security: *const SecurityAttributes, disposition: u32, flags: u32, template: Handle) -> Handle;
        fn MoveFileExW(source: *const u16, destination: *const u16, flags: u32) -> i32;
    }
    let path = if path.is_absolute() { path.to_path_buf() } else { std::env::current_dir()?.join(path) };
    let mut directories = Vec::new();
    for parent in path.parent().ok_or(std::io::ErrorKind::InvalidInput)?.ancestors().collect::<Vec<_>>().into_iter().rev() {
        let directory = std::fs::OpenOptions::new().read(true).share_mode(3).custom_flags(0x00200000 | 0x02000000).open(parent)
            .map_err(|error| std::io::Error::new(error.kind(), "Open Keys and connections, check your data-folder permissions and choose a folder without links, then retry."))?;
        let metadata = directory.metadata()?;
        if !metadata.is_dir() || metadata.file_attributes() & 0x400 != 0 {
            return Err(std::io::Error::new(std::io::ErrorKind::PermissionDenied, "Choose a folder without links in Keys and connections, then retry."));
        }
        directories.push(directory);
    }
    let temp = path.with_file_name(format!(".supymem-{}.tmp", uuid::Uuid::new_v4()));
    let mut token = std::ptr::null_mut();
    let mut sid_text = std::ptr::null_mut();
    let mut descriptor = std::ptr::null_mut();
    let result = (|| unsafe {
        if OpenProcessToken(GetCurrentProcess(), 8, &mut token) == 0 { return Err(std::io::Error::last_os_error()); }
        let mut needed = 0;
        GetTokenInformation(token, 1, std::ptr::null_mut(), 0, &mut needed);
        let mut buffer = vec![0u8; needed as usize];
        if GetTokenInformation(token, 1, buffer.as_mut_ptr().cast(), needed, &mut needed) == 0 { return Err(std::io::Error::last_os_error()); }
        let sid = std::ptr::read_unaligned(buffer.as_ptr().cast::<Handle>());
        if ConvertSidToStringSidW(sid, &mut sid_text) == 0 { return Err(std::io::Error::last_os_error()); }
        let mut length = 0;
        while *sid_text.add(length) != 0 { length += 1; }
        let sid = String::from_utf16_lossy(std::slice::from_raw_parts(sid_text, length));
        let sddl = format!("O:{sid}D:P(A;;FA;;;{sid})\0").encode_utf16().collect::<Vec<_>>();
        if ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.as_ptr(), 1, &mut descriptor, std::ptr::null_mut()) == 0 { return Err(std::io::Error::last_os_error()); }
        let attributes = SecurityAttributes { size: std::mem::size_of::<SecurityAttributes>() as u32, descriptor, inherit: 0 };
        let source = temp.as_os_str().encode_wide().chain(Some(0)).collect::<Vec<_>>();
        let destination = path.as_os_str().encode_wide().chain(Some(0)).collect::<Vec<_>>();
        let handle = CreateFileW(source.as_ptr(), 0xC0000000, 0, &attributes, 1, 0x00200080, std::ptr::null_mut());
        if handle as isize == -1 { return Err(std::io::Error::last_os_error()); }
        let mut file = std::fs::File::from_raw_handle(handle);
        if !crate::local_credentials::private_windows(&file) { return Err(std::io::Error::new(std::io::ErrorKind::PermissionDenied, "Open Keys and connections, choose a private folder you own, then retry.")); }
        file.write_all(body)?;
        file.sync_all()?;
        drop(file);
        if MoveFileExW(source.as_ptr(), destination.as_ptr(), 9) == 0 { return Err(std::io::Error::last_os_error()); }
        Ok(())
    })();
    unsafe {
        if !token.is_null() { CloseHandle(token); }
        if !sid_text.is_null() { LocalFree(sid_text.cast()); }
        if !descriptor.is_null() { LocalFree(descriptor); }
    }
    if result.is_err() { let _ = std::fs::remove_file(&temp); }
    result.map_err(|error| std::io::Error::new(error.kind(), "Your settings could not be saved. Open Keys and connections, check your private data-folder permissions and free disk space, then retry."))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn url(raw: &str) -> Url { raw.parse().unwrap() }
    #[test]
    fn production_denies_localhost_and_arbitrary_configured_origins() {
        for raw in ["http://localhost:3100", "http://127.0.0.1:3100", "https://evil.test", "https://app.example.invalid:444", "https://app.example.invalid.evil.test", "https://user@app.example.invalid", "http://tauri.localhost:4242", "tauri://localhost:4242/setup.html", "blob:https://app.example.invalid/owned-preview"] {
            assert!(!trusted_window("main", &url(raw), raw, false, false, true), "{raw}");
        }
        assert!(trusted_window("main", &url("https://app.example.invalid/v/a"), "https://app.example.invalid", false, false, false));
        assert!(!trusted_window("main", &url("https://app.staging.example.invalid"), "https://app.staging.example.invalid", false, false, false));
        for raw in ["https://venture-preview.example.invalid", "http://127.0.0.1:3001", "about:srcdoc", "blob:https://app.example.invalid/preview"] {
            assert!(!trusted_window("main", &url(raw), "https://app.example.invalid", false, false, true));
        }
    }
    #[test]
    fn development_is_exact_origin_and_auxiliary_is_bundled_only() {
        assert!(trusted_window("main", &url("http://127.0.0.1:3100/v/a"), "http://127.0.0.1:3100", true, false, false));
        assert!(!trusted_window("main", &url("http://127.0.0.1:9999"), "http://127.0.0.1:3100", true, false, true));
        assert!(trusted_window("main", &url("https://app.staging.example.invalid"), "https://app.staging.example.invalid", false, true, false));
        assert!(trusted_window("setup", &url("tauri://localhost/setup.html"), "", false, false, true));
        assert!(!trusted_window("setup", &url("https://app.example.invalid/setup.html"), "https://app.example.invalid", false, false, true));
        assert!(!trusted_window("coach", &url("tauri://localhost/setup.html"), "", false, false, true));
        assert!(!trusted_window("setup", &url("tauri://localhost/setup.html"), "", false, false, false));
        assert!(!trusted_window("main", &url("data:text/html,hello"), "", true, false, true));
    }
    #[test]
    fn bundled_origin_has_no_network_port_or_macos_http_alias() {
        assert!(!bundled(&url("http://tauri.localhost:4242/setup.html")));
        assert!(!bundled(&url("tauri://localhost:4242/setup.html")));
        assert_eq!(bundled(&url("http://tauri.localhost/setup.html")), !cfg!(target_os = "macos"));
        assert!(bundled(&url("tauri://localhost/setup.html")));
    }
    #[test]
    #[cfg(unix)]
    fn private_write_replaces_symlink_without_disclosing_secret() {
        use std::os::unix::fs::{PermissionsExt, symlink};
        let dir = std::env::temp_dir().join(format!("supymem-private-write-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&dir).unwrap();
        let victim = dir.join("victim"); let dest = dir.join(".env");
        std::fs::write(&victim, "untouched").unwrap(); symlink(&victim, &dest).unwrap();
        write_private(&dest, b"synthetic-key").unwrap();
        assert_eq!(std::fs::read_to_string(&victim).unwrap(), "untouched");
        assert_eq!(std::fs::metadata(&dest).unwrap().permissions().mode() & 0o777, 0o600);
        assert_eq!(std::fs::read_to_string(&dest).unwrap(), "synthetic-key");
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    #[cfg(windows)]
    fn private_windows_write_enforces_acl_on_creation_and_replacement() {
        for domain in ["factory-inventory", "family-gardening"] {
            let directory = std::env::temp_dir().join(format!("{domain}-{}", uuid::Uuid::new_v4()));
            std::fs::create_dir(&directory).unwrap();
            let path = directory.join("settings.json");
            for body in [b"first".as_slice(), b"replacement".as_slice()] {
                write_private(&path, body).unwrap();
                let file = std::fs::File::open(&path).unwrap();
                assert!(crate::local_credentials::private_windows(&file));
                assert_eq!(std::fs::read(&path).unwrap(), body);
            }
            std::fs::remove_dir_all(directory).unwrap();
        }
    }
}
