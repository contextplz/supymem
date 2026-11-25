//! Closed visual diagnostics only. No input, account, task, or completion authority.
use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

pub struct Document { generation: AtomicU64, started: AtomicBool }
impl Default for Document {
    fn default() -> Self { Self { generation: AtomicU64::new(1), started: AtomicBool::new(false) } }
}
impl Document {
    pub fn start(&self) {
        if self.started.swap(true, Ordering::SeqCst) { self.generation.fetch_add(1, Ordering::SeqCst); }
    }
    pub fn current(&self) -> u64 { self.generation.load(Ordering::SeqCst) }
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Delivery {
    pub generation: u64,
    pub document_generation: u64,
    pub sequence: u64,
    pub owner: Option<String>,
    pub kind: String,
    pub activity: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Rendered {
    pub generation: u64,
    pub document_generation: u64,
    pub sequence: u64,
    pub owner: Option<String>,
    pub kind: String,
    pub activity: String,
    pub content_visible: bool,
    pub animation_frames: u8,
    pub animation_running: bool,
    pub reduced_motion: bool,
}

#[derive(Default)]
pub struct Receipt {
    pub delivered: Option<Delivery>,
    pub rendered: Option<Rendered>,
}
impl Receipt {
    pub fn deliver(&mut self, delivery: Delivery) {
        self.delivered = Some(delivery);
        self.rendered = None;
    }
    pub fn acknowledge(&mut self, ack: Rendered) -> bool {
        let Some(delivery) = &self.delivered else { return false };
        if ack.generation != delivery.generation || ack.sequence != delivery.sequence
            || ack.document_generation != delivery.document_generation
            || ack.owner != delivery.owner || ack.kind != delivery.kind
            || ack.activity != delivery.activity || ack.animation_frames != 2
            || (ack.reduced_motion && ack.animation_running)
            || (ack.kind == "end" && ack.content_visible) {
            return false;
        }
        self.rendered = Some(ack);
        true
    }
}

/// Explicit native-menu diagnostics: fresh private files, never clipboard or network.
#[cfg(unix)]
pub fn save_private(value: &serde_json::Value) -> Result<std::path::PathBuf, String> {
    use std::io::Write;
    use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
    static EXPORTS: AtomicU64 = AtomicU64::new(0);
    if EXPORTS.fetch_add(1, Ordering::SeqCst) >= 16 { return Err("cursor_diagnostic_export_limit".into()); }
    let bytes = serde_json::to_vec(value).map_err(|_| "cursor_diagnostic_unavailable")?;
    if bytes.len() > 48_000 { return Err("cursor_diagnostic_export_limit".into()); }
    let dir = std::env::temp_dir().join(format!("supymem-cursor-diagnostic-{}-{}", std::process::id(), uuid::Uuid::new_v4()));
    std::fs::DirBuilder::new().mode(0o700).create(&dir).map_err(|_| "cursor_diagnostic_unavailable")?;
    let path = dir.join(".receipt.pending");
    let final_path = dir.join("receipt.json");
    let result = (|| {
        let mut file = std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(&path)?;
        file.write_all(&bytes)?; file.sync_all()?;
        std::fs::rename(&path, &final_path)?;
        Ok::<(), std::io::Error>(())
    })();
    if result.is_err() { let _ = std::fs::remove_file(&path); let _ = std::fs::remove_dir(&dir); }
    result.map(|_| final_path).map_err(|_| "cursor_diagnostic_unavailable".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn delivery(sequence: u64) -> Delivery {
        Delivery { generation: 3, document_generation:1, sequence, owner: Some("a".repeat(32)), kind: "read".into(), activity: "Working on code".into() }
    }
    fn ack() -> Rendered {
        Rendered { generation: 3, document_generation:1, sequence: 7, owner: Some("a".repeat(32)), kind: "read".into(), activity: "Working on code".into(), content_visible: true, animation_frames: 2, animation_running: true, reduced_motion: false }
    }
    #[test] fn exact_current_visual_receipt_only() {
        let mut r = Receipt::default();
        assert!(!r.acknowledge(ack()));
        r.deliver(delivery(7)); assert!(r.acknowledge(ack()));
        r.deliver(delivery(8)); assert!(r.rendered.is_none()); assert!(!r.acknowledge(ack()));
    }
    #[test] fn stale_generation_owner_phase_and_incomplete_frames_refuse() {
        let mut r = Receipt::default(); r.deliver(delivery(7));
        let mut variants = vec![];
        let mut a=ack(); a.generation=2; variants.push(a);
        let mut a=ack(); a.document_generation=2; variants.push(a);
        let mut a=ack(); a.owner=Some("b".repeat(32)); variants.push(a);
        let mut a=ack(); a.activity="Private task".into(); variants.push(a);
        let mut a=ack(); a.animation_frames=1; variants.push(a);
        let mut a=ack(); a.reduced_motion=true; variants.push(a);
        for a in variants { assert!(!r.acknowledge(a)); }
        let mut a=ack(); a.reduced_motion=true; a.animation_running=false; assert!(r.acknowledge(a));
    }
    #[test] fn document_reload_immediately_retires_prior_ack_identity() {
        let doc=Document::default(); assert_eq!(doc.current(),1);
        doc.start(); assert_eq!(doc.current(),1);
        doc.start(); assert_eq!(doc.current(),2);
        let mut r=Receipt::default(); let mut d=delivery(7); d.document_generation=doc.current();r.deliver(d);
        assert!(!r.acknowledge(ack()));
        let mut a=ack();a.document_generation=doc.current();assert!(r.acknowledge(a));
    }
    #[test] fn ended_owner_cannot_report_visible_activity_or_replay_previous_ack() {
        let mut r=Receipt::default(); let mut d=delivery(8); d.kind="end".into(); d.activity="".into(); r.deliver(d);
        assert!(!r.acknowledge(ack()));
        let mut a=ack(); a.sequence=8; a.kind="end".into(); a.activity="".into(); assert!(!r.acknowledge(a.clone()));
        a.content_visible=false; a.animation_running=false; assert!(r.acknowledge(a));
    }
    #[test] fn receipt_schema_cannot_export_coordinates_or_prose_extensions() {
        let mut v=serde_json::to_value(ack()).unwrap(); v["x"]=serde_json::json!(123);
        assert!(serde_json::from_value::<Rendered>(v).is_err());
    }
    #[cfg(unix)]
    #[test] fn diagnostic_export_refuses_oversized_status_without_partial_file() {
        assert_eq!(save_private(&serde_json::json!({"oversized":"x".repeat(48_001)})).unwrap_err(), "cursor_diagnostic_export_limit");
    }
    #[cfg(unix)]
    #[test] fn native_menu_export_is_fresh_private_complete_and_has_no_effect_channel() {
        use std::os::unix::fs::PermissionsExt;
        let value=serde_json::json!({"execution_verified":false,"completion_verified":false});
        let path=save_private(&value).unwrap();
        assert_eq!(serde_json::from_slice::<serde_json::Value>(&std::fs::read(&path).unwrap()).unwrap(),value);
        assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,0o600);
        assert_eq!(std::fs::metadata(path.parent().unwrap()).unwrap().permissions().mode() & 0o777,0o700);
        assert!(!path.parent().unwrap().join(".receipt.pending").exists());
        std::fs::remove_file(&path).unwrap();std::fs::remove_dir(path.parent().unwrap()).unwrap();
    }
}
