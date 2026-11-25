//! Corporate auto-update: optional (Later allowed) vs force (no skip).
//!
//! Policy is baked into `latest.json` `notes` as an HTML comment so Tauri's
//! fixed manifest schema stays untouched:
//!
//! ```text
//! <!--cos-update {"mandatory":true,"min_version":"0.1.0"}-->
//! ```
//!
//! - `mandatory: true` → force (Update now only).
//! - current version < `min_version` → also force.
//! - otherwise optional (Install now / Later; remind next launch).
//!
//! Endpoint: the public Supymem update channel,
//! overridable with ``COMPANY_OS_UPDATE_ENDPOINT`` for local fixtures.

use serde::{Deserialize, Serialize};
use std::path::Path;
use std::sync::{atomic::{AtomicBool, Ordering}, Mutex};
use std::time::{Duration, Instant};

pub const POLICY_MARKER_START: &str = "<!--cos-update";
pub const POLICY_MARKER_END: &str = "-->";
pub const DEFAULT_UPDATE_ENDPOINT: &str =
    "https://supymem.com/downloads/latest.json";
pub const STAGING_UPDATE_ENDPOINT: &str = "https://supymem.com/downloads/staging/latest.json";
pub const UPDATE_ENDPOINT_ENV: &str = "COMPANY_OS_UPDATE_ENDPOINT";

#[derive(Debug, Deserialize)]
pub struct LocalRelease {
    pub tag_name: String,
    #[serde(default)]
    pub draft: bool,
    #[serde(default)]
    pub prerelease: bool,
    #[serde(default)]
    pub body: Option<String>,
    #[serde(default)]
    pub assets: Vec<LocalReleaseAsset>,
}

#[derive(Debug, Deserialize)]
pub struct LocalReleaseAsset {
    pub name: String,
}

fn local_release_version(tag: &str) -> Option<&str> {
    let version = tag.strip_prefix("v").or_else(|| tag.strip_prefix("desktop-v"))?;
    if version.split('.').count() != 3 || version.split('.').any(|part|
        part.is_empty() || !part.bytes().all(|byte| byte.is_ascii_digit()) || part.parse::<u64>().is_err()) {
        return None;
    }
    Some(version)
}

fn newest_local_release(releases: Vec<LocalRelease>) -> Option<LocalRelease> {
    releases.into_iter().filter(|release| !release.draft && !release.prerelease
        && local_release_version(&release.tag_name).is_some()
        && release.assets.iter().any(|asset| asset.name.ends_with(".dmg") || asset.name.ends_with(".deb")
            || asset.name.ends_with(".AppImage") || asset.name.ends_with("-setup.exe")))
        .max_by_key(|release| parse_dotted_version(local_release_version(&release.tag_name).unwrap()))
}

pub fn local_update_repository() -> Option<&'static str> {
    let repository = env!("SUPYMEM_OSS_REPOSITORY");
    (!repository.is_empty()).then_some(repository)
}

fn local_release_endpoint(repository: &str) -> String {
    format!("https://api.github.com/repos/{repository}/releases")
}

pub async fn local_update_check(current: &str, allow_github: bool) -> UpdateCheckResult {
    local_update_check_for_repository(current, allow_github, local_update_repository()).await
}

async fn local_update_check_for_repository(current: &str, allow_github: bool, repository: Option<&str>) -> UpdateCheckResult {
    let Some(repository) = repository else {
        return UpdateCheckResult::none(current, "",
            "Update checks are off for your build. If you built the app, set SUPYMEM_UPDATE_REPO to your public GitHub owner/repository and rebuild. Otherwise, ask whoever supplied your installer for a newer one.");
    };
    let endpoint = local_release_endpoint(repository);
    if !allow_github {
        return UpdateCheckResult::none(current, &endpoint,
            "Open Check for updates, then allow GitHub update checks to look for a new version.");
    }
    match latest_local_release(&endpoint).await {
        Ok(release) => local_release_notice(current, &release, repository, &endpoint),
        Err(error) => {
            let mut result = UpdateCheckResult::none(current, &endpoint, error);
            result.release_url = Some(format!("https://github.com/{repository}/releases"));
            result
        }
    }
}

