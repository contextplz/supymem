//! Device credentials are confined to the OS credential store and native HTTP.
//! No credential-bearing type implements Debug or is returned through IPC.
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    io::Read,
    ops::{Deref, DerefMut},
    sync::{atomic::{AtomicBool, AtomicU64, Ordering}, Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use url::Url;

const STORE_SERVICE: &str = "com.supymem.desktop.device-auth.v1";
const STORE_USER: &str = "gateway-device";
const MAX_RESPONSE: u64 = 16 * 1024 * 1024;
fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
fn random() -> String {
    format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    )
}
fn urlsafe(value: &str, min: usize, max: usize) -> bool {
    (min..=max).contains(&value.len())
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
}

#[derive(Clone, Serialize)]
pub struct Status {
    /// Public PKCE challenge URL only; never the verifier or a session token.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub login_url: Option<String>,
    pub state: String,
    pub subject: Option<String>,
    pub device_id: Option<String>,
    pub error: Option<String>,
    /// Closed HTTP class for a failed device-session check. Never a response body.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub http_status_class: Option<String>,
    pub persisted: bool,
    pub bridge_connected: bool,
    pub restoring: bool,
}
#[derive(Serialize)]
pub struct Reply {
    pub status: u16,
    pub data: Value,
    #[serde(rename = "requestId", skip_serializing_if = "Option::is_none")]
    pub request_id: Option<String>,
}
impl Reply {
    pub(crate) fn error(status: u16, code: &str) -> Self {
        Self {
            status,
            data: json!({"error":{"code":code,"message":code.replace('_', " ")}}),
            request_id: None,
        }
    }
    pub(crate) fn sent_unconfirmed(code: &str) -> Self {
        Self { status: 503, data: json!({"error":{"code":code,
            "message":"The request was sent, but account verification is still pending. Check its result after account access resumes before repeating a change.",
            "request_sent":true,"automatic_retry_allowed":false}}), request_id: None }
    }
    fn success(&self) -> bool {
        (200..300).contains(&self.status)
    }
}

/// Availability is independent of the credential mutex. macOS may keep a
/// credential-store call open while the founder considers its protected prompt.
#[derive(Default)]
struct StoreActivity {
    pending: AtomicBool,
    epoch: AtomicU64,
    notice_epoch: AtomicU64,
    heartbeat_receipt: AtomicU64,
    reported_busy: AtomicBool,
    tainted: AtomicBool,
    restoring: AtomicBool,
    quiescing: AtomicBool,
    listener: Mutex<Option<Arc<dyn Fn(Status) + Send + Sync>>>,
}
impl StoreActivity {
    fn enter(self: &Arc<Self>) -> StoreAccess {
        self.pending.store(true, Ordering::SeqCst);
        self.epoch.fetch_add(1, Ordering::SeqCst);
        self.emit(waiting_status(true, self.restoring.load(Ordering::SeqCst)));
        StoreAccess(self.clone())
    }
    fn emit(&self, status: Status) {
        let listener = self.listener.lock().ok().and_then(|listener| listener.clone());
        if let Some(listener) = listener { listener(status); }
    }
}
struct StoreAccess(Arc<StoreActivity>);
impl Drop for StoreAccess {
    fn drop(&mut self) { self.0.pending.store(false, Ordering::SeqCst); }
}
fn waiting_status(store: bool, restoring: bool) -> Status {
    Status {
        login_url: None,
        state: if store { "secure_store_pending" } else if restoring { "restoring" } else { "native_auth_busy" }.into(),
        subject: None, device_id: None, persisted: false, bridge_connected: false,
        http_status_class: None,
        restoring,
        error: Some(if store {
            "Waiting for macOS secure credential access. Respond to the Supymem Keychain prompt; the app will continue automatically."
        } else if restoring {
            "Restoring your saved session with Supymem."
        } else {
            "An account request is finishing. Wait for it to complete before starting another action."
        }.into()),
    }
}

pub struct SharedAuth {
    inner: Arc<tokio::sync::Mutex<NativeAuth>>,
    activity: Arc<StoreActivity>,
}
pub(crate) struct AuthQuiescence {
    guard: Option<tokio::sync::OwnedMutexGuard<NativeAuth>>,
    activity: Arc<StoreActivity>,
}
impl Drop for AuthQuiescence {
    fn drop(&mut self) {
        drop(self.guard.take());
        self.activity.quiescing.store(false, Ordering::SeqCst);
    }
}
pub(crate) struct HeartbeatCustody {
    subject: String,
    device_id: String,
    notice_epoch: u64,
    receipt: u64,
}
impl SharedAuth {
    pub fn new(auth: NativeAuth) -> Self {
        Self { activity: auth.activity.clone(), inner: Arc::new(tokio::sync::Mutex::new(auth)) }
    }
    fn access(&self, guard: tokio::sync::OwnedMutexGuard<NativeAuth>) -> Result<AuthAccess, ()> {
        if self.activity.tainted.load(Ordering::SeqCst) || self.activity.quiescing.load(Ordering::SeqCst) { return Err(()); }
        Ok(AuthAccess { before: guard.status().state, error_before: guard.status().error, notice_before: notice_identity(&guard.status()), epoch: self.store_epoch(),
            guard: Some(guard), activity: self.activity.clone(), observation: None })
    }
    pub fn try_lock(&self) -> Result<AuthAccess, ()> {
        self.access(self.inner.clone().try_lock_owned().map_err(|_| ())?)
    }
    /// Async admission preserves parallel UI loads without occupying a worker
    /// for each mutex waiter. No authenticated HTTP is replayed by this wait.
    pub async fn acquire(&self) -> Result<AuthAccess, String> {
        let pending=self.inner.clone().lock_owned();tokio::pin!(pending);
        loop {
            if self.activity.quiescing.load(Ordering::SeqCst) { return Err("native_auth_quiescing".into()); }
            if self.activity.tainted.load(Ordering::SeqCst) { return Err("native_auth_unavailable".into()); }
            tokio::select! {
                guard=&mut pending => return self.access(guard).map_err(|_| "native_auth_unavailable".into()),
                _=tokio::time::sleep(Duration::from_millis(50)) => (),
            }
        }
    }
    pub(crate) async fn acquire_with_timeout(&self, deadline: Duration) -> Result<AuthAccess, String> {
        tokio::time::timeout(deadline, self.acquire()).await
            .map_err(|_| "native_auth_busy".to_string())?
    }
    pub(crate) async fn send_overlay_request(&self, request: PreparedApi,
        admitted: impl FnOnce(&AuthAccess) -> bool) -> Result<(PreparedApi, Result<Reply, NetworkError>), Reply> {
        let auth = self.acquire_with_timeout(Duration::from_secs(2)).await.map_err(|code| {
            let mut reply = Reply::error(503, if code == "native_auth_busy" { "suggestions_send_busy" } else { &code });
            reply.data["error"]["request_sent"] = json!(false);
            reply.data["error"]["automatic_retry_allowed"] = json!(true);
            reply
        })?;
        if !admitted(&auth) { return Err(Reply::error(409, "suggestions_owner_changed")); }
        drop(auth);
        // An account/presentation may retire between this check and HTTP dispatch.
        // Send once with the prepared identity; the caller rechecks custody afterward
        // and returns sent_unconfirmed on retirement, never replaying the request.
        tauri::async_runtime::spawn_blocking(move || {
            let response = request.send(); (request, response)
        }).await.map_err(|_| Reply::sent_unconfirmed("native_transport_unavailable"))
    }
    pub(crate) async fn quiesce(&self) -> Result<AuthQuiescence, String> {
        if self.activity.quiescing.swap(true, Ordering::SeqCst) {
            return Err("An app restart is already in progress.".into());
        }
        let mut lease = AuthQuiescence { guard: None, activity: self.activity.clone() };
        lease.guard = Some(tokio::time::timeout(Duration::from_secs(10), self.inner.clone().lock_owned())
            .await.map_err(|_| "Account credentials are still being saved. Restart was withheld; approve any Keychain prompt and retry.".to_string())?);
        if self.activity.tainted.load(Ordering::SeqCst) {
            return Err("Account credential persistence is unconfirmed. Restart was withheld.".into());
        }
        if lease.guard.as_ref().is_some_and(|auth| auth.persistence_pending || auth.status.state == "keychain_unavailable") {
            return Err("Account credentials could not be saved. Unlock your Keychain and reconnect before restarting.".into());
        }
        Ok(lease)
    }
    pub fn status(&self) -> Status {
        if self.activity.tainted.load(Ordering::SeqCst) { return unavailable_status(); }
        match self.inner.try_lock() {
            Ok(auth) => auth.status(),
            Err(_) => {
                self.activity.reported_busy.store(true, Ordering::SeqCst);
                waiting_status(self.store_pending(), self.activity.restoring.load(Ordering::SeqCst))
            },
        }
    }
    pub fn notice_custody(&self) -> Option<(String, String, u64)> {
        if self.activity.tainted.load(Ordering::SeqCst) { return None; }
        let auth = self.inner.try_lock().ok()?;
        let status = auth.status();
        if status.state != "connected" || !status.bridge_connected { return None; }
        Some((status.subject?, status.device_id?, self.activity.notice_epoch.load(Ordering::SeqCst)))
    }
    /// Bind a private heartbeat before sending, without loading credentials or starting sign-in.
    pub(crate) fn heartbeat_custody(&self) -> Option<HeartbeatCustody> {
        if self.activity.tainted.load(Ordering::SeqCst) { return None; }
        let auth = self.inner.try_lock().ok()?;
        let (subject, device_id) = auth.heartbeat_identity()?;
        Some(HeartbeatCustody { subject, device_id,
            notice_epoch: self.activity.notice_epoch.load(Ordering::SeqCst),
            receipt: self.activity.heartbeat_receipt.fetch_add(1, Ordering::SeqCst).wrapping_add(1) })
    }
    /// Admit one current 401 repair; the auth lease stays held through private bridge synchronization.
    pub(crate) fn renew_heartbeat(&self, expected: &HeartbeatCustody, sync: impl FnOnce(&mut NativeAuth)) -> bool {
        let Ok(mut auth) = self.try_lock() else { return false; };
        if self.activity.notice_epoch.load(Ordering::SeqCst) != expected.notice_epoch
            || auth.heartbeat_identity().as_ref() != Some(&(expected.subject.clone(), expected.device_id.clone()))
            || self.activity.heartbeat_receipt.compare_exchange(expected.receipt,
                expected.receipt.wrapping_add(1), Ordering::SeqCst, Ordering::SeqCst).is_err() {
            return false;
        }
        // The private helper rejected its session. A cached matching token does not prove it is still connected.
        auth.bridge_access = None;
        auth.set_bridge_connected(false);
        let _ = auth.request_identity();
        sync(&mut auth); // Failed/uncertain admission clears the handoff through the existing sync policy.
        true
    }
    /// Retry only the saved session's refresh or verification. Feature operations
    /// are never replayed, and no new credential-store read is started here.
    pub(crate) fn retry_refresh(&self, sync: impl FnOnce(&mut NativeAuth)) -> bool {
        let Ok(mut auth) = self.try_lock() else { return false; };
        if !auth.refresh_retry_due() { return false; }
        let _ = auth.request_identity();
        sync(&mut auth);
        true
    }
    /// The generation changes only on actual identity/state retirement, not
    /// while an unchanged account's credential mutex is temporarily occupied.
    pub fn notice_generation(&self) -> u64 { self.activity.notice_epoch.load(Ordering::SeqCst) }
    /// Account-qualified dispatch. Permission preparation and main-thread waits
    /// happen before admission; the lease lasts through the callback's quick
    /// UI/registration operation. Network sends use send_overlay_request instead.
    pub fn with_notice_custody<R>(&self, expected: &(String, String, u64), dispatch: impl FnOnce() -> R) -> Option<R> {
        let auth = self.try_lock().ok()?;
        let status = auth.status();
        if status.state != "connected" || !status.bridge_connected
            || status.subject.as_ref() != Some(&expected.0)
            || status.device_id.as_ref() != Some(&expected.1)
            || self.activity.notice_epoch.load(Ordering::SeqCst) != expected.2 { return None; }
        // Keep the credential mutex through the source-owned registration.
        // A concurrent account transition cannot pass between check and UI dispatch.
        let result = dispatch();
        drop(auth);
        Some(result)
    }
    pub fn store_pending(&self) -> bool { self.activity.pending.load(Ordering::SeqCst) }
    pub fn store_epoch(&self) -> u64 { self.activity.epoch.load(Ordering::SeqCst) }
    pub fn busy_code(&self) -> &'static str {
        self.activity.reported_busy.store(true, Ordering::SeqCst);
        if self.store_pending() { "secure_store_pending" } else { "native_auth_busy" }
    }
    pub fn set_listener(&self, listener: impl Fn(Status) + Send + Sync + 'static) {
        if let Ok(mut slot) = self.activity.listener.lock() { *slot = Some(Arc::new(listener)); }
    }
}

fn unavailable_status() -> Status {
    Status { login_url: None, state: "restoring".into(), subject: None, device_id: None,
        error: Some("Native account access is unavailable. Quit and reopen Supymem before reconnecting.".into()), http_status_class: None,
        persisted: false, bridge_connected: false, restoring: true }
}
pub struct AuthAccess {
    guard: Option<tokio::sync::OwnedMutexGuard<NativeAuth>>,
    activity: Arc<StoreActivity>,
    before: String,
    notice_before: (String, Option<String>, Option<String>, bool),
    error_before: Option<String>,
    epoch: u64,
    observation: Option<Observation>,
}
#[derive(Clone, Default)]
pub struct Observation(Arc<AtomicBool>);
impl AuthAccess {
    pub fn observe(&mut self, observation: &Observation) {
        observation.0.store(true, Ordering::SeqCst);
        self.observation=Some(observation.clone());
    }
    /// Read custody while this admission already owns the credential mutex.
    /// Calling SharedAuth's try-lock getter here would recursively fail.
    pub fn notice_custody(&self) -> Option<(String, String, u64)> {
        let status = self.status();
        if self.activity.tainted.load(Ordering::SeqCst)
            || status.state != "connected" || !status.bridge_connected { return None; }
        Some((status.subject?, status.device_id?, self.activity.notice_epoch.load(Ordering::SeqCst)))
    }
}
impl Deref for AuthAccess {
    type Target = NativeAuth;
    fn deref(&self) -> &Self::Target { self.guard.as_deref().expect("live auth access") }
}
impl DerefMut for AuthAccess {
    fn deref_mut(&mut self) -> &mut Self::Target { self.guard.as_deref_mut().expect("live auth access") }
}
fn notice_identity(status: &Status) -> (String, Option<String>, Option<String>, bool) {
    (status.state.clone(), status.subject.clone(), status.device_id.clone(), status.bridge_connected)
}
impl Drop for AuthAccess {
    fn drop(&mut self) {
        let guard=self.guard.take().expect("live auth access");
        let panicked=std::thread::panicking();
        if panicked { self.activity.tainted.store(true,Ordering::SeqCst); }
        let status=if panicked { unavailable_status() } else { guard.status() };
        let reported=self.activity.reported_busy.swap(false, Ordering::SeqCst);
        let notice_changed = self.notice_before != notice_identity(&status);
        let notify=panicked || reported || notice_changed || self.error_before!=status.error || self.before!=status.state || self.epoch!=self.activity.epoch.load(Ordering::SeqCst);
        if panicked || notice_changed {
            self.activity.notice_epoch.fetch_add(1, Ordering::SeqCst);
        }
        drop(guard);
        if let Some(observation)=self.observation.take() { observation.0.store(false, Ordering::SeqCst); }
        // Publish only after the mutex is released: a connected listener may
        // immediately fetch its session. Ordinary successful API calls emit no
        // event, avoiding connected → session-check → connected loops.
        if notify { self.activity.emit(status); }
    }
}

/// Dropping the join handle detaches, rather than cancels, the single authorized
/// OS call. Its worker retains the mutex until it finishes and emits completion.
/// Interactive account commands return pending; HTTP callers await the worker
/// and share its loaded, refreshed and verified session through acquire().
pub async fn await_store<T>(task: tauri::async_runtime::JoinHandle<T>, shared: &SharedAuth, observation: &Observation) -> Result<T, String> {
    tokio::pin!(task);
    loop {
        tokio::select! {
            result = &mut task => return result.map_err(|_| "native_auth_unavailable".into()),
            _ = tokio::time::sleep(Duration::from_millis(500)) => {
                if observation.0.load(Ordering::SeqCst) && shared.store_pending() { return Err("secure_store_pending".into()); }
            }
        }
    }
}
#[derive(Clone, Serialize, Deserialize)]
struct Session {
    gateway: String,
    subject: String,
    device_id: String,
    access_token: String,
    refresh_token: String,
    expires_at: u64,
    #[serde(default)]
    refresh_in_flight: bool,
    #[serde(default)]
    revocation_pending: bool,
}
#[derive(Clone, Serialize, Deserialize)]
struct Pending {
    gateway: String,
    verifier: String,
    state: String,
    created: u64,
    expected_subject: Option<String>,
}
#[derive(Default, Serialize, Deserialize)]
struct Record {
    session: Option<Session>,
    pending: Option<Pending>,
    /// Retain the non-secret account identity across credential retirement.
    /// A later browser approval must match it unless the founder
    /// explicitly signs out this Mac.
    #[serde(default)]
    last_subject: Option<String>,
}

