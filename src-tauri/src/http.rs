//! A stable product identity for native requests. Managed edge rules reject
//! requests without a User-Agent; this does not impersonate a browser or bypass
//! authentication, rate limiting, or any other authorization check.
pub const USER_AGENT: &str = concat!("SupymemDesktop/", env!("SUPYMEM_DESKTOP_VERSION"));
