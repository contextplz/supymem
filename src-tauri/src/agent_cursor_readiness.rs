//! Bounded visual-only delivery until the current overlay page has loaded.
use std::collections::VecDeque;
const MAX_PENDING: usize = 32;

#[derive(Default)]
pub struct Renderer {
    generation: u64,
    ready: bool,
    pending: VecDeque<String>,
}
impl Renderer {
    pub fn new(generation: u64) -> Self { Self { generation, ..Self::default() } }
    pub fn send(&mut self, script: String) -> Option<String> {
        if self.ready { return Some(script); }
        if self.pending.len() == MAX_PENDING { self.pending.pop_front(); }
        self.pending.push_back(script);
        None
    }
    pub fn loaded(&mut self, generation: u64, init: String) -> Vec<String> {
        if generation != self.generation { return Vec::new(); }
        // A new document in the same Webview still needs its exact origin.
        // Consumed hints stay retired; duplicate finish can never replay them.
        if self.ready { return vec![init]; }
        self.ready = true;
        std::iter::once(init).chain(self.pending.drain(..)).collect()
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test] fn early_init_precedes_queued_events() {
        let mut r = Renderer::new(1);
        assert_eq!(r.send("begin".into()), None);
        assert_eq!(r.send("click-global-234-181".into()), None);
        assert_eq!(r.loaded(1, "init-origin-200-100".into()),
            vec!["init-origin-200-100", "begin", "click-global-234-181"]);
        assert_eq!(r.send("end".into()), Some("end".into()));
    }
    #[test] fn old_page_cannot_drain_rebuilt_display() {
        let mut r = Renderer::new(2);
        r.send("begin-new-owner".into());
        assert!(r.loaded(1, "old-origin".into()).is_empty());
        assert_eq!(r.loaded(2, "new-origin".into()), vec!["new-origin", "begin-new-owner"]);
        assert_eq!(r.loaded(2, "duplicate-init".into()), vec!["duplicate-init"]);
    }
    #[test] fn same_window_reload_reinitializes_origin_without_replaying_retired_hints() {
        let mut r = Renderer::new(2);
        r.send("begin-owner".into());
        r.send("end-owner".into());
        assert_eq!(r.loaded(2, "origin-200-100".into()),
            vec!["origin-200-100", "begin-owner", "end-owner"]);
        assert_eq!(r.loaded(2, "origin-200-100".into()), vec!["origin-200-100"]);
        assert!(r.loaded(1, "old-origin".into()).is_empty());
    }
    #[test] fn bounded_backpressure_retains_terminal_order() {
        let mut r = Renderer::new(1);
        for n in 0..100 { r.send(format!("hint-{n}")); }
        r.send("end-old-owner".into());
        r.send("begin-new-owner".into());
        r.send("end-new-owner".into());
        let scripts = r.loaded(1, "init".into());
        assert_eq!(scripts.len(), MAX_PENDING + 1);
        assert_eq!(&scripts[scripts.len()-3..], ["end-old-owner", "begin-new-owner", "end-new-owner"]);
    }
}