pub trait Vault: Send {
    fn is_persistent(&self) -> bool { true }
    fn load(&self) -> Result<Option<String>, ()>;
    fn save(&self, value: &str) -> Result<(), ()>;
    fn clear(&self) -> Result<(), ()>;
}
/// Explicit staging acceptance storage. The gateway still authenticates every
/// session; dropping this native instance discards its saved credentials.
#[derive(Default)]
struct SessionVault(Mutex<Option<String>>);
impl Vault for SessionVault {
    fn is_persistent(&self) -> bool { false }
    fn load(&self) -> Result<Option<String>, ()> {
        self.0.lock().map(|value| value.clone()).map_err(|_| ())
    }
    fn save(&self, value: &str) -> Result<(), ()> {
        *self.0.lock().map_err(|_| ())? = Some(value.to_owned());
        Ok(())
    }
    fn clear(&self) -> Result<(), ()> {
        *self.0.lock().map_err(|_| ())? = None;
        Ok(())
    }
}
pub struct OsVault {
    gateway: String,
}
impl OsVault {
    fn entry(&self) -> Result<keyring::Entry, ()> {
        // keyring's unsupported-platform default is a mock. Never use that as storage.
        if !cfg!(any(target_os = "macos", target_os = "windows")) {
            return Err(());
        }
        let profile = URL_SAFE_NO_PAD.encode(Sha256::digest(self.gateway.as_bytes()));
        keyring::Entry::new(STORE_SERVICE, &format!("{STORE_USER}-{profile}")).map_err(|_| ())
    }
}
impl Vault for OsVault {
    fn load(&self) -> Result<Option<String>, ()> {
        match self.entry()?.get_password() {
            Ok(value) => Ok(Some(value)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(_) => Err(()),
        }
    }
    fn save(&self, value: &str) -> Result<(), ()> {
        self.entry()?.set_password(value).map_err(|_| ())
    }
    fn clear(&self) -> Result<(), ()> {
        match self.entry()?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(_) => Err(()),
        }
    }
}
#[derive(Clone, Copy)]
pub enum NetworkError {
    NotSent,
    Uncertain,
}
pub trait Http: Send + Sync {
    fn send(
        &self,
        origin: &str,
        method: &str,
        path: &str,
        bearer: Option<&str>,
        body: Option<&Value>,
        headers: &HashMap<String, String>,
    ) -> Result<Reply, NetworkError>;
}
pub struct NativeHttp;
fn native_request_timeout(method: &str, path: &str) -> Duration {
    // A chat turn may include a live tool call before Core returns. Keep the
    // response window longer than the normal API window so a completed turn
    // is not reported as an uncertain send and then duplicated by a retry.
    let parts: Vec<_> = path.split('?').next().unwrap_or("").split('/').collect();
    if method == "POST"
        && matches!(parts.as_slice(), ["", "api", "ventures", venture, "chat"]
            if venture.starts_with("ven_") && venture.len() > 4)
    {
        Duration::from_secs(60)
    } else {
        Duration::from_secs(15)
    }
}
impl Http for NativeHttp {
    fn send(
        &self,
        origin: &str,
        method: &str,
        path: &str,
        bearer: Option<&str>,
        body: Option<&Value>,
        headers: &HashMap<String, String>,
    ) -> Result<Reply, NetworkError> {
        let client = reqwest::blocking::Client::builder().user_agent(crate::http::USER_AGENT)
            .timeout(native_request_timeout(method, path))
            .redirect(reqwest::redirect::Policy::none())
            .no_proxy()
            .build()
            .map_err(|_| NetworkError::NotSent)?;
        let method =
            reqwest::Method::from_bytes(method.as_bytes()).map_err(|_| NetworkError::NotSent)?;
        let mut request = client.request(method, format!("{origin}{path}"));
        if let Some(token) = bearer {
            request = request.bearer_auth(token);
        }
        if let Some(value) = body {
            request = request.json(value);
        }
        for (name, value) in headers {
            request = request.header(name, value);
        }
        let response = request.send().map_err(|e| {
            if e.is_connect() {
                NetworkError::NotSent
            } else {
                NetworkError::Uncertain
            }
        })?;
        let status = response.status().as_u16();
        let retry_after = response.headers().get("retry-after")
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.parse::<u64>().ok());
        let request_id = response.headers().get("x-request-id")
            .and_then(|value| value.to_str().ok())
            .filter(|value| value.len() <= 64)
            .map(str::to_owned);
        let mut bytes = Vec::new();
        response
            .take(MAX_RESPONSE + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| NetworkError::Uncertain)?;
        if bytes.len() as u64 > MAX_RESPONSE {
            return Ok(Reply::error(502, "response_too_large"));
        }
        if status == 429 {
            return Ok(Reply { status, data: json!({"error":{"code":"rate_limited",
                "retry_after":retry_after.or_else(|| serde_json::from_slice::<Value>(&bytes).ok()
                    .and_then(|data| data.pointer("/error/retry_after").or_else(|| data.pointer("/detail/retry_after")).and_then(Value::as_u64)))
                    .unwrap_or(60).clamp(1, 86400)}}), request_id });
        }
        let data = if bytes.is_empty() {
            Value::Null
        } else {
            match serde_json::from_slice(&bytes) {
                Ok(data) => data,
                Err(_) => return Ok(Reply::error(502, "non_json_response")),
            }
        };
        Ok(Reply { status, data, request_id })
    }
}

#[derive(Clone)]
pub(crate) struct RequestIdentity {
    pub gateway: String,
    pub access_token: String,
    pub subject: String,
    pub device_id: String,
    pub expires_at: u64,
}

pub(crate) struct PreparedApi {
    identity: RequestIdentity,
    http: Arc<dyn Http>,
    method: String,
    path: String,
    body: Option<Value>,
    headers: HashMap<String,String>,
}
impl PreparedApi {
    pub(crate) fn send(&self) -> Result<Reply,NetworkError> {
        self.http.send(&self.identity.gateway,&self.method,&self.path,Some(&self.identity.access_token),self.body.as_ref(),&self.headers)
    }
}

pub struct NativeAuth {
    gateway: String,
    vault: Box<dyn Vault>,
    http: Arc<dyn Http>,
    record: Record,
    loaded: bool,
    persistence_pending: bool,
    verified_at: u64,
    refresh_retry_at: u64,
    refresh_retry_attempts: u32,
    status: Status,
    activity: Arc<StoreActivity>,
    pub(crate) bridge_access: Option<String>,
}
fn valid_device_subject(subject: &str, gateway: &str, packaged_local_review: bool) -> bool {
    (subject.starts_with("usr_") && subject.len() <= 128)
        || (packaged_local_review && gateway == "http://127.0.0.1:3200" && subject == "founder")
}