async fn latest_local_release(endpoint: &str) -> Result<LocalRelease, String> {
    let error = "Your release could not be checked. Choose Open your release page to download your installer manually.";
    let client = reqwest::Client::builder().user_agent(crate::http::USER_AGENT)
        .https_only(true).redirect(reqwest::redirect::Policy::none()).no_proxy()
        .timeout(Duration::from_secs(12))
        .build().map_err(|_| error.to_string())?;
    let mut latest: Option<LocalRelease> = None;
    let mut page = 1;
    loop {
        let releases: Vec<LocalRelease> = client.get(format!("{endpoint}?per_page=100&page={page}"))
            .send().await.map_err(|_| error.to_string())?
            .error_for_status().map_err(|_| error.to_string())?
            .json().await.map_err(|_| error.to_string())?;
        let complete = releases.len() < 100;
        if let Some(release) = newest_local_release(releases) {
            if latest.as_ref().map_or(true, |current| version_gt(
                local_release_version(&release.tag_name).unwrap(), local_release_version(&current.tag_name).unwrap())) {
                latest = Some(release);
            }
        }
        if complete { break; }
        page += 1;
    }
    latest.ok_or_else(|| error.into())
}

fn local_release_notice(current: &str, release: &LocalRelease, repository: &str, endpoint: &str) -> UpdateCheckResult {
    let Some(version) = local_release_version(&release.tag_name) else {
        return UpdateCheckResult::none(current, endpoint,
            "Your release version could not be read. Open your GitHub Releases page to choose an installer.");
    };
    let mut result = UpdateCheckResult::none(current, endpoint,
        "A new version is available. Open the release page to install it. Your saved work stays on your machine.");
    result.available = version_gt(version, current) || current.strip_prefix(version).is_some_and(|suffix| suffix.starts_with('-'));
    if result.available {
        result.version = Some(version.to_string());
        result.notes = release.body.clone();
    } else {
        result.honesty = Some("Your app is current. Check again when you want to look for another release.".into());
    }
    result.release_url = Some(format!("https://github.com/{repository}/releases/tag/{}", release.tag_name));
    result
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct UpdatePolicy {
    #[serde(default)]
    pub mandatory: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub min_version: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct LatestManifest {
    pub version: String,
    #[serde(default)]
    pub notes: Option<String>,
    #[serde(default)]
    #[allow(dead_code)]
    pub pub_date: Option<String>,
    #[serde(default)]
    #[allow(dead_code)]
    pub platforms: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct UpdateCheckResult {
    pub available: bool,
    /// True only when the signed release channel answered successfully.
    pub channel_verified: bool,
    pub current_version: String,
    pub version: Option<String>,
    pub notes: Option<String>,
    pub mandatory: bool,
    pub min_version: Option<String>,
    pub force: bool,
    pub can_defer: bool,
    pub honesty: Option<String>,
    pub endpoint: String,
    /// Binds the founder confirmation to the exact native checked package.
    pub check_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub release_url: Option<String>,
}

impl UpdateCheckResult {
    pub fn none(current_version: &str, endpoint: &str, honesty: impl Into<String>) -> Self {
        Self {
            available: false,
            channel_verified: false,
            current_version: current_version.to_string(),
            version: None,
            notes: None,
            mandatory: false,
            min_version: None,
            force: false,
            can_defer: true,
            honesty: Some(honesty.into()),
            endpoint: endpoint.to_string(),
            check_id: None,
            release_url: None,
        }
    }

    pub fn up_to_date(current_version: &str, endpoint: &str) -> Self {
        let mut result = Self::none(
            current_version,
            endpoint,
            honesty_up_to_date(current_version, endpoint),
        );
        result.channel_verified = true;
        result
    }
}

pub fn resolve_update_endpoint() -> String {
    if crate::local_stack::is_local() {
        return local_update_repository().map(local_release_endpoint).unwrap_or_default();
    }
    if crate::channel::staging() { return STAGING_UPDATE_ENDPOINT.to_string(); }
    // Release builds never let a surrounding shell redirect the update channel.
    if !cfg!(debug_assertions) { return DEFAULT_UPDATE_ENDPOINT.to_string(); }
    std::env::var(UPDATE_ENDPOINT_ENV)
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| DEFAULT_UPDATE_ENDPOINT.to_string())
}

pub fn parse_update_policy(notes: &str) -> UpdatePolicy {
    let Some(start) = notes.find(POLICY_MARKER_START) else {
        return UpdatePolicy::default();
    };
    let rest = &notes[start + POLICY_MARKER_START.len()..];
    let Some(end) = rest.find(POLICY_MARKER_END) else {
        return UpdatePolicy::default();
    };
    let raw = rest[..end].trim();
    serde_json::from_str(raw).unwrap_or_default()
}

pub fn parse_latest_manifest(body: &str) -> Result<LatestManifest, String> {
    serde_json::from_str(body).map_err(|e| format!("latest.json is not valid updater JSON: {e}"))
}

/// Compare dotted versions (`0.1.0`, `v0.2.1`). Missing parts count as 0.
pub fn parse_dotted_version(raw: &str) -> (u64, u64, u64) {
    let s = raw.trim().trim_start_matches('v').trim_start_matches('V');
    let mut parts = s.split('.');
    let major = parts
        .next()
        .and_then(|p| p.chars().take_while(|c| c.is_ascii_digit()).collect::<String>().parse().ok())
        .unwrap_or(0);
    let minor = parts
        .next()
        .and_then(|p| p.chars().take_while(|c| c.is_ascii_digit()).collect::<String>().parse().ok())
        .unwrap_or(0);
    let patch = parts
        .next()
        .and_then(|p| p.chars().take_while(|c| c.is_ascii_digit()).collect::<String>().parse().ok())
        .unwrap_or(0);
    (major, minor, patch)
}

pub fn version_lt(current: &str, other: &str) -> bool {
    parse_dotted_version(current) < parse_dotted_version(other)
}

pub fn version_gt(left: &str, right: &str) -> bool {
    parse_dotted_version(left) > parse_dotted_version(right)
}

pub fn should_force(current: &str, policy: &UpdatePolicy) -> bool {
    if policy.mandatory {
        return true;
    }
    policy
        .min_version
        .as_deref()
        .map(|min| version_lt(current, min))
        .unwrap_or(false)
}

pub fn can_defer(force: bool) -> bool {
    !force
}

pub fn endpoint_is_local(endpoint: &str) -> bool {
    url::Url::parse(endpoint).is_ok_and(|u| {
        matches!(u.host_str(), Some("127.0.0.1" | "localhost" | "[::1]"))
    })
}

pub fn honesty_for_http(status: Option<u16>, endpoint: &str) -> String {
    let local = endpoint_is_local(endpoint);
    match status {
        Some(code @ 404) | Some(code @ 403) => {
            if local {
                format!(
                    "No signed update on this channel (HTTP {code} at {endpoint}). This is a local test endpoint — Nothing was installed."
                )
            } else {
                format!(
                    "No signed update on this channel (HTTP {code} at {endpoint}). Common for a local or unsigned build, or a draft GitHub release. Nothing was installed."
                )
            }
        }
        Some(code) => format!(
            "Update check failed (HTTP {code} at {endpoint}). Nothing was installed."
        ),
        None => format!(
            "Could not reach the update channel ({endpoint}). Nothing was installed."
        ),
    }
}

pub fn honesty_up_to_date(current: &str, endpoint: &str) -> String {
    if endpoint_is_local(endpoint) {
        format!("You're on {current}. The local fixture has no newer signed release.")
    } else {
        format!("You're on {current}. No newer signed release.")
    }
}

pub fn honesty_for_install_error(err: &str) -> String {
    let e = err.to_ascii_lowercase();
    if e.contains("signature") || e.contains("minisign") || e.contains("public key") {
        return format!(
            "The update file was not signed with this app's updater key ({err}). Nothing was installed."
        );
    }
    if e.contains("404") || e.contains("release not found") {
        return format!(
            "No signed update artifact was found ({err}). Nothing was installed."
        );
    }
    format!("Update did not install ({err}). The app was not replaced.")
}

pub fn evaluate_manifest(
    current_version: &str,
    endpoint: &str,
    manifest: &LatestManifest,
) -> UpdateCheckResult {
    let policy = parse_update_policy(manifest.notes.as_deref().unwrap_or(""));
    let remote = manifest.version.trim();
    if !version_gt(remote, current_version) {
        return UpdateCheckResult::up_to_date(current_version, endpoint);
    }
    let force = should_force(current_version, &policy);
    let notes = manifest.notes.clone().map(|n| {
        if let Some(idx) = n.find(POLICY_MARKER_END) {
            n[idx + POLICY_MARKER_END.len()..].trim().to_string()
        } else {
            n
        }
    });
    UpdateCheckResult {
        available: true,
        channel_verified: true,
        current_version: current_version.to_string(),
        version: Some(remote.to_string()),
        notes,
        mandatory: policy.mandatory,
        min_version: policy.min_version.clone(),
        force,
        can_defer: can_defer(force),
        honesty: None,
        endpoint: endpoint.to_string(),
        check_id: None,
        release_url: None,
    }
}

pub fn fetch_update_check(current_version: &str, endpoint: &str) -> UpdateCheckResult {
    let client = match reqwest::blocking::Client::builder()
        .user_agent(crate::http::USER_AGENT)
        .timeout(std::time::Duration::from_secs(12))
        .build()
    {
        Ok(c) => c,
        Err(e) => {
            return UpdateCheckResult::none(
                current_version,
                endpoint,
                format!("Could not start an update check ({e}). Nothing was installed."),
            );
        }
    };
    let res = match client.get(endpoint).send() {
        Ok(r) => r,
        Err(_) => {
            return UpdateCheckResult::none(
                current_version,
                endpoint,
                honesty_for_http(None, endpoint),
            );
        }
    };
    let status = res.status();
    if !status.is_success() {
        return UpdateCheckResult::none(
            current_version,
            endpoint,
            honesty_for_http(Some(status.as_u16()), endpoint),
        );
    }
    let body = match res.text() {
        Ok(b) => b,
        Err(e) => {
            return UpdateCheckResult::none(
                current_version,
                endpoint,
                format!("Update channel returned an unreadable body ({e}). Nothing was installed."),
            );
        }
    };
    match parse_latest_manifest(&body) {
        Ok(manifest) => evaluate_manifest(current_version, endpoint, &manifest),
        Err(e) => UpdateCheckResult::none(current_version, endpoint, format!("{e} Nothing was installed.")),
    }
}

pub fn persist_status(data_dir: &Path, result: &UpdateCheckResult) -> Result<(), String> {
    std::fs::create_dir_all(data_dir).map_err(|e| e.to_string())?;
    let path = data_dir.join("update_status.json");
    let body = serde_json::to_string_pretty(result).map_err(|e| e.to_string())?;
    std::fs::write(path, body).map_err(|e| e.to_string())
}

/// Only one check/download/install operation is allowed, including across windows.
#[derive(Default)]
pub struct Controller {
    busy: AtomicBool,
    pub candidate: Mutex<Option<Candidate>>,
}
pub struct Candidate {
    pub package: tauri_plugin_updater::Update,
    pub result: UpdateCheckResult,
    pub receipt: Receipt,
    pub identity: CandidateIdentity,
}
#[derive(Clone, PartialEq, Eq)]
pub struct CandidateIdentity {
    pub version: String,
    pub url: String,
    pub signature: String,
    pub notes: Option<String>,
}
#[derive(Clone)]
pub struct Receipt { pub id: String, checked: Instant }
impl Receipt {
    pub fn new() -> Self { Self { id: uuid::Uuid::new_v4().to_string(), checked: Instant::now() } }
    pub fn for_candidate(previous: Option<(&CandidateIdentity, &Receipt)>, next: &CandidateIdentity) -> Self {
        if let Some((identity, receipt)) = previous {
            if identity == next && receipt.confirm(&receipt.id).is_ok() {
                return receipt.clone();
            }
        }
        Self::new()
    }
    pub fn confirm(&self, id: &str) -> Result<(), String> {
        if self.id != id || self.checked.elapsed() > Duration::from_secs(30 * 60) {
            Err("This update confirmation expired or changed. Check for updates again.".into())
        } else { Ok(()) }
    }
}
pub struct Operation<'a>(&'a AtomicBool);
impl Controller {
    pub fn prevent_close(&self) -> bool {
        self.busy.load(Ordering::Acquire) || self.candidate.lock()
            .map(|value| value.as_ref().is_some_and(|candidate| candidate.result.force))
            .unwrap_or(true)
    }
    pub fn begin(&self) -> Result<Operation<'_>, String> {
        self.busy.compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map(|_| Operation(&self.busy))
            .map_err(|_| "An update operation is already running. Wait for its result.".into())
    }
}
impl Drop for Operation<'_> { fn drop(&mut self) { self.0.store(false, Ordering::Release); } }

