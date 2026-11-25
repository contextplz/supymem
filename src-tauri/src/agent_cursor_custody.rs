//! Visual feed ownership only; never grants native input/window authority.
#[derive(Default)]
pub struct FeedCustody {
    owner: Option<String>,
}
impl FeedCustody {
    pub fn admit(&mut self, kind: &str, owner: Option<&str>) -> bool {
        if kind == "begin" {
            match owner {
                Some(value) => self.owner = Some(value.to_owned()),
                None if self.owner.is_some() => return false,
                None => {}
            }
            return true;
        }
        if self.owner.as_deref() != owner {
            return false;
        }
        if kind == "end" {
            self.owner = None;
        }
        true
    }
    pub fn owned_active(&self) -> bool {
        self.owner.is_some()
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn old_end_and_update_cannot_erase_new_execution() {
        let mut feed = FeedCustody::default();
        assert!(feed.admit("begin", Some("first")));
        assert!(feed.admit("click", Some("first")));
        assert!(feed.admit("begin", Some("second")));
        assert!(!feed.admit("end", Some("first")));
        assert!(!feed.admit("type", Some("first")));
        assert!(feed.owned_active());
        assert!(feed.admit("read", Some("second")));
        assert!(feed.admit("end", Some("second")));
        assert!(!feed.admit("click", Some("second")));
    }
    #[test]
    fn legacy_is_accepted_only_without_owned_execution() {
        let mut feed = FeedCustody::default();
        assert!(feed.admit("begin", None));
        assert!(feed.admit("read", None));
        assert!(feed.admit("begin", Some("owned")));
        for kind in ["begin", "read", "done", "end"] {
            assert!(!feed.admit(kind, None));
        }
        assert!(feed.admit("end", Some("owned")));
        assert!(feed.admit("begin", None));
    }
    #[test]
    fn update_without_begin_cannot_claim_visual_lease() {
        let mut feed = FeedCustody::default();
        assert!(!feed.admit("read", Some("unknown")));
        assert!(!feed.admit("end", Some("unknown")));
        assert!(!feed.owned_active());
    }
}
