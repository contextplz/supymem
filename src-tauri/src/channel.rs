//! Public build channels are fixed pairs, never independently supplied origins.
pub fn staging() -> bool {
    env!("SUPYMEM_DESKTOP_CHANNEL") == "staging"
}
pub fn local_review() -> bool {
    packaged_local_review() || (staging() && option_env!("SUPYMEM_STAGING_LOCAL_REVIEW") == Some("enabled"))
}
pub fn packaged_local_review() -> bool {
    option_env!("SUPYMEM_LOCAL_REVIEW") == Some("enabled")
}
pub fn session_only() -> bool {
    session_only_for(packaged_local_review(), staging(), env!("SUPYMEM_STAGING_SESSION_ONLY") == "enabled")
}
fn session_only_for(packaged_review: bool, staging_channel: bool, staging_setting: bool) -> bool {
    packaged_review || (staging_channel && staging_setting)
}
pub fn updater_enabled() -> bool {
    !local_review() && (crate::local_stack::is_local() || !staging() || env!("SUPYMEM_STAGING_UPDATER") == "enabled")
}
pub fn gateway() -> &'static str {
    if staging() {
        "http://127.0.0.1:3200"
    } else {
        "http://127.0.0.1:3200"
    }
}
pub fn console() -> &'static str {
    if staging() {
        "http://127.0.0.1:3100"
    } else {
        "http://127.0.0.1:3100"
    }
}
pub fn callback_scheme() -> &'static str {
    if staging() {
        "supymem-staging"
    } else {
        "supymem"
    }
}
pub fn data_name() -> &'static str {
    if staging() {
        "Supymem Staging"
    } else {
        "Supymem"
    }
}
#[cfg(test)]
mod tests {
    #[test]
    fn channel_is_a_fixed_pair_with_its_own_callback() {
        if super::staging() {
            assert_eq!(super::gateway(), "http://127.0.0.1:3200");
            assert_eq!(super::console(), "http://127.0.0.1:3100");
            assert_eq!(super::callback_scheme(), "supymem-staging");
            assert_ne!(super::data_name(), "Supymem");
        } else {
            assert_eq!(super::gateway(), "http://127.0.0.1:3200");
            assert_eq!(super::console(), "http://127.0.0.1:3100");
            assert_eq!(super::callback_scheme(), "supymem");
        }
    }

    #[test]
    fn packaged_local_review_never_persists_a_device_credential() {
        assert!(super::session_only_for(true, false, false));
        assert!(super::session_only_for(true, true, false));
        assert!(super::session_only_for(false, true, true));
        assert!(!super::session_only_for(false, false, true));
        assert!(!super::session_only_for(false, false, false));
    }
}