pub fn validate_download_url(raw: &str, allow_local: bool) -> Result<url::Url, String> {
    let url = url::Url::parse(raw).map_err(|_| "Invalid update address. Nothing was installed.".to_string())?;
    if !url.username().is_empty() || url.password().is_some() || url.fragment().is_some()
        || !(url.scheme() == "https" || (allow_local && url.scheme() == "http" && endpoint_is_local(raw))) {
        return Err("Updates require a secure release address. Nothing was installed.".into());
    }
    Ok(url)
}

/// The updater dialog may run when the hosted console is unavailable. Other
/// local windows and remote origins must not invoke update installation.
pub fn trusted_window(label: &str, location: &url::Url, console: &str) -> bool {
    if label == "main" { return crate::auth::trusted_account_window(label, location, console); }
    label == "updates" && location.username().is_empty() && location.password().is_none()
        && location.path() == "/update.html"
        && ((location.scheme() == "tauri" && location.host_str() == Some("localhost"))
            || (location.scheme() == "http" && location.host_str() == Some("tauri.localhost")))
}

#[derive(Clone, Serialize)]
pub struct Progress {
    pub phase: &'static str,
    pub downloaded: u64,
    pub total: Option<u64>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::net::TcpListener;
    use std::path::PathBuf;
    use std::thread;