impl NativeAuth {
    fn valid_subject(&self, subject: &str) -> bool {
        valid_device_subject(subject, &self.gateway, crate::channel::packaged_local_review())
    }
    pub fn new(gateway: String) -> Self {
        let vault: Box<dyn Vault> = if crate::channel::session_only() {
            Box::new(SessionVault::default())
        } else {
            Box::new(OsVault { gateway: gateway.clone() })
        };
        Self::with_dependencies(gateway, vault, Box::new(NativeHttp))
    }
    fn with_dependencies(gateway: String, vault: Box<dyn Vault>, http: Box<dyn Http>) -> Self {
        Self {
            gateway,
            vault,
            http: Arc::from(http),
            record: Record::default(),
            loaded: false,
            persistence_pending: false,
            verified_at: 0,
            refresh_retry_at: 0,
            refresh_retry_attempts: 0,
            status: Status {
                login_url: None,
                state: "restoring".into(),
                subject: None,
                device_id: None,
                error: None,
                http_status_class: None,
                persisted: false,
                bridge_connected: false,
                restoring: true,
            },
            bridge_access: None,
            activity: Arc::new(StoreActivity { restoring: AtomicBool::new(true), ..StoreActivity::default() }),
        }
    }
    pub(crate) fn request_identity(&mut self) -> Result<RequestIdentity, Reply> {
        self.ensure(None)?;
        let s = self
            .record
            .session
            .as_ref()
            .ok_or_else(|| Reply::error(401, "signed_out"))?;
        Ok(RequestIdentity {
            gateway: s.gateway.clone(),
            access_token: s.access_token.clone(),
            subject: s.subject.clone(),
            device_id: s.device_id.clone(),
            expires_at: s.expires_at,
        })
    }
    fn heartbeat_identity(&self) -> Option<(String, String)> {
        let session = self.record.session.as_ref()?;
        if !matches!(self.status.state.as_str(), "connected" | "offline")
            || session.gateway != self.gateway || session.revocation_pending || session.refresh_in_flight
            || self.status.subject.as_ref() != Some(&session.subject)
            || self.status.device_id.as_ref() != Some(&session.device_id) { return None; }
        Some((session.subject.clone(), session.device_id.clone()))
    }
    pub(crate) fn matches_identity(&self, identity: &RequestIdentity) -> bool {
        matches!(self.status.state.as_str(), "connected" | "offline" | "keychain_unavailable")
            && self.record.session.as_ref().is_some_and(|s| {
                s.gateway == self.gateway && s.gateway == identity.gateway
                    && s.subject == identity.subject
                    && s.device_id == identity.device_id
                    && !s.revocation_pending
            })
    }
    pub(crate) fn can_retry_request(method: &str, headers: &HashMap<String, String>) -> bool {
        matches!(method, "GET" | "HEAD") || headers.iter().any(|(name, value)| {
            name.eq_ignore_ascii_case("Idempotency-Key") && !value.trim().is_empty()
        })
    }
    /// A feature endpoint's 401 does not prove that the device bearer is
    /// revoked. Confirm it with the gateway before changing account state.
    pub(crate) fn confirm_feature_unauthorized(&mut self, identity: &RequestIdentity) -> Result<(), Reply> {
        if !self.matches_identity(identity) {
            return Err(Reply::error(401, "account_changed_during_request"));
        }
        if !self.record.session.as_ref().is_some_and(|s| s.access_token == identity.access_token) {
            return Ok(());
        }
        match self.verify() {
            Ok(()) => Ok(()),
            Err(reply) if self.status.state == "reauth_required" || self.status.state == "account_mismatch" => Err(reply),
            Err(_) => Err(Reply::error(503, "session_verification_failed")),
        }
    }
    pub fn status(&self) -> Status {
        let mut status = self.status.clone();
        status.restoring = self.activity.restoring.load(Ordering::SeqCst);
        status.login_url = if status.state == "authorizing" { self.login_url() } else { None };
        status
    }
    fn login_url(&self) -> Option<String> {
        let pending = self.record.pending.as_ref()?;
        if now().saturating_sub(pending.created) > 300 || pending.gateway != self.gateway { return None; }
        let challenge = URL_SAFE_NO_PAD.encode(Sha256::digest(pending.verifier.as_bytes()));
        let mut url = Url::parse(&format!("{}/auth/device/approve", self.gateway)).ok()?;
        url.query_pairs_mut()
            .append_pair("code_challenge", &challenge)
            .append_pair("state", &pending.state)
            .append_pair("device_name", "Supymem for Mac")
            .append_pair("platform", "macos");
        Some(url.to_string())
    }
    fn state(&mut self, state: &str, error: Option<&str>) {
        if state == "restoring" {
            self.activity.restoring.store(true, Ordering::SeqCst);
        } else if matches!(state, "connected" | "signed_out" | "authorizing" | "reauth_required" | "account_mismatch" | "revocation_pending") {
            self.activity.restoring.store(false, Ordering::SeqCst);
        }
        self.status.state = state.into();
        self.status.error = error.map(str::to_owned);
        self.status.http_status_class = None;
        self.status.subject = self.record.session.as_ref().map(|s| s.subject.clone())
            .or_else(|| self.record.last_subject.clone());
        self.status.device_id = self.record.session.as_ref().map(|s| s.device_id.clone());
        if state != "connected" {
            self.status.bridge_connected = false;
        }
    }
    fn persist(&mut self) -> Result<(), Reply> {
        self.persistence_pending = true;
        let access = self.vault.is_persistent().then(|| self.activity.enter());
        let result = if self.record.session.is_none() && self.record.pending.is_none()
            && self.record.last_subject.is_none() {
            self.vault.clear()
        } else {
            self.vault.save(
                &serde_json::to_string(&self.record)
                    .map_err(|_| Reply::error(503, "credential_store_failed"))?,
            )
        };
        drop(access);
        if result.is_err() {
            self.state(
                "keychain_unavailable",
                Some("Secure credential storage is unavailable. Unlock your Keychain and retry."),
            );
            self.status.persisted = false;
            return Err(Reply::error(503, "keychain_unavailable"));
        }
        self.status.persisted = self.vault.is_persistent()
            && (self.record.session.is_some() || self.record.pending.is_some() || self.record.last_subject.is_some());
        self.persistence_pending = false;
        Ok(())
    }
    fn load(&mut self) -> Result<(), Reply> {
        if self.loaded {
            return Ok(());
        }
        if !valid_origin(&self.gateway) {
            self.state("reauth_required", Some("invalid_gateway_origin"));
            return Err(Reply::error(400, "invalid_gateway_origin"));
        }
        let access = self.vault.is_persistent().then(|| self.activity.enter());
        let loaded = self.vault.load();
        drop(access);
        match loaded {
            Ok(Some(raw)) => match serde_json::from_str::<Record>(&raw) {
                Ok(record) => self.record = record,
                Err(_) => {
                    // A corrupt record cannot authorize anything. Clear it so
                    // an explicit new sign-in can recover without manual edits.
                    self.record = Record::default();
                    self.loaded = true;
                    self.persist()?;
                    self.state("reauth_required", Some("invalid_saved_credentials"));
                    return Err(Reply::error(401, "invalid_saved_credentials"));
                }
            },
            Ok(None) => (),
            Err(_) => {
                self.state(
                    "keychain_unavailable",
                    Some("Unlock your Keychain to reconnect."),
                );
                return Err(Reply::error(503, "keychain_unavailable"));
            }
        }
        self.loaded = true;
        self.status.persisted = self.vault.is_persistent()
            && (self.record.session.is_some() || self.record.pending.is_some() || self.record.last_subject.is_some());
        if self
            .record
            .session
            .as_ref()
            .is_some_and(|s| s.gateway != self.gateway)
        {
            self.state(
                "account_mismatch",
                Some("Saved credentials belong to a different gateway."),
            );
            return Err(Reply::error(401, "gateway_mismatch"));
        }
        if self.record.pending.is_some() {
            self.state("authorizing", None);
        } else if self.record.session.is_some() {
            self.state("restoring", None);
        } else if self.record.last_subject.is_some() {
            self.state("reauth_required", None);
        } else {
            self.state("signed_out", None);
        }
        Ok(())
    }
    fn disable(&mut self, state: &str, code: &str) -> Reply {
        if let Some(session) = self.record.session.as_ref() {
            self.record.last_subject = Some(session.subject.clone());
        }
        self.record.session = None;
        self.verified_at = 0;
        if let Err(error) = self.persist() {
            return error;
        }
        let message = if state == "account_mismatch" {
            "Supymem could not verify the saved account. Sign in to the intended account and reconnect."
        } else {
            "Your Supymem session has expired or was disconnected. Sign in again to reconnect."
        };
        self.state(state, Some(message));
        Reply::error(401, code)
    }
    pub fn start(&mut self, expected: Option<String>) -> Result<String, Reply> {
        self.load()?;
        let expected = expected.or_else(|| self.record.last_subject.clone());
        if expected.as_ref().is_some_and(|s| !self.valid_subject(s))
        {
            return Err(Reply::error(400, "invalid_expected_subject"));
        }
        if self.record.session.is_some() {
            return Err(Reply::error(
                409,
                "sign_out_before_connecting_another_account",
            ));
        }
        let verifier = random();
        let state = random();
        self.record.pending = Some(Pending {
            gateway: self.gateway.clone(),
            verifier: verifier.clone(),
            state: state.clone(),
            created: now(),
            expected_subject: expected,
        });
        self.persist()?;
        self.state("authorizing", None);
        self.login_url().ok_or_else(|| Reply::error(400, "invalid_gateway_origin"))
    }
    pub fn browser_failed(&mut self) {
        // Keep the pending challenge valid so the founder can copy its link.
        self.state("authorizing", Some("The browser could not open. Copy the sign-in link and open it in your browser."));
    }
    pub fn complete_with_code(&mut self, code: &str) -> Result<(), Reply> {
        // Manual fallback when the browser cannot deliver a custom-scheme URL.
        // The code alone has no authority: the existing PKCE verifier and
        // pending state are still checked and consumed by callback().
        if !urlsafe(code, 16, 256) {
            return Err(Reply::error(400, "invalid_auth_code"));
        }
        self.load()?;
        let pending = self.record.pending.as_ref()
            .ok_or_else(|| Reply::error(401, "no_pending_authorization"))?;
        let raw = format!("{}://auth/callback?code={code}&state={}",
                          crate::channel::callback_scheme(), pending.state);
        self.callback(&raw)
    }
    pub fn callback(&mut self, raw: &str) -> Result<(), Reply> {
        self.load()?;
        let (code, state) =
            callback_values(raw).ok_or_else(|| Reply::error(400, "invalid_auth_callback"))?;
        let pending = self
            .record
            .pending
            .clone()
            .ok_or_else(|| Reply::error(401, "no_pending_authorization"))?;
        if pending.gateway != self.gateway || !constant_time_eq(&state, &pending.state) {
            return Err(Reply::error(401, "callback_state_mismatch"));
        }
        // Consume the attempt durably before exchange. A crash or lost response
        // cannot cause a previously used authorization code to be replayed.
        self.record.pending = None;
        self.persist()?;
        if now().saturating_sub(pending.created) > 300 || pending.created > now() + 5 {
            self.state(
                "reauth_required",
                Some("Authorization expired. Connect again."),
            );
            return Err(Reply::error(401, "authorization_expired"));
        }
        self.state("connecting", None);
        let response = self
            .http
            .send(
                &self.gateway,
                "POST",
                "/auth/device/token",
                None,
                Some(&json!({"code":code,"code_verifier":pending.verifier})),
                &HashMap::new(),
            )
            .map_err(|_| {
                self.state(
                    "reauth_required",
                    Some("Authorization response was not received. Connect again."),
                );
                Reply::error(0, "authorization_outcome_unknown")
            })?;
        if !response.success() {
            self.state(
                "reauth_required",
                Some("Authorization was rejected. Connect again."),
            );
            return Err(Reply::error(401, "authorization_rejected"));
        }
        let session = self.parse_tokens(&response.data).ok_or_else(|| {
            self.state("reauth_required", Some("Invalid authorization response."));
            Reply::error(502, "invalid_device_token_response")
        })?;
        if pending
            .expected_subject
            .as_ref()
            .is_some_and(|s| s != &session.subject)
        {
            self.revoke_best_effort(&session);
            // Preserve the account the founder intended to reconnect.  The
            // rejected browser identity must never become the new default.
            self.record.last_subject = pending.expected_subject.clone();
            self.persist()?;
            self.state("account_mismatch",Some("The browser approved a different account. Sign in to the intended account and reconnect."));
            return Err(Reply::error(401, "account_mismatch"));
        }
        self.record.session = Some(session);
        self.record.last_subject = self.record.session.as_ref().map(|s| s.subject.clone());
        self.persist()?;
        self.verify()?;
        Ok(())
    }
    fn parse_tokens(&self, data: &Value) -> Option<Session> {
        let text = |key: &str| {
            data.get(key)?
                .as_str()
                .filter(|v| !v.is_empty() && v.len() <= 16384)
                .map(str::to_owned)
        };
        let subject = text("subject")?;
        let device = text("device_id")?;
        let expires = data.get("expires_in")?.as_u64()?;
        if !self.valid_subject(&subject)
            || device.len() > 128
            || !(1..=3600).contains(&expires)
        {
            return None;
        }
        Some(Session {
            gateway: self.gateway.clone(),
            subject,
            device_id: device,
            access_token: text("access_token")?,
            refresh_token: text("refresh_token")?,
            expires_at: now() + expires,
            refresh_in_flight: false,
            revocation_pending: false,
        })
    }
    fn revoke_best_effort(&self, s: &Session) {
        let _ = self.http.send(
            &self.gateway,
            "POST",
            "/auth/device/revoke",
            None,
            Some(&json!({"device_id":s.device_id,"refresh_token":s.refresh_token})),
            &HashMap::new(),
        );
    }
    fn verify(&mut self) -> Result<(), Reply> {
        self.verify_session(true)
    }
    pub(crate) fn verify_session(&mut self, allow_refresh: bool) -> Result<(), Reply> {
        if self.status.state == "offline" && now() < self.refresh_retry_at {
            return Err(Reply::error(503, "gateway_unreachable"));
        }
        let s = self
            .record
            .session
            .clone()
            .ok_or_else(|| Reply::error(401, "signed_out"))?;
        let response = self
            .http
            .send(
                &self.gateway,
                "GET",
                "/auth/session",
                Some(&s.access_token),
                None,
                &HashMap::new(),
            )
            .map_err(|_| self.refresh_offline(0, None))?;
        if response.status == 401 {
            if !allow_refresh { return Err(self.refresh_offline(503, Some("4xx"))); }
            self.refresh()?;
            return self.verify_session(false);
        }
        // A 403 can be an access policy or edge-layer denial. It is not proof
        // that the device bearer was revoked; retain it, but admit no work
        // until a later /auth/session check succeeds for the same subject.
        if !response.success() {
            let class = match response.status {
                400..=499 => "4xx", 500..=599 => "5xx", _ => "other",
            };
            self.refresh_offline(response.status, Some(class));
            return Err(Reply::error(response.status, "session_verification_failed"));
        }
        if response.data.get("authenticated") == Some(&Value::Bool(false)) {
            if !allow_refresh { return Err(self.refresh_offline(503, Some("other"))); }
            self.refresh()?;
            return self.verify_session(false);
        }
        if response.data.get("authenticated") != Some(&Value::Bool(true))
            || response.data.get("subject").and_then(Value::as_str) != Some(s.subject.as_str())
        {
            return Err(self.disable("account_mismatch", "session_identity_mismatch"));
        }
        self.refresh_retry_at = 0;
        self.refresh_retry_attempts = 0;
        self.verified_at = now();
        self.state("connected", None);
        Ok(())
    }
    fn refresh_retry_due(&self) -> bool {
        self.loaded && self.status.state == "offline" && self.refresh_retry_at > 0
            && now() >= self.refresh_retry_at
            && self.record.session.as_ref().is_some_and(|session| {
                session.gateway == self.gateway && !session.revocation_pending
            })
    }
    fn refresh_offline(&mut self, status: u16, class: Option<&str>) -> Reply {
        self.refresh_retry_attempts = self.refresh_retry_attempts.saturating_add(1).min(6);
        self.refresh_retry_at = now() + (1u64 << (self.refresh_retry_attempts - 1)).min(30);
        self.state("offline", Some("Reconnecting to Supymem. Retry when connected."));
        self.status.http_status_class = class.map(str::to_owned);
        Reply::error(status, "gateway_unreachable")
    }
    fn refresh(&mut self) -> Result<(), Reply> {
        let s = self.record.session.clone().ok_or_else(|| Reply::error(401, "signed_out"))?;
        if now() < self.refresh_retry_at {
            return Err(Reply::error(503, "gateway_unreachable"));
        }
        self.record.session.as_mut().unwrap().refresh_in_flight = true;
        self.persist()?;
        let response = self.http.send(
            &self.gateway, "POST", "/auth/device/refresh", None,
            Some(&json!({"device_id":s.device_id,"refresh_token":s.refresh_token})),
            &HashMap::new(),
        );
        let response = match response {
            Ok(response) => response,
            Err(NetworkError::NotSent) => {
                self.record.session.as_mut().unwrap().refresh_in_flight = s.refresh_in_flight;
                self.persist()?;
                return Err(self.refresh_offline(0, None));
            }
            Err(NetworkError::Uncertain) => return Err(self.refresh_offline(503, None)),
        };
        if !response.success() {
            let class = match response.status {
                400..=499 => "4xx", 500..=599 => "5xx", _ => "other",
            };
            let code = response.data.pointer("/error/code").and_then(Value::as_str);
            if response.status == 401 && matches!(code, Some("invalid_refresh" | "token_reuse" | "revoked" | "unknown_device")) {
                let rejected = self.disable("reauth_required", "device_revoked_or_expired");
                self.status.http_status_class = Some(class.into());
                return Err(rejected);
            }
            if response.status == 429 {
                let reply = self.refresh_offline(503, Some(class));
                let retry = response.data.pointer("/error/retry_after")
                    .or_else(|| response.data.pointer("/detail/retry_after"))
                    .and_then(Value::as_u64).unwrap_or(60).clamp(1, 86400);
                self.record.session.as_mut().unwrap().refresh_in_flight = s.refresh_in_flight;
                self.persist()?;
                self.refresh_retry_at = now() + retry;
                return Err(reply);
            }
            return Err(self.refresh_offline(503, Some(class)));
        }
        let Some(new) = self.parse_tokens(&response.data) else {
            return Err(self.refresh_offline(503, Some("other")));
        };
        if new.subject != s.subject || new.device_id != s.device_id {
            self.revoke_best_effort(&new);
            return Err(self.disable("account_mismatch", "refresh_identity_mismatch"));
        }
        // Backoff resets only when verification succeeds; resetting here let a
        // persistent authenticated:false rotate the refresh token every second.
        self.record.session = Some(new);
        self.record.last_subject = self.record.session.as_ref().map(|session| session.subject.clone());
        self.persist()?;
        self.verified_at = 0;
        Ok(())
    }
    fn ensure(&mut self, expected: Option<&str>) -> Result<(), Reply> {
        self.load()?;
        let s = self
            .record
            .session
            .clone()
            .ok_or_else(|| Reply::error(401, "signed_out"))?;
        if s.gateway != self.gateway {
            self.state(
                "account_mismatch",
                Some("Saved credentials belong to a different gateway."),
            );
            return Err(Reply::error(401, "gateway_mismatch"));
        }
        if self.status.state == "account_mismatch" && expected.is_none() {
            return Err(Reply::error(401, "account_mismatch"));
        }
        if expected.is_some_and(|e| e != s.subject) {
            self.state(
                "account_mismatch",
                Some("This Mac is connected to another account. Sign out before switching."),
            );
            return Err(Reply::error(401, "account_mismatch"));
        }
        if s.revocation_pending {
            self.state(
                "revocation_pending",
                Some("Signed out on this Mac. Reconnect to finish revoking this device."),
            );
            return Err(Reply::error(401, "revocation_pending"));
        }
        if self.persistence_pending || self.status.state == "keychain_unavailable" {
            self.persist()?;
        }
        let refreshed = s.refresh_in_flight || s.expires_at <= now() + 30;
        if refreshed {
            self.refresh()?;
        }
        if self.verified_at == 0
            || now().saturating_sub(self.verified_at) >= 60
            || self.status.state != "connected"
        {
            self.verify_session(!refreshed)?;
        }
        Ok(())
    }
    pub fn reconnect(&mut self, expected: Option<&str>) -> Status {
        // A denied or failed protected-store read is one completed attempt.
        // Do not fall through to ensure(), which would immediately prompt again.
        if self.load().is_err() { return self.status(); }
        if self
                .record
                .session
                .as_ref()
                .is_some_and(|s| s.revocation_pending)
        {
            self.sign_out();
        } else {
            let _ = self.ensure(expected);
        }
        self.status()
    }
    pub(crate) fn prepare_sign_out(&mut self) -> bool {
        if self.load().is_err() {
            return false;
        }
        if self
            .record
            .session
            .as_ref()
            .is_some_and(|s| s.gateway != self.gateway)
        {
            self.state("account_mismatch", Some("Disconnect this saved device from its original gateway before switching environments."));
            return false;
        }
        self.record.pending = None;
        self.verified_at = 0;
        if let Some(s) = self.record.session.as_mut() {
            s.revocation_pending = true;
        }
        self.state("revocation_pending", None);
        if self.persist().is_err() {
            return false;
        }
        true
    }
    pub fn sign_out(&mut self) -> Status {
        if !self.prepare_sign_out() {
            return self.status();
        }
        if let Some(s) = self.record.session.clone() {
            let response = self.http.send(
                &self.gateway,
                "POST",
                "/auth/device/revoke",
                None,
                Some(&json!({"device_id":s.device_id,"refresh_token":s.refresh_token})),
                &HashMap::new(),
            );
            if !response.as_ref().is_ok_and(|r| r.success()) {
                self.state("revocation_pending",Some("Signed out on this Mac. Server revocation is not confirmed; retry when connected."));
                return self.status();
            }
        }
        self.record = Record::default();
        if self.persist().is_ok() {
            self.state("signed_out", None);
        }
        self.status()
    }
    pub fn api(
        &mut self,
        method: &str,
        path: &str,
        body: Option<Value>,
        headers: HashMap<String, String>,
    ) -> Reply {
        let request=match self.prepare_api(method,path,body,headers) { Ok(request)=>request, Err(reply)=>return reply };
        let response=request.send();
        self.complete_api(&request,response)
    }
    pub(crate) fn prepare_api(&mut self, method:&str, path:&str, body:Option<Value>, headers:HashMap<String,String>) -> Result<PreparedApi,Reply> {
        if !allowed_request(method, path, &headers) {
            return Err(Reply::error(400, "native_request_denied"));
        }
        if body
            .as_ref()
            .is_some_and(|b| b.to_string().len() > 8 * 1024 * 1024)
        {
            return Err(Reply::error(413, "request_too_large"));
        }
        let identity=self.request_identity()?;
        Ok(PreparedApi { identity,http:self.http.clone(),method:method.into(),path:path.into(),body,headers })
    }
    pub(crate) fn complete_api(&mut self, request:&PreparedApi, response:Result<Reply,NetworkError>) -> Reply {
        self.complete_api_attempt(request, response, true)
    }
    fn complete_api_attempt(&mut self, request:&PreparedApi, response:Result<Reply,NetworkError>, retry:bool) -> Reply {
        if !self.matches_identity(&request.identity) {
            return Reply::error(401,"account_changed_during_request");
        }
        let mut response = match response {
            Ok(r) => r,
            Err(NetworkError::NotSent) if retry && Self::can_retry_request(&request.method, &request.headers)
                && self.record.session.as_ref().is_some_and(|s| s.access_token != request.identity.access_token) => {
                let renewed = match self.prepare_api(&request.method, &request.path, request.body.clone(), request.headers.clone()) {
                    Ok(renewed) => renewed, Err(reply) => return reply,
                };
                let response = renewed.send();
                return self.complete_api_attempt(&renewed, response, false);
            }
            Err(_) => {
                self.state("offline",Some("Request outcome is unconfirmed. Check its status before repeating a change."));
                return Reply::error(0, "request_outcome_unknown");
            }
        };
        // Older gateways answer an expired bearer with 200 authenticated:false.
        // That is an expired token (sleep, deploy), not another account: refresh.
        let expired_session = request.path == "/auth/session" && response.success()
            && response.data.get("authenticated") == Some(&Value::Bool(false));
        if response.status == 401 || expired_session {
            if retry {
                let checked = if request.path == "/auth/session" {
                    if self.record.session.as_ref().is_some_and(|s| s.access_token == request.identity.access_token) {
                        self.refresh().and_then(|_| self.verify_session(false))
                    } else { Ok(()) }
                } else { self.confirm_feature_unauthorized(&request.identity) };
                if let Err(reply) = checked { return reply; }
                if !self.matches_identity(&request.identity) {
                    return Reply::error(401,"account_changed_during_request");
                }
                if Self::can_retry_request(&request.method, &request.headers)
                    && self.record.session.as_ref().is_some_and(|s| s.access_token != request.identity.access_token) {
                    let renewed = match self.prepare_api(&request.method, &request.path, request.body.clone(), request.headers.clone()) {
                        Ok(renewed) => renewed, Err(reply) => return reply,
                    };
                    let response = renewed.send();
                    return self.complete_api_attempt(&renewed, response, false);
                }
            } else if let Err(reply) = self.verify_session(false) {
                return reply;
            }
            return response;
        }
        if request.path == "/auth/session" && response.success() {
            if response.data.get("authenticated") != Some(&Value::Bool(true))
                || response.data.get("subject").and_then(Value::as_str) != Some(request.identity.subject.as_str())
            {
                return self.disable("account_mismatch", "session_identity_mismatch");
            }
            if let Some(object) = response.data.as_object_mut() {
                object.remove("csrf");
            }
            self.verified_at = now();
            self.state("connected", None);
        }
        response
    }
    // Called only by native code. Access tokens are handed to a private loopback
    // route, refresh tokens never leave the native gateway transport/Keychain.
    pub(crate) fn bridge_session(&self) -> Option<Value> {
        if self.status.state != "connected" {
            return None;
        }
        self.record.session.as_ref().map(|s|json!({"account_id":s.subject,"device_id":s.device_id,"access_token":s.access_token,"expires_at":s.expires_at}))
    }
    pub(crate) fn set_bridge_connected(&mut self, connected: bool) {
        self.status.bridge_connected = connected;
    }
    pub(crate) fn set_bridge_error(&mut self, error: Option<&str>) {
        if self.status.state == "connected" {
            self.status.error = error.map(str::to_owned);
        }
    }
}
pub(crate) fn trusted_account_window(label: &str, location: &Url, console: &str) -> bool {
    crate::native_security::trusted_window(label, location, console,
        cfg!(debug_assertions) || crate::channel::local_review() || crate::local_stack::is_local(), crate::channel::staging(), false)
}

