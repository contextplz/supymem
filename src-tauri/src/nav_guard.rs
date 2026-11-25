//! Keeps every webview on the console: links to any other site open in the
//! system browser instead of replacing the app (there is no back button).
use tauri::{plugin::TauriPlugin, Manager, Wry};
use url::Url;
use std::io::Read;

#[derive(Debug, PartialEq)]
pub(crate) enum Nav {
    Allow,
    OpenExternally,
    Block,
}

/// `console` is the origin the main window loads (hosted, staging or local).
fn decide(target: &Url, console: Option<&Url>, local: bool) -> Nav {
    // WKWebView sends subframe navigation through this callback too. Preserve
    // sandboxed srcdoc/venture/local-product previews; IPC trust is separately
    // restricted by capabilities and native_security::trusted_window.
    let preview = (target.scheme() == "about" && matches!(target.as_str(), "about:blank" | "about:srcdoc"))
        || (!local && ((target.scheme() == "https" && target.host_str().is_some_and(|host| host.ends_with(".supymem.com")))
            || (target.scheme() == "http" && matches!(target.host_str(), Some("127.0.0.1" | "localhost" | "[::1]")))));
    let allowed = crate::native_security::bundled(target) || preview
        || console.is_some_and(|c| crate::native_security::console_origin_allowed(c,
            local || cfg!(debug_assertions) || crate::channel::local_review(), crate::channel::staging())
            && c.origin() == target.origin());
    match target.scheme() {
        _ if allowed => Nav::Allow,
        "http" | "https" | "mailto" => Nav::OpenExternally,
        _ => Nav::Block,
    }
}

pub(crate) fn plugin() -> TauriPlugin<Wry> {
    tauri::plugin::Builder::<Wry>::new("nav-guard")
        .on_navigation(|webview, url| {
            let console = webview
                .try_state::<crate::AppState>()
                .and_then(|s| s.supervisor.console_url().parse::<Url>().ok());
            match decide(url, console.as_ref(), crate::local_stack::is_local()) {
                Nav::Allow => true,
                Nav::OpenExternally => {
                    let _ = open::that(url.as_str());
                    false
                }
                Nav::Block => false,
            }
        })
        .build()
}

pub(crate) fn owner_secret(path: &std::path::Path) -> std::io::Result<String> {
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.custom_flags(0x00200000);
    }
    let file = options.open(path)?;
    let metadata = file.metadata()?;
    let mut private = metadata.is_file();
    #[cfg(windows)]
    { private &= crate::local_credentials::private_windows(&file); }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        private = private && metadata.mode() & 0o077 == 0 && metadata.uid() == unsafe { libc::geteuid() };
    }
    if !private {
        return Err(std::io::Error::new(std::io::ErrorKind::PermissionDenied,
            "Check your data-folder permissions, then restart your app."));
    }
    let mut secret = String::new();
    file.take(129).read_to_string(&mut secret)?;
    let secret = secret.trim();
    if secret.len() != 64 || !secret.bytes().all(|character| character.is_ascii_digit() || (b'a'..=b'f').contains(&character)) {
        return Err(std::io::Error::new(std::io::ErrorKind::PermissionDenied,
            "Check your data-folder permissions, then restart your app."));
    }
    Ok(secret.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(u: &str) -> Nav {
        decide(&u.parse().unwrap(), Some(&crate::channel::console().parse().unwrap()), false)
    }

    #[test]
    fn console_stays_external_leaves() {
        assert_eq!(d(&format!("{}/v/ven_1/now", crate::channel::console())), Nav::Allow);
        assert_eq!(d("http://127.0.0.1:3100/"), Nav::Allow);
        assert_eq!(d("https://venture-preview.supymem.com/"), Nav::Allow);
        assert_eq!(d("about:srcdoc"), Nav::Allow);
        assert_eq!(d("tauri://localhost/setup.html"), Nav::Allow);
        assert_eq!(d("https://www.linkedin.com/pulse/x"), Nav::OpenExternally);
        assert_eq!(d("https://evil.test/?u=app.supymem.com"), Nav::OpenExternally);
        assert_eq!(d("https://app.supymem.com.evil.test/"), Nav::OpenExternally);
        assert_eq!(d("mailto:a@b.co"), Nav::OpenExternally);
        assert_eq!(d("file:///etc/passwd"), Nav::Block);
        let custom: Url = "https://console.example.com".parse().unwrap();
        assert_eq!(decide(&"https://console.example.com/x".parse().unwrap(), Some(&custom), false), Nav::OpenExternally);
    }

    #[test]
    #[cfg(feature = "local-stack")]
    fn local_console_requires_its_exact_origin() {
        let console: Url = "http://127.0.0.1:3100".parse().unwrap();
        for domain in ["inventory.example", "gardening.example"] {
            assert_eq!(decide(&console.join(domain).unwrap(), Some(&console), true), Nav::Allow);
            for location in [format!("http://127.0.0.1:8000/{domain}"), format!("https://{domain}"),
                format!("https://app.supymem.com/{domain}"), format!("https://app.staging.supymem.com/{domain}")] {
                assert_eq!(decide(&location.parse().unwrap(), Some(&console), true), Nav::OpenExternally);
            }
        }
    }

    #[test]
    #[cfg(unix)]
    fn owner_session_rejects_public_files_and_links() {
        use std::os::unix::fs::{symlink, PermissionsExt};
        for domain in ["inventory.example", "gardening.example"] {
            let root = std::env::temp_dir().join(format!("owner-{}", uuid::Uuid::new_v4()));
            std::fs::create_dir(&root).unwrap();
            let path = root.join(domain);
            let secret = format!("{}{}", uuid::Uuid::new_v4().simple(), uuid::Uuid::new_v4().simple());
            std::fs::write(&path, &secret).unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
            assert!(owner_secret(&path).is_ok());
            let link = root.join("link");
            symlink(&path, &link).unwrap();
            assert!(owner_secret(&link).is_err());
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
            assert!(owner_secret(&path).is_err());
            std::fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    #[cfg(windows)]
    fn owner_session_requires_your_private_windows_acl_and_rejects_aliases() {
        for domain in ["inventory.example", "gardening.example"] {
            let root = std::env::temp_dir().join(format!("owner-{}", uuid::Uuid::new_v4()));
            std::fs::create_dir(&root).unwrap();
            let path = root.join(domain);
            let secret = format!("{}{}", uuid::Uuid::new_v4().simple(), uuid::Uuid::new_v4().simple());
            std::fs::write(&path, &secret).unwrap();
            let status = std::process::Command::new("powershell.exe")
                .args(["-NoProfile", "-NonInteractive", "-Command", r#"
$ErrorActionPreference = 'Stop'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = New-Object System.Security.AccessControl.FileSecurity
$acl.SetOwner($identity)
$acl.SetAccessRuleProtection($true, $false)
$acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'Allow'))
Set-Acl -LiteralPath $env:SUPYMEM_OWNER_TEST_FILE -AclObject $acl
"#]).env("SUPYMEM_OWNER_TEST_FILE", &path).status().unwrap();
            assert!(status.success());
            assert!(owner_secret(&path).is_ok());
            let link = root.join("alias");
            std::fs::hard_link(&path, &link).unwrap();
            assert!(owner_secret(&link).is_err());
            std::fs::remove_file(&link).unwrap();
            let status = std::process::Command::new("icacls.exe")
                .arg(&path).args(["/grant", "*S-1-1-0:R"]).status().unwrap();
            assert!(status.success());
            assert!(owner_secret(&path).is_err());
            std::fs::remove_dir_all(root).unwrap();
        }
    }
}