    #[tokio::test]
    async fn local_release_checks_require_opt_in() {
        for version in ["1.0.0", "2.0.0-rc.1"] {
            let result = local_update_check_for_repository(version, false, Some("example.test/desktop")).await;
            assert!(!result.channel_verified && !result.available && result.release_url.is_none());
            assert_eq!(result.endpoint, "https://api.github.com/repos/example.test/desktop/releases");
            assert!(result.honesty.unwrap().contains("allow GitHub update checks"));
        }
    }

    #[tokio::test]
    async fn local_release_checks_stay_off_without_a_configured_repository() {
        let configured = option_env!("SUPYMEM_UPDATE_REPO")
            .filter(|repository| cfg!(feature = "local-stack") && !repository.is_empty());
        assert!(local_update_repository() == configured, "Only the explicit build setting may configure updates");
        for (version, domain) in [("1.0.0", "warehouse.example"), ("2.0.0-rc.1", "fitness.example")] {
            for allow_github in [false, true] {
                let result = if configured.is_none() {
                    local_update_check(version, allow_github).await
                } else {
                    local_update_check_for_repository(version, allow_github, None).await
                };
                assert!(!result.channel_verified && !result.available && result.release_url.is_none(), "{domain}");
                assert!(result.endpoint.is_empty());
                assert!(result.honesty.unwrap().contains("Update checks are off"));
            }
        }
    }

