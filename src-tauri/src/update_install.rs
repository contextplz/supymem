//! Recover the existing Mac bundle if the upstream installer fails after moving it.
//! The plugin's temporary backup is otherwise dropped on an error path.
use std::path::{Path, PathBuf};

#[cfg(target_os = "macos")]
pub fn current_app() -> Result<PathBuf, String> {
    let exe = std::env::current_exe().map_err(|_| "Could not locate this application.".to_string())?;
    let app = exe.ancestors().find(|path| path.extension().is_some_and(|ext| ext == "app"))
        .ok_or("Updates can only replace an installed Mac app bundle.")?.to_path_buf();
    if app.starts_with("/Volumes") {
        return Err("Move Supymem from the disk image into Applications, then reopen it before installing an update.".into());
    }
    Ok(app)
}

fn copy_bundle(source: &Path, dest: &Path) -> Result<(), String> {
    let result = std::process::Command::new("/usr/bin/ditto").arg(source).arg(dest)
        .output().map_err(|_| "Could not copy the app for update recovery.".to_string())?;
    if result.status.success() { Ok(()) }
    else { Err("Could not copy the app for update recovery. Nothing was installed.".into()) }
}

/// Back up on the same volume, before entering the plugin's replacement path.
/// On any returned install error, restore the exact previous bundle. Running
/// native helpers are kept alive until installation succeeds.
pub fn install_with_recovery<F>(app: &Path, install: F) -> Result<(), String>
where F: FnOnce() -> Result<(), String> {
    let parent = app.parent().ok_or("Application location is invalid.")?;
    if app.symlink_metadata().map_err(|_| "Application is missing.")?.file_type().is_symlink() {
        return Err("Reopen the installed app directly before updating.".into());
    }
    let backup_dir = parent.join(format!(".supymem-update-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&backup_dir).map_err(|_| "This app location is not writable. Move Supymem to your Applications folder, then reopen it.")?;
    let backup = backup_dir.join("previous.app");
    if let Err(error) = copy_bundle(app, &backup) {
        let _ = std::fs::remove_dir_all(&backup_dir);
        return Err(error);
    }
    match install() {
        Ok(()) => {
            if let Err(error) = std::fs::remove_dir_all(&backup_dir) {
                log::warn!("desktop_update phase=backup_cleanup_failed error_kind={:?}", error.kind());
            }
            Ok(())
        }
        Err(_) => {
            // The plugin may have removed or partially replaced the original.
            let removed = match app.symlink_metadata() {
                Ok(meta) if meta.is_dir() && !meta.file_type().is_symlink() => std::fs::remove_dir_all(app),
                Ok(_) => std::fs::remove_file(app),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
                Err(error) => Err(error),
            };
            if removed.is_ok() && std::fs::rename(&backup, app).is_ok() {
                let _ = std::fs::remove_dir_all(&backup_dir);
                Err("The update could not be installed. Your previous app was restored and is still running.".into())
            } else {
                Err(format!("The update failed and automatic recovery could not finish. The previous app is preserved at {}. Keep this window open and restore that app before restarting.", backup.display()))
            }
        }
    }
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::*;
    fn fixture() -> (PathBuf, PathBuf) {
        let dir = std::env::temp_dir().join(format!("supymem-update-test-{}", uuid::Uuid::new_v4()));
        let app = dir.join("Supymem.app");
        std::fs::create_dir_all(app.join("Contents/MacOS")).unwrap();
        std::fs::write(app.join("Contents/MacOS/version"), "previous").unwrap();
        (dir, app)
    }
    #[test]
    fn failure_after_original_removal_restores_previous_app() {
        let (dir, app) = fixture();
        let result = install_with_recovery(&app, || {
            std::fs::remove_dir_all(&app).unwrap();
            Err("injected rename failure".into())
        });
        assert!(result.unwrap_err().contains("previous app was restored"));
        assert_eq!(std::fs::read_to_string(app.join("Contents/MacOS/version")).unwrap(), "previous");
        assert_eq!(std::fs::read_dir(&dir).unwrap().count(), 1);
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    fn failure_with_partial_new_app_restores_previous_app() {
        let (dir, app) = fixture();
        let result = install_with_recovery(&app, || {
            std::fs::write(app.join("Contents/MacOS/version"), "partial").unwrap();
            Err("injected write failure".into())
        });
        assert!(result.is_err());
        assert_eq!(std::fs::read_to_string(app.join("Contents/MacOS/version")).unwrap(), "previous");
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    fn success_keeps_updated_app_and_removes_owned_backup() {
        let (dir, app) = fixture();
        install_with_recovery(&app, || {
            std::fs::write(app.join("Contents/MacOS/version"), "new").unwrap();
            Ok(())
        }).unwrap();
        assert_eq!(std::fs::read_to_string(app.join("Contents/MacOS/version")).unwrap(), "new");
        assert_eq!(std::fs::read_dir(&dir).unwrap().count(), 1);
        std::fs::remove_dir_all(dir).unwrap();
    }
}