fn valid_origin(origin: &str) -> bool {
    Url::parse(origin).is_ok_and(|u| {
        u.username().is_empty()
            && u.password().is_none()
            && u.query().is_none()
            && u.fragment().is_none()
            && u.path() == "/"
            && (u.scheme() == "https"
                || (u.scheme() == "http"
                    && matches!(u.host_str(), Some("127.0.0.1" | "localhost" | "[::1]"))))
            && u.host().is_some()
    })
}
fn constant_time_eq(a: &str, b: &str) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.bytes()
        .zip(b.bytes())
        .fold(0, |diff, (x, y)| diff | (x ^ y))
        == 0
}
fn callback_values(raw: &str) -> Option<(String, String)> {
    let u = Url::parse(raw).ok()?;
    if u.scheme() != crate::channel::callback_scheme()
        || u.host_str() != Some("auth")
        || u.path() != "/callback"
        || u.port().is_some()
        || !u.username().is_empty()
        || u.password().is_some()
        || u.fragment().is_some()
    {
        return None;
    }
    let pairs: Vec<_> = u.query_pairs().collect();
    if pairs.len() != 2 {
        return None;
    }
    let code = pairs.iter().find(|(k, _)| k == "code")?.1.to_string();
    let state = pairs.iter().find(|(k, _)| k == "state")?.1.to_string();
    if !urlsafe(&code, 16, 256) || !urlsafe(&state, 32, 128) {
        return None;
    }
    Some((code, state))
}
pub(crate) fn allowed_request(method: &str, path: &str, headers: &HashMap<String, String>) -> bool {
    if !matches!(method, "GET" | "POST" | "PUT" | "PATCH" | "DELETE" | "HEAD") {
        return false;
    }
    if headers.iter().any(|(k, v)| {
        !matches!(
            k.to_ascii_lowercase().as_str(),
            "idempotency-key" | "if-match" | "x-workspace-id" | "x-request-id"
        ) || v.len() > 512
            || v.bytes().any(|c| c < 32 || c == 127)
    }) {
        return false;
    }
    if path == "/auth/session" {
        return method == "GET";
    }
    let Ok(decoded) = percent_encoding::percent_decode_str(path).decode_utf8() else {
        return false;
    };
    if decoded.len() > 8192
        || decoded.contains('%')
        || decoded.contains('\\')
        || decoded.bytes().any(|b| b < 32 || b == 127)
        || !decoded.starts_with("/api/")
        || decoded.starts_with("/api/auth/")
    {
        return false;
    }
    let Ok(url) = Url::parse(&format!("https://native.invalid{decoded}")) else {
        return false;
    };
    if url.fragment().is_some() || url.host_str() != Some("native.invalid") {
        return false;
    }
    !decoded
        .split('?')
        .next()
        .unwrap_or("")
        .split('/')
        .any(|p| matches!(p, "." | ".."))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        collections::VecDeque,
        sync::{Arc, Mutex},
    };
    #[derive(Default)]
    struct Memory {
        raw: Option<String>,
        fail: bool,
    }
    struct TestVault(Arc<Mutex<Memory>>);
    impl Vault for TestVault {
        fn load(&self) -> Result<Option<String>, ()> {
            let m = self.0.lock().unwrap();
            if m.fail {
                Err(())
            } else {
                Ok(m.raw.clone())
            }
        }
        fn save(&self, value: &str) -> Result<(), ()> {
            let mut m = self.0.lock().unwrap();
            if m.fail {
                Err(())
            } else {
                m.raw = Some(value.into());
                Ok(())
            }
        }
        fn clear(&self) -> Result<(), ()> {
            let mut m = self.0.lock().unwrap();
            if m.fail {
                Err(())
            } else {
                m.raw = None;
                Ok(())
            }
        }
    }
    #[derive(Default)]
    struct Wire {
        responses: VecDeque<Result<Reply, NetworkError>>,
        paths: Vec<String>,
        bearers: Vec<Option<String>>,
        bodies: Vec<Option<Value>>,
    }
    struct TestHttp(Arc<Mutex<Wire>>);
    impl Http for TestHttp {
        fn send(
            &self,
            _: &str,
            _: &str,
            path: &str,
            bearer: Option<&str>,
            body: Option<&Value>,
            _: &HashMap<String, String>,
        ) -> Result<Reply, NetworkError> {
            let mut w = self.0.lock().unwrap();
            w.paths.push(path.into());
            w.bodies.push(body.cloned());
            w.bearers.push(bearer.map(str::to_owned));
            w.responses.pop_front().expect("unexpected HTTP request")
        }
    }
    fn fixture() -> (NativeAuth, Arc<Mutex<Memory>>, Arc<Mutex<Wire>>) {
        let vault = Arc::new(Mutex::new(Memory::default()));
        let wire = Arc::new(Mutex::new(Wire::default()));
        (
            NativeAuth::with_dependencies(
                "https://gateway.supymem.com".into(),
                Box::new(TestVault(vault.clone())),
                Box::new(TestHttp(wire.clone())),
            ),
            vault,
            wire,
        )
    }
    struct BlockingVault {
        raw: Option<String>,
        started: std::sync::mpsc::Sender<()>,
        release: std::sync::mpsc::Receiver<()>,
        calls: Arc<std::sync::atomic::AtomicUsize>,
        deny: bool,
    }
    impl Vault for BlockingVault {
        fn load(&self) -> Result<Option<String>, ()> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            self.started.send(()).unwrap();
            self.release.recv_timeout(Duration::from_secs(5)).unwrap();
            if self.deny { Err(()) } else { Ok(self.raw.clone()) }
        }
        fn save(&self, _: &str) -> Result<(), ()> { Ok(()) }
        fn clear(&self) -> Result<(), ()> { Ok(()) }
    }
    fn blocked_fixture(raw: Option<String>, deny: bool) -> (
        Arc<SharedAuth>, std::sync::mpsc::Receiver<()>, std::sync::mpsc::Sender<()>,
        Arc<std::sync::atomic::AtomicUsize>, Arc<Mutex<Wire>>, Arc<Mutex<Vec<String>>>,
    ) {
        let (started, ready)=std::sync::mpsc::channel();
        let (release, resume)=std::sync::mpsc::channel();
        let calls=Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let wire=Arc::new(Mutex::new(Wire::default()));
        let auth=NativeAuth::with_dependencies("https://gateway.supymem.com".into(),
            Box::new(BlockingVault { raw, started, release:resume, calls:calls.clone(), deny }),
            Box::new(TestHttp(wire.clone())));
        let shared=Arc::new(SharedAuth::new(auth));
        let notifications=Arc::new(Mutex::new(Vec::new()));
        let seen=notifications.clone(); let reentrant=Arc::downgrade(&shared);
        shared.set_listener(move |status| {
            // Completion must release the auth mutex before notifying the UI.
            if status.state!="secure_store_pending" {
                assert!(reentrant.upgrade().unwrap().try_lock().is_ok());
            }
            seen.lock().unwrap().push(status.state);
        });
        (shared,ready,release,calls,wire,notifications)
    }
    #[test]
    fn blocked_store_has_immediate_status_no_queued_access_and_recovers_same_account() {
        let (mut connected,memory,wire)=fixture();
        connect(&mut connected,&wire);
        let raw=memory.lock().unwrap().raw.clone();
        let (shared,ready,resume,calls,wire,notifications)=blocked_fixture(raw,false);
        assert_eq!(shared.status().state,"restoring");
        assert!(shared.status().restoring);
        wire.lock().unwrap().responses.push_back(ok(identity()));
        let worker_auth=shared.clone();
        let worker=std::thread::spawn(move || worker_auth.try_lock().unwrap().reconnect(None));
        ready.recv_timeout(Duration::from_secs(2)).unwrap();
        for _ in 0..32 {
            assert_eq!(shared.status().state,"secure_store_pending");
            assert!(shared.status().restoring);
            assert_eq!(shared.busy_code(),"secure_store_pending");
            assert!(shared.try_lock().is_err());
        }
        assert_eq!(calls.load(Ordering::SeqCst),1);
        assert!(wire.lock().unwrap().paths.is_empty());
        resume.send(()).unwrap(); worker.join().unwrap();
        assert_eq!(shared.status().state,"connected");
        assert!(!shared.status().restoring);
        assert_eq!(shared.status().subject.as_deref(),Some("usr_a"));
        assert_eq!(notifications.lock().unwrap().last().map(String::as_str),Some("connected"));
        let mut auth=shared.try_lock().unwrap();
        let proof=auth.request_identity().ok().unwrap();
        auth.reconnect(Some("usr_b"));
        assert_eq!(auth.status().state,"account_mismatch");
        assert!(!auth.matches_identity(&proof));
    }
    #[test]
    fn denied_store_emits_recoverable_failure_without_authorizing() {
        let (shared,ready,resume,_,wire,notifications)=blocked_fixture(None,true);
        let worker_auth=shared.clone();
        let worker=std::thread::spawn(move || worker_auth.try_lock().unwrap().reconnect(None));
        ready.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(shared.status().state,"secure_store_pending");
        resume.send(()).unwrap();worker.join().unwrap();
        assert_eq!(shared.status().state,"keychain_unavailable");
        assert!(shared.status().restoring);
        assert!(shared.status().subject.is_none());
        assert!(wire.lock().unwrap().paths.is_empty());
        assert_eq!(notifications.lock().unwrap().last().map(String::as_str),Some("keychain_unavailable"));
    }
    #[test]
    fn ordinary_busy_account_work_is_not_a_keychain_prompt_and_completion_clears_it() {
        let (mut auth,_,_)=fixture();assert_eq!(auth.reconnect(None).state,"signed_out");
        let shared=Arc::new(SharedAuth::new(auth));
        let states=Arc::new(Mutex::new(Vec::new()));let seen=states.clone();
        shared.set_listener(move |status| seen.lock().unwrap().push(status.state));
        let operation=shared.try_lock().unwrap();
        assert_eq!(shared.status().state,"native_auth_busy");
        assert_eq!(shared.busy_code(),"native_auth_busy");
        assert!(!shared.store_pending());
        drop(operation);
        assert_eq!(states.lock().unwrap().as_slice(),["signed_out"]);
        // A normal read with no reported contention must not start a session loop.
        drop(shared.try_lock().unwrap());
        assert_eq!(states.lock().unwrap().len(),1);
    }
    #[tokio::test]
    async fn ipc_wait_returns_pending_while_only_original_os_operation_remains_active() {
        let (shared,ready,resume,calls,_,_)=blocked_fixture(None,false);
        let worker_auth=shared.clone();let (done,completed)=std::sync::mpsc::channel();
        let observation=Observation::default();let worker_observation=observation.clone();
        let worker=tauri::async_runtime::spawn_blocking(move || {
            let mut auth=worker_auth.try_lock().unwrap();auth.observe(&worker_observation);
            let result=auth.reconnect(None);drop(auth);
            done.send(()).unwrap();result
        });
        ready.recv_timeout(Duration::from_secs(2)).unwrap();
        let returned=tokio::time::timeout(Duration::from_secs(2),await_store(worker,&shared,&observation)).await.unwrap();
        assert!(matches!(returned,Err(code) if code=="secure_store_pending"));
        assert!(shared.try_lock().is_err());
        assert_eq!(calls.load(Ordering::SeqCst),1);
        resume.send(()).unwrap();completed.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(shared.status().state,"signed_out");
    }
    #[tokio::test]
    async fn unrelated_request_is_not_interrupted_by_another_operations_keychain_prompt() {
        let (shared,ready,resume,_,_,_)=blocked_fixture(None,false);
        let blocked=shared.clone();
        let protected=std::thread::spawn(move || blocked.try_lock().unwrap().reconnect(None));
        ready.recv_timeout(Duration::from_secs(2)).unwrap();
        let request=tauri::async_runtime::spawn(async {
            tokio::time::sleep(Duration::from_millis(600)).await;
            "completed response"
        });
        let observation=Observation::default();
        assert_eq!(await_store(request,&shared,&observation).await.unwrap(),"completed response");
        resume.send(()).unwrap();protected.join().unwrap();
    }
    #[tokio::test]
    async fn concurrent_acquire_waits_for_one_keychain_load_and_verification() {
        let (mut connected,memory,wire)=fixture();connect(&mut connected,&wire);
        let raw=memory.lock().unwrap().raw.clone();
        let (shared,ready,resume,calls,wire,_)=blocked_fixture(raw,false);
        shared.set_listener(|_| {});
        wire.lock().unwrap().responses.push_back(ok(identity()));
        let blocked=shared.clone();
        let protected=std::thread::spawn(move || blocked.try_lock().unwrap().reconnect(None));
        ready.recv_timeout(Duration::from_secs(2)).unwrap();
        let mut readers=Vec::new();
        for _ in 0..8 {
            let shared=shared.clone();
            readers.push(tokio::spawn(async move {
                let mut auth=shared.acquire().await.unwrap();
                tauri::async_runtime::spawn_blocking(move || auth.request_identity().ok().unwrap()).await.unwrap()
            }));
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert!(readers.iter().all(|reader| !reader.is_finished()));
        assert_eq!(calls.load(Ordering::SeqCst),1);
        resume.send(()).unwrap();protected.join().unwrap();
        for reader in readers {
            let identity=tokio::time::timeout(Duration::from_secs(2),reader).await.unwrap().unwrap();
            assert_eq!(identity.subject,"usr_a");assert_eq!(identity.device_id,"device-a");
            assert_eq!(identity.access_token,"access-a");
        }
        assert_eq!(calls.load(Ordering::SeqCst),1);
        assert_eq!(wire.lock().unwrap().paths,["/auth/session"]);
    }
    async fn execute_prepared(shared:Arc<SharedAuth>,method:&str,path:&str,headers:HashMap<String,String>) -> Reply {
        let request={
            let mut auth=shared.acquire().await.unwrap();
            auth.prepare_api(method,path,None,headers).ok().unwrap()
        };
        let (request,response)=tauri::async_runtime::spawn_blocking(move || {
            let response=request.send();(request,response)
        }).await.unwrap();
        let mut auth=shared.acquire().await.unwrap();
        auth.complete_api(&request,response)
    }
    #[tokio::test(flavor="multi_thread",worker_threads=4)]
    async fn parallel_native_gets_and_mutation_keep_results_while_brief_identity_work_finishes() {
        use std::{io::Write,net::TcpListener,sync::Condvar};
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();
        let origin=format!("http://{}",listener.local_addr().unwrap());
        let gate=Arc::new((Mutex::new((0usize,false)),Condvar::new()));
        let observed=Arc::new(Mutex::new(Vec::new()));
        let server_gate=gate.clone();let requests=observed.clone();
        let server=std::thread::spawn(move || {
            let mut handlers=Vec::new();
            for _ in 0..4 {
                let (mut socket,_)=listener.accept().unwrap();let gate=server_gate.clone();let requests=requests.clone();
                handlers.push(std::thread::spawn(move || {
                    socket.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
                    let mut bytes=Vec::new();let mut chunk=[0;1024];
                    while !bytes.windows(4).any(|w|w==b"\r\n\r\n") {
                        let read=socket.read(&mut chunk).unwrap();assert!(read>0);bytes.extend_from_slice(&chunk[..read]);
                    }
                    let headers=String::from_utf8(bytes).unwrap();
                    let line=headers.lines().next().unwrap().to_owned();
                    if line.starts_with("POST") { assert!(headers.to_lowercase().contains("idempotency-key: operation-a")); }
                    requests.lock().unwrap().push(line.clone());
                    let (lock,notify)=&*gate;let mut state=lock.lock().unwrap();state.0+=1;notify.notify_all();
                    let (state,wait)=notify.wait_timeout_while(state,Duration::from_secs(3),|state|!state.1).unwrap();
                    assert!(!wait.timed_out(),"all requests must reach HTTP concurrently");drop(state);
                    let status=if line.starts_with("POST") {201}else{200};let body=json!({"request":line,"result":"saved"}).to_string();
                    write!(socket,"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len()).unwrap();
                }));
            }
            for handler in handlers {handler.join().unwrap();}
        });
        let (mut native,_,wire)=fixture();connect(&mut native,&wire);
        native.gateway=origin.clone();native.record.session.as_mut().unwrap().gateway=origin;
        native.http=Arc::new(NativeHttp);
        let shared=Arc::new(SharedAuth::new(native));let mut tasks=Vec::new();
        for path in ["/api/ventures", "/api/credits", "/api/plans"] {
            tasks.push(tokio::spawn(execute_prepared(shared.clone(),"GET",path,HashMap::new())));
        }
        tasks.push(tokio::spawn(execute_prepared(shared.clone(),"POST","/api/build/jobs",HashMap::from([("Idempotency-Key".into(),"operation-a".into())]))));
        // While HTTP is active, status remains readable and a different short
        // account operation can run; successful responses await its release.
        let (lock,notify)=&*gate;
        let (state,wait)=notify.wait_timeout_while(lock.lock().unwrap(),Duration::from_secs(3),|state|state.0<4).unwrap();
        assert!(!wait.timed_out());drop(state);
        assert_eq!(shared.status().state,"connected");
        let auth_operation=shared.acquire().await.unwrap();
        lock.lock().unwrap().1=true;notify.notify_all();
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert!(tasks.iter().all(|task|!task.is_finished()));
        drop(auth_operation);
        let mut statuses=Vec::new();for task in tasks {
            let reply=task.await.unwrap();assert_eq!(reply.data["result"],"saved");statuses.push(reply.status);
        }
        assert_eq!(statuses,[200,200,200,201]);
        assert_eq!(observed.lock().unwrap().len(),4,"HTTP calls must not be replayed");
        server.join().unwrap();
    }
    #[test]
    fn prepared_response_never_crosses_account_change_and_session_redaction_is_preserved() {
        let (mut auth,_,wire)=fixture();connect(&mut auth,&wire);
        let session=auth.prepare_api("GET","/auth/session",None,HashMap::new()).ok().unwrap();
        let reply=auth.complete_api(&session,ok(identity()));
        assert_eq!(reply.status,200);assert!(reply.data.get("csrf").is_none());
        let mutation=auth.prepare_api("POST","/api/build/jobs",None,HashMap::new()).ok().unwrap();
        auth.reconnect(Some("usr_b"));
        let reply=auth.complete_api(&mutation,ok(json!({"private":"previous account"})));
        assert_eq!(reply.status,401);assert!(reply.data.get("private").is_none());
    }
    #[test]
    fn prepared_mutation_with_unknown_network_outcome_is_never_replayed() {
        let (mut auth,_,wire)=fixture();connect(&mut auth,&wire);
        let previous=wire.lock().unwrap().paths.len();
        wire.lock().unwrap().responses.push_back(Err(NetworkError::Uncertain));
        let request=auth.prepare_api("POST","/api/build/jobs",None,HashMap::new()).ok().unwrap();
        let response=request.send();let reply=auth.complete_api(&request,response);
        assert_eq!(reply.status,0);assert_eq!(reply.data["error"]["code"],"request_outcome_unknown");
        assert_eq!(auth.status().state,"offline");assert_eq!(wire.lock().unwrap().paths.len(),previous+1);
        let pending=Reply::sent_unconfirmed("secure_store_pending");
        assert_eq!(pending.status,503);assert_eq!(pending.data["error"]["request_sent"],true);
        assert_eq!(pending.data["error"]["automatic_retry_allowed"],false);
    }

    #[test]
    fn u68_concurrent_expired_requests_refresh_once_and_retry_each_once() {
        let (mut auth, vault, wire) = fixture(); connect(&mut auth, &wire);
        let first = auth.prepare_api("POST", "/api/telemetry", Some(json!({"event":"test"})), HashMap::from([("Idempotency-Key".into(), "telemetry-test".into())])).ok().unwrap();
        let second = auth.prepare_api("GET", "/api/ventures", None, HashMap::new()).ok().unwrap();
        wire.lock().unwrap().responses.extend([
            Ok(Reply::error(401,"unauthenticated")), ok(tokens("usr_a","access-new","refresh-new")), ok(identity()),
            ok(json!({"saved":true})), ok(json!({"items":[]})),
        ]);
        let rejected = || Ok(Reply::error(401,"unauthenticated"));
        assert_eq!(auth.complete_api(&first,rejected()).status,200);
        assert_eq!(auth.complete_api(&second,rejected()).status,200);
        assert_eq!(wire.lock().unwrap().paths.iter().filter(|path| *path == "/auth/device/refresh").count(),1);
        assert!(vault.lock().unwrap().raw.as_ref().unwrap().contains("refresh-new"));
        assert_eq!(auth.status().state,"connected");
        for domain in ["factory maintenance teams", "hobby cooking clubs"] {
            let body = json!({"audience": domain});
            let headers = HashMap::from([("Idempotency-Key".into(), "confirmation-test".into())]);
            for state in ["connected", "offline", "keychain_unavailable"] {
                auth.state("connected", None);
                let request = auth.prepare_api("POST", "/api/build/jobs", Some(body.clone()), headers.clone()).ok().unwrap();
                auth.record.session.as_mut().unwrap().access_token = format!("rotated-{state}");
                auth.state(state, None);
                let sent = wire.lock().unwrap().paths.len();
                let reply = auth.complete_api(&request, ok(body.clone()));
                assert_eq!(reply.status, 200);
                assert_eq!(reply.data, body);
                assert_eq!(wire.lock().unwrap().paths.len(), sent, "Saved success must not be replayed");
            }
            auth.state("connected", None);
            let request = auth.prepare_api("POST", "/api/build/jobs", Some(body.clone()), headers.clone()).ok().unwrap();
            auth.record.session.as_mut().unwrap().access_token = "retry-token".into();
            wire.lock().unwrap().responses.push_back(ok(body.clone()));
            let reply = auth.complete_api(&request, Err(NetworkError::NotSent));
            assert_eq!(reply.status, 200);
            assert_eq!(wire.lock().unwrap().bodies.last(), Some(&Some(body)));
            let unkeyed = auth.prepare_api("POST", "/api/build/jobs", None, HashMap::new()).ok().unwrap();
            auth.record.session.as_mut().unwrap().access_token = "another-token".into();
            let sent = wire.lock().unwrap().paths.len();
            assert_eq!(auth.complete_api(&unkeyed, Ok(Reply::error(401, "unauthenticated"))).status, 401);
            assert_eq!(wire.lock().unwrap().paths.len(), sent, "Never retry an unkeyed mutation");
        }
    }
    #[tokio::test]
    async fn a_panicked_auth_operation_fails_closed_after_releasing_the_async_mutex() {
        let (auth,_,_)=fixture();let shared=Arc::new(SharedAuth::new(auth));let worker=shared.clone();
        let result=std::thread::spawn(move || {let _auth=worker.try_lock().unwrap();panic!("injected auth failure");}).join();
        assert!(result.is_err());assert_eq!(shared.status().state,"restoring");
        assert!(shared.try_lock().is_err());assert!(shared.acquire().await.is_err());
    }
    #[test]
    fn notice_custody_changes_across_a_b_a_and_is_unavailable_during_auth_work() {
        let (mut auth,_,wire)=fixture();connect(&mut auth,&wire);auth.set_bridge_connected(true);
        let shared=SharedAuth::new(auth);
        let old=shared.notice_custody().unwrap();
        let mut guard=shared.try_lock().unwrap();
        assert!(shared.notice_custody().is_none());
        guard.status.subject=Some("usr_b".into());drop(guard);
        let other=shared.notice_custody().unwrap();assert_ne!(old,other);
        let mut guard=shared.try_lock().unwrap();guard.status.subject=Some("usr_a".into());drop(guard);
        let returned=shared.notice_custody().unwrap();assert_eq!(old.0,returned.0);assert_ne!(old.2,returned.2);
        let guard=shared.try_lock().unwrap();drop(guard);
        assert_eq!(shared.notice_custody().unwrap(),returned,"read-only auth access must not retire a notice");
        let mut guard=shared.try_lock().unwrap();guard.set_bridge_connected(false);drop(guard);
        assert!(shared.notice_custody().is_none());
    }
    fn connected_notice_auth() -> SharedAuth {
        let (mut auth, _, wire) = fixture(); connect(&mut auth, &wire);
        auth.set_bridge_connected(true); SharedAuth::new(auth)
    }
    #[test]
    fn bridge_recovery_error_notifies_once_without_changing_cloud_identity() {
        let shared = connected_notice_auth();
        let observed = Arc::new(Mutex::new(Vec::new()));
        let capture = observed.clone();
        shared.set_listener(move |status| capture.lock().unwrap().push(status));
        for _ in 0..3 {
            let mut auth = shared.try_lock().unwrap();
            auth.set_bridge_connected(false);
            auth.set_bridge_error(Some("The local helper is unavailable. Quit and reopen Supymem."));
        }
        {
            let statuses = observed.lock().unwrap();
            assert_eq!(statuses.len(), 1);
            assert_eq!(statuses[0].state, "connected");
            assert!(!statuses[0].bridge_connected);
            assert!(statuses[0].error.as_ref().unwrap().contains("Quit and reopen"));
        }
        let mut auth = shared.try_lock().unwrap();
        auth.set_bridge_connected(true);
        auth.set_bridge_error(None);
        drop(auth);
        assert_eq!(observed.lock().unwrap().len(), 2);
        assert!(observed.lock().unwrap()[1].error.is_none());
    }
    #[test]
    fn every_notice_identity_transition_notifies_the_overlay_retirement_listener() {
        let shared = connected_notice_auth(); let observed = Arc::new(Mutex::new(Vec::new()));
        let capture = observed.clone(); shared.set_listener(move |status| capture.lock().unwrap().push(notice_identity(&status)));
        let mut auth = shared.try_lock().unwrap(); auth.status.subject = Some("usr_b".into()); drop(auth);
        let mut auth = shared.try_lock().unwrap(); auth.status.device_id = Some("device_b".into()); drop(auth);
        let mut auth = shared.try_lock().unwrap(); auth.set_bridge_connected(false); drop(auth);
        assert_eq!(observed.lock().unwrap().len(), 3, "same state must still retire subject/device/bridge generations");
        let auth = shared.try_lock().unwrap(); drop(auth);
        assert_eq!(observed.lock().unwrap().len(), 3, "ordinary read must not retire the overlay");
    }
    #[test]
    fn notice_generation_survives_a_busy_unchanged_account_and_retires_on_real_change() {
        let shared = connected_notice_auth(); let before = shared.notice_generation();
        let mut auth = shared.try_lock().unwrap();
        assert!(shared.notice_custody().is_none(), "ordinary try-lock cannot distinguish busy from disconnected");
        assert_eq!(shared.notice_generation(), before, "busy alone must not retire a shown overlay");
        auth.status.subject = Some("usr_b".into()); drop(auth);
        assert!(shared.notice_generation() > before);
        let changed = shared.notice_generation(); let auth = shared.try_lock().unwrap(); drop(auth);
        assert_eq!(shared.notice_generation(), changed);
    }
    #[tokio::test]
    async fn notice_custody_async_reader_waits_for_emit_lease_and_reads_without_relocking() {
        let shared = Arc::new(connected_notice_auth()); let expected = shared.notice_custody().unwrap();
        let emit_lease = shared.try_lock().unwrap();
        let reader = shared.clone();
        let pending = tokio::spawn(async move {
            let auth = reader.acquire().await.unwrap(); auth.notice_custody()
        });
        tokio::task::yield_now().await;
        assert!(!pending.is_finished(), "a valid event waits instead of dropping on a busy emit lease");
        drop(emit_lease);
        assert_eq!(tokio::time::timeout(Duration::from_secs(1), pending).await.unwrap().unwrap(), Some(expected));
    }
    #[tokio::test]
    async fn suggestions_custody_reader_times_out_and_can_retry_without_retiring_owner() {
        let shared = connected_notice_auth(); let expected = shared.notice_custody().unwrap();
        let lease = shared.try_lock().unwrap();
        let result = shared.acquire_with_timeout(Duration::from_millis(20)).await;
        assert!(matches!(result, Err(code) if code == "native_auth_busy"));
        drop(lease);
        let reader = shared.acquire_with_timeout(Duration::from_secs(1)).await.unwrap();
        assert_eq!(reader.notice_custody(), Some(expected));
    }
    #[tokio::test]
    async fn suggestions_long_overlay_send_does_not_block_auth_acquire() {
        struct BlockingOverlayHttp {
            started: Arc<tokio::sync::Notify>,
            release: Mutex<std::sync::mpsc::Receiver<()>>,
        }
        impl Http for BlockingOverlayHttp {
            fn send(&self, _: &str, _: &str, path: &str, _: Option<&str>, _: Option<&Value>,
                _: &HashMap<String, String>) -> Result<Reply, NetworkError> {
                assert_eq!(path, "/api/suggestions/sug_test/decision");
                self.started.notify_one();
                self.release.lock().unwrap().recv_timeout(Duration::from_secs(5)).unwrap();
                Ok(Reply { status: 200, data: json!({}), request_id: None })
            }
        }
        let shared = Arc::new(connected_notice_auth()); let expected = shared.notice_custody().unwrap();
        let mut request = shared.acquire().await.unwrap().prepare_api("POST",
            "/api/suggestions/sug_test/decision", Some(json!({"decision":"accept"})), HashMap::new()).ok().unwrap();
        let started = Arc::new(tokio::sync::Notify::new());
        let (release, receiver) = std::sync::mpsc::channel();
        request.http = Arc::new(BlockingOverlayHttp { started: started.clone(), release: Mutex::new(receiver) });
        let sender = shared.clone(); let owner = expected.clone();
        let pending = tokio::spawn(async move {
            sender.send_overlay_request(request, |auth| auth.notice_custody() == Some(owner)).await
        });
        tokio::time::timeout(Duration::from_secs(1), started.notified()).await.unwrap();
        let reader = shared.acquire_with_timeout(Duration::from_millis(100)).await.unwrap();
        assert_eq!(reader.notice_custody(), Some(expected));
        assert!(!pending.is_finished(), "HTTP is still blocked while auth admission succeeds");
        drop(reader); release.send(()).unwrap();
        let (_, response) = pending.await.unwrap().ok().unwrap();
        assert_eq!(response.ok().unwrap().status, 200);
    }
    #[tokio::test]
    async fn suggestions_overlay_dispatch_waits_for_busy_auth_then_sends_once() {
        let (mut auth, _, wire) = fixture(); connect(&mut auth, &wire); auth.set_bridge_connected(true);
        let shared = Arc::new(SharedAuth::new(auth)); let expected = shared.notice_custody().unwrap();
        let request = shared.acquire().await.unwrap().prepare_api("POST",
            "/api/suggestions/sug_test/decision", Some(json!({"decision":"accept"})), HashMap::new()).ok().unwrap();
        wire.lock().unwrap().responses.push_back(Ok(Reply { status: 200, data: json!({}), request_id: None }));
        let lease = shared.acquire().await.unwrap(); let sender = shared.clone();
        let pending = tokio::spawn(async move {
            sender.send_overlay_request(request, |auth| auth.notice_custody() == Some(expected)).await
        });
        tokio::task::yield_now().await;
        assert!(!pending.is_finished());
        assert!(!wire.lock().unwrap().paths.iter().any(|path| path.ends_with("/decision")));
        drop(lease);
        let (_, response) = tokio::time::timeout(Duration::from_secs(1), pending).await.unwrap().unwrap().ok().unwrap();
        assert_eq!(response.ok().unwrap().status, 200);
        assert_eq!(wire.lock().unwrap().paths.iter().filter(|path| path.ends_with("/decision")).count(), 1);
    }
    #[tokio::test]
    async fn suggestions_overlay_dispatch_timeout_is_retryable_and_does_not_send() {
        let shared = connected_notice_auth();
        let request = shared.acquire().await.unwrap().prepare_api("POST",
            "/api/suggestions/sug_test/decision", None, HashMap::new()).ok().unwrap();
        let lease = shared.acquire().await.unwrap();
        let reply = shared.send_overlay_request(request, |_| panic!("busy dispatch was admitted")).await.err().unwrap();
        assert_eq!(reply.status, 503);
        assert_eq!(reply.data["error"]["code"], "suggestions_send_busy");
        assert_eq!(reply.data["error"]["request_sent"], false);
        assert_eq!(reply.data["error"]["automatic_retry_allowed"], true);
        drop(lease);
    }
    #[tokio::test]
    async fn suggestions_overlay_dispatch_rejects_retired_custody_without_sending() {
        let shared = connected_notice_auth();
        let request = shared.acquire().await.unwrap().prepare_api("POST",
            "/api/suggestions/sug_test/decision", None, HashMap::new()).ok().unwrap();
        let reply = shared.send_overlay_request(request, |_| {
            assert!(shared.try_lock().is_err(), "custody and presentation are checked under the auth lock");
            false
        }).await.err().unwrap();
        assert_eq!(reply.status, 409);
        assert_eq!(reply.data["error"]["code"], "suggestions_owner_changed");
    }
    #[test]
    fn notice_permission_preparation_allows_transition_and_old_registration_has_no_effect() {
        let shared = connected_notice_auth(); let expected = shared.notice_custody().unwrap();
        // A permission dialog is outside the lease: an account operation can
        // finish while preparation is pending, before any private registration.
        let mut sent = false;
        let result = crate::mac_notice::prepare_then_dispatch(|| {
            let mut auth = shared.try_lock().unwrap();
            auth.status.subject = Some("usr_b".into()); drop(auth); Ok(())
        }, |()| shared.with_notice_custody(&expected, || {sent = true; Ok(())})
            .unwrap_or(Err("account_changed")));
        assert_eq!(result, Err("account_changed"));
        assert!(!sent);
        let current = shared.notice_custody().unwrap();
        assert_eq!(shared.with_notice_custody(&current, || 1), Some(1));
    }
    #[test]
    fn notice_denied_permission_does_not_enter_account_dispatch() {
        let shared = connected_notice_auth(); let expected = shared.notice_custody().unwrap();
        let mut sent = false;
        let result = crate::mac_notice::prepare_then_dispatch(|| Err::<(), _>("denied"), |()| {
            shared.with_notice_custody(&expected, || {sent = true; Ok(())}).unwrap_or(Err("account_changed"))
        });
        assert_eq!(result, Err("denied")); assert!(!sent);
        assert_eq!(shared.notice_custody().unwrap(), expected);
    }
    #[test]
    fn notice_registration_serializes_account_transition_and_releases_after_error() {
        let shared = Arc::new(connected_notice_auth()); let expected = shared.notice_custody().unwrap();
        let competing = shared.clone();
        let result = shared.with_notice_custody(&expected, || {
            assert!(std::thread::spawn(move || competing.try_lock().is_err()).join().unwrap(),
                    "account transition must not interleave OS registration");
            Err::<(), _>("injected_registration_failure")
        });
        assert_eq!(result, Some(Err("injected_registration_failure")));
        assert!(shared.try_lock().is_ok(), "failed local registration releases its lease");
        assert_eq!(shared.notice_custody().unwrap(), expected);
    }
    #[test]
    fn delayed_notice_click_and_suggestion_callbacks_reject_a_b_a_generation() {
        let shared = connected_notice_auth(); let queued = shared.notice_custody().unwrap();
        for subject in ["usr_b", "usr_a"] {
            let mut auth = shared.try_lock().unwrap(); auth.status.subject = Some(subject.into()); drop(auth);
        }
        assert_eq!(shared.notice_custody().unwrap().0, queued.0);
        let mut actions = Vec::new();
        for action in ["navigate", "show", "focus", "suggestions_present"] {
            assert!(shared.with_notice_custody(&queued, || actions.push(action)).is_none());
        }
        assert!(actions.is_empty());
        let current = shared.notice_custody().unwrap();
        assert!(shared.with_notice_custody(&current, || actions.push("current_present")).is_some());
        assert_eq!(actions, ["current_present"]);
    }
    #[test]
    fn notice_callbacks_refuse_busy_disconnected_and_device_changed_receipts() {
        let shared = connected_notice_auth(); let queued = shared.notice_custody().unwrap();
        let mut auth = shared.try_lock().unwrap();
        assert!(shared.with_notice_custody(&queued, || panic!("busy callback ran")).is_none());
        auth.set_bridge_connected(false); drop(auth);
        assert!(shared.with_notice_custody(&queued, || panic!("disconnected callback ran")).is_none());
        let mut auth = shared.try_lock().unwrap();
        auth.set_bridge_connected(true); auth.status.device_id = Some("other-device".into()); drop(auth);
        assert!(shared.with_notice_custody(&queued, || panic!("old-device callback ran")).is_none());
    }
    #[test]
    fn panicked_notice_dispatch_releases_mutex_and_retires_custody() {
        let shared = connected_notice_auth(); let expected = shared.notice_custody().unwrap();
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            shared.with_notice_custody(&expected, || panic!("injected notice dispatch failure"));
        }));
        assert!(result.is_err()); assert!(shared.inner.try_lock().is_ok());
        assert!(shared.notice_custody().is_none()); assert_eq!(shared.status().state, "restoring");
    }
    fn ok(data: Value) -> Result<Reply, NetworkError> {
        Ok(Reply { status: 200, data, request_id: None })
    }
    fn tokens(subject: &str, access: &str, refresh: &str) -> Value {
        json!({"subject":subject,"device_id":"device-a","access_token":access,"refresh_token":refresh,"expires_in":900})
    }
    fn identity() -> Value {
        json!({"authenticated":true,"subject":"usr_a","csrf":"ambient-cookie-csrf"})
    }
    fn start(auth: &mut NativeAuth) -> String {
        auth.start(Some("usr_a".into())).ok().unwrap();
        format!(
            "{}://auth/callback?code=fixture_code_1234567890123456&state={}",
            crate::channel::callback_scheme(),
            auth.record.pending.as_ref().unwrap().state
        )
    }
    fn connect(auth: &mut NativeAuth, wire: &Arc<Mutex<Wire>>) {
        let callback = start(auth);
        wire.lock()
            .unwrap()
            .responses
            .extend([ok(tokens("usr_a", "access-a", "refresh-a")), ok(identity())]);
        assert!(auth.callback(&callback).is_ok());
        assert_eq!(auth.status().state, "connected");
    }
    struct BlockingRotationHttp {
        wire: Arc<Mutex<Wire>>,
        started: std::sync::mpsc::Sender<()>,
        resume: Mutex<std::sync::mpsc::Receiver<()>>,
    }
    impl Http for BlockingRotationHttp {
        fn send(&self, gateway: &str, method: &str, path: &str, bearer: Option<&str>, body: Option<&Value>, headers: &HashMap<String, String>) -> Result<Reply, NetworkError> {
            if path == "/auth/device/refresh" {
                self.started.send(()).unwrap();
                self.resume.lock().unwrap().recv_timeout(Duration::from_secs(5)).unwrap();
            }
            TestHttp(self.wire.clone()).send(gateway, method, path, bearer, body, headers)
        }
    }
    #[tokio::test]
    async fn concurrent_acquire_shares_one_refresh_and_keeps_identity_checks() {
        for subject in ["usr_a", "usr_b"] {
            let (mut auth,_,wire)=fixture();connect(&mut auth,&wire);
            let previous=auth.request_identity().ok().unwrap();
            auth.record.session.as_mut().unwrap().expires_at=1;
            wire.lock().unwrap().responses.extend([ok(tokens(subject,"access-new","refresh-new")),
                if subject=="usr_a" {ok(identity())} else {ok(json!({}))}]);
            let (started,ready)=std::sync::mpsc::channel();
            let (release,resume)=std::sync::mpsc::channel();
            auth.http=Arc::new(BlockingRotationHttp {wire:wire.clone(),started,resume:Mutex::new(resume)});
            let shared=Arc::new(SharedAuth::new(auth));
            let mut admitted=shared.acquire().await.unwrap();
            let rotation=tauri::async_runtime::spawn_blocking(move || admitted.request_identity());
            ready.recv_timeout(Duration::from_secs(2)).unwrap();
            let mut readers=Vec::new();
            for _ in 0..8 {
                let shared=shared.clone();
                readers.push(tokio::spawn(async move {
                    let mut auth=shared.acquire().await.unwrap();
                    tauri::async_runtime::spawn_blocking(move || auth.request_identity()).await.unwrap()
                }));
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
            assert!(readers.iter().all(|reader| !reader.is_finished()));
            release.send(()).unwrap();
            let first=rotation.await.unwrap();
            assert_eq!(first.is_ok(),subject=="usr_a");
            for reader in readers {
                let result=tokio::time::timeout(Duration::from_secs(2),reader).await.unwrap().unwrap();
                if subject=="usr_a" {
                    let identity=result.ok().unwrap();
                    assert_eq!(identity.access_token,"access-new");
                    assert_eq!(identity.subject,"usr_a");assert_eq!(identity.device_id,"device-a");
                } else { assert!(result.is_err()); }
            }
            assert_eq!(wire.lock().unwrap().paths.iter().filter(|path| path.as_str()=="/auth/device/refresh").count(),1);
            assert_eq!(shared.try_lock().unwrap().matches_identity(&previous),subject=="usr_a");
        }
    }
    #[tokio::test]
    async fn update_or_exit_during_rotation_drains_persistence_and_restores_same_subject() {
        let (mut auth, memory, wire) = fixture();
        connect(&mut auth, &wire);
        auth.record.session.as_mut().unwrap().expires_at = 1;
        assert!(auth.persist().is_ok());
        wire.lock().unwrap().responses.extend([ok(tokens("usr_a", "access-new", "refresh-new")), ok(identity())]);
        let (started, ready) = std::sync::mpsc::channel();
        let (release, resume) = std::sync::mpsc::channel();
        auth.http = Arc::new(BlockingRotationHttp { wire: wire.clone(), started, resume: Mutex::new(resume) });
        let shared = Arc::new(SharedAuth::new(auth));
        let worker_auth = shared.clone();
        let rotation = std::thread::spawn(move || assert!(worker_auth.try_lock().unwrap().request_identity().is_ok()));
        ready.recv_timeout(Duration::from_secs(2)).unwrap();
        let updater = shared.clone();
        let update = tokio::spawn(async move { updater.quiesce().await });
        tokio::time::timeout(Duration::from_secs(2), async {
            while !shared.activity.quiescing.load(Ordering::SeqCst) { tokio::task::yield_now().await; }
        }).await.unwrap();
        assert!(!update.is_finished());
        assert!(shared.try_lock().is_err());
        assert_eq!(shared.acquire().await.err().unwrap(), "native_auth_quiescing");
        let before: Record = serde_json::from_str(memory.lock().unwrap().raw.as_ref().unwrap()).unwrap();
        assert!(before.session.unwrap().refresh_in_flight);
        release.send(()).unwrap();
        let quiescence = update.await.unwrap().unwrap();
        rotation.join().unwrap();
        let saved: Record = serde_json::from_str(memory.lock().unwrap().raw.as_ref().unwrap()).unwrap();
        let session = saved.session.unwrap();
        assert_eq!(session.subject, "usr_a");
        assert_eq!(session.refresh_token, "refresh-new");
        assert!(!session.refresh_in_flight);
        assert!(shared.try_lock().is_err());
        wire.lock().unwrap().responses.push_back(ok(identity()));
        let mut restored = NativeAuth::with_dependencies("https://gateway.supymem.com".into(), Box::new(TestVault(memory)), Box::new(TestHttp(wire)));
        assert_eq!(restored.status().state, "restoring");
        assert_eq!(restored.reconnect(None).state, "connected");
        assert_eq!(restored.status().subject.as_deref(), Some("usr_a"));
        drop(quiescence);
        assert!(shared.try_lock().is_ok());
    }
    #[tokio::test]
    async fn cancelled_restart_wait_reopens_admission_without_changing_credentials() {
        let (mut auth, memory, wire) = fixture();
        connect(&mut auth, &wire);
        let saved = memory.lock().unwrap().raw.clone();
        let shared = SharedAuth::new(auth);
        let operation = shared.try_lock().unwrap();
        assert!(tokio::time::timeout(Duration::from_millis(25), shared.quiesce()).await.is_err());
        assert!(!shared.activity.quiescing.load(Ordering::SeqCst));
        assert_eq!(memory.lock().unwrap().raw, saved);
        drop(operation);
        assert!(shared.try_lock().is_ok());
    }
    #[tokio::test]
    async fn restart_timeout_keeps_credentials_and_reopens_admission() {
        let (mut auth, memory, wire) = fixture();
        connect(&mut auth, &wire);
        let saved = memory.lock().unwrap().raw.clone();
        let shared = SharedAuth::new(auth);
        let operation = shared.try_lock().unwrap();
        let result = tokio::time::timeout(Duration::from_secs(11), shared.quiesce()).await.unwrap();
        assert!(result.err().unwrap().contains("Restart was withheld"));
        assert!(!shared.activity.quiescing.load(Ordering::SeqCst));
        assert_eq!(memory.lock().unwrap().raw, saved);
        drop(operation);
        assert!(shared.try_lock().is_ok());
    }
    #[tokio::test]
    async fn restart_preserves_uncertain_refresh_and_refuses_unpersisted_successor() {
        let (mut auth, memory, wire) = fixture();
        connect(&mut auth, &wire);
        auth.record.session.as_mut().unwrap().expires_at = 1;
        wire.lock().unwrap().responses.push_back(Err(NetworkError::Uncertain));
        assert!(auth.request_identity().is_err());
        let saved = memory.lock().unwrap().raw.clone();
        let shared = SharedAuth::new(auth);
        let quiescence = shared.quiesce().await.unwrap();
        assert_eq!(memory.lock().unwrap().raw, saved);
        assert!(quiescence.guard.as_ref().unwrap().record.session.as_ref().unwrap().refresh_in_flight);
        drop(quiescence);
        {
            let mut auth = shared.try_lock().unwrap();
            auth.record.session.as_mut().unwrap().refresh_token = "refresh-new".into();
            memory.lock().unwrap().fail = true;
            assert!(auth.persist().is_err());
            auth.state("connected", None);
        }
        assert!(shared.quiesce().await.is_err());
        assert_eq!(memory.lock().unwrap().raw, saved);
        assert!(shared.try_lock().is_ok());
        memory.lock().unwrap().fail = false;
        assert!(shared.try_lock().unwrap().persist().is_ok());
        assert!(shared.quiesce().await.is_ok());
    }
    fn heartbeat_fixture() -> (Arc<SharedAuth>, Arc<Mutex<Wire>>) {
        let (mut auth, _, wire) = fixture();
        connect(&mut auth, &wire);
        auth.bridge_access = Some("access-a".into());
        auth.set_bridge_connected(true);
        (Arc::new(SharedAuth::new(auth)), wire)
    }
    #[test]
    fn heartbeat_renews_expired_access_once_through_verified_native_owner() {
        let (shared, wire) = heartbeat_fixture();
        shared.try_lock().unwrap().record.session.as_mut().unwrap().expires_at = now();
        let expected = shared.heartbeat_custody().unwrap();
        wire.lock().unwrap().responses.extend([
            ok(tokens("usr_a", "renewed-access", "renewed-refresh")), ok(identity()),
        ]);
        assert!(shared.renew_heartbeat(&expected, |auth| {
            assert!(auth.bridge_access.is_none());
            assert!(!auth.status().bridge_connected);
            let body = auth.bridge_session().unwrap();
            assert_eq!(body["account_id"], "usr_a");
            assert_eq!(body["device_id"], "device-a");
            assert_eq!(body["access_token"], "renewed-access");
            assert!(body.get("refresh_token").is_none());
            auth.bridge_access = Some("renewed-access".into());
            auth.set_bridge_connected(true);
        }));
        assert!(!shared.renew_heartbeat(&expected, |_| panic!("duplicate heartbeat repair")));
        assert_eq!(wire.lock().unwrap().paths, [
            "/auth/device/token", "/auth/session", "/auth/device/refresh", "/auth/session",
        ]);
    }
    #[test]
    fn heartbeat_forces_same_access_handoff_and_only_latest_response_can_repair() {
        let (shared, wire) = heartbeat_fixture();
        let older = shared.heartbeat_custody().unwrap();
        let current = shared.heartbeat_custody().unwrap();
        assert!(!shared.renew_heartbeat(&older, |_| panic!("retired heartbeat repair")));
        assert!(shared.renew_heartbeat(&current, |auth| {
            assert!(auth.bridge_access.is_none());
            assert!(!auth.status().bridge_connected);
            assert_eq!(auth.bridge_session().unwrap()["access_token"], "access-a");
            auth.bridge_access = Some("access-a".into());
            auth.set_bridge_connected(true);
        }));
        assert!(!shared.renew_heartbeat(&current, |_| panic!("duplicate same-access repair")));
        assert_eq!(wire.lock().unwrap().paths.len(), 2, "unexpired verified access does not repeat token renewal");
    }
    #[test]
    fn heartbeat_skips_busy_auth_without_queuing_or_duplicating_the_repair() {
        let (shared, _) = heartbeat_fixture();
        let expected = shared.heartbeat_custody().unwrap();
        let guard = shared.try_lock().unwrap();
        assert!(shared.heartbeat_custody().is_none());
        assert!(!shared.renew_heartbeat(&expected, |_| panic!("busy repair queued")));
        drop(guard);
        assert!(shared.renew_heartbeat(&expected, |_| {}));
        assert!(!shared.renew_heartbeat(&expected, |_| panic!("duplicate admitted repair")));
    }
    #[test]
    fn heartbeat_rejects_signed_out_and_returned_account_generations() {
        let (shared, wire) = heartbeat_fixture();
        let expected = shared.heartbeat_custody().unwrap();
        { shared.try_lock().unwrap().state("signed_out", None); }
        assert!(shared.heartbeat_custody().is_none());
        assert!(!shared.renew_heartbeat(&expected, |_| panic!("signed-out repair")));
        {
            let mut auth = shared.try_lock().unwrap();
            auth.record.session.as_mut().unwrap().subject = "usr_b".into();
            auth.state("connected", None);
        }
        {
            let mut auth = shared.try_lock().unwrap();
            auth.record.session.as_mut().unwrap().subject = "usr_a".into();
            auth.state("connected", None);
        }
        assert!(shared.heartbeat_custody().is_some());
        assert!(!shared.renew_heartbeat(&expected, |_| panic!("A→B→A restored a retired heartbeat")));
        assert_eq!(wire.lock().unwrap().paths.len(), 2);
    }
    #[test]
    fn heartbeat_only_admits_connected_or_offline_owned_sessions() {
        let (shared, wire) = heartbeat_fixture();
        for state in ["signed_out", "authorizing", "account_mismatch", "reauth_required", "revocation_pending", "keychain_unavailable"] {
            shared.try_lock().unwrap().state(state, None);
            assert!(shared.heartbeat_custody().is_none(), "{state} must not start background auth");
        }
        shared.try_lock().unwrap().state("offline", None);
        assert!(shared.heartbeat_custody().is_some());
        shared.try_lock().unwrap().record.session.as_mut().unwrap().refresh_in_flight = true;
        assert!(shared.heartbeat_custody().is_none());
        assert_eq!(wire.lock().unwrap().paths.len(), 2);
    }
    #[test]
    fn heartbeat_uncertain_refresh_keeps_session_without_replaying_renewal() {
        let (shared, wire) = heartbeat_fixture();
        shared.try_lock().unwrap().record.session.as_mut().unwrap().expires_at = now();
        let expected = shared.heartbeat_custody().unwrap();
        wire.lock().unwrap().responses.push_back(Err(NetworkError::Uncertain));
        assert!(shared.renew_heartbeat(&expected, |auth| {
            assert_eq!(auth.status().state, "offline");
            assert!(auth.record.session.as_ref().unwrap().refresh_in_flight);
            assert!(auth.bridge_session().is_none());
            assert!(auth.bridge_access.is_none());
        }));
        assert!(shared.heartbeat_custody().is_none());
        assert!(!shared.renew_heartbeat(&expected, |_| panic!("unknown refresh replayed")));
        assert_eq!(wire.lock().unwrap().paths, ["/auth/device/token", "/auth/session", "/auth/device/refresh"]);
    }
    #[test]
    fn automatic_retry_verifies_saved_successor_after_post_refresh_outage() {
        for status in [0, 429, 503] {
            let (mut auth, vault, wire) = fixture();
            connect(&mut auth, &wire);
            auth.record.session.as_mut().unwrap().expires_at = 1;
            let unavailable = if status == 0 { Err(NetworkError::Uncertain) }
                else { Ok(Reply::error(status, "private_upstream_detail")) };
            wire.lock().unwrap().responses.extend([
                ok(tokens("usr_a", "access-new", "refresh-new")), unavailable,
            ]);
            assert_eq!(auth.reconnect(None).state, "offline");
            assert!(auth.refresh_retry_at > 0, "verification must schedule recovery");
            let session = auth.record.session.as_ref().unwrap();
            assert_eq!(session.refresh_token, "refresh-new");
            assert!(!session.refresh_in_flight);
            assert!(vault.lock().unwrap().raw.as_ref().unwrap().contains("refresh-new"));
            assert!(auth.bridge_session().is_none());
            auth.refresh_retry_at = now() + 30;
            let shared = SharedAuth::new(auth);
            assert!(!shared.retry_refresh(|_| panic!("backoff was bypassed")));
            assert_eq!(shared.try_lock().unwrap().reconnect(None).state, "offline");
            assert_eq!(wire.lock().unwrap().paths.len(), 4);
            shared.try_lock().unwrap().refresh_retry_at = now().saturating_sub(1);
            wire.lock().unwrap().responses.push_back(ok(identity()));
            assert!(shared.retry_refresh(|auth| {
                assert_eq!(auth.bridge_session().unwrap()["access_token"], "access-new");
                auth.bridge_access = Some("access-new".into());
                auth.set_bridge_connected(true);
            }));
            assert_eq!(shared.status().state, "connected");
            assert!(shared.status().bridge_connected);
            assert_eq!(shared.try_lock().unwrap().refresh_retry_attempts, 0);
            let wire = wire.lock().unwrap();
            assert_eq!(wire.paths, ["/auth/device/token", "/auth/session", "/auth/device/refresh", "/auth/session", "/auth/session"]);
            assert_eq!(wire.bearers[3..], [Some("access-new".into()), Some("access-new".into())]);
        }
    }
    #[test]
    fn automatic_refresh_retry_recovers_idle_session_after_backoff_and_skips_busy_auth() {
        let (shared, wire) = heartbeat_fixture();
        shared.try_lock().unwrap().record.session.as_mut().unwrap().expires_at = now();
        let expected = shared.heartbeat_custody().unwrap();
        wire.lock().unwrap().responses.push_back(Err(NetworkError::Uncertain));
        shared.renew_heartbeat(&expected, |_| {});
        assert!(!shared.retry_refresh(|_| panic!("backoff was bypassed")));
        assert!(shared.heartbeat_custody().is_none());
        let mut auth = shared.try_lock().unwrap();
        auth.refresh_retry_at = now().saturating_sub(1);
        assert!(!shared.retry_refresh(|_| panic!("busy auth was queued")));
        drop(auth);
        wire.lock().unwrap().responses.extend([
            ok(tokens("usr_a", "access-new", "refresh-new")), ok(identity()),
        ]);
        assert!(shared.retry_refresh(|auth| {
            assert_eq!(auth.status().state, "connected");
            assert_eq!(auth.bridge_session().unwrap()["access_token"], "access-new");
            auth.bridge_access = Some("access-new".into());
            auth.set_bridge_connected(true);
        }));
        assert_eq!(shared.status().state, "connected");
        assert!(shared.status().bridge_connected);
        let bodies = wire.lock().unwrap().bodies.clone();
        assert_eq!(bodies[2], bodies[3]);
        assert!(!shared.retry_refresh(|_| panic!("connected session retried")));
    }
    #[test]
    fn automatic_refresh_retry_never_reopens_retired_or_blocked_account_states() {
        let (shared, _) = heartbeat_fixture();
        for state in ["signed_out", "authorizing", "account_mismatch", "reauth_required", "revocation_pending", "keychain_unavailable"] {
            let mut auth = shared.try_lock().unwrap();
            auth.record.session.as_mut().unwrap().refresh_in_flight = true;
            auth.refresh_retry_at = now().saturating_sub(1);
            auth.state(state, None);
            drop(auth);
            assert!(!shared.retry_refresh(|_| panic!("retired account retried")));
        }
    }
    #[test]
    fn selected_storage_matches_explicit_build_channel() {
        let auth = NativeAuth::new(crate::channel::gateway().into());
        assert_eq!(auth.vault.is_persistent(), !crate::channel::session_only());
    }
    #[test]
    fn local_founder_device_subject_is_never_accepted_by_hosted_builds() {
        assert!(valid_device_subject("usr_account", "https://gateway.supymem.com", false));
        assert!(!valid_device_subject("founder", "https://gateway.supymem.com", false));
        assert!(!valid_device_subject("founder", "https://gateway.supymem.com", true));
        assert!(!valid_device_subject("founder", "http://127.0.0.1:3200", false));
        assert!(valid_device_subject("founder", "http://127.0.0.1:3200", true));
    }
    #[test]
    fn temporary_store_has_only_instance_lifetime_and_can_be_cleared() {
        let store = SessionVault::default();
        assert!(!store.is_persistent());
        store.save("temporary test credential").unwrap();
        assert_eq!(store.load().unwrap().as_deref(), Some("temporary test credential"));
        assert!(SessionVault::default().load().unwrap().is_none());
        store.clear().unwrap();
        assert!(store.load().unwrap().is_none());
    }
    #[test]
    fn temporary_session_still_requires_pkce_and_server_verification_without_os_wait() {
        let wire = Arc::new(Mutex::new(Wire::default()));
        let mut auth = NativeAuth::with_dependencies(crate::channel::gateway().into(),
            Box::new(SessionVault::default()), Box::new(TestHttp(wire.clone())));
        let notices = Arc::new(Mutex::new(Vec::new()));
        let observed = notices.clone();
        *auth.activity.listener.lock().unwrap() = Some(Arc::new(move |status| {
            observed.lock().unwrap().push(status.state);
        }));
        assert_eq!(auth.reconnect(None).state, "signed_out");
        connect(&mut auth, &wire);
        assert!(!auth.status().persisted);
        assert!(!auth.activity.pending.load(Ordering::SeqCst));
        assert!(notices.lock().unwrap().is_empty());
        assert_eq!(wire.lock().unwrap().paths, ["/auth/device/token", "/auth/session"]);
        let raw_status = serde_json::to_string(&auth.status()).unwrap();
        assert!(!raw_status.contains("access-a") && !raw_status.contains("refresh-a"));
        drop(auth);
        let mut reopened = NativeAuth::with_dependencies(crate::channel::gateway().into(),
            Box::new(SessionVault::default()), Box::new(TestHttp(wire.clone())));
        assert_eq!(reopened.reconnect(None).state, "signed_out");
        assert_eq!(wire.lock().unwrap().paths.len(), 2);
    }
    #[test]
    fn temporary_session_rejects_foreign_callback_and_unknown_exchange_without_replay() {
        let wire = Arc::new(Mutex::new(Wire::default()));
        let mut auth = NativeAuth::with_dependencies(crate::channel::gateway().into(),
            Box::new(SessionVault::default()), Box::new(TestHttp(wire.clone())));
        let callback = start(&mut auth);
        let foreign = callback.replace(auth.record.pending.as_ref().unwrap().state.as_str(), &random());
        assert_eq!(auth.callback(&foreign).err().unwrap().data["error"]["code"], "callback_state_mismatch");
        assert!(wire.lock().unwrap().paths.is_empty());
        wire.lock().unwrap().responses.push_back(Err(NetworkError::Uncertain));
        assert!(auth.callback(&callback).is_err());
        assert!(auth.callback(&callback).is_err());
        assert_eq!(wire.lock().unwrap().paths, ["/auth/device/token"]);
        assert_eq!(auth.status().state, "reauth_required");
        assert!(!auth.status().persisted);
    }
    #[test]
    fn temporary_session_refresh_and_signout_use_real_native_protocol_without_persistence() {
        let wire = Arc::new(Mutex::new(Wire::default()));
        let mut auth = NativeAuth::with_dependencies(crate::channel::gateway().into(),
            Box::new(SessionVault::default()), Box::new(TestHttp(wire.clone())));
        connect(&mut auth, &wire);
        auth.record.session.as_mut().unwrap().expires_at = now();
        wire.lock().unwrap().responses.extend([
            ok(tokens("usr_a", "access-refreshed", "refresh-refreshed")), ok(identity()), ok(json!({"ok":true}))
        ]);
        assert_eq!(auth.reconnect(None).state, "connected");
        assert_eq!(auth.record.session.as_ref().unwrap().access_token, "access-refreshed");
        assert!(!auth.status().persisted);
        assert_eq!(auth.sign_out().state, "signed_out");
        assert!(auth.record.session.is_none() && auth.vault.load().unwrap().is_none());
        assert_eq!(wire.lock().unwrap().paths, ["/auth/device/token", "/auth/session", "/auth/device/refresh", "/auth/session", "/auth/device/revoke"]);
        assert_eq!(auth.activity.epoch.load(Ordering::SeqCst), 0);
    }
    #[test]
    fn copyable_login_link_exposes_only_challenge_and_survives_browser_failure() {
        let (mut a, _, _) = fixture();
        let url = a.start(None).ok().unwrap();
        let verifier = a.record.pending.as_ref().unwrap().verifier.clone();
        assert_eq!(a.status().login_url.as_deref(), Some(url.as_str()));
        assert!(!serde_json::to_string(&a.status()).unwrap().contains(&verifier));
        a.browser_failed();
        assert_eq!(a.status().state, "authorizing");
        assert_eq!(a.status().login_url.as_deref(), Some(url.as_str()));
        a.record.pending.as_mut().unwrap().created = now() - 301;
        assert!(a.status().login_url.is_none());
        a.sign_out();
        assert!(a.status().login_url.is_none());
    }
    #[test]
    fn pkce_is_random_s256_and_pending_survives_restart() {
        let (mut a, v, w) = fixture();
        let url = a.start(Some("usr_a".into())).ok().unwrap();
        let pending = a.record.pending.clone().unwrap();
        let parsed = Url::parse(&url).unwrap();
        let q: HashMap<_, _> = parsed.query_pairs().collect();
        assert_eq!(
            q["code_challenge"],
            URL_SAFE_NO_PAD.encode(Sha256::digest(pending.verifier.as_bytes()))
        );
        assert_eq!(q["state"], pending.state);
        assert!(!url.contains(&pending.verifier));
        let mut restored = NativeAuth::with_dependencies(
            a.gateway.clone(),
            Box::new(TestVault(v)),
            Box::new(TestHttp(w.clone())),
        );
        w.lock()
            .unwrap()
            .responses
            .extend([ok(tokens("usr_a", "access-a", "refresh-a")), ok(identity())]);
        assert!(restored
            .callback(&format!(
                "{}://auth/callback?code=fixture_code_1234567890&state={}",
                crate::channel::callback_scheme(),
                pending.state
            ))
            .is_ok());
        assert_eq!(restored.status().state, "connected");
    }
    #[test]
    fn callback_rejects_state_duplicate_token_links_and_foreign_origins() {
        let (mut a, _, w) = fixture();
        let callback = start(&mut a);
        for bad in [
            callback.replace(&format!("{}:", crate::channel::callback_scheme()), "https:"),
            format!("{callback}&code=duplicate"),
            format!("{callback}#secret"),
            callback.replace("/callback", "/callback/other"),
            callback.replace("state=", "state=wrong"),
            "supymem://auth/callback?token=unsafe".into(),
        ] {
            assert!(a.callback(&bad).is_err());
            assert!(a.record.pending.is_some());
        }
        assert!(w.lock().unwrap().paths.is_empty());
    }
    #[test]
    fn expired_and_replayed_callback_cannot_exchange() {
        let (mut a, _, w) = fixture();
        let callback = start(&mut a);
        a.record.pending.as_mut().unwrap().created = now() - 301;
        assert!(a.callback(&callback).is_err());
        assert!(a.record.pending.is_none());
        assert!(a.callback(&callback).is_err());
        assert!(w.lock().unwrap().paths.is_empty());
    }
    #[test]
    fn callback_one_use_after_success_and_lost_response() {
        let (mut a, _, w) = fixture();
        let callback = start(&mut a);
        w.lock()
            .unwrap()
            .responses
            .push_back(Err(NetworkError::Uncertain));
        assert!(a.callback(&callback).is_err());
        assert!(a.callback(&callback).is_err());
        assert_eq!(w.lock().unwrap().paths.len(), 1);
        connect(&mut a, &w);
        assert!(a.callback(&callback).is_err());
    }
    #[test]
    fn mismatched_approved_account_is_never_connected() {
        let (mut a, _, w) = fixture();
        let callback = start(&mut a);
        w.lock().unwrap().responses.extend([
            ok(tokens("usr_b", "foreign", "refresh-b")),
            ok(json!({"ok":true})),
        ]);
        assert!(a.callback(&callback).is_err());
        assert_eq!(a.status().state, "account_mismatch");
        assert!(a.bridge_session().is_none());
    }
    #[test]
    fn keychain_unavailable_stops_before_browser_authorization() {
        let (mut a, v, w) = fixture();
        v.lock().unwrap().fail = true;
        assert!(a.start(None).is_err());
        assert_eq!(a.status().state, "keychain_unavailable");
        assert!(w.lock().unwrap().paths.is_empty());
        assert_eq!(
            a.api("GET", "/auth/session", None, HashMap::new()).status,
            503
        );
    }
    #[test]
    fn verified_session_never_exports_credentials_or_ambient_csrf() {
        let (mut a, v, w) = fixture();
        connect(&mut a, &w);
        w.lock().unwrap().responses.push_back(ok(identity()));
        let reply = a.api("GET", "/auth/session", None, HashMap::new());
        let public = serde_json::to_string(&(a.status(), reply)).unwrap();
        for secret in ["access-a", "refresh-a", "ambient-cookie-csrf", "verifier"] {
            assert!(!public.contains(secret));
        }
        assert!(v
            .lock()
            .unwrap()
            .raw
            .as_ref()
            .unwrap()
            .contains("refresh-a")); // fixture secure vault only
        assert_eq!(a.bridge_session().unwrap()["access_token"], "access-a");
        assert!(a.bridge_session().unwrap().get("refresh_token").is_none());
    }
    #[test]
    fn revoked_session_clears_secure_credentials_and_bridge() {
        let (mut a, v, w) = fixture();
        connect(&mut a, &w);
        w.lock()
            .unwrap()
            .responses
            .extend([Ok(Reply::error(401, "revoked")), Ok(Reply::error(401, "unauthenticated")), Ok(Reply::error(401, "revoked"))]);
        assert_eq!(
            a.api("GET", "/api/ventures", None, HashMap::new()).status,
            401
        );
        let retained = v.lock().unwrap().raw.clone().unwrap();
        assert!(!retained.contains("access-a"));
        assert!(!retained.contains("refresh-a"));
        assert!(retained.contains("usr_a"));
        assert!(a.bridge_session().is_none());
        assert_eq!(a.status().state, "reauth_required");
    }
    #[test]
    fn feature_401_preserves_device_after_same_subject_session_verification() {
        let (mut a, v, w) = fixture();
        connect(&mut a, &w);
        w.lock().unwrap().responses.extend([
            Ok(Reply::error(401, "feature_credential_required")),
            ok(identity()),
        ]);
        let reply = a.api("GET", "/api/ventures", None, HashMap::new());
        assert_eq!(reply.status, 401);
        assert_eq!(reply.data["error"]["code"], "feature_credential_required");
        assert_eq!(a.status().state, "connected");
        assert_eq!(a.status().subject.as_deref(), Some("usr_a"));
        assert!(v.lock().unwrap().raw.as_ref().unwrap().contains("refresh-a"));
        assert_eq!(w.lock().unwrap().paths.last().map(String::as_str), Some("/auth/session"));
    }
    #[test]
    fn background_feature_401_only_revokes_after_session_rejects_bearer() {
        let (mut a, v, w) = fixture();
        connect(&mut a, &w);
        let request_identity = a.request_identity().ok().expect("connected identity");
        // Raw transfers and event streams use this same confirmation path.
        w.lock().unwrap().responses.push_back(ok(identity()));
        assert!(a.confirm_feature_unauthorized(&request_identity).is_ok());
        assert_eq!(a.status().state, "connected");
        assert!(v.lock().unwrap().raw.as_ref().unwrap().contains("refresh-a"));

        w.lock().unwrap().responses.extend([Ok(Reply::error(401, "unauthenticated")), Ok(Reply::error(401,"revoked"))]);
        assert_eq!(a.confirm_feature_unauthorized(&request_identity).unwrap_err().status, 401);
        assert_eq!(a.status().state, "reauth_required");
        assert!(!v.lock().unwrap().raw.as_ref().unwrap().contains("refresh-a"));
        assert_eq!(w.lock().unwrap().paths.last().map(String::as_str), Some("/auth/device/refresh"));
    }
    #[test]
    fn background_feature_401_does_not_erase_bearer_on_inconclusive_check() {
        let (mut a, v, w) = fixture();
        connect(&mut a, &w);
        let identity = a.request_identity().ok().expect("connected identity");
        w.lock().unwrap().responses.push_back(Ok(Reply::error(403, "access_denied")));
        let result = a.confirm_feature_unauthorized(&identity).unwrap_err();
        assert_eq!(result.status, 503);
        assert_eq!(a.status().state, "offline");
        assert!(v.lock().unwrap().raw.as_ref().unwrap().contains("refresh-a"));
        assert!(a.bridge_session().is_none());
    }
    #[test]
    fn denied_session_verification_keeps_credentials_but_admits_no_work() {
        let (mut a, v, w) = fixture();
        connect(&mut a, &w);
        // A transient edge-layer denial is not a bearer revocation receipt.
        a.verified_at = 0;
        w.lock().unwrap().responses.push_back(Ok(Reply::error(403, "access_denied")));
        let reply = a.api("GET", "/api/ventures", None, HashMap::new());
        assert_eq!(reply.status, 403);
        assert_eq!(a.status().state, "offline");
        assert!(v.lock().unwrap().raw.as_ref().unwrap().contains("refresh-a"));
        assert_eq!(w.lock().unwrap().paths.last().map(String::as_str), Some("/auth/session"));
        // Until a fresh successful verification, the requested feature call
        // must never be dispatched.
        w.lock().unwrap().responses.extend([ok(identity()), ok(json!({"items":[]}))]);
        a.refresh_retry_at = now() + 30;
        assert_eq!(a.api("GET", "/api/ventures", None, HashMap::new()).status, 503);
        assert_eq!(w.lock().unwrap().paths.last().map(String::as_str), Some("/auth/session"));
        a.refresh_retry_at = now().saturating_sub(1);
        let next = a.api("GET", "/api/ventures", None, HashMap::new());
        assert_eq!(next.status, 200);
        assert_eq!(a.status().state, "connected");
    }
    #[test]
    fn feature_401_with_inconclusive_session_check_stays_offline() {
        let (mut a, v, w) = fixture();
        connect(&mut a, &w);
        w.lock().unwrap().responses.extend([
            Ok(Reply::error(401, "feature_credential_required")),
            Ok(Reply::error(403, "access_denied")),
        ]);
        let reply = a.api("GET", "/api/ventures", None, HashMap::new());
        assert_eq!(reply.status, 503);
        assert_eq!(reply.data["error"]["code"], "session_verification_failed");
        assert_eq!(a.status().state, "offline");
        assert!(v.lock().unwrap().raw.as_ref().unwrap().contains("refresh-a"));
        assert!(a.bridge_session().is_none());
        assert_eq!(w.lock().unwrap().paths.last().map(String::as_str), Some("/auth/session"));
    }
    #[test]
    fn expired_access_rotates_once_and_saves_before_use() {
        let (mut a, v, w) = fixture();
        connect(&mut a, &w);
        a.record.session.as_mut().unwrap().expires_at = 1;
        w.lock().unwrap().responses.extend([
            ok(tokens("usr_a", "access-new", "refresh-new")),
            ok(identity()),
            ok(json!({"items":[]})),
            ok(json!({"items":[]})),
        ]);
        assert_eq!(
            a.api("GET", "/api/ventures", None, HashMap::new()).status,
            200
        );
        assert!(v
            .lock()
            .unwrap()
            .raw
            .as_ref()
            .unwrap()
            .contains("refresh-new"));
        assert_eq!(
            a.api("GET", "/api/ventures", None, HashMap::new()).status,
            200
        );
        let w = w.lock().unwrap();
        assert_eq!(
            w.paths
                .iter()
                .filter(|p| *p == "/auth/device/refresh")
                .count(),
            1
        );
        assert_eq!(w.bearers.last().unwrap().as_deref(), Some("access-new"));
    }
    #[test]
    fn interrupted_rotation_recovers_same_token_on_restart() {
        let (mut a, v, w) = fixture();
        connect(&mut a, &w);
        a.record.session.as_mut().unwrap().refresh_in_flight = true;
        assert!(a.persist().is_ok());
        let mut restored = NativeAuth::with_dependencies(
            a.gateway.clone(), Box::new(TestVault(v)), Box::new(TestHttp(w.clone())),
        );
        w.lock().unwrap().responses.extend([
            ok(tokens("usr_a", "access-new", "refresh-new")), ok(identity()), ok(json!({"items":[]})),
        ]);
        assert_eq!(restored.api("GET", "/api/ventures", None, HashMap::new()).status, 200);
        assert!(!restored.record.session.as_ref().unwrap().refresh_in_flight);
        assert_eq!(w.lock().unwrap().bodies[2].as_ref().unwrap()["refresh_token"], "refresh-a");
    }
    #[test]
    fn lost_refresh_response_retries_same_token_after_backoff() {
        let (mut a, v, w) = fixture();
        connect(&mut a, &w);
        a.record.session.as_mut().unwrap().expires_at = 1;
        w.lock().unwrap().responses.push_back(Err(NetworkError::Uncertain));
        assert_eq!(a.api("GET", "/api/ventures", None, HashMap::new()).status, 503);
        assert_eq!(a.status().state, "offline");
        assert!(a.record.session.as_ref().unwrap().refresh_in_flight);
        assert!(v.lock().unwrap().raw.as_ref().unwrap().contains("refresh-a"));
        assert!(a.refresh_retry_at > now());
        assert_eq!(a.api("GET", "/api/ventures", None, HashMap::new()).status, 503);
        assert_eq!(w.lock().unwrap().paths.len(), 3);
        a.refresh_retry_at = 0;
        w.lock().unwrap().responses.extend([
            ok(tokens("usr_a", "access-new", "refresh-new")), ok(identity()), ok(json!({"items":[]})),
        ]);
        assert_eq!(a.api("GET", "/api/ventures", None, HashMap::new()).status, 200);
        assert_eq!(a.status().state, "connected");
        assert!(!a.record.session.as_ref().unwrap().refresh_in_flight);
        assert_eq!(a.refresh_retry_attempts, 0);
        let wire = w.lock().unwrap();
        assert_eq!(wire.bodies[2], wire.bodies[3]);
        assert_eq!(wire.bodies[3].as_ref().unwrap()["refresh_token"], "refresh-a");
    }
    #[test]
    fn uncertain_refresh_restart_cannot_silently_switch_accounts() {
        let (mut a, v, w) = fixture();
        connect(&mut a, &w);
        a.record.session.as_mut().unwrap().expires_at = 1;
        w.lock().unwrap().responses.push_back(Err(NetworkError::Uncertain));
        assert_eq!(a.api("GET", "/api/ventures", None, HashMap::new()).status, 503);
        let mut restored = NativeAuth::with_dependencies(
            a.gateway.clone(), Box::new(TestVault(v)), Box::new(TestHttp(w.clone())),
        );
        assert_eq!(restored.start(None).err().unwrap().status, 409);
        assert_eq!(restored.reconnect(Some("usr_b")).state, "account_mismatch");
        assert_eq!(restored.record.session.as_ref().unwrap().subject, "usr_a");
        assert_eq!(w.lock().unwrap().paths.len(), 3);
        w.lock().unwrap().responses.extend([
            ok(tokens("usr_b", "foreign", "foreign-refresh")), ok(json!({"revoked":true})),
        ]);
        assert_eq!(restored.reconnect(Some("usr_a")).state, "account_mismatch");
        assert_eq!(restored.status().subject.as_deref(), Some("usr_a"));
        assert!(restored.record.session.is_none());
    }
    #[test]
    fn inconclusive_refresh_keeps_session_and_reports_only_closed_response_class() {
        for (status, class) in [(401, "4xx"), (400, "4xx"), (429, "4xx"), (503, "5xx"), (502, "5xx")] {
            let (mut a, vault, wire) = fixture();
            connect(&mut a, &wire);
            a.record.session.as_mut().unwrap().expires_at = 1;
            wire.lock().unwrap().responses.push_back(Ok(Reply::error(status, "private_upstream_detail")));
            assert_eq!(a.api("GET", "/api/ventures", None, HashMap::new()).status, 503);
            let state = a.status();
            assert_eq!(state.state, "offline");
            assert_eq!(state.http_status_class.as_deref(), Some(class));
            assert_eq!(a.record.session.as_ref().unwrap().refresh_in_flight, status != 429);
            assert!(vault.lock().unwrap().raw.as_ref().unwrap().contains("refresh-a"));
            assert!(!serde_json::to_string(&state).unwrap().contains("private_upstream_detail"));
            a.refresh_retry_at = 0;
            wire.lock().unwrap().responses.extend([
                ok(tokens("usr_a", "access-new", "refresh-new")), ok(identity()),
            ]);
            assert_eq!(a.reconnect(None).state, "connected");
            let wire = wire.lock().unwrap();
            assert_eq!(wire.bodies[2], wire.bodies[3]);
        }
    }
    #[test]
    fn rate_limited_refresh_preserves_credentials_and_honors_retry_time() {
        let (mut auth, vault, wire) = fixture();
        connect(&mut auth, &wire);
        auth.record.session.as_mut().unwrap().expires_at = 1;
        wire.lock().unwrap().responses.push_back(Ok(Reply {
            status: 429,
            data: json!({"error":{"code":"rate_limited","retry_after":900}}),
            request_id: None,
        }));
        let before = now();
        assert_eq!(auth.api("GET", "/api/ventures", None, HashMap::new()).status, 503);
        assert!(auth.refresh_retry_at >= before + 900);
        assert!(!auth.record.session.as_ref().unwrap().refresh_in_flight);
        assert!(vault.lock().unwrap().raw.as_ref().unwrap().contains("refresh-a"));
        let requests = wire.lock().unwrap().paths.len();
        assert_eq!(auth.api("GET", "/api/ventures", None, HashMap::new()).status, 503);
        assert_eq!(wire.lock().unwrap().paths.len(), requests);
        auth.refresh_retry_at = 0;
        wire.lock().unwrap().responses.extend([
            ok(tokens("usr_a", "access-new", "refresh-new")), ok(identity()),
        ]);
        assert_eq!(auth.reconnect(None).state, "connected");
    }
    #[test]
    fn definitive_refresh_401_retires_saved_credentials() {
        for code in ["token_reuse", "revoked", "invalid_refresh", "unknown_device"] {
            let (mut a, vault, wire) = fixture();
            connect(&mut a, &wire);
            a.record.session.as_mut().unwrap().expires_at = 1;
            wire.lock().unwrap().responses.push_back(Ok(Reply::error(401, code)));
            let reply = a.api("GET", "/api/ventures", None, HashMap::new());
            assert_eq!(reply.status, 401);
            assert_eq!(reply.data["error"]["code"], "device_revoked_or_expired");
            assert_eq!(a.status().state, "reauth_required");
            assert_eq!(a.status().error.as_deref(), Some("Your Supymem session has expired or was disconnected. Sign in again to reconnect."));
            assert!(a.record.session.is_none());
            assert!(!vault.lock().unwrap().raw.as_ref().unwrap().contains("refresh-a"));
        }
    }
    #[test]
    fn malformed_success_retains_refresh_and_uncertain_retry_backoff_is_capped() {
        let (mut a, _, wire) = fixture();
        connect(&mut a, &wire);
        a.record.session.as_mut().unwrap().expires_at = 1;
        wire.lock().unwrap().responses.push_back(ok(json!({"bad":"response"})));
        assert_eq!(a.reconnect(None).state, "offline");
        assert!(a.record.session.as_ref().unwrap().refresh_in_flight);
        for _ in 0..8 {
            a.refresh_retry_at = 0;
            wire.lock().unwrap().responses.push_back(Err(NetworkError::NotSent));
            assert_eq!(a.reconnect(None).state, "offline");
            assert!(a.record.session.as_ref().unwrap().refresh_in_flight);
            assert!((1..=30).contains(&a.refresh_retry_at.saturating_sub(now())));
        }
        assert_eq!(a.refresh_retry_attempts, 6);
    }
    #[test]
    fn connect_failure_preserves_refresh_and_retries_only_unsent_request() {
        let (mut a, _, w) = fixture();
        connect(&mut a, &w);
        a.record.session.as_mut().unwrap().expires_at = 1;
        w.lock()
            .unwrap()
            .responses
            .push_back(Err(NetworkError::NotSent));
        assert_eq!(
            a.api("GET", "/api/ventures", None, HashMap::new()).status,
            0
        );
        assert_eq!(a.status().state, "offline");
        assert!(!a.record.session.as_ref().unwrap().refresh_in_flight);
    }
    #[test]
    fn refresh_cannot_switch_identity() {
        let (mut a, _, w) = fixture();
        connect(&mut a, &w);
        a.record.session.as_mut().unwrap().expires_at = 1;
        w.lock().unwrap().responses.extend([
            ok(tokens("usr_b", "foreign", "foreign-refresh")),
            ok(json!({"ok":true})),
        ]);
        let reply = a.api("GET", "/api/ventures", None, HashMap::new());
        assert_eq!(reply.status, 401);
        assert_eq!(reply.data["error"]["code"], "refresh_identity_mismatch");
        assert_eq!(a.status().state, "account_mismatch");
        assert_eq!(a.status().error.as_deref(), Some("Supymem could not verify the saved account. Sign in to the intended account and reconnect."));
        assert!(a.bridge_session().is_none());
    }
    #[test]
    fn gateway_session_subject_is_verified_independently() {
        let (mut a, _, w) = fixture();
        let callback = start(&mut a);
        w.lock().unwrap().responses.extend([
            ok(tokens("usr_a", "access-a", "refresh-a")),
            ok(json!({"authenticated":true,"subject":"usr_b"})),
        ]);
        assert!(a.callback(&callback).is_err());
        assert_eq!(a.status().state, "account_mismatch");
    }
    #[test]
    fn unauthenticated_session_refreshes_once_and_persists_before_verification() {
        let (mut auth, vault, wire) = fixture();
        connect(&mut auth, &wire);
        auth.verified_at = 0;
        wire.lock().unwrap().responses.extend([
            ok(json!({"authenticated":false})),
            ok(tokens("usr_a", "access-new", "refresh-new")), ok(identity()),
        ]);
        assert_eq!(auth.reconnect(None).state, "connected");
        assert!(vault.lock().unwrap().raw.as_ref().unwrap().contains("refresh-new"));
        assert_eq!(wire.lock().unwrap().paths.iter().filter(|path| *path == "/auth/device/refresh").count(), 1);
    }
    #[test]
    fn expired_session_reply_on_api_refreshes_instead_of_signing_out() {
        // A request prepared before sleep (or straddling a deploy) reaches the
        // gateway with an expired bearer; the saved device must survive.
        for reply in [ok(json!({"authenticated":false})), Ok(Reply::error(401, "unauthenticated"))] {
            let (mut auth, vault, wire) = fixture();
            connect(&mut auth, &wire);
            let request = auth.prepare_api("GET", "/auth/session", None, HashMap::new()).ok().unwrap();
            wire.lock().unwrap().responses.extend([
                reply, ok(tokens("usr_a", "access-new", "refresh-new")), ok(identity()), ok(identity()),
            ]);
            let response = request.send();
            let completed = auth.complete_api(&request, response);
            assert_eq!(completed.status, 200);
            assert_eq!(auth.status().state, "connected");
            assert!(vault.lock().unwrap().raw.as_ref().unwrap().contains("refresh-new"));
        }
    }
    #[test]
    fn unauthenticated_session_only_definitive_refresh_401_disables() {
        for code in ["invalid_refresh", "token_reuse", "revoked", "unknown_device", "unauthenticated"] {
            let (mut auth, vault, wire) = fixture();
            connect(&mut auth, &wire);
            auth.verified_at = 0;
            wire.lock().unwrap().responses.extend([
                ok(json!({"authenticated":false})), Ok(Reply::error(401, code)),
            ]);
            let definitive = code != "unauthenticated";
            assert_eq!(auth.reconnect(None).state, if definitive { "reauth_required" } else { "offline" });
            assert_eq!(vault.lock().unwrap().raw.as_ref().unwrap().contains("refresh-a"), !definitive);
            assert_eq!(wire.lock().unwrap().paths.iter().filter(|path| *path == "/auth/device/refresh").count(), 1);
        }
    }
    #[test]
    fn unauthenticated_session_after_refresh_stays_offline_without_a_second_refresh() {
        for expired in [false, true] {
            for response in [ok(json!({"authenticated":false})), Ok(Reply::error(401, "unauthenticated"))] {
                let (mut auth, vault, wire) = fixture();
                connect(&mut auth, &wire);
                auth.verified_at = 0;
                if expired { auth.record.session.as_mut().unwrap().expires_at = 1; }
                if !expired { wire.lock().unwrap().responses.push_back(ok(json!({"authenticated":false}))); }
                wire.lock().unwrap().responses.extend([
                    ok(tokens("usr_a", "access-new", "refresh-new")), response,
                ]);
                assert_eq!(auth.reconnect(None).state, "offline");
                assert!(vault.lock().unwrap().raw.as_ref().unwrap().contains("refresh-new"));
                assert_eq!(wire.lock().unwrap().paths.iter().filter(|path| *path == "/auth/device/refresh").count(), 1);
            }
        }
    }
    #[test]
    fn signout_offline_is_inactive_and_revocation_retries_across_restart() {
        let (mut a, v, w) = fixture();
        connect(&mut a, &w);
        w.lock()
            .unwrap()
            .responses
            .push_back(Err(NetworkError::NotSent));
        assert_eq!(a.sign_out().state, "revocation_pending");
        assert!(a.bridge_session().is_none());
        assert_eq!(
            a.api("GET", "/api/ventures", None, HashMap::new()).status,
            401
        );
        let mut restored = NativeAuth::with_dependencies(
            a.gateway.clone(),
            Box::new(TestVault(v.clone())),
            Box::new(TestHttp(w.clone())),
        );
        w.lock()
            .unwrap()
            .responses
            .push_back(ok(json!({"revoked":true})));
        assert_eq!(restored.reconnect(None).state, "signed_out");
        assert!(v.lock().unwrap().raw.is_none());
    }
    #[test]
    fn account_and_gateway_changes_require_explicit_signout() {
        let (mut a, v, w) = fixture();
        connect(&mut a, &w);
        assert_eq!(a.reconnect(Some("usr_b")).state, "account_mismatch");
        assert!(a.bridge_session().is_none());
        assert_eq!(
            a.api("GET", "/api/ventures", None, HashMap::new()).status,
            401
        );
        let mut other = NativeAuth::with_dependencies(
            "https://gateway.staging.supymem.com".into(),
            Box::new(TestVault(v)),
            Box::new(TestHttp(w.clone())),
        );
        assert_eq!(
            other
                .api("GET", "/api/ventures", None, HashMap::new())
                .status,
            401
        );
        assert_eq!(other.sign_out().state, "account_mismatch");
        assert_eq!(other.sign_out().state, "account_mismatch");
        assert_eq!(w.lock().unwrap().paths.len(), 2);
    }
    #[test]
    fn no_auth_headers_arbitrary_origins_or_traversal_from_webview() {
        for path in [
            "https://evil.test/api/ventures",
            "//evil.test/api/ventures",
            "/api/../auth/device/token",
            "/api/%2e%2e/auth/device/token",
            "/api/%252e%252e/auth/device/token",
            "/api/auth/device/token",
            "/api/tasks#leak",
            "/api/\\evil",
            "/auth/device/token",
        ] {
            assert!(!allowed_request("GET", path, &HashMap::new()), "{path}");
        }
        for header in [
            "Authorization",
            "Cookie",
            "Origin",
            "Host",
            "X-Forwarded-For",
        ] {
            assert!(!allowed_request(
                "GET",
                "/api/ventures",
                &HashMap::from([(header.into(), "secret".into())])
            ));
        }
        assert!(allowed_request(
            "POST",
            "/api/ventures",
            &HashMap::from([
                ("Idempotency-Key".into(), "op-1".into()),
                ("X-Workspace-ID".into(), "ven_a".into()),
                ("If-Match".into(), "v2".into())
            ])
        ));
        assert!(allowed_request("GET", "/api/ventures", &HashMap::from([
            ("X-Request-ID".into(), "req_abc012345678".into())
        ])));
        assert!(!allowed_request("POST", "/auth/session", &HashMap::new()));
    }
    #[test]
    fn secure_store_failure_does_not_claim_signout_or_connection() {
        let (mut a, v, w) = fixture();
        connect(&mut a, &w);
        v.lock().unwrap().fail = true;
        assert_eq!(a.sign_out().state, "keychain_unavailable");
        assert!(!a.status().persisted);
        assert!(a.bridge_session().is_none());
    }
    #[test]
    fn gateway_origin_requires_tls_or_loopback() {
        for origin in ["https://gateway.supymem.com", "http://127.0.0.1:3200"] {
            assert!(valid_origin(origin));
        }
        for origin in [
            "http://gateway.supymem.com",
            "https://user:secret@gateway.supymem.com",
            "https://gateway.supymem.com/evil",
            "https://gateway.supymem.com#evil",
        ] {
            assert!(!valid_origin(origin));
        }
    }
    #[test]
    fn native_http_does_not_follow_redirects_or_export_token() {
        use std::net::TcpListener;
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let thread = std::thread::spawn(move || {
            use std::io::Write;
            let (mut stream, _) = listener.accept().unwrap();
            let mut buf = [0; 2048];
            let n = stream.read(&mut buf).unwrap();
            let request = String::from_utf8_lossy(&buf[..n]);
            assert!(request.contains("Bearer fixture-access"));
            stream.write_all(b"HTTP/1.1 302 Found\r\nLocation: https://evil.invalid/secret\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").unwrap();
        });
        let response = NativeHttp.send(
            &format!("http://{addr}"),
            "GET",
            "/auth/session",
            Some("fixture-access"),
            None,
            &HashMap::new(),
        );
        assert_eq!(response.ok().unwrap().status, 302);
        thread.join().unwrap();
    }
    #[test]
    fn chat_post_gets_a_longer_response_window_without_extending_other_actions() {
        assert_eq!(native_request_timeout("POST", "/api/ventures/ven_example/chat"), Duration::from_secs(60));
        assert_eq!(native_request_timeout("POST", "/api/ventures/ven_example/chat?draft=1"), Duration::from_secs(60));
        assert_eq!(native_request_timeout("GET", "/api/ventures/ven_example/chat"), Duration::from_secs(15));
        assert_eq!(native_request_timeout("POST", "/api/ventures/ven_example/chat/confirm"), Duration::from_secs(15));
        assert_eq!(native_request_timeout("GET", "/auth/session"), Duration::from_secs(15));
        assert_eq!(native_request_timeout("POST", "/auth/device/refresh"), Duration::from_secs(15));
        assert_eq!(native_request_timeout("POST", "/api/suggestions/sug_example/decision"), Duration::from_secs(15));
    }
    #[test]
    #[cfg(target_os = "macos")]
    #[ignore = "explicit native Keychain integration check; creates and removes an isolated test item"]
    fn macos_keychain_ephemeral_roundtrip() {
        let service = format!("com.supymem.desktop.test.{}", random());
        let entry = keyring::Entry::new(&service, "fixture").unwrap();
        let secret = random();
        entry
            .set_password(&secret)
            .expect("native Keychain write failed");
        let result = keyring::Entry::new(&service, "fixture")
            .unwrap()
            .get_password();
        let removed = entry.delete_credential();
        assert_eq!(result.ok().as_deref(), Some(secret.as_str()));
        assert!(removed.is_ok());
        assert!(matches!(entry.get_password(), Err(keyring::Error::NoEntry)));
    }
    #[test]
    fn native_account_ipc_requires_main_window_and_exact_console_origin() {
        let console = "https://app.example.invalid";
        for location in [
            "https://app.example.invalid/companies",
            "tauri://localhost/index.html",
        ] {
            assert!(trusted_account_window(
                "main",
                &Url::parse(location).unwrap(),
                console
            ));
        }
        for location in [
            "http://localhost:4242",
            "https://app.staging.example.invalid",
            "https://app.example.invalid.evil.test",
            "https://user:secret@app.example.invalid",
            "file:///tmp/unsafe.html",
        ] {
            assert!(!trusted_account_window(
                "main",
                &Url::parse(location).unwrap(),
                console
            ));
        }
        assert!(!trusted_account_window(
            "coach",
            &Url::parse(console).unwrap(),
            console
        ));
        assert!(trusted_account_window(
            "main",
            &Url::parse("http://localhost:3100/idea").unwrap(),
            "http://localhost:3100"
        ));
        assert!(!trusted_account_window(
            "main",
            &Url::parse("http://localhost:4242/idea").unwrap(),
            "http://localhost:3100"
        ));
    }
    #[test]
    fn in_flight_file_and_stream_proofs_are_invalid_after_account_change_or_signout() {
        let (mut auth, _, wire) = fixture();
        connect(&mut auth, &wire);
        let proof = auth.request_identity().ok().unwrap();
        assert!(auth.matches_identity(&proof));
        auth.reconnect(Some("usr_b"));
        assert!(!auth.matches_identity(&proof));
        wire.lock().unwrap().responses.push_back(ok(identity()));
        auth.reconnect(Some("usr_a"));
        assert!(auth.matches_identity(&proof));
        assert!(auth.prepare_sign_out());
        assert!(!auth.matches_identity(&proof));
    }
}