    #[test]
    fn local_desktop_release_tags_keep_version_and_release_url() {
        for body in ["warehouse.example", "fitness.example"] {
            for tag in ["v2.3.4", "desktop-v2.3.4"] {
                let release = LocalRelease { tag_name: tag.into(), draft: false, prerelease: false,
                    body: Some(body.into()), assets: vec![] };
                let result = local_release_notice("1.0.0", &release, "example.test/desktop", "");
                assert!(result.available);
                assert_eq!(result.version.as_deref(), Some("2.3.4"));
                assert_eq!(result.release_url.unwrap(), format!("https://github.com/example.test/desktop/releases/tag/{tag}"));
                assert_eq!(result.notes.as_deref(), Some(body));
            }
            for tag in ["2.3.4", "v2.3", "v2.3.4-rc.1", "desktop-v2.x.4", "desktop-v2..4", "desktop-v2.3.18446744073709551616"] {
                assert!(local_release_version(tag).is_none());
            }
        }
    }

    #[test]
    fn local_update_ignores_source_only_releases_drafts_and_newer_prereleases() {
        for body in ["warehouse.example", "fitness.example"] {
            let releases = serde_json::from_value(serde_json::json!([
                {"tag_name": "v9.0.0", "assets": [{"name": "SHA256SUMS"}]},
                {"tag_name": "desktop-v8.0.0", "draft": true, "assets": [{"name": "app.dmg"}]},
                {"tag_name": "desktop-v7.0.0", "prerelease": true, "assets": [{"name": "app.dmg"}]},
                {"tag_name": "desktop-v6.0.0", "assets": []},
                {"tag_name": "v2.0.0", "body": body, "assets": [{"name": "app.dmg"}]},
                {"tag_name": "desktop-v1.0.0", "assets": [{"name": "app.dmg"}]}
            ])).unwrap();
            let release = newest_local_release(releases).unwrap();
            assert_eq!(release.tag_name, "v2.0.0");
            let notice = local_release_notice("2.0.0-rc.1", &release, "example.test/desktop", "");
            assert!(notice.available);
            assert_eq!(notice.notes.as_deref(), Some(body));
            assert!(notice.check_id.is_none());
        }
    }

