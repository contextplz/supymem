//! Charlie visibility is a local preference, independent of venture execution.
use std::{fs, io::{self, Write}, path::Path};
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize)]
struct Preference { visible: bool }

pub fn load(dir: &Path) -> bool {
    fs::read(dir.join("charlie-visibility.json")).ok()
        .and_then(|bytes| serde_json::from_slice::<Preference>(&bytes).ok())
        .map_or(true, |prefs| prefs.visible)
}

pub fn save(dir: &Path, visible: bool) -> io::Result<()> {
    fs::create_dir_all(dir)?;
    let temporary = dir.join(format!(".charlie-visibility-{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| {
        let mut file = fs::OpenOptions::new().write(true).create_new(true).open(&temporary)?;
        file.write_all(serde_json::to_string(&Preference { visible })?.as_bytes())?;
        file.sync_all()?;
        fs::rename(&temporary, dir.join("charlie-visibility.json"))
    })();
    if result.is_err() { let _ = fs::remove_file(temporary); }
    result
}

#[cfg(test)]
mod tests {
    #[test]
    fn preference_survives_reload_and_is_reversible() {
        let dir = std::env::temp_dir().join(format!("charlie-test-{}", uuid::Uuid::new_v4()));
        assert!(super::load(&dir));
        super::save(&dir, false).unwrap();
        assert!(!super::load(&dir));
        super::save(&dir, true).unwrap();
        assert!(super::load(&dir));
        std::fs::write(dir.join("charlie-visibility.json"), b"broken").unwrap();
        assert!(super::load(&dir));
        std::fs::remove_dir_all(dir).unwrap();
    }
}