    #[test]
    fn operation_guard_rejects_overlap_and_releases_after_failure() {
        let controller = Controller::default();
        let first = controller.begin().unwrap();
        assert!(controller.begin().is_err());
        drop(first);
        assert!(controller.begin().is_ok());
    }

    #[test]
    fn confirmation_binds_exact_candidate_and_expires() {
        let receipt = Receipt::new();
        assert!(receipt.confirm(&receipt.id).is_ok());
        assert!(receipt.confirm("different-release-check").is_err());
        let expired = Receipt { id: "old".into(), checked: Instant::now() - Duration::from_secs(1801) };
        assert!(expired.confirm("old").is_err());
    }

    fn candidate_identity() -> CandidateIdentity {
        CandidateIdentity { version: "0.1.32".into(), url: "https://supymem.com/downloads/staging/a.app.tar.gz".into(), signature: "signed-package-a".into(), notes: Some("optional release".into()) }
    }

    #[test]
    fn hosted_recheck_does_not_revoke_the_bundled_dialog_confirmation() {
        let selected = candidate_identity();
        let bundled = Receipt::for_candidate(None, &selected);
        let founder_saw = bundled.id.clone();
        // Loading the hosted UI checks the same release after the native dialog.
        let hosted = Receipt::for_candidate(Some((&selected, &bundled)), &selected);
        assert_eq!(hosted.id, founder_saw);
        assert!(hosted.confirm(&founder_saw).is_ok());
    }

    #[test]
    fn changed_artifact_or_policy_rejects_the_previously_reviewed_receipt() {
        let selected = candidate_identity();
        let original = Receipt::for_candidate(None, &selected);
        let mut changed = vec![selected.clone(); 4];
        changed[0].version = "0.1.33".into();
        changed[1].url.push_str("?different-object");
        changed[2].signature = "different-signature".into();
        changed[3].notes = Some("<!--cos-update {\"mandatory\":true}-->Required".into());
        for candidate in changed {
            let next = Receipt::for_candidate(Some((&selected, &original)), &candidate);
            assert!(next.confirm(&original.id).is_err());
        }
    }

    #[test]
    fn rechecking_the_same_artifact_after_expiry_requires_new_confirmation() {
        let selected = candidate_identity();
        let old = Receipt { id: "expired-dialog".into(), checked: Instant::now() - Duration::from_secs(1801) };
        let refreshed = Receipt::for_candidate(Some((&selected, &old)), &selected);
        assert!(refreshed.confirm(&old.id).is_err());
        assert!(refreshed.confirm(&refreshed.id).is_ok());
    }

    #[test]
    fn local_testing_cannot_match_a_remote_hostname_or_credentials() {
        assert!(endpoint_is_local("http://127.0.0.1:9999/latest.json"));
        for url in ["http://localhost.example.com/latest.json", "http://example.com/localhost", "http://example.com/?127.0.0.1", "https://user:secret@example.com/file"] {
            assert!(validate_download_url(url, true).is_err(), "{url}");
        }
        assert!(validate_download_url("http://127.0.0.1/latest.json", false).is_err());
        assert!(validate_download_url("https://supymem.com/downloads/latest.json", false).is_ok());
    }

    #[test]
    fn updater_window_is_bundled_and_cannot_be_remote() {
        for url in ["tauri://localhost/update.html", "http://tauri.localhost/update.html"] {
            assert!(trusted_window("updates", &url::Url::parse(url).unwrap(), "https://app.supymem.com"));
        }
        for (label, url) in [("updates", "https://app.supymem.com/update.html"), ("updates", "tauri://localhost/setup.html"), ("setup", "tauri://localhost/update.html"), ("main", "https://evil.example")] {
            assert!(!trusted_window(label, &url::Url::parse(url).unwrap(), "https://app.supymem.com"));
        }
    }

    fn notes(policy: &str, rest: &str) -> String {
        format!("{POLICY_MARKER_START} {policy}{POLICY_MARKER_END}\n{rest}")
    }

    #[test]
    fn optional_policy_allows_defer() {
        let p = parse_update_policy(&notes(r#"{"mandatory":false}"#, "nice to have"));
        assert!(!p.mandatory);
        assert!(!should_force("0.1.0", &p));
        assert!(can_defer(false));
    }

    #[test]
    fn mandatory_policy_denies_defer() {
        let p = parse_update_policy(&notes(r#"{"mandatory":true}"#, "security"));
        assert!(p.mandatory);
        assert!(should_force("0.9.0", &p));
        assert!(!can_defer(true));
    }

    #[test]
    fn min_version_forces_older_current() {
        let p = parse_update_policy(&notes(
            r#"{"mandatory":false,"min_version":"0.2.0"}"#,
            "floor",
        ));
        assert!(should_force("0.1.0", &p));
        assert!(!should_force("0.2.0", &p));
        assert!(!should_force("0.3.0", &p));
    }

    #[test]
    fn missing_marker_is_optional() {
        let p = parse_update_policy("plain release notes");
        assert_eq!(p, UpdatePolicy::default());
        assert!(!should_force("0.1.0", &p));
    }

    #[test]
    fn latest_json_parse_reads_version_and_notes() {
        let body = r#"{
            "version": "0.1.1",
            "notes": "<!--cos-update {\"mandatory\":true}-->\nForce fixture",
            "pub_date": "2026-08-22T00:00:00Z",
            "platforms": {"darwin-aarch64": {"signature": "x", "url": "http://127.0.0.1/app.tar.gz"}}
        }"#;
        let m = parse_latest_manifest(body).unwrap();
        assert_eq!(m.version, "0.1.1");
        let result = evaluate_manifest("0.1.0", "http://127.0.0.1/latest.json", &m);
        assert!(result.available);
        assert!(result.force);
        assert!(!result.can_defer);
        assert_eq!(result.notes.as_deref(), Some("Force fixture"));
    }

    #[test]
    fn same_or_older_remote_is_not_an_update() {
        let m = LatestManifest {
            version: "0.1.0".into(),
            notes: Some(notes(r#"{"mandatory":true}"#, "same")),
            pub_date: None,
            platforms: None,
        };
        let result = evaluate_manifest("0.1.0", DEFAULT_UPDATE_ENDPOINT, &m);
        assert!(!result.available);
        assert!(result.honesty.unwrap().contains("No newer signed release"));
    }

    #[test]
    fn current_version_skips_even_when_mandatory() {
        let m = LatestManifest {
            version: "0.2.0".into(),
            notes: Some(notes(r#"{"mandatory":true}"#, "already on it")),
            pub_date: None,
            platforms: None,
        };
        let result = evaluate_manifest("0.2.0", DEFAULT_UPDATE_ENDPOINT, &m);
        assert!(!result.available);
        assert!(!result.force);
    }

    #[test]
    fn older_version_sees_optional_update() {
        let m = LatestManifest {
            version: "0.2.0".into(),
            notes: Some(notes(r#"{"mandatory":false}"#, "optional")),
            pub_date: None,
            platforms: None,
        };
        let result = evaluate_manifest("0.1.0", DEFAULT_UPDATE_ENDPOINT, &m);
        assert!(result.available);
        assert!(!result.force);
        assert!(result.can_defer);
    }

    #[test]
    fn persist_status_writes_force_flag() {
        let dir = std::env::temp_dir().join(format!("cos-update-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let result = UpdateCheckResult {
            available: true,
            channel_verified: true,
            current_version: "0.1.0".into(),
            version: Some("0.1.1".into()),
            notes: None,
            mandatory: true,
            min_version: None,
            force: true,
            can_defer: false,
            honesty: None,
            endpoint: DEFAULT_UPDATE_ENDPOINT.into(),
            check_id: None,
            release_url: None,
        };
        persist_status(&dir, &result).unwrap();
        let raw = std::fs::read_to_string(dir.join("update_status.json")).unwrap();
        assert!(raw.contains("\"force\": true"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn honesty_404_does_not_claim_install() {
        let msg = honesty_for_http(Some(404), "http://127.0.0.1:9/latest.json");
        assert!(msg.contains("404"));
        assert!(msg.contains("Nothing was installed"));
        assert!(msg.contains("local test endpoint"));
    }

    #[test]
    fn local_http_fixture_optional_and_force() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let optional = r#"{
            "version":"0.1.1",
            "notes":"<!--cos-update {\"mandatory\":false}-->\nOptional fixture",
            "platforms":{"darwin-aarch64":{"signature":"x","url":"http://127.0.0.1/opt.tar.gz"}}
        }"#;
        thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                let mut buf = [0u8; 512];
                let _ = stream.peek(&mut buf);
                let resp = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    optional.len(),
                    optional
                );
                let _ = stream.write_all(resp.as_bytes());
            }
        });
        let endpoint = format!("http://{addr}/latest.json");
        let result = fetch_update_check("0.1.0", &endpoint);
        assert!(result.available, "{result:?}");
        assert!(!result.force);
        assert!(result.can_defer);
        assert_eq!(result.version.as_deref(), Some("0.1.1"));
    }

    #[test]
    fn local_http_fixture_404_is_honest() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                let body = "not found";
                let resp = format!(
                    "HTTP/1.1 404 Not Found\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = stream.write_all(resp.as_bytes());
            }
        });
        let endpoint = format!("http://{addr}/latest.json");
        let result = fetch_update_check("0.1.0", &endpoint);
        assert!(!result.available);
        let honesty = result.honesty.unwrap();
        assert!(honesty.contains("404"));
        assert!(honesty.contains("Nothing was installed"));
    }

    #[test]
    fn install_error_never_claims_success() {
        let msg = honesty_for_install_error("minisign: signature verification failed");
        assert!(msg.contains("Nothing was installed"));
        assert!(!msg.to_ascii_lowercase().contains("installed successfully"));
    }

    #[test]
    fn committed_fixtures_optional_vs_force() {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../tests/fixtures/desktop_update");
        let optional = parse_latest_manifest(
            &std::fs::read_to_string(root.join("latest_optional.json")).unwrap(),
        )
        .unwrap();
        let force = parse_latest_manifest(
            &std::fs::read_to_string(root.join("latest_force.json")).unwrap(),
        )
        .unwrap();
        let opt = evaluate_manifest("0.1.0", "http://127.0.0.1/latest.json", &optional);
        let forced = evaluate_manifest("0.1.0", "http://127.0.0.1/latest.json", &force);
        assert!(opt.available && opt.can_defer && !opt.force);
        assert!(forced.available && forced.force && !forced.can_defer);
    }
}
