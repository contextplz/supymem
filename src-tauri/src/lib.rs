mod auth;
mod dictation;
mod device_order;
mod channel;
#[cfg(target_os = "macos")]
mod cua_host;
mod console_connection;
mod local_stack;
mod local_credentials;
mod local_diagnostics;
mod webview_diagnostics;
#[cfg(feature = "local-stack")]
pub use local_stack::smoke as local_stack_smoke;
mod http;
mod transport;
mod events;
mod crash;
mod docker;
mod ide;
mod install;
mod mac_notice;
mod suggestions;
mod nav_guard;
mod native_security;
mod venture_link;
mod perms;
mod error_capture;
mod shell_events;
mod permissions;
mod pty;
mod stdio;
mod supervisor;
mod tools;
mod update;
#[cfg(target_os = "macos")]
mod update_install;
mod agent_cursor;
#[cfg(target_os = "macos")]
mod agent_stop_pill;
mod emergency_stop;
mod charlie_prefs;
#[cfg(target_os = "macos")]
#[allow(dead_code)]
mod mascot;

use std::path::PathBuf;
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::Duration;

use keyring::Entry;
use pty::PtyHub;
use serde::Deserialize;
use stdio::StdioHub;
use supervisor::{conventional_data_dir, resolve_repo_root, resolve_runtime_bin, Supervisor};
use tauri::{AppHandle, Emitter, Manager, State};
use tauri_plugin_updater::UpdaterExt;
use update::UpdateCheckResult;

static LOCAL_STARTED: std::sync::OnceLock<std::time::Instant> = std::sync::OnceLock::new();

struct AppState {
    supervisor: Arc<Supervisor>,
    product_painted: std::sync::atomic::AtomicBool,
    auth: Arc<auth::SharedAuth>,
    exit_waiting: std::sync::atomic::AtomicBool,
    // Some(None): quiesce failed; quit anyway rather than trap the user in the app.
    exit_quiescence: Mutex<Option<Option<auth::AuthQuiescence>>>,
    streams: Arc<events::Hub>,
    pty: Arc<PtyHub>,
    stdio: Arc<StdioHub>,
    active_venture: Mutex<Option<String>>,
    /// Venture/order currently owned by a local Start. While set, the main
    /// window must stay visible so the founder can watch progress; Close-to-tray
    /// is deferred until the run reaches a terminal state. The bool is
    /// `focus_on_finish` (true only for app-navigation tasks).
    active_native_order: Mutex<Option<(String, String, bool)>>,
    #[cfg(target_os = "macos")]
    cua_host: Mutex<Option<(String, cua_host::CuaTaskHost)>>,
}

/// Founder-facing result of a tray/mascot pause or resume call.
fn tray_pause_message(pause: bool, status: u16, data: &serde_json::Value) -> String {
    let verb = if pause { "pause" } else { "resume" };
    if (200..300).contains(&status) {
        return if pause {
            "Paused. This venture's agents, tasks and spend are stopped.".into()
        } else {
            "Resumed. This venture's work can continue.".into()
        };
    }
    let reason = data["error"]["message"]
        .as_str()
        .or_else(|| data["detail"]["error"]["message"].as_str());
    match (reason, status) {
        (Some(r), _) => r.to_string(),
        (None, 0 | 401 | 503) => format!("Couldn't {verb}. Open Supymem and make sure you're signed in."),
        (None, _) => format!("Couldn't {verb} this venture. Open Supymem and try from Settings."),
    }
}

/// Tray / mascot Pause and Resume go through the signed-in hosted API, like the Console.
fn tray_venture_action(app: &AppHandle, pause: bool) {
    use tauri_plugin_notification::NotificationExt;
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let state = app.state::<AppState>();
        let vid = state.active_venture.lock().ok().and_then(|g| g.clone());
        let message = match (vid, app.get_webview_window("main")) {
            (Some(vid), Some(window)) => {
                let action = if pause { "pause" } else { "resume" };
                let body = if pause { serde_json::json!({ "reason": "desktop-tray" }) } else { serde_json::json!({}) };
                let control = if pause { "tray_pause" } else { "tray_resume" };
                match desktop_api_request(window, state.clone(), "POST".into(), format!("/api/ventures/{vid}/{action}"), Some(body), None).await {
                    Ok(reply) => {
                        shell_events::record("outcome", control, Some(if (200..300).contains(&reply.status) { "ok" } else { "failed" }));
                        tray_pause_message(pause, reply.status, &reply.data)
                    }
                    Err(_) => {
                        shell_events::record("outcome", control, Some("failed"));
                        tray_pause_message(pause, 0, &serde_json::Value::Null)
                    }
                }
            }
            _ => "Open a venture in Supymem first, then try again.".to_string(),
        };
        let _ = app.notification().builder()
            .title(if pause { "Pause venture" } else { "Resume venture" })
            .body(message)
            .show();
    });
}

fn sync_auth_bridge(auth: &mut auth::NativeAuth, supervisor: &Supervisor) {
    if auth.status().state != "connected" { connect_auth_bridge(auth, supervisor); }
}

fn connect_auth_bridge(auth: &mut auth::NativeAuth, supervisor: &Supervisor) {
    if (supervisor.runtime_mode() != "hosted" && !crate::channel::local_review())
        || !supervisor.is_ready() { return; }
    // The WebView and native process can outlive the private bridge helper.
    // Repair that boundary before comparing cached credentials: a replacement
    // bridge has no session even when NativeAuth still says it was connected.
    match supervisor.ensure_bridge() {
        Ok(true) => { auth.bridge_access = None; auth.set_bridge_connected(false); }
        Ok(false) => {}
        Err(error) => {
            auth.set_bridge_connected(false);
            auth.set_bridge_error(Some(&error));
            if auth.bridge_session().is_none() && (auth.bridge_access.is_some() || auth.status().state == "revocation_pending") {
                supervisor.stop_bridge(); auth.bridge_access = None;
            }
            return;
        }
    }
    auth.set_bridge_error(None);
    let body = auth.bridge_session();
    let next_access = body.as_ref().and_then(|b|b.get("access_token")).and_then(serde_json::Value::as_str).map(str::to_owned);
    if next_access == auth.bridge_access && auth.status().bridge_connected == next_access.is_some() { return; }
    let result = reqwest::blocking::Client::builder().user_agent(crate::http::USER_AGENT).timeout(Duration::from_secs(8))
        .redirect(reqwest::redirect::Policy::none()).no_proxy().build().ok().and_then(|client| {
            let path = if body.is_some() { "/native/session" } else { "/native/clear" };
            let mut request = client.post(format!("{}{path}",supervisor.bridge_url()))
                .header("x-native-token",supervisor.native_bridge_token());
            if let Some(body) = body { request = request.json(&body); }
            request.send().ok().map(|r|r.status().is_success())
        }).unwrap_or(false);
    auth.set_bridge_connected(result && next_access.is_some());
    if !result {
        let error = "The local helper could not confirm its connection. Quit and reopen Supymem; your saved sign-in and work are retained.";
        auth.set_bridge_error(Some(error));
        supervisor.defer_bridge_sync(error);
    }
    if result { auth.bridge_access = next_access; }
    else if next_access.is_none() {
        // A helper that cannot confirm credential removal must not retain an
        // authenticated outbox after local sign-out. Restart the app to restore it.
        supervisor.stop_bridge();
        auth.bridge_access = None;
    }
}

#[cfg(target_os = "macos")]
fn runtime_heartbeat_body(session_id: &str, app_version: &str) -> serde_json::Value {
    serde_json::json!({
        "event_id": uuid::Uuid::new_v4().to_string(),
        "session_id": session_id,
        "app_version": app_version,
    })
}

#[cfg(target_os = "macos")]
fn start_runtime_heartbeat(supervisor: Arc<Supervisor>, shared_auth: Arc<auth::SharedAuth>) {
    let session_id = uuid::Uuid::new_v4().to_string();
    let app_version = env!("SUPYMEM_DESKTOP_VERSION").to_string();
    std::thread::spawn(move || {
        let mut next_heartbeat = std::time::Instant::now();
        loop {
            if supervisor.runtime_mode() == "hosted" {
                shared_auth.retry_refresh(|auth| sync_auth_bridge(auth, &supervisor));
            }
            if std::time::Instant::now() >= next_heartbeat
                && supervisor.runtime_mode() == "hosted" && supervisor.is_ready() {
                next_heartbeat = std::time::Instant::now() + Duration::from_secs(60);
                let custody = shared_auth.heartbeat_custody();
                let body = runtime_heartbeat_body(&session_id, &app_version);
                let result = reqwest::blocking::Client::builder()
                    .user_agent(crate::http::USER_AGENT)
                    .timeout(Duration::from_secs(5))
                    .redirect(reqwest::redirect::Policy::none())
                    .no_proxy()
                    .build()
                    .and_then(|client| {
                        client.post(format!("{}/native/telemetry/runtime", supervisor.bridge_url()))
                            .header("x-native-token", supervisor.native_bridge_token())
                            .json(&body)
                            .send()
                    });
                if let Ok(response) = result {
                    if response.status().as_u16() == 401 {
                        if let Some(custody) = custody {
                            shared_auth.renew_heartbeat(&custody, |auth| connect_auth_bridge(auth, &supervisor));
                        }
                    } else if !response.status().is_success() {
                        log::warn!("desktop_runtime_heartbeat rejected={}", response.status());
                    }
                }
            }
            std::thread::sleep(Duration::from_secs(1));
        }
    });
}

fn require_native_account_window(window: &tauri::WebviewWindow, state: &AppState) -> Result<(),String> {
    let location=window.url().map_err(|_| "native_account_origin_denied".to_string())?;
    if !auth::trusted_account_window(window.label(),&location,&state.supervisor.console_url()) {
        return Err("native_account_origin_denied".into());
    }
    Ok(())
}

fn require_native_command_window(window: &tauri::WebviewWindow, state: &AppState) -> Result<(), String> {
    let location = window.url().map_err(|_| "native_command_origin_denied".to_string())?;
    if crate::native_security::trusted_window(window.label(), &location, &state.supervisor.console_url(),
        cfg!(debug_assertions) || crate::channel::local_review() || local_stack::is_local(), crate::channel::staging(), true) { Ok(()) }
    else { Err("native_command_origin_denied".into()) }
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
enum CharlieMascotState { Idle, Listening, Speaking }

impl CharlieMascotState {
    fn name(self) -> &'static str {
        match self { Self::Idle => "idle", Self::Listening => "listening", Self::Speaking => "speaking" }
    }
}

fn charlie_voice_level(level: Option<f32>) -> Result<Option<f32>, String> {
    match level {
        Some(value) if !value.is_finite() => Err("charlie_invalid_level".into()),
        Some(value) => Ok(Some(value.clamp(0.0, 1.0))),
        None => Ok(None),
    }
}

#[tauri::command]
fn charlie_mascot_state(window: tauri::WebviewWindow, app_state: State<'_, AppState>, app: AppHandle,
    state: CharlieMascotState, level: Option<f32>) -> Result<(), String> {
    require_native_command_window(&window, &app_state)?;
    if window.label() != "main" { return Err("native_command_origin_denied".into()); }
    let level = charlie_voice_level(level)?;
    static PENDING: Mutex<Option<(CharlieMascotState, Option<f32>)>> = Mutex::new(None);
    let mut pending = PENDING.lock().unwrap_or_else(|error| error.into_inner());
    let scheduled = pending.is_some();
    *pending = Some((state, level));
    if !scheduled {
        thread::spawn(move || {
            thread::sleep(Duration::from_millis(50));
            let mut pending = PENDING.lock().unwrap_or_else(|error| error.into_inner());
            if let Some((state, level)) = pending.take() {
                #[cfg(target_os = "macos")]
                mascot::set_state(&app, state.name(), level);
                #[cfg(not(target_os = "macos"))]
                let _ = (app, state.name(), level);
            }
        });
    }
    Ok(())
}

fn require_native_local_window(window: &tauri::WebviewWindow, state: &AppState) -> Result<(), String> {
    require_native_command_window(window, state)?;
    let location = window.url().map_err(|_| "native_command_origin_denied".to_string())?;
    if native_security::bundled(&location) || ((cfg!(debug_assertions) || channel::local_review() || local_stack::is_local()) && location.scheme() == "http") { Ok(()) }
    else { Err("native_command_origin_denied".into()) }
}

async fn native_auth_action(_app: AppHandle, state: State<'_, AppState>, action: &'static str, expected: Option<String>) -> Result<auth::Status,String> {
    let shared=state.auth.clone(); let sup=state.supervisor.clone();
    let observed=shared.clone(); let streams=state.streams.clone();
    let mut auth=match shared.acquire().await { Ok(auth)=>auth, Err(_)=>return Ok(shared.status()) };
    let observation=auth::Observation::default();let worker_observation=observation.clone();
    let worker=tauri::async_runtime::spawn_blocking(move || {
        auth.observe(&worker_observation);
        let outcome = if action=="start" || action=="prepare_link" {
            match auth.start(expected) {
                Ok(url) => { if action=="start" && open::that(url).is_err() { auth.browser_failed(); } Ok(()) },
                Err(e) => Err(e.data["error"]["message"].as_str().unwrap_or("Device connection failed").to_string()),
            }
        } else if action=="sign_out" {
            streams.stop_all();
            auth.prepare_sign_out();
            sync_auth_bridge(&mut auth,&sup);
            auth.sign_out();
            Ok(())
        } else { auth.reconnect(expected.as_deref()); Ok(()) };
        sync_auth_bridge(&mut auth,&sup);
        let status=auth.status(); drop(auth);
        outcome.map(|_| status)
    });
    match auth::await_store(worker,&observed,&observation).await {
        Ok(result) => result,
        Err(code) if code=="secure_store_pending" => Ok(observed.status()),
        Err(code) => Err(code),
    }
}
#[tauri::command]
fn desktop_auth_status(window: tauri::WebviewWindow, state: State<'_,AppState>) -> Result<auth::Status,String> {
    require_native_account_window(&window,&state)?;
    Ok(state.auth.status())
}
#[tauri::command]
fn desktop_product_painted(window: tauri::WebviewWindow, state: State<'_,AppState>) -> Result<(),String> {
    require_native_account_window(&window,&state)?;
    let first = !state.product_painted.swap(true, std::sync::atomic::Ordering::SeqCst);
    if local_stack::is_local() && first {
        if let Some(started) = LOCAL_STARTED.get() {
            log::info!("desktop_startup phase=usable_first_screen ms={}", started.elapsed().as_millis());
        }
    }
    Ok(())
}
#[tauri::command]
async fn desktop_auth_start(app: AppHandle,window: tauri::WebviewWindow,state: State<'_,AppState>,expected_subject: Option<String>,open_browser: Option<bool>) -> Result<auth::Status,String> {
    require_native_account_window(&window,&state)?;
    native_auth_action(app,state,if open_browser == Some(false) { "prepare_link" } else { "start" },expected_subject).await
}
#[tauri::command]
async fn desktop_auth_reconnect(app: AppHandle,window: tauri::WebviewWindow,state: State<'_,AppState>,expected_subject: Option<String>) -> Result<auth::Status,String> {
    require_native_account_window(&window,&state)?;
    native_auth_action(app,state,"reconnect",expected_subject).await
}
#[tauri::command]
async fn desktop_auth_sign_out(app: AppHandle,window: tauri::WebviewWindow,state: State<'_,AppState>) -> Result<auth::Status,String> {
    require_native_account_window(&window,&state)?;
    native_auth_action(app,state,"sign_out",None).await
}
#[tauri::command]
async fn desktop_auth_complete_code(window: tauri::WebviewWindow,state: State<'_,AppState>,code:String) -> Result<auth::Status,String> {
    require_native_account_window(&window,&state)?;
    let shared=state.auth.clone(); let sup=state.supervisor.clone();
    let observed=shared.clone();
    let mut auth=shared.acquire().await.map_err(|_| "secure_store_pending".to_string())?;
    let worker=tauri::async_runtime::spawn_blocking(move || {
        let started = std::time::Instant::now();
        let result=auth.complete_with_code(&code)
            .map_err(|reply| reply.data["error"]["code"].as_str().unwrap_or("authorization_rejected").to_string());
        sync_auth_bridge(&mut auth,&sup);
        log::info!("desktop_startup phase=device_approval_verified ok={} ms={}", result.is_ok(), started.elapsed().as_millis());
        let status=auth.status(); drop(auth);
        result.map(|_| status)
    });
    match worker.await {
        Ok(result)=>result,
        Err(_)=>Ok(observed.status()),
    }
}
#[tauri::command]
async fn desktop_api_request(window:tauri::WebviewWindow,state:State<'_,AppState>,method:String,path:String,body:Option<serde_json::Value>,headers:Option<std::collections::HashMap<String,String>>) -> Result<auth::Reply,String> {
    require_native_account_window(&window,&state)?;
    native_api_request(&state,method,path,body,headers).await
}

/// The signed-in transport behind `desktop_api_request`; callers must have vetted the window and path.
async fn native_api_request(state:&AppState,method:String,path:String,body:Option<serde_json::Value>,headers:Option<std::collections::HashMap<String,String>>) -> Result<auth::Reply,String> {
    native_api_request_scoped(state, method, path, body, headers, None).await
}
async fn native_api_request_scoped(state:&AppState,method:String,path:String,body:Option<serde_json::Value>,
    headers:Option<std::collections::HashMap<String,String>>, scope:Option<(NoticeCustody,String)>) -> Result<auth::Reply,String> {
    let overlay_load = scope.is_some() && method == "GET"
        && (path == "/api/suggestions" || path.starts_with("/api/suggestions?venture_id="));
    let shared=state.auth.clone(); let sup=state.supervisor.clone();
    let mut auth=match shared.acquire().await {Ok(auth)=>auth,Err(code)=>return Ok(auth::Reply::error(503,&code))};
    if scope.as_ref().is_some_and(|(owner, id)| !scoped_overlay_admitted(&auth, owner, id)) {
        return Ok(auth::Reply::error(409,"suggestions_owner_changed"));
    }
    let prepare_sup=sup.clone();
    let prepare=tauri::async_runtime::spawn_blocking(move || {
        let request=auth.prepare_api(&method,&path,body,headers.unwrap_or_default());
        sync_auth_bridge(&mut auth,&prepare_sup);
        request
    });
    let request=match prepare.await.map_err(|_| "native_auth_unavailable".to_string()) {
        Ok(Ok(request))=>request,Ok(Err(reply))=>return Ok(reply),
        Err(code)=>return Ok(auth::Reply::error(503,&code)),
    };
    let (request,response) = if let Some((owner,id)) = scope.as_ref() {
        match shared.send_overlay_request(request, |auth| scoped_overlay_admitted(auth,owner,id)).await {
            Ok(sent)=>sent, Err(reply)=>return Ok(reply),
        }
    } else {
        tauri::async_runtime::spawn_blocking(move || { let response=request.send(); (request,response) })
            .await.map_err(|_| "native_transport_unavailable".to_string())?
    };
    let mut auth=match shared.acquire().await {Ok(auth)=>auth,Err(code)=>return Ok(auth::Reply::sent_unconfirmed(&code))};
    if scope.as_ref().is_some_and(|(owner,id)| !scoped_overlay_admitted(&auth,owner,id)) {
        return Ok(auth::Reply::sent_unconfirmed("suggestions_owner_changed"));
    }
    let finish=tauri::async_runtime::spawn_blocking(move || {
        let reply=auth.complete_api(&request,response);
        if overlay_load && reply.status == 200 && reply.data.get("items").is_some_and(|items| items.is_array()) {
            if let Some((owner,id)) = scope { suggestions::record_loaded(&id,&owner); }
        }
        sync_auth_bridge(&mut auth,&sup);reply
    });
    match finish.await {
        Ok(reply)=>Ok(reply),Err(_)=>Ok(auth::Reply::sent_unconfirmed("native_auth_unavailable")),
    }
}
fn scoped_overlay_admitted(auth: &auth::AuthAccess, owner: &NoticeCustody, id: &str) -> bool {
    auth.notice_custody() == Some((owner.subject.clone(),owner.device_id.clone(),owner.auth_epoch))
        && suggestions::presentation_current(id,owner)
}

#[tauri::command]
fn device_work_order_capabilities(window: tauri::WebviewWindow, state: State<'_, AppState>)
    -> Result<device_order::NativeWorkOrderCapabilities, String> {
    if local_stack::is_local() { require_native_local_window(&window, &state)?; }
    else { require_native_account_window(&window, &state)?; }
    Ok(device_order::native_wire_capabilities())
}

#[tauri::command]
async fn inspect_device_work_order(window: tauri::WebviewWindow, state: State<'_, AppState>,
    venture_id: String, order_id: String) -> Result<device_order::WorkOrder, String> {
    let path = device_order::review_path(&venture_id, &order_id)?;
    if local_stack::is_local() {
        require_native_local_window(&window, &state)?;
        let reply = local_build_request(state.supervisor.clone(), "GET", path, None).await?;
        let device = reply.data.get("device_id").and_then(serde_json::Value::as_str)
            .ok_or("Open Builds and retry loading your build's activity.")?.to_string();
        return device_order::validate_review(reply.data, &venture_id, &order_id, &device)
            .map_err(|_| "Your saved build could not be verified. Open Builds, reload its review, then retry your requested build.".into());
    }
    // Native credentials and response identity fencing stay in the existing transport.
    // Only record identifiers come from the page; no manifest, argv or prompt does.
    let reply = desktop_api_request(window, state.clone(), "GET".into(), path, None, None).await?;
    if reply.status != 200 { return Err("device_order_review_unavailable".into()); }
    // `SharedAuth::status()` reports a transient busy state while another
    // authenticated page request owns the mutex. Wait for the actual identity
    // before deciding that a saved review belongs to a different account.
    let status = {
        let auth = state.auth.acquire().await
            .map_err(|_| "device_order_review_unavailable".to_string())?;
        auth.status()
    };
    if status.state != "connected" { return Err("device_order_account_changed".into()); }
    let device = status.device_id.ok_or_else(|| "device_order_device_missing".to_string())?;
    device_order::validate_review(reply.data, &venture_id, &order_id, &device)
}

fn native_bridge_request(client: &reqwest::blocking::Client, supervisor: &Supervisor,
    method: reqwest::Method, path: &str) -> reqwest::blocking::RequestBuilder {
    client.request(method, format!("{}{path}", supervisor.bridge_url()))
        .header("x-native-token", supervisor.native_bridge_token())
}

fn local_build_http_request(client: &reqwest::blocking::Client, supervisor: &Supervisor,
    method: reqwest::Method, path: &str, owner: &str) -> reqwest::blocking::RequestBuilder {
    if path.starts_with("/native/") {
        return native_bridge_request(client, supervisor, method, path);
    }
    let path = if path.starts_with("/api/") { &path[4..] } else { path };
    client.request(method, format!("{}{path}", supervisor.core_url()))
        .header("X-Local-Proxy-Secret", owner)
}

async fn local_build_request(supervisor: Arc<Supervisor>, method: &str, path: String,
    body: Option<serde_json::Value>) -> Result<auth::Reply, String> {
    let method = reqwest::Method::from_bytes(method.as_bytes())
        .map_err(|_| "Open Builds and retry your requested build.")?;
    tauri::async_runtime::spawn_blocking(move || {
        use std::io::Read;
        let next_step = "Your local build connection is unavailable. Quit and reopen your app, then open Builds to check its activity before retrying. Your files are kept.";
        let secret_path = supervisor.data_dir().join("owner-session.secret");
        let owner = nav_guard::owner_secret(&secret_path).map_err(|_| next_step.to_string())?;
        let client = reqwest::blocking::Client::builder().no_proxy()
            .redirect(reqwest::redirect::Policy::none()).timeout(Duration::from_secs(20))
            .build().map_err(|_| next_step.to_string())?;
        let mut request = local_build_http_request(&client, &supervisor, method, &path, &owner);
        if let Some(body) = body { request = request.json(&body); }
        let response = request.send().map_err(|_| next_step.to_string())?;
        let status = response.status();
        let mut bytes = Vec::new();
        response.take(262145).read_to_end(&mut bytes).map_err(|_| next_step.to_string())?;
        if bytes.len() > 262144 { return Err(next_step.to_string()); }
        if nav_guard::owner_secret(&secret_path).ok().as_deref() != Some(owner.as_str()) {
            return Err("Your local owner session changed. Open Builds to check whether your build started before retrying.".into());
        }
        let data: serde_json::Value = serde_json::from_slice(&bytes).map_err(|_| next_step.to_string())?;
        if !status.is_success() {
            if let Some(message) = data.pointer("/detail/message").or_else(|| data.pointer("/error/message"))
                .and_then(serde_json::Value::as_str).filter(|message| !message.is_empty() && message.len() <= 1000) {
                return Err(format!("{} Open Builds to check your build before retrying.",
                    crate::local_diagnostics::redact_startup_line(message)));
            }
            return Err(if matches!(status.as_u16(), 404 | 405) { next_step.to_string() } else {
                "Your coding tool is not ready for this build. Open Keys and connections, connect and verify your tool, then retry from Builds.".to_string()
            });
        }
        Ok(auth::Reply { status: status.as_u16(), data, request_id: None })
    }).await.map_err(|_| "Open Builds to check your build's activity before retrying.".to_string())?
}

#[tauri::command]
async fn desktop_app_inventory(window: tauri::WebviewWindow, state: State<'_, AppState>,
    bundle_id: Option<String>)
    -> Result<serde_json::Value, String> {
    require_native_account_window(&window, &state)?;
    if bundle_id.as_ref().is_some_and(|bundle| bundle.len() > 200 || bundle.split('.').count() < 2
        || bundle.split('.').any(|part| part.is_empty() || !part.bytes().all(|byte|
            byte.is_ascii_alphanumeric() || byte == b'-'))) {
        return Err("native_desktop_bundle_invalid".into());
    }
    let before = {
        let auth = state.auth.acquire().await
            .map_err(|_| "native_desktop_inventory_unavailable".to_string())?;
        auth.status()
    };
    if before.state != "connected" || before.device_id.is_none() {
        return Err("device_order_account_changed".into());
    }
    await_bridge_ready(state.supervisor.clone()).await?;
    let sup = state.supervisor.clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        use std::io::Read;
        let client = reqwest::blocking::Client::builder().user_agent(crate::http::USER_AGENT)
            .timeout(Duration::from_secs(15)).redirect(reqwest::redirect::Policy::none())
            .no_proxy().build().map_err(|_| "native_desktop_inventory_unavailable".to_string())?;
        let mut request = client.get(format!("{}/native/desktop/apps", sup.bridge_url()))
            .header("x-native-token", sup.native_bridge_token());
        if let Some(bundle) = bundle_id.as_ref() { request = request.query(&[("bundle_id", bundle)]); }
        let response = request.send()
            .map_err(|_| "native_desktop_inventory_unavailable".to_string())?;
        let success = response.status().is_success();
        let mut raw = Vec::new();
        response.take(65537).read_to_end(&mut raw)
            .map_err(|_| "native_desktop_inventory_unavailable".to_string())?;
        if !success || raw.len() > 65536 { return Err("native_desktop_inventory_unavailable".into()); }
        serde_json::from_slice::<serde_json::Value>(&raw)
            .map_err(|_| "native_desktop_inventory_unavailable".to_string())
    }).await.map_err(|_| "native_desktop_inventory_unavailable".to_string())?;
    let after = {
        let auth = state.auth.acquire().await
            .map_err(|_| "native_desktop_inventory_unavailable".to_string())?;
        auth.status()
    };
    if after.state != "connected" || before.device_id != after.device_id
        || before.subject != after.subject {
        return Err("native_order_account_changed".into());
    }
    result
}

#[tauri::command]
async fn desktop_permission_status(window: tauri::WebviewWindow, state: State<'_, AppState>)
    -> Result<serde_json::Value, String> {
    require_native_account_window(&window, &state)?;
    let sup = state.supervisor.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let client = reqwest::blocking::Client::builder().timeout(Duration::from_secs(15))
            .no_proxy().build().map_err(|_| "native_desktop_permissions_unavailable".to_string())?;
        let response = client.get(format!("{}/native/desktop/permissions", sup.bridge_url()))
            .header("x-native-token", sup.native_bridge_token()).send()
            .map_err(|_| "native_desktop_permissions_unavailable".to_string())?;
        if !response.status().is_success() { return Err("native_desktop_permissions_unavailable".into()); }
        response.json::<serde_json::Value>()
            .map_err(|_| "native_desktop_permissions_unavailable".into())
    }).await.map_err(|_| "native_desktop_permissions_unavailable".to_string())?
}

#[tauri::command]
fn desktop_permission_targets(window: tauri::WebviewWindow, state: State<'_, AppState>)
    -> Result<serde_json::Value, String> {
    require_native_account_window(&window, &state)?;
    let executable = std::env::current_exe()
        .map_err(|_| "native_desktop_permission_target_unavailable".to_string())?;
    let app = executable.parent().and_then(|macos| macos.parent()).and_then(|contents| contents.parent())
        .ok_or_else(|| "native_desktop_permission_target_unavailable".to_string())?;
    let helper = app.join("Contents/MacOS/peekaboo");
    if app.extension().and_then(|value| value.to_str()) != Some("app") || !helper.is_file() {
        return Err("native_desktop_permission_target_unavailable".into());
    }
    Ok(serde_json::json!({"app_path": app.display().to_string(),
                          "helper_path": helper.display().to_string()}))
}

#[tauri::command]
async fn desktop_permission_request(window: tauri::WebviewWindow, state: State<'_, AppState>,
    kind: String) -> Result<serde_json::Value, String> {
    require_native_account_window(&window, &state)?;
    if !matches!(kind.as_str(), "screen-recording" | "accessibility" | "event-synthesizing") {
        return Err("native_desktop_permission_kind_invalid".into());
    }
    let sup = state.supervisor.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let client = reqwest::blocking::Client::builder().timeout(Duration::from_secs(25))
            .no_proxy().build().map_err(|_| "native_desktop_permission_request_unavailable".to_string())?;
        let response = client.post(format!("{}/native/desktop/permissions/request", sup.bridge_url()))
            .header("x-native-token", sup.native_bridge_token())
            .json(&serde_json::json!({"kind": kind})).send()
            .map_err(|_| "native_desktop_permission_request_unavailable".to_string())?;
        if !response.status().is_success() { return Err("native_desktop_permission_request_unavailable".into()); }
        response.json::<serde_json::Value>()
            .map_err(|_| "native_desktop_permission_request_unavailable".into())
    }).await.map_err(|_| "native_desktop_permission_request_unavailable".to_string())?
}

#[tauri::command]
async fn desktop_launch_installed_app(window: tauri::WebviewWindow, state: State<'_, AppState>,
    bundle_id: String, ensure_window: Option<bool>) -> Result<serde_json::Value, String> {
    require_native_account_window(&window, &state)?;
    if bundle_id.len() > 200 || bundle_id.split('.').count() < 2
        || bundle_id.split('.').any(|part| part.is_empty() || !part.bytes().all(|byte|
            byte.is_ascii_alphanumeric() || byte == b'-')) {
        return Err("native_desktop_bundle_invalid".into());
    }
    let before = {
        let auth = state.auth.acquire().await
            .map_err(|_| "native_desktop_launch_unavailable".to_string())?;
        auth.status()
    };
    if before.state != "connected" || before.device_id.is_none() {
        return Err("device_order_account_changed".into());
    }
    let sup = state.supervisor.clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        use std::io::Read;
        let client = reqwest::blocking::Client::builder().user_agent(crate::http::USER_AGENT)
            .timeout(Duration::from_secs(20)).redirect(reqwest::redirect::Policy::none())
            .no_proxy().build().map_err(|_| "native_desktop_launch_unavailable".to_string())?;
        let response = client.post(format!("{}/native/desktop/apps/launch", sup.bridge_url()))
            .header("x-native-token", sup.native_bridge_token())
            .json(&serde_json::json!({"bundle_id": bundle_id,
                                      "ensure_window": ensure_window.unwrap_or(false)}))
            .send().map_err(|_| "native_desktop_launch_unavailable".to_string())?;
        let status = response.status();
        let success = status.is_success();
        let mut raw = Vec::new();
        response.take(65537).read_to_end(&mut raw)
            .map_err(|_| "native_desktop_launch_unavailable".to_string())?;
        if raw.len() > 65536 { return Err("native_desktop_launch_unavailable".into()); }
        if !success { return Err(native_order_error(status.as_u16(), &raw)); }
        serde_json::from_slice::<serde_json::Value>(&raw)
            .map_err(|_| "native_desktop_launch_unavailable".to_string())
    }).await.map_err(|_| "native_desktop_launch_unavailable".to_string())?;
    let after = {
        let auth = state.auth.acquire().await
            .map_err(|_| "native_desktop_launch_unavailable".to_string())?;
        auth.status()
    };
    if after.state != "connected" || before.device_id != after.device_id
        || before.subject != after.subject {
        return Err("native_order_account_changed".into());
    }
    result
}

/// Block until the private bridge answers /health. After a cold start the
/// frozen Python helper needs ~20s; a Mac task started in that window must
/// wait for it instead of failing with a transport error.
async fn await_bridge_ready(supervisor: Arc<Supervisor>) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || supervisor.wait_bridge())
        .await.map_err(|_| "The local helper could not be checked. Quit and reopen Supymem.".to_string())?
}

fn native_order_error(status: u16, raw: &[u8]) -> String {
    if let Ok(parsed) = serde_json::from_slice::<serde_json::Value>(raw) {
        let candidates = [
            parsed.get("detail").and_then(|value| value.as_str()),
            parsed.pointer("/detail/code").and_then(|value| value.as_str()),
            parsed.pointer("/detail/error/code").and_then(|value| value.as_str()),
            parsed.pointer("/error/code").and_then(|value| value.as_str()),
        ];
        if let Some(code) = candidates.into_iter().flatten().find(|code| {
            code.len() <= 80
                && code.bytes().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_')
                && (code.starts_with("native_") || code.starts_with("desktop_") || code.starts_with("device_")
                    || code.starts_with("cu_")
                    || *code == "verified_account_required"
                    || code.starts_with("authenticated_device_"))
        }) {
            return code.to_string();
        }
    }
    // FastAPI/proxy failures may be plain text or HTML. Preserve only a
    // bounded status category, never an upstream body or arbitrary error code.
    match status {
        401 | 403 => "native_order_bridge_auth_rejected",
        404 | 405 => "native_order_bridge_route_unavailable",
        409 => "native_order_bridge_conflict_unclassified",
        422 => "native_order_bridge_request_invalid",
        429 => "native_order_bridge_rate_limited",
        500..=599 => "native_order_bridge_server_error",
        _ => "native_order_request_rejected",
    }.into()
}

fn native_order_auth_lock_error(request_sent: bool) -> &'static str {
    // Before the bridge POST, no task can have started. After the POST, the
    // lock may be delayed by Keychain or another account request; the result
    // is uncertain and must be inspected rather than retried automatically.
    if request_sent { "native_order_result_unconfirmed" }
    else { "native_order_auth_unavailable_before_send" }
}

#[tauri::command]
async fn device_work_order_action(window: tauri::WebviewWindow, state: State<'_, AppState>,
    venture_id: String, order_id: String, action: String) -> Result<serde_json::Value, String> {
    if !matches!(action.as_str(), "preflight" | "start" | "status" | "cancel" | "reconcile" | "continue" | "handoff" | "source-upload" | "soft-stop") {
        return Err("native_order_action_invalid".into());
    }
    if local_stack::is_local() {
        require_native_local_window(&window, &state)?;
        device_order::review_path(&venture_id, &order_id)?;
        if matches!(action.as_str(), "handoff" | "source-upload") {
            return Err("Your build stays on your laptop. Open Builds to review its result folder and local preview.".into());
        }
        if action != "cancel" && action != "soft-stop" {
            let reviewed = inspect_device_work_order(window, state.clone(), venture_id.clone(), order_id.clone()).await?;
            if reviewed.manifest.coding_loop_verification.is_none() {
                return Err("Open your venture chat and request a build with your connected coding tool, then watch it in Builds.".into());
            }
        }
        let method = if matches!(action.as_str(), "start" | "cancel" | "reconcile" | "continue" | "soft-stop") { "POST" } else { "GET" };
        return local_build_request(state.supervisor.clone(), method,
            format!("/native/device-work-orders/{venture_id}/{order_id}/{action}"), None).await.map(|reply| reply.data);
    }
    await_bridge_ready(state.supervisor.clone()).await?;
    // Reuse the authenticated, identity-fenced native review path. No URLs,
    // credentials, executable paths, manifests or scripts come from WebView.
    let reviewed = inspect_device_work_order(window.clone(), state.clone(),
        venture_id.clone(), order_id.clone()).await?;
    let before = {
        let auth = state.auth.acquire().await
            .map_err(|_| native_order_auth_lock_error(false).to_string())?;
        auth.status()
    };
    if before.device_id.as_deref() != Some(reviewed.device_id.as_str())
        && !matches!(action.as_str(), "status" | "reconcile") {
        return Err("device_order_identity_mismatch".into());
    }
    if action == "start" {
        // The private PyInstaller bridge may have restarted since the review
        // page loaded. Restore its in-memory, native-owned session before
        // starting the bounded driver or attempting to claim an order.
        let shared = state.auth.clone();
        let sup = state.supervisor.clone();
        let mut auth = shared.acquire().await?;
        let observation = auth::Observation::default();
        auth.observe(&observation);
        let ready = auth::await_store(tauri::async_runtime::spawn_blocking(move || {
            connect_auth_bridge(&mut auth, &sup);
            auth.status().bridge_connected
        }), &shared, &observation).await?;
        if !ready { return Err("native_bridge_session_unavailable".into()); }
    }
    #[cfg(target_os = "macos")]
    let cua_socket = if action == "start" {
        if let Some(desktop) = reviewed.manifest.desktop_verification.as_ref() {
            let binary = std::env::current_exe().ok()
                .and_then(|exe| exe.parent().map(|dir| dir.join("cua-driver")));
            if let Some(binary) = binary.filter(|path| path.is_file()) {
                let mut guard = state.cua_host.lock()
                    .map_err(|_| "desktop_cua_host_unavailable".to_string())?;
                if guard.is_some() {
                    return Err("desktop_cua_host_busy".into());
                }
                let modal_parents = desktop.modal_policy.as_ref().map(|policy| policy.parents.iter()
                    .map(|row|(row.process_id,row.window_id,row.process_start_identity.clone())).collect::<Vec<_>>())
                    .unwrap_or_default();
                let host = if !modal_parents.is_empty() {
                    let surfaces = desktop.reviewed_surfaces.as_ref().map(|rows| rows.iter()
                        .map(|row|(row.process_id,row.window_id)).collect::<Vec<_>>())
                        .unwrap_or_else(||vec![(desktop.process_id,desktop.window_id)]);
                    cua_host::CuaTaskHost::start_reviewed_modals(&binary,&surfaces,&modal_parents)?
                } else if desktop.operation == "desktop_goal_v2" {
                    // Server-fetched, native-validated saved review; never WebView authority.
                    let surfaces = desktop.reviewed_surfaces.as_ref()
                        .ok_or("desktop_scope_unsupported")?.iter()
                        .map(|row| (row.process_id, row.window_id)).collect::<Vec<_>>();
                    cua_host::CuaTaskHost::start_surfaces(&binary, &surfaces)?
                } else {
                    cua_host::CuaTaskHost::start(&binary, desktop.process_id, desktop.window_id)?
                };
                let socket = host.socket().to_string_lossy().to_string();
                *guard = Some((order_id.clone(), host));
                Some(socket)
            } else { None }
        } else { None }
    } else { None };
    #[cfg(not(target_os = "macos"))]
    let cua_socket: Option<String> = None;
    let sup = state.supervisor.clone();
    let path = format!("/native/device-work-orders/{venture_id}/{order_id}/{action}");
    let native_action = action.clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        use std::io::Read;
        let client = reqwest::blocking::Client::builder().user_agent(crate::http::USER_AGENT)
            .timeout(Duration::from_secs(if native_action == "source-upload" { 90 } else { 20 }))
            .redirect(reqwest::redirect::Policy::none()).no_proxy()
            .build().map_err(|_| "native_order_transport_unavailable".to_string())?;
        let url = format!("{}{}", sup.bridge_url(), path);
        let request = if matches!(native_action.as_str(), "start" | "cancel" | "reconcile" | "continue" | "handoff" | "source-upload" | "soft-stop") { client.post(url) } else { client.get(url) };
        let mut request = request.header("x-native-token", sup.native_bridge_token());
        if let Some(socket) = cua_socket.as_ref() {
            request = request.header("x-supymem-cua-socket", socket)
                .header("x-supymem-cua-host-pid", std::process::id());
            #[cfg(target_os = "macos")]
            { request = request.header("x-supymem-cua-protocol", cua_host::OBSERVATION_PROTOCOL); }
        }
        let response = request.send().map_err(|error| {
            if error.is_timeout() { "native_order_bridge_timeout" }
            else { "native_order_bridge_no_response" }.to_string()
        })?;
        let status = response.status();
        let success = status.is_success();
        let mut raw = Vec::new();
        response.take(262145).read_to_end(&mut raw)
            .map_err(|_| "native_order_bridge_response_unconfirmed".to_string())?;
        if raw.len() > 262144 { return Err("native_order_response_bounds".to_string()); }
        if !success { return Err(native_order_error(status.as_u16(), &raw)); }
        serde_json::from_slice::<serde_json::Value>(&raw)
            .map_err(|_| "native_order_response_invalid".to_string())
    }).await.map_err(|_| "native_order_bridge_worker_unconfirmed".to_string())?;
    // A successful bridge POST may already have started a one-use task. A
    // concurrent credential operation is not an account change and must not
    // be reported as one; leave its result uncertain for receipt inspection.
    let after = {
        let auth = state.auth.acquire().await
            .map_err(|_| native_order_auth_lock_error(true).to_string())?;
        auth.status()
    };
    if before.state != "connected" || after.state != "connected"
        || before.device_id != after.device_id || before.subject != after.subject {
        return Err("native_order_account_changed".into());
    }
    if action == "start" {
        if result.is_ok() {
            let focus_on_finish = reviewed.manifest.app_navigation_verification.is_some();
            restore_window_if_needed(&window);
            mark_native_run(&state, &venture_id, &order_id, focus_on_finish);
            watch_native_run_visibility(
                window.app_handle().clone(),
                state.supervisor.clone(),
                venture_id.clone(),
                order_id.clone(),
                focus_on_finish,
            );
            if let Some(verification) = reviewed.manifest.app_navigation_verification.as_ref() {
                complete_reviewed_app_navigation(&window, &state.supervisor,
                    &venture_id, &order_id, &verification.route).await?;
                // App-navigation's job is to show a Supymem screen: take focus.
                finish_native_run_window(&window, true);
            } else {
                // Leave focus alone so terminal/browser/Cursor targets can operate.
                // The #172 native notice is what brings the founder back.
                finish_native_run_window(&window, false);
            }
        }
    } else if matches!(action.as_str(), "cancel" | "reconcile" | "status") {
        if let Ok(payload) = result.as_ref() {
            if payload.get("state").and_then(|value| value.as_str())
                .is_some_and(|state| native_action_reaps_cua_host(&action, state)) {
                let focus_on_finish = native_run_focus_on_finish(&state);
                clear_native_run(&state, &venture_id, &order_id);
                #[cfg(target_os = "macos")]
                if let Ok(mut host) = state.cua_host.lock() {
                    if host.as_ref().is_some_and(|(id, _)| id == &order_id) { host.take(); }
                }
                finish_native_run_window(&window, focus_on_finish);
            }
        }
    }
    result
}

async fn complete_reviewed_app_navigation(window: &tauri::WebviewWindow,
    supervisor: &Supervisor, venture_id: &str, order_id: &str, route: &str) -> Result<(), String> {
    if !matches!(route, "builds" | "settings") {
        return Err("native_navigation_scope_invalid".into());
    }
    let bridge = supervisor.bridge_url();
    let token = supervisor.native_bridge_token();
    let status_url = format!("{bridge}/native/device-work-orders/{venture_id}/{order_id}/status");
    let result_url = format!("{bridge}/native/device-work-orders/{venture_id}/{order_id}/app-navigation-result");
    let ready = tauri::async_runtime::spawn_blocking(move || -> Result<(), String> {
        let client = reqwest::blocking::Client::builder().timeout(Duration::from_secs(3))
            .redirect(reqwest::redirect::Policy::none()).no_proxy().build()
            .map_err(|_| "native_order_transport_unavailable".to_string())?;
        for _ in 0..30 {
            let response = client.get(&status_url).header("x-native-token", &token).send()
                .map_err(|_| "native_order_result_unconfirmed".to_string())?;
            let status: serde_json::Value = response.json()
                .map_err(|_| "native_order_result_unconfirmed".to_string())?;
            if status.get("state").and_then(|value| value.as_str()) == Some("awaiting_native_navigation") {
                return Ok(());
            }
            if matches!(status.get("state").and_then(|value| value.as_str()),
                Some("stopped" | "not_started" | "reconciliation_required")) {
                return Err("native_navigation_not_ready".into());
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        Err("native_navigation_not_ready".into())
    }).await.map_err(|_| "native_order_result_unconfirmed".to_string())?;
    ready?;
    let navigation = async {
        let base = supervisor.console_url().parse::<url::Url>()
            .map_err(|_| "native_navigation_origin_invalid".to_string())?;
        if (base.scheme() != "https" || !matches!(base.host_str(),
                Some("app.example.invalid" | "app.staging.example.invalid")))
            && !(base.scheme() == "http" && matches!(base.host_str(), Some("127.0.0.1" | "localhost"))) {
            return Err("native_navigation_origin_invalid".into());
        }
        let current = window.url().map_err(|_| "native_navigation_window_unavailable".to_string())?;
        if current.origin() != base.origin() {
            return Err("native_navigation_window_origin_changed".into());
        }
        let target = base.join(&format!("/v/{venture_id}/{route}"))
            .map_err(|_| "native_navigation_target_invalid".to_string())?;
        window.navigate(target.clone()).map_err(|_| "native_navigation_failed".to_string())?;
        for _ in 0..30 {
            if let Ok(actual) = window.url() {
                if actual == target {
                    return Ok(actual.to_string());
                }
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        Err("native_navigation_not_verified".into())
    }.await;
    // Always release the claimed worker promptly, even if the window cannot move.
    let observed = navigation.as_ref().cloned().unwrap_or_default();
    let token = supervisor.native_bridge_token();
    let delivered = tauri::async_runtime::spawn_blocking(move || -> Result<(), String> {
        let client = reqwest::blocking::Client::builder().timeout(Duration::from_secs(10))
            .redirect(reqwest::redirect::Policy::none()).no_proxy().build()
            .map_err(|_| "native_order_transport_unavailable".to_string())?;
        client.post(result_url).header("x-native-token", token)
            .json(&serde_json::json!({"observed_url": observed})).send()
            .map_err(|_| "native_order_result_unconfirmed".to_string())?
            .error_for_status().map_err(|_| "native_navigation_result_rejected".to_string())?;
        Ok(())
    }).await.map_err(|_| "native_order_result_unconfirmed".to_string())?;
    delivered?;
    navigation.map(|_| ())
}

#[tauri::command]
async fn desktop_http_request(window:tauri::WebviewWindow,state:State<'_,AppState>,method:String,path:String,headers:Option<std::collections::HashMap<String,String>>,body:Option<transport::Body>) -> Result<transport::RawReply,String> {
    require_native_account_window(&window,&state)?;
    let shared=state.auth.clone();let sup=state.supervisor.clone();
    let mut auth=match shared.acquire().await {Ok(auth)=>auth,Err(code)=>return Ok(transport::RawReply::error(503,&code))};
    let prepare_sup=sup.clone();
    let prepare=tauri::async_runtime::spawn_blocking(move || {
        let request=transport::prepare(method,path,headers.unwrap_or_default(),body)?;
        let identity=auth.request_identity();
        sync_auth_bridge(&mut auth,&prepare_sup);
        match identity {Ok(identity)=>Ok((request,identity)),Err(reply)=>Err(transport::RawReply::error(reply.status,reply.data["error"]["code"].as_str().unwrap_or("authentication_required")))}
    });
    let (request,identity)=match prepare.await.map_err(|_| "native_auth_unavailable".to_string()) {
        Ok(Ok(request))=>request,Ok(Err(reply))=>return Ok(reply),Err(code)=>return Ok(transport::RawReply::error(503,&code)),
    };
    let (reply,identity,request)=tauri::async_runtime::spawn_blocking(move || (transport::send(&identity,request.clone()),identity,request))
        .await.map_err(|_| "native_transport_unavailable".to_string())?;
    let mut auth=match shared.acquire().await {Ok(auth)=>auth,Err(code)=>return Ok(transport::RawReply::sent_unconfirmed(&code))};
    let finish=tauri::async_runtime::spawn_blocking(move || {
        if !auth.matches_identity(&identity){return transport::RawReply::error(401,"account_changed_during_request");}
        if reply.status==401 {
            let checked=auth.confirm_feature_unauthorized(&identity);
            sync_auth_bridge(&mut auth,&sup);
            if let Err(reason)=checked {
                return transport::RawReply::error(reason.status,reason.data["error"]["code"].as_str().unwrap_or("session_verification_failed"));
            }
            let renewed = match auth.request_identity() {
                Ok(renewed) => renewed,
                Err(reason) => return transport::RawReply::error(reason.status,reason.data["error"]["code"].as_str().unwrap_or("session_verification_failed")),
            };
            if renewed.access_token != identity.access_token && auth::NativeAuth::can_retry_request(&request.method, &request.headers) {
                let retried = transport::send(&renewed,request);
                if !auth.matches_identity(&renewed) { return transport::RawReply::sent_unconfirmed("account_changed_during_request"); }
                if retried.status == 401 {
                    if let Err(reason) = auth.verify_session(false) {
                        sync_auth_bridge(&mut auth,&sup);
                        return transport::RawReply::error(reason.status,reason.data["error"]["code"].as_str().unwrap_or("session_verification_failed"));
                    }
                }
                sync_auth_bridge(&mut auth,&sup);
                return retried;
            }
        }
        reply
    });
    match finish.await {
        Ok(reply)=>Ok(reply),Err(_)=>Ok(transport::RawReply::sent_unconfirmed("native_auth_unavailable")),
    }
}

#[tauri::command]
fn desktop_events_subscribe(app:AppHandle,window:tauri::WebviewWindow,state:State<AppState>,id:String,path:String,last_event_id:Option<String>) -> Result<(),String> {
    require_native_account_window(&window,&state)?;
    state.streams.subscribe(app,state.auth.clone(),state.supervisor.clone(),id,path,last_event_id)
}
#[tauri::command]
fn desktop_events_unsubscribe(window:tauri::WebviewWindow,state:State<AppState>,id:String) -> Result<(),String> {
    require_native_account_window(&window,&state)?;
    state.streams.unsubscribe(&id);Ok(())
}

const KEYRING_SERVICE: &str = "com.companyos.app";
const KEYRING_USER: &str = "console_passphrase";

#[tauri::command]
fn backend_ready(state: State<AppState>) -> bool {
    state.supervisor.is_ready()
}

#[tauri::command]
fn gateway_url(state: State<AppState>) -> String {
    state.supervisor.gateway_url()
}

#[tauri::command]
fn console_url(state: State<AppState>) -> String {
    state.supervisor.console_url()
}

#[tauri::command]
fn backend_status(state: State<AppState>) -> Vec<serde_json::Value> {
    let message = state.supervisor.local_maintenance_message();
    state.supervisor.status().into_iter().map(|role| {
        let mut value = serde_json::to_value(&role).expect("serializable role status");
        if role.role == "core" && !message.is_empty() { value["maintenance_message"] = message.clone().into(); }
        value
    }).collect()
}

#[tauri::command]
fn reveal_in_file_manager(state: State<'_, AppState>, window: tauri::WebviewWindow, path: String) -> Result<(), String> {
    require_native_command_window(&window, &state)?;
    reveal_path(path)
}

fn reveal_path(path: String) -> Result<(), String> {
    let p = PathBuf::from(&path).canonicalize()
        .map_err(|_| "Could not find the file to reveal.".to_string())?;
    // Reveal the item, rather than launching a file's registered application.
    #[cfg(target_os = "macos")]
    {
        let status = std::process::Command::new("/usr/bin/open").arg("-R").arg(&p)
            .status().map_err(|_| "Could not reveal the file in Finder.".to_string())?;
        if status.success() { Ok(()) } else { Err("Could not reveal the file in Finder.".into()) }
    }
    #[cfg(not(target_os = "macos"))]
    { open::that(p.parent().unwrap_or(&p)).map_err(|e| e.to_string()) }
}

/// Open only Composio's short-lived hosted account-authorization links.
/// The native account window check prevents an unrelated webview from using
/// this command as a general browser launcher.
#[tauri::command]
fn open_composio_connect_link(
    window: tauri::WebviewWindow,
    state: State<'_, AppState>,
    url: String,
) -> Result<(), String> {
    require_native_account_window(&window, &state)?;
    let parsed = url::Url::parse(&url).map_err(|_| "invalid_composio_link".to_string())?;
    if parsed.scheme() != "https"
        || parsed.host_str() != Some("connect.composio.dev")
        || !parsed.path().starts_with("/link/")
        || parsed.path() == "/link/"
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.fragment().is_some()
    {
        return Err("invalid_composio_link".into());
    }
    open::that(parsed.as_str()).map_err(|_| "composio_browser_open_failed".to_string())
}

#[tauri::command]
fn store_passphrase(state: State<'_, AppState>, window: tauri::WebviewWindow, passphrase: String) -> Result<(), String> {
    require_native_local_window(&window, &state)?;
    let entry = Entry::new(KEYRING_SERVICE, KEYRING_USER).map_err(|e| e.to_string())?;
    entry.set_password(&passphrase).map_err(|e| e.to_string())
}

#[tauri::command]
fn clear_passphrase(state: State<'_, AppState>, window: tauri::WebviewWindow, ) -> Result<(), String> {
    require_native_local_window(&window, &state)?;
    let entry = Entry::new(KEYRING_SERVICE, KEYRING_USER).map_err(|e| e.to_string())?;
    match entry.delete_credential() {
        Ok(()) => Ok(()),
        Err(keyring::Error::NoEntry) => Ok(()),
        Err(e) => Err(e.to_string()),
    }
}

#[tauri::command]
fn notify(app: AppHandle, title: String, body: String) -> Result<(), String> {
    use tauri_plugin_notification::NotificationExt;
    app.notification()
        .builder()
        .title(title)
        .body(body)
        .show()
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn data_dir_path(state: State<AppState>) -> String {
    state.supervisor.data_dir().display().to_string()
}

#[tauri::command]
fn venture_workspace_path(window: tauri::WebviewWindow,
    state: State<AppState>,
    venture_id: String,
    folder_name: Option<String>,
) -> Result<String, String> {
    require_native_command_window(&window, &state)?;
    ide::ensure_venture_workspace(
        &state.supervisor.data_dir(),
        &venture_id,
        folder_name.as_deref(),
    )
    .map(|p| p.to_string_lossy().into_owned())
}

#[tauri::command]
fn set_active_venture(window: tauri::WebviewWindow, state: State<AppState>, venture_id: String, collapse_suggestions: Option<bool>) -> Result<(), String> {
    require_native_command_window(&window, &state)?;
    if let Ok(mut g) = state.active_venture.lock() {
        let next = if venture_id.is_empty() {
            None
        } else {
            Some(venture_id)
        };
        if *g != next {
            *g = next;
            let _ = window.app_handle().emit_to(suggestions::LABEL, "suggestions-refresh", ());
        }
    }
    if collapse_suggestions == Some(true) {
        let _ = window.app_handle().emit_to(suggestions::LABEL, "suggestions-collapse", ());
    }
    Ok(())
}

#[tauri::command]
fn is_desktop() -> bool {
    true
}

#[derive(Deserialize)]
struct SessionPlan {
    ok: Option<bool>,
    argv: Vec<String>,
    cwd: Option<String>,
    scrub_vars: Option<Vec<String>>,
}

#[tauri::command]
fn spawn_agent_session(window: tauri::WebviewWindow,
    app: AppHandle,
    state: State<AppState>,
    provider: String,
    venture_id: Option<String>,
    auto_approve: bool,
    model: Option<String>,
) -> Result<String, String> {
    require_native_command_window(&window, &state)?;
    let (argv, cwd, scrub) = plan_session(&state, &provider, &venture_id, auto_approve, model.as_deref(), "interactive", None, None)?;
    state.pty.spawn(
        app,
        provider,
        argv,
        cwd,
        state.supervisor.shell_path(),
        &scrub,
    )
}

fn admitted_legacy_session<T>(runtime_mode: &str, packaged_review: bool,
    plan: impl FnOnce() -> Result<T, String>) -> Result<T, String> {
    if runtime_mode == "hosted" || packaged_review {
        return Err("Raw local agent sessions are unavailable in this beta. Use an authenticated, explicitly approved device work order for supported coding or browser tasks.".into());
    }
    // Developer local sessions retain their separate explicit provider consent.
    plan()
}

fn plan_session(
    state: &AppState,
    provider: &str,
    venture_id: &Option<String>,
    auto_approve: bool,
    model: Option<&str>,
    mode: &str,
    prompt: Option<&str>,
    resume: Option<&str>,
) -> Result<(Vec<String>, Option<String>, Vec<String>), String> {
    if local_stack::is_local() {
        return Err("Open your venture chat and request a build with your connected coding tool, then use Builds to watch or stop it.".into());
    }
    admitted_legacy_session(state.supervisor.runtime_mode(), crate::channel::packaged_local_review(), || {
    let core = state.supervisor.core_url();
    let client = reqwest::blocking::Client::builder().user_agent(crate::http::USER_AGENT)
        .timeout(Duration::from_secs(2))
        .build()
        .map_err(|e| e.to_string())?;
    let mut body = serde_json::json!({
        "provider": provider,
        "auto_approve": auto_approve,
        "mode": mode,
    });
    if let Some(vid) = venture_id {
        body["venture_id"] = serde_json::Value::String(vid.clone());
    }
    if let Some(m) = model {
        body["model"] = serde_json::Value::String(m.to_string());
    }
    if let Some(p) = prompt {
        body["prompt"] = serde_json::Value::String(p.to_string());
    }
    if let Some(r) = resume {
        body["resume"] = serde_json::Value::String(r.to_string());
    }
    let res = client
        .post(format!("{core}/coding/sessions/plan"))
        .json(&body)
        .send()
        .map_err(|e| e.to_string())?;
    let status = res.status();
    let text = res.text().map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(plan_error_message(status.as_u16(), &text));
    }
    let plan: SessionPlan = serde_json::from_str(&text).map_err(|e| e.to_string())?;
    if plan.ok == Some(false) {
        return Err("session plan rejected".into());
    }
    Ok((plan.argv, plan.cwd, plan.scrub_vars.unwrap_or_default()))
    })
}

#[tauri::command]
fn list_local_clis(state: State<AppState>) -> Vec<stdio::LocalCli> {
    stdio::list_local_clis(state.supervisor.shell_path())
}

#[tauri::command]
async fn list_local_tools(state: State<'_, AppState>) -> Result<Vec<tools::LocalTool>, String> {
    let sup = state.supervisor.clone();
    tauri::async_runtime::spawn_blocking(move || {
        use std::io::Read;
        let mut tools = tools::list_local_tools(sup.shell_path().as_deref());
        #[derive(Deserialize)]
        struct Statuses { agents: Vec<tools::CodingAgentStatus> }
        let result = (|| -> Option<Statuses> {
            let client = reqwest::blocking::Client::builder()
                .timeout(std::time::Duration::from_secs(12))
                .redirect(reqwest::redirect::Policy::none()).no_proxy().build().ok()?;
            let secret_path = sup.data_dir().join("owner-session.secret");
            let owner = if local_stack::is_local() {
                Some(nav_guard::owner_secret(&secret_path).ok()?)
            } else { None };
            let response = native_bridge_request(&client, &sup, reqwest::Method::GET, "/native/coding-agents").send().ok()?;
            if !response.status().is_success() { return None; }
            let mut raw = Vec::new();
            response.take(4097).read_to_end(&mut raw).ok()?;
            if raw.len() > 4096 { return None; }
            if owner.is_some() && nav_guard::owner_secret(&secret_path).ok() != owner {
                return None;
            }
            serde_json::from_slice(&raw).ok()
        })();
        if let Some(status) = result { tools::apply_coding_statuses(&mut tools, &status.agents); }
        tools
    }).await.map_err(|_| "local_coding_detection_unavailable".to_string())
}

#[tauri::command]
fn connect_local_tool(window: tauri::WebviewWindow,
    app: AppHandle,
    state: State<AppState>,
    name: String,
) -> Result<tools::LocalTool, String> {
    require_native_command_window(&window, &state)?;
    match tools::connect_local_tool(&name, state.supervisor.shell_path().as_deref()) {
        Ok(tool) => Ok(tool),
        Err(err) => {
            let st = perms::status_for(&name);
            if !st.ready && st.kind != "cli" {
                let _ = show_coach_window(&app, &st);
            }
            Err(err)
        }
    }
}

#[tauri::command]
fn macos_control_status(tool: Option<String>) -> perms::MacControlStatus {
    perms::status_for(tool.as_deref().unwrap_or("cursor"))
}

#[tauri::command]
fn open_macos_privacy_pane(state: State<'_, AppState>, window: tauri::WebviewWindow, pane: Option<String>) -> Result<perms::MacControlStatus, String> {
    require_native_command_window(&window, &state)?;
    let pane = pane.unwrap_or_else(|| "accessibility".into());
    match pane.as_str() {
        "automation" => perms::open_privacy_pane("Privacy_Automation"),
        "screen-recording" => perms::open_privacy_pane("Privacy_ScreenCapture"),
        "docker" => {}
        _ => perms::open_privacy_pane("Privacy_Accessibility"),
    }
    Ok(perms::status_for("cursor"))
}

#[tauri::command]
fn show_permission_coach(state: State<'_, AppState>, window: tauri::WebviewWindow, app: AppHandle, tool: Option<String>) -> Result<perms::MacControlStatus, String> {
    require_native_command_window(&window, &state)?;
    let name = tool.unwrap_or_else(|| "cursor".into());
    let st = perms::request_tool_access(&name);
    if st.kind == "cli" || st.ready {
        let _ = hide_permission_coach_window(app);
        return Ok(st);
    }
    show_coach_window(&app, &st)?;
    Ok(st)
}

#[tauri::command]
fn hide_permission_coach(state: State<'_, AppState>, window: tauri::WebviewWindow, app: AppHandle) -> Result<(), String> {
    require_native_command_window(&window, &state)?;
    hide_permission_coach_window(app)
}

fn hide_permission_coach_window(app: AppHandle) -> Result<(), String> {
    if let Some(w) = app.get_webview_window("coach") { w.hide().map_err(|e| e.to_string())?; }
    Ok(())
}

#[tauri::command]
fn confirm_macos_permission(state: State<'_, AppState>, window: tauri::WebviewWindow, app: AppHandle, tool: Option<String>) -> Result<perms::MacControlStatus, String> {
    require_native_command_window(&window, &state)?;
    let name = tool.unwrap_or_else(|| "cursor".into());
    let st = perms::honor_user_confirmation(&name);
    // Always dismiss — the founder confirmed. Do not reopen Settings or the coach
    // just because AXIsProcessTrusted is still false (stale DMG identity).
    let _ = hide_permission_coach_window(app);
    Ok(st)
}

#[tauri::command]
async fn relaunch_app(window: tauri::WebviewWindow, state: State<'_, AppState>, app: AppHandle) -> Result<(), String> {
    require_native_command_window(&window, &state)?;
    let _auth_quiescence = state.auth.quiesce().await?;
    // restart() exits without RunEvent::Exit, so stop the sidecar here.
    if let Some(state) = app.try_state::<AppState>() {
        let _ = state.stdio.kill_all();
        state.pty.kill_all();
        state.streams.stop_all();
        state.supervisor.stop_all();
    }
    error_capture::end_session(&conventional_data_dir());
    app.restart();
}

#[tauri::command]
fn mark_mac_setup_seen(window: tauri::WebviewWindow, state: State<'_, AppState>) -> Result<(), String> {
    require_native_command_window(&window, &state)?;
    tools::mark_setup_seen()
}

#[tauri::command]
fn docker_status(state: State<AppState>) -> docker::DockerStatus {
    docker::status(state.supervisor.shell_path().as_deref())
}

#[tauri::command]
fn start_docker(state: State<'_, AppState>, window: tauri::WebviewWindow, app: AppHandle) -> Result<install::InstallResult, String> {
    require_native_command_window(&window, &state)?;
    let result = docker::start_docker();
    let st = perms::status_for("docker");
    if !st.ready {
        let _ = show_coach_window(&app, &st);
    }
    result
}

#[tauri::command]
async fn install_docker(window: tauri::WebviewWindow,
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<install::InstallResult, String> {
    require_native_command_window(&window, &state)?;
    let shell_path = state.supervisor.shell_path();
    let progress_app = app.clone();
    let result = tokio::task::spawn_blocking(move || {
        docker::install_docker(shell_path.as_deref(), |msg| {
            let _ = progress_app.emit("install-progress", msg);
        })
    })
    .await
    .map_err(|e| format!("Docker install was interrupted ({e})."))?;
    let st = perms::status_for("docker");
    if !st.ready {
        let _ = show_coach_window(&app, &st);
    }
    result
}

#[tauri::command]
fn open_setup_window(state: State<'_, AppState>, window: tauri::WebviewWindow, app: AppHandle) -> Result<(), String> {
    require_native_command_window(&window, &state)?;
    show_setup_window(&app)
}

#[tauri::command]
async fn close_setup_window(state: State<'_, AppState>, window: tauri::WebviewWindow, app: AppHandle) -> Result<(), String> {
    require_native_command_window(&window, &state)?;
    tools::mark_setup_seen().map_err(|_| "mac_setup_preferences_not_saved".to_string())?;
    let window = app.get_webview_window("setup")
        .ok_or_else(|| "mac_setup_window_unavailable".to_string())?;
    window.hide().map_err(|_| "mac_setup_window_close_failed".to_string())
}

#[tauri::command]
async fn install_local_builder(window: tauri::WebviewWindow,
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<install::InstallResult, String> {
    require_native_command_window(&window, &state)?;
    let shell_path = state.supervisor.shell_path();
    tokio::task::spawn_blocking(move || {
        install::install_local_builder(shell_path.as_deref(), |msg| {
            let _ = app.emit("install-progress", msg);
        })
    })
    .await
    .map_err(|e| format!("Install was interrupted ({e})."))?
}

#[tauri::command]
fn uninstall_local_builder(state: State<'_, AppState>, window: tauri::WebviewWindow, ) -> Result<install::InstallResult, String> {
    require_native_command_window(&window, &state)?;
    install::uninstall_local_builder()
}

#[tauri::command]
fn write_hermes_config(state: State<'_, AppState>, window: tauri::WebviewWindow, base_url: String, model: String, api_key: String) -> Result<(), String> {
    require_native_command_window(&window, &state)?;
    install::write_hermes_config(&base_url, &model, &api_key)
}

#[tauri::command]
fn write_hermes_files(state: State<'_, AppState>, window: tauri::WebviewWindow, config_yaml: String, env_file: String) -> Result<(), String> {
    require_native_local_window(&window, &state)?;
    install::write_hermes_files(&config_yaml, &env_file)
}

#[tauri::command]
fn spawn_coding_control(window: tauri::WebviewWindow,
    app: AppHandle,
    state: State<AppState>,
    provider: String,
    venture_id: Option<String>,
    auto_approve: bool,
    mode: Option<String>,
    prompt: Option<String>,
    resume: Option<String>,
) -> Result<String, String> {
    require_native_command_window(&window, &state)?;
    let mode = mode.unwrap_or_else(|| "acp".into());
    let (argv, cwd, scrub) = plan_session(
        &state,
        &provider,
        &venture_id,
        auto_approve,
        None,
        &mode,
        prompt.as_deref(),
        resume.as_deref(),
    )?;
    state.stdio.spawn(
        app,
        provider,
        argv,
        cwd,
        state.supervisor.shell_path(),
        &scrub,
    )
}

#[tauri::command]
async fn start_cursor_ide_session(window: tauri::WebviewWindow,
    state: State<'_, AppState>,
    venture_id: String,
    prompt: String,
    folder_name: Option<String>,
) -> Result<ide::IdeSessionStart, String> {
    require_native_local_window(&window, &state)?;
    if !local_stack::is_local() {
        return Err("Open your venture chat and request a build with your connected coding tool, then watch it in Builds.".into());
    }
    if prompt.trim().is_empty() || prompt.len() > 8000 {
        return Err("Describe what you want built in your venture chat, then select Build with your tool.".into());
    }
    let path = device_order::review_path(&venture_id, "from-chat")?;
    let prepared = local_build_request(state.supervisor.clone(), "POST", path,
        Some(serde_json::json!({"instruction": format!("Build this with Cursor: {prompt}")}))).await?;
    let order_id = prepared.data.get("id").and_then(serde_json::Value::as_str)
        .ok_or("Open Builds to check your requested build before retrying.")?.to_string();
    let reviewed = inspect_device_work_order(window.clone(), state.clone(), venture_id.clone(), order_id.clone()).await?;
    let verification = reviewed.manifest.coding_loop_verification
        .ok_or("Open Keys and connections, verify Cursor, then request your build again in your venture chat.")?;
    if verification.provider != "cursor" {
        return Err("Your request did not select Cursor. Open Keys and connections, connect Cursor, then request your build again in your venture chat.".into());
    }
    let cwd = ide::ensure_reviewed_workspace(&state.supervisor.data_dir(), &venture_id, &verification.workspace_slug)?;
    device_work_order_action(window, state.clone(), venture_id, order_id.clone(), "start".into()).await?;
    let _ = folder_name;
    Ok(ide::IdeSessionStart { cwd: cwd.to_string_lossy().into_owned(), provider: "cursor".into(), opened: false, order_id,
        message: "Open Builds to watch or stop your build, see its preview, or select Open folder.".into() })
}

#[tauri::command]
fn read_venture_landing(window: tauri::WebviewWindow,
    state: State<AppState>,
    venture_id: String,
    folder_name: Option<String>,
) -> Result<ide::LandingSnapshot, String> {
    require_native_command_window(&window, &state)?;
    ide::read_venture_landing(
        &state.supervisor.data_dir(),
        &venture_id,
        folder_name.as_deref(),
    )
}

#[tauri::command]
fn open_coding_ide(window: tauri::WebviewWindow,
    state: State<AppState>,
    provider: String,
    venture_id: Option<String>,
    folder_name: Option<String>,
) -> Result<String, String> {
    require_native_command_window(&window, &state)?;
    let vid = venture_id
        .or_else(|| state.active_venture.lock().ok().and_then(|g| g.clone()))
        .ok_or_else(|| "no venture to open".to_string())?;
    let dir = ide::ensure_venture_workspace(
        &state.supervisor.data_dir(),
        &vid,
        folder_name.as_deref(),
    )?;
    stdio::open_coding_ide(&provider, &dir)
}

#[tauri::command]
fn stdio_write(window: tauri::WebviewWindow, state: State<AppState>, id: String, data: String) -> Result<(), String> {
    require_native_command_window(&window, &state)?;
    state.stdio.write(&id, &data)
}

#[tauri::command]
fn kill_coding_control(window: tauri::WebviewWindow, state: State<AppState>, id: String) -> Result<(), String> {
    require_native_command_window(&window, &state)?;
    stdio::stop_coding_process(&state.stdio, &state.pty, &id)
}

#[tauri::command]
fn pty_write(window: tauri::WebviewWindow, state: State<AppState>, id: String, data: String) -> Result<(), String> {
    require_native_command_window(&window, &state)?;
    state.pty.write(&id, &data)
}

#[tauri::command]
fn pty_resize(window: tauri::WebviewWindow, state: State<AppState>, id: String, cols: u16, rows: u16) -> Result<(), String> {
    require_native_command_window(&window, &state)?;
    state.pty.resize(&id, cols, rows)
}

#[tauri::command]
fn kill_agent_session(window: tauri::WebviewWindow, state: State<AppState>, id: String) -> Result<(), String> {
    require_native_command_window(&window, &state)?;
    state.pty.kill(&id)
}

#[tauri::command]
fn list_agent_sessions(state: State<AppState>) -> Vec<pty::SessionInfo> {
    state.pty.list()
}

#[tauri::command]
fn autostart_enabled(app: AppHandle) -> Result<bool, String> {
    #[cfg(desktop)]
    {
        use tauri_plugin_autostart::ManagerExt;
        return app.autolaunch().is_enabled().map_err(|e| e.to_string());
    }
    #[cfg(not(desktop))]
    {
        let _ = app;
        Ok(false)
    }
}

#[tauri::command]
fn agent_cursor_prefs() -> serde_json::Value {
    let p = agent_cursor::load_prefs(&conventional_data_dir());
    serde_json::json!({ "enabled": p.enabled, "idle_hide_secs": p.idle_hide_secs })
}

#[tauri::command]
async fn agent_cursor_diagnostic_status(app: AppHandle, window: tauri::WebviewWindow, state: State<'_, AppState>) -> Result<serde_json::Value, String> {
    require_native_account_window(&window, &state)?;
    #[cfg(target_os = "macos")]
    { let result = agent_cursor::diagnostic_status(&app).await;
      require_native_account_window(&window, &state)?;
      result }
    #[cfg(not(target_os = "macos"))]
    { let _ = app; Ok(serde_json::json!({"installed":false})) }
}

#[tauri::command]
async fn agent_cursor_rendered(window: tauri::WebviewWindow, receipt: agent_cursor::Rendered) -> bool {
    #[cfg(target_os = "macos")]
    { agent_cursor::rendered(&window, receipt) }
    #[cfg(not(target_os = "macos"))]
    { let _ = (window, receipt); false }
}

#[tauri::command]
fn set_agent_cursor_prefs(state: State<'_, AppState>, window: tauri::WebviewWindow, app: AppHandle, enabled: bool, idle_hide_secs: u64) -> Result<(), String> {
    require_native_command_window(&window, &state)?;
    let prefs = agent_cursor::Prefs::new(enabled, idle_hide_secs);
    agent_cursor::save_prefs(&conventional_data_dir(), prefs).map_err(|e| e.to_string())?;
    #[cfg(target_os = "macos")]
    agent_cursor::apply_prefs(&app, prefs);
    #[cfg(not(target_os = "macos"))]
    let _ = app;
    Ok(())
}

#[tauri::command]
fn app_version(app: AppHandle) -> String {
    app.package_info().version.to_string()
}

fn require_update_window(window: &tauri::WebviewWindow, state: &AppState) -> Result<(), String> {
    let location = window.url().map_err(|_| "Update window location is unavailable.".to_string())?;
    if update::trusted_window(window.label(), &location, &state.supervisor.console_url()) { Ok(()) }
    else { Err("Updates can only be controlled from Supymem's own window.".into()) }
}

/// The launch check shares this code with the founder-initiated update screen.
/// Caller authorization remains at the IPC boundary; launch checks are native.
async fn run_update_check(app: &AppHandle, updates: &update::Controller, allow_github: bool) -> Result<UpdateCheckResult, String> {
    let _operation = updates.begin()?;
    let current = app.package_info().version.to_string();
    if local_stack::is_local() {
        *updates.candidate.lock().map_err(|_| "Updater state unavailable.")? = None;
        return Ok(update::local_update_check(&current, allow_github).await);
    }
    if !channel::updater_enabled() {
        return Ok(UpdateCheckResult::none(&current, "", "This staging review build has no automatic update channel. Install a newly reviewed staging artifact to update it."));
    }
    let endpoint = update::resolve_update_endpoint();
    let parsed = update::validate_download_url(&endpoint, cfg!(debug_assertions))?;
    let updater = app.updater_builder().endpoints(vec![parsed])
        .map_err(|_| "Update channel configuration is invalid.")?
        .timeout(Duration::from_secs(12))
        .configure_client(|client| client.user_agent(crate::http::USER_AGENT).https_only(!cfg!(debug_assertions)))
        .build().map_err(|_| "Update checker could not start.")?;
    let found = updater.check().await;
    let result = match found {
        Ok(Some(mut package)) => {
            update::validate_download_url(package.download_url.as_str(), cfg!(debug_assertions))?;
            package.timeout = Some(Duration::from_secs(180));
            let manifest = update::LatestManifest { version: package.version.clone(), notes: package.body.clone(), pub_date: None, platforms: None };
            let mut result = update::evaluate_manifest(&current, &endpoint, &manifest);
            let identity = update::CandidateIdentity { version: package.version.clone(), url: package.download_url.to_string(), signature: package.signature.clone(), notes: package.body.clone() };
            let mut candidate = updates.candidate.lock().map_err(|_| "Updater state unavailable.")?;
            let receipt = update::Receipt::for_candidate(candidate.as_ref().map(|old| (&old.identity, &old.receipt)), &identity);
            result.check_id = Some(receipt.id.clone());
            *candidate = Some(update::Candidate { package, result: result.clone(), receipt, identity });
            result
        }
        Ok(None) => {
            *updates.candidate.lock().map_err(|_| "Updater state unavailable.")? = None;
            UpdateCheckResult::up_to_date(&current, &endpoint)
        }
        Err(_) => {
            *updates.candidate.lock().map_err(|_| "Updater state unavailable.")? = None;
            UpdateCheckResult::none(&current, &endpoint, "The update channel could not be verified or has no published release. Nothing was installed. Try another check after connectivity or release availability changes.")
        },
    };
    log::info!("desktop_update phase=checked available={} version={:?}", result.available, result.version);
    shell_events::record("activate", "updater_check", None);
    shell_events::record("outcome", "updater_check", Some(if result.available || result.channel_verified { "ok" } else { "failed" }));
    if result.available {
        shell_events::record("activate", "updater_offered", None);
    }
    update::persist_status(&conventional_data_dir(), &result)?;
    Ok(result)
}

#[tauri::command]
async fn check_for_update(app: AppHandle, window: tauri::WebviewWindow, state: State<'_, AppState>,
                          updates: State<'_, update::Controller>, allow_github: Option<bool>) -> Result<UpdateCheckResult, String> {
    require_update_window(&window, &state)?;
    run_update_check(&app, &updates, window.label() == "updates" && allow_github == Some(true)).await
}

/// Menu "Check for updates": say so in a notification when already current (updates window if notifications are off), else open the updater window.
fn check_updates_with_feedback(app: &AppHandle) {
    if local_stack::is_local() {
        if let Err(error) = show_update_window(app) {
            log::warn!("desktop_update phase=window_failed message={error}");
        }
        return;
    }
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let result = match app.try_state::<update::Controller>() {
            Some(updates) => run_update_check(&app, &updates, false).await.ok(),
            None => None,
        };
        match result {
            Some(r) if !r.available && r.channel_verified => {
                // A notification is invisible when macOS notifications are off; fall back to the updates window.
                let body = format!("You're on {} \u{2014} up to date.", r.current_version);
                if deliver_native_notice(&app, "app-update-check", "Supymem", &body, None).is_err() {
                    if let Err(error) = show_update_window(&app) {
                        log::warn!("desktop_update phase=window_failed message={error}");
                    }
                }
            }
            _ => {
                if let Err(error) = show_update_window(&app) {
                    log::warn!("desktop_update phase=window_failed message={error}");
                }
            }
        }
    });
}

fn show_update_window(app: &AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("updates") {
        window.show().map_err(|e| e.to_string())?;
        window.set_focus().map_err(|e| e.to_string())?;
        return Ok(());
    }
    let release_app = app.clone();
    let local = local_stack::is_local();
    let repository = update::local_update_repository();
    let update_repo_configured = repository.is_some();
    tauri::WebviewWindowBuilder::new(app, "updates", tauri::WebviewUrl::App("update.html".into()))
        .title("Supymem updates").inner_size(500.0, 440.0).resizable(true)
        .initialization_script(format!("window.__SUPYMEM_LOCAL_UPDATES__ = {local}; window.__SUPYMEM_UPDATE_REPO_CONFIGURED__ = {update_repo_configured};"))
        .on_navigation(move |location| {
            if local && location.scheme() == "https" && location.host_str() == Some("github.com")
                && location.username().is_empty() && location.password().is_none()
                && repository.is_some_and(|repository| location.path() == format!("/{repository}/releases")
                    || location.path().starts_with(&format!("/{repository}/releases/"))) {
                if open::that(location.as_str()).is_err() {
                    if let Some(window) = release_app.get_webview_window("updates") {
                        let _ = window.eval("document.getElementById('status').textContent='Your release page could not be opened. Copy the link into your browser to download your update.'");
                    }
                }
                return false;
            }
            !local || update::trusted_window("updates", location, "")
        })
        .build().map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn request_update_check(app: AppHandle, window: tauri::WebviewWindow, state: State<'_, AppState>) -> Result<(), String> {
    require_update_window(&window, &state)?;
    show_update_window(&app)
}

#[tauri::command]
fn defer_update(window: tauri::WebviewWindow, state: State<'_, AppState>, updates: State<'_, update::Controller>) -> Result<(), String> {
    require_update_window(&window, &state)?;
    let candidate = updates.candidate.lock().map_err(|_| "Updater state unavailable.")?;
    if candidate.as_ref().is_some_and(|candidate| candidate.result.force) {
        return Err("This update is required and cannot be deferred.".into());
    }
    Ok(())
}

#[tauri::command]
async fn install_update(app: AppHandle, window: tauri::WebviewWindow, state: State<'_, AppState>,
                        updates: State<'_, update::Controller>, check_id: String) -> Result<(), String> {
    require_update_window(&window, &state)?;
    if local_stack::is_local() {
        return Err("Open your update screen and choose the GitHub release page to download your local installer manually.".into());
    }
    if !channel::updater_enabled() {
        return Err("Automatic update installation is disabled in this staging build.".into());
    }
    let _operation = updates.begin()?;
    let package = {
        let candidate = updates.candidate.lock().map_err(|_| "Updater state unavailable.")?;
        let candidate = candidate.as_ref().ok_or("Check for updates before installing.")?;
        candidate.receipt.confirm(&check_id)?;
        candidate.package.clone()
    };
    #[cfg(target_os = "macos")]
    let app_path = update_install::current_app()?;
    let mut downloaded = 0u64;
    let mut last_emit = std::time::Instant::now() - Duration::from_secs(1);
    let progress_app = app.clone();
    log::info!("desktop_update phase=downloading version={}", package.version);
    let _ = app.emit("supymem-update-progress", update::Progress { phase: "downloading", downloaded: 0, total: None });
    let bytes = package.download(move |chunk, total| {
        downloaded += chunk as u64;
        if last_emit.elapsed() >= Duration::from_millis(250) {
            let _ = progress_app.emit("supymem-update-progress", update::Progress { phase: "downloading", downloaded, total });
            last_emit = std::time::Instant::now();
        }
    }, || {}).await.map_err(|error| {
        log::warn!("desktop_update phase=download_failed");
        shell_events::record("activate", "updater_failed", None);
        error_capture::report("shell_updater");
        update::honesty_for_install_error(&error.to_string())
    })?;
    // Tauri verifies the pinned Minisign signature before download returns.
    let _ = app.emit("supymem-update-progress", update::Progress { phase: "installing", downloaded: bytes.len() as u64, total: Some(bytes.len() as u64) });
    log::info!("desktop_update phase=signature_verified version={}", package.version);
    let _auth_quiescence = state.auth.quiesce().await?;
    #[cfg(target_os = "macos")]
    let installed = tokio::task::spawn_blocking(move || update_install::install_with_recovery(&app_path, || package.install(bytes).map_err(|e| e.to_string())))
        .await.map_err(|_| "Update installation was interrupted. Inspect the app before restarting.")?;
    #[cfg(not(target_os = "macos"))]
    let installed = package.install(bytes).map_err(|e| update::honesty_for_install_error(&e.to_string()));
    if installed.is_err() { shell_events::record("activate", "updater_failed", None); error_capture::report("shell_updater"); }
    installed?;
    shell_events::record("activate", "updater_installed", None);
    // Failed downloads/installs leave native tools and the authenticated bridge running.
    state.stdio.kill_all().map_err(|_| "Update installed, but a local process could not be stopped. Restart was withheld.".to_string())?;
    state.pty.kill_all();
    state.streams.stop_all();
    state.supervisor.stop_all();
    log::info!("desktop_update phase=installed_restarting");
    let _ = app.emit("supymem-update-progress", update::Progress { phase: "restarting", downloaded: 0, total: None });
    error_capture::end_session(&conventional_data_dir());
    app.restart();
}

/// Usage events the shell recorded while the Console was signed out or not running; the signed-in Console
/// sends them on its normal telemetry path. Empties the ring.
#[tauri::command]
fn drain_native_events(window: tauri::WebviewWindow, state: State<'_, AppState>) -> Result<Vec<serde_json::Value>, String> {
    require_native_account_window(&window, &state)?;
    Ok(shell_events::drain_default())
}

#[tauri::command]
fn set_autostart(state: State<'_, AppState>, window: tauri::WebviewWindow, app: AppHandle, enabled: bool) -> Result<(), String> {
    require_native_command_window(&window, &state)?;
    #[cfg(desktop)]
    {
        use tauri_plugin_autostart::ManagerExt;
        if enabled {
            app.autolaunch().enable().map_err(|e| e.to_string())
        } else {
            app.autolaunch().disable().map_err(|e| e.to_string())
        }
    }
    #[cfg(not(desktop))]
    {
        let _ = (app, enabled);
        Ok(())
    }
}

fn plan_error_message(status: u16, text: &str) -> String {
    if let Ok(v) = serde_json::from_str::<serde_json::Value>(text) {
        if let Some(msg) = v
            .pointer("/detail/error/message")
            .and_then(|x| x.as_str())
            .filter(|s| !s.is_empty())
        {
            return msg.to_string();
        }
        if let Some(code) = v.pointer("/detail/error/code").and_then(|x| x.as_str()) {
            return format!("session plan failed: {code}");
        }
    }
    if text.trim().is_empty() {
        format!("session plan failed: {status}")
    } else {
        format!("session plan failed: {text}")
    }
}

#[tauri::command]
async fn desktop_retry_console(app:AppHandle,window:tauri::WebviewWindow,state:State<'_,AppState>) -> Result<console_connection::ConnectionReport,String> {
    require_native_account_window(&window,&state)?;
    let sup=state.supervisor.clone();
    if local_stack::is_local() {
        return tauri::async_runtime::spawn_blocking(move || {
            run_local_startup(&app, sup)?;
            Ok(console_connection::ConnectionReport::ready())
        }).await.map_err(|_| "connection_retry_failed".to_string())?;
    }
    tauri::async_runtime::spawn_blocking(move || show_main_with(&app,&sup.console_url(),&sup.gateway_url(),&sup.bridge_url(),&sup.bridge_token(),sup.runtime_mode()))
        .await.map_err(|_|"connection_retry_failed".into())
}

fn show_splash_error(app: &AppHandle, reason: &str) {
    error_capture::report_reason("shell_start_failed", reason);
    let js = format!(
        r#"var el=document.getElementById("status");if(el){{el.dataset.final="1";el.textContent={msg};}}"#,
        msg = serde_json::to_string("Supymem couldn’t open. Quit and reopen the app to retry. Your saved sign-in and work are retained.").unwrap(),
    );
    if let Some(main) = app.get_webview_window("main") {
        let _ = main.eval(&js);
        let _ = main.show();
    }
}

fn show_local_error(app: &AppHandle, supervisor: &Supervisor) {
    show_local_startup_page(app, supervisor, false);
}

fn show_local_startup_page(app: &AppHandle, supervisor: &Supervisor, starting: bool) {
    let Some(log_path) = supervisor.local_log_path() else { return; };
    if let Some(main) = app.get_webview_window("main") {
        let origin = if cfg!(target_os = "windows") { "http://tauri.localhost" } else { "tauri://localhost" };
        if let Ok(mut target) = format!("{origin}/local-error.html").parse::<url::Url>() {
            target.query_pairs_mut().append_pair("log", &log_path.to_string_lossy());
            if starting { target.query_pairs_mut().append_pair("starting", "1"); }
            else { target.query_pairs_mut().append_pair("reason", &supervisor.local_error_message()); }
            let _ = main.navigate(target);
            bring_window_forward(&main);
        }
    }
}

fn run_local_startup(app: &AppHandle, supervisor: Arc<Supervisor>) -> Result<(), String> {
    show_local_startup_page(app, &supervisor, true);
    if let Err(error) = supervisor.start_all_with_console(|| {
        let console = supervisor.console_url();
        show_main_with(app, &console, &supervisor.gateway_url(), &supervisor.bridge_url(), &supervisor.bridge_token(), "local");
        if !finish_main_navigation(app, &console) {
            return Err("Local console navigation failed".into());
        }
        Ok(())
    }) {
        let _ = app.emit("backend-error", &error);
        show_local_error(app, &supervisor);
        return Err(error);
    }
    let _ = app.emit("backend-ready", supervisor.gateway_url());
    let app = app.clone();
    std::thread::spawn(move || {
        let sup = supervisor;
        while sup.is_ready() {
            if let Some(error) = sup.local_failure() {
                let _ = app.emit("backend-error", &error);
                show_local_error(&app, &sup);
                break;
            }
            std::thread::sleep(Duration::from_secs(1));
        }
    });
    Ok(())
}

fn show_main(app: &AppHandle, console: &str, gateway: &str) {
    show_main_with(app, console, gateway, "http://127.0.0.1:3500", "", "local");
}

fn show_main_with(
    app: &AppHandle,
    console: &str,
    _gateway: &str,
    _bridge: &str,
    _bridge_token: &str,
    mode: &str,
) -> console_connection::ConnectionReport {
    let report = if mode == "hosted" { console_connection::check(console) } else { console_connection::ConnectionReport::ready() };
    console_connection::record(&report, &conventional_data_dir());
    if !report.connected {
        error_capture::report("shell_console_unreachable");
        if let Some(main)=app.get_webview_window("main") {
            let origin=if cfg!(target_os="windows") {"http://tauri.localhost"}else{"tauri://localhost"};
            if let Ok(url)=format!("{origin}/offline.html").parse::<url::Url>() {
                if main.url().ok().as_ref()!=Some(&url) {
                    let _=main.navigate(url);
                    bring_window_forward(&main);
                }
            }
        }
        return report;
    }
    navigate_main(app, console);
    keep_page_guards(app.clone());
    maybe_open_setup(app);
    report
}

fn navigate_main(app: &AppHandle, console: &str) -> bool {
    let Some(main) = app.get_webview_window("main") else { return false; };
    let Ok(target) = console.parse::<url::Url>() else { return false; };
    if main.url().ok().as_ref() == Some(&target) {
        bring_window_forward(&main);
        return true;
    }
    // `eval` can report that a script was queued while the bundled splash
    // document is still becoming executable, then silently drop that script.
    // Native navigation has an observable URL result, so retry that operation
    // directly and let the bounded caller verify the transition.
    let _ = main.navigate(target.clone());
    bring_window_forward(&main);
    main.url().ok().as_ref() == Some(&target)
}

fn finish_main_navigation(app: &AppHandle, console: &str) -> bool {
    for _ in 0..80 {
        if navigate_main(app, console) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    false
}

static MAIN_FULLSCREEN: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

fn bring_window_forward(window: &tauri::WebviewWindow) {
    let _ = window.unminimize();
    let _ = window.show();
    let _ = window.set_focus();
    nudge_repaint(window);
}

/// WKWebView can keep a stale (black) layer after native full screen, minimize
/// or hide/show. Ask AppKit and the page to redraw now and again once the
/// transition animation has settled. Cheap and state-preserving (no reload).
fn nudge_repaint(window: &tauri::WebviewWindow) {
    fn once(w: &tauri::WebviewWindow) {
        #[cfg(target_os = "macos")]
        if let Ok(ptr) = w.ns_window() {
            // SAFETY: called on the main thread with the live NSWindow of `w`.
            let ns = unsafe { &*(ptr as *const objc2_app_kit::NSWindow) };
            if let Some(view) = ns.contentView() {
                view.setNeedsDisplay(true);
            }
            ns.displayIfNeeded();
        }
        let _ = w.eval("void document.documentElement.offsetHeight;requestAnimationFrame(function(){window.dispatchEvent(new Event('resize'))})");
    }
    let w = window.clone();
    thread::spawn(move || {
        for ms in [0u64, 350, 900] {
            thread::sleep(Duration::from_millis(ms));
            let again = w.clone();
            let _ = w.run_on_main_thread(move || once(&again));
        }
    });
}

/// Keep the shell on screen without stealing focus from a computer-use target.
fn keep_window_visible(window: &tauri::WebviewWindow) {
    let _ = window.unminimize();
    let _ = window.show();
}

/// Only restore when the shell is actually gone from the screen. Calling
/// `show()` every tick would order the window front and cover the task target.
fn should_restore_window(is_visible: bool, is_minimized: bool) -> bool {
    !is_visible || is_minimized
}

/// Only app-navigation (whose purpose is to show a Supymem screen) may steal
/// focus when a run finishes. Other tasks leave focus alone; the #172 notice
/// is what gets the founder back.
fn should_focus_on_finish(is_app_navigation: bool) -> bool {
    is_app_navigation
}

fn restore_window_if_needed(window: &tauri::WebviewWindow) {
    let visible = window.is_visible().unwrap_or(true);
    let minimized = window.is_minimized().unwrap_or(false);
    if should_restore_window(visible, minimized) {
        keep_window_visible(window);
    }
}

fn finish_native_run_window(window: &tauri::WebviewWindow, focus_on_finish: bool) {
    if should_focus_on_finish(focus_on_finish) {
        bring_window_forward(window);
    } else {
        restore_window_if_needed(window);
    }
}

/// Closed control id for a tray, app-menu, Help-menu or mascot-menu item (see the server feature catalog).
fn menu_control(id: &str) -> Option<&'static str> {
    Some(match id {
        "show" => "tray_show",
        "new_venture" => "tray_new_venture",
        "setup" => "tray_setup_mac",
        "pause" => "tray_pause",
        "resume" => "tray_resume",
        "help" => "tray_help",
        "report" => "feedback_bug_report",
        "permissions" => "tray_setup_permissions",
        "updates" | "app-updates" => "tray_check_updates",
        "reveal" => "tray_reveal_data",
        "quit" => "tray_quit",
        _ => return None,
    })
}

/// Shared by the macOS app menu, the mascot context menu and the non-macOS tray.
pub(crate) fn handle_menu_action(app: &AppHandle, id: &str) {
    if let Some(control) = menu_control(id) {
        shell_events::record("activate", control, None);
    }
    match id {
        "show" => {
            bring_main_forward(app);
        }
        "new_venture" => {
            bring_main_forward(app);
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.eval("window.location.assign('/new')");
            }
        }
        #[cfg(target_os = "macos")]
        "charlie_visibility" => {
            if let Err(error) = mascot::set_visible(app, !mascot::is_visible()) {
                log::warn!("Charlie visibility could not be saved: {error}");
            }
        }
        "setup" => {
            let _ = show_setup_window(app);
        }
        "pause" => tray_venture_action(app, true),
        "resume" => tray_venture_action(app, false),
        "help" => {
            if local_stack::is_local() {
                bring_main_forward(app);
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.eval("window.location.assign('/guide')");
                }
            } else {
                let _ = open::that("https://supymem.com");
            }
        }
        "report" => {
            // The Settings page holds the desktop bug-report form; without an open venture, open the home page.
            bring_main_forward(app);
            if local_stack::is_local() {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.eval("window.location.assign('/settings/local/support')");
                }
                return;
            }
            let vid = app.try_state::<AppState>().and_then(|s| s.active_venture.lock().ok().and_then(|g| g.clone()));
            let path = vid.map_or("/".to_string(), |v| format!("/v/{}/settings", v.replace(|c: char| !(c.is_ascii_alphanumeric() || c == '_' || c == '-'), "")));
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.eval(format!("window.location.assign('{path}')"));
            }
        }
        #[cfg(target_os = "macos")]
        "cursor-diagnostic" => {
            let handle = app.clone();
            tauri::async_runtime::spawn(async move {
                if agent_cursor::export_diagnostic(&handle).await.is_err() {
                    log::warn!("cursor_diagnostic_export_unavailable");
                }
            });
        }
        "permissions" if tools::BETA_COMPUTER_USE => {
            bring_main_forward(app);
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.eval("window.location.assign('/setup/permissions')");
            }
        }
        "updates" | "app-updates" => check_updates_with_feedback(app),
        "reveal" => {
            if let Some(state) = app.try_state::<AppState>() {
                let _ = reveal_path(
                    state.supervisor.data_dir().display().to_string(),
                );
            }
        }
        "quit" => {
            if let Some(state) = app.try_state::<AppState>() {
                if let Err(error) = state.stdio.kill_all() {
                    log::error!("Local process shutdown failed: {error}");
                    return;
                }
                state.pty.kill_all();
                state.streams.stop_all();
                state.supervisor.stop_all();
            }
            app.exit(0);
        }
        _ => {}
    }
}

fn bring_main_forward(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        bring_window_forward(&w);
    }
}

fn restore_main_if_needed(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        restore_window_if_needed(&w);
    }
}

fn finish_main_native_run(app: &AppHandle, focus_on_finish: bool) {
    if let Some(w) = app.get_webview_window("main") {
        finish_native_run_window(&w, focus_on_finish);
    }
}

/// Terminal local-execution states. Matching ones clear the keep-visible guard.
fn native_execution_is_terminal(state: &str) -> bool {
    matches!(
        state,
        "stopped"
            | "not_started"
            | "reconciliation_required"
            | "interrupted_reconciliation_required"
    )
}

// Status can race a new start and return the preceding not_started receipt.
// Only explicit terminal actions may synchronously release the CUA host;
// the start watcher owns terminal cleanup for read-only status observations.
fn native_action_reaps_cua_host(action: &str, state: &str) -> bool {
    matches!(action, "cancel" | "reconcile") && native_execution_is_terminal(state)
}

// The bridge may briefly expose the previous `not_started` receipt while a
// newly approved start initializes its per-task CUA proxy. Status reads must
// never reap the host; this watcher gives a new start enough time to claim or
// report a real failure before it closes the daemon and its private socket.
fn native_watch_terminal(state: &str, age: Duration) -> bool {
    native_execution_is_terminal(state)
        && (state != "not_started" || age >= Duration::from_secs(20))
}

/// Close-to-tray is the normal macOS shell behavior, but not while a reviewed
/// Mac task is still running: the founder must keep the review surface.
fn close_requested_should_hide_main(native_run_active: bool) -> bool {
    !native_run_active
}

fn native_run_is_active(state: &AppState) -> bool {
    state
        .active_native_order
        .lock()
        .map(|guard| guard.is_some())
        .unwrap_or(false)
}

fn native_run_focus_on_finish(state: &AppState) -> bool {
    state
        .active_native_order
        .lock()
        .map(|guard| guard.as_ref().is_some_and(|(_, _, focus)| *focus))
        .unwrap_or(false)
}

fn mark_native_run(state: &AppState, venture_id: &str, order_id: &str, focus_on_finish: bool) {
    if let Ok(mut guard) = state.active_native_order.lock() {
        *guard = Some((
            venture_id.to_string(),
            order_id.to_string(),
            focus_on_finish,
        ));
    }
}

fn clear_native_run(state: &AppState, venture_id: &str, order_id: &str) {
    if let Ok(mut guard) = state.active_native_order.lock() {
        if guard
            .as_ref()
            .is_some_and(|(v, o, _)| v == venture_id && o == order_id)
        {
            *guard = None;
        }
    }
}

fn watch_native_run_visibility(
    app: AppHandle,
    supervisor: Arc<Supervisor>,
    venture_id: String,
    order_id: String,
    focus_on_finish: bool,
) {
    thread::spawn(move || {
        let watch_started = std::time::Instant::now();
        let status_url = format!(
            "{}/native/device-work-orders/{venture_id}/{order_id}/status",
            supervisor.bridge_url()
        );
        let token = supervisor.native_bridge_token();
        let client = match reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(3))
            .redirect(reqwest::redirect::Policy::none())
            .no_proxy()
            .build()
        {
            Ok(client) => client,
            Err(_) => {
                shell_events::record("error", "device_work_order_action", None);
                finish_main_native_run(&app, focus_on_finish);
                return;
            }
        };
        // Bound the watch so a stuck worker cannot pin the keep-visible guard forever.
        let mut terminal_observed = false;
        for _ in 0..900 {
            // Only act when the shell is actually hidden or minimized. Do not
            // call show() every tick: that would cover the task's target app.
            restore_main_if_needed(&app);
            let done = match client
                .get(&status_url)
                .header("x-native-token", &token)
                .send()
                .and_then(|response| response.json::<serde_json::Value>())
            {
                Ok(status) => status
                    .get("state")
                    .and_then(|value| value.as_str())
                    .is_some_and(|state| native_watch_terminal(state, watch_started.elapsed())),
                Err(_) => false,
            };
            if done {
                terminal_observed = true;
                break;
            }
            thread::sleep(Duration::from_millis(500));
        }
        // The status watch ran out without seeing the task finish: the Console never got a failure to report.
        if !terminal_observed {
            shell_events::record("error", "device_work_order_action", None);
        }
        if let Some(state) = app.try_state::<AppState>() {
            clear_native_run(&state, &venture_id, &order_id);
            #[cfg(target_os = "macos")]
            if terminal_observed {
                if let Ok(mut host) = state.cua_host.lock() {
                    if host.as_ref().is_some_and(|(id, _)| id == &order_id) { host.take(); }
                }
            }
        }
        finish_main_native_run(&app, focus_on_finish);
    });
}

fn keep_page_guards(app: AppHandle) {
    thread::spawn(move || {
        for i in 0..50 {
            thread::sleep(Duration::from_millis(if i < 12 { 350 } else { 1200 }));
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.eval(tools::desktop_page_guards_js());
            }
        }
    });
}

fn show_setup_window(app: &AppHandle) -> Result<(), String> {
    if let Some(w) = app.get_webview_window("setup") {
        let _ = w.show();
        let _ = w.set_focus();
        return Ok(());
    }
    Err("Setup window is missing from this build.".into())
}

fn show_coach_window(app: &AppHandle, st: &perms::MacControlStatus) -> Result<(), String> {
    if st.ready || st.kind == "cli" {
        if let Some(w) = app.get_webview_window("coach") {
            let _ = w.hide();
        }
        return Ok(());
    }
    let w = app
        .get_webview_window("coach")
        .ok_or_else(|| "Coach window is missing from this build.".to_string())?;
    if local_stack::is_local() {
        w.eval("window.__SUPYMEM__ = {mode: 'local'};").map_err(|error| error.to_string())?;
    }
    let _ = app.emit("permission-coach", st);
    let _ = w.show();
    Ok(())
}

fn maybe_open_setup(app: &AppHandle) {
    if tools::setup_needed() {
        let _ = show_setup_window(app);
    }
}

fn trusted_notice_target(console: &str, path: &str) -> Option<url::Url> {
    if path.contains('?') || path.contains("..") || !path.starts_with("/v/") || path.contains('%') {
        return None;
    }
    let (route, fragment) = path.split_once('#').map_or((path, None), |(route, fragment)| (route, Some(fragment)));
    let parts: Vec<&str> = route.split('/').collect();
    let venture_ok = parts.get(2).is_some_and(|venture| venture.starts_with("ven_")
        && venture.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-')));
    let route_ok = parts.len() == 4 && fragment.is_none()
        && matches!(parts[3], "now" | "work" | "outreach" | "connections" | "analytics" | "settings" | "suggestions");
    let work_order_ok = parts.len() == 5 && fragment.is_none()
        && parts[3] == "device-work-orders"
        && parts[4].starts_with("dwo_")
        && parts[4].chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-'));
    let attention_ok = parts.len() == 3 && fragment.is_some_and(|value| value.starts_with("attention-ht_")
        && value.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-')));
    if !venture_ok || (!route_ok && !work_order_ok && !attention_ok) {
        return None;
    }
    let base = console.parse::<url::Url>().ok()?;
    if (base.scheme() != "https"
        || !matches!(base.host_str(), Some("app.example.invalid" | "app.staging.example.invalid")))
        && !(base.scheme() == "http" && matches!(base.host_str(), Some("127.0.0.1" | "localhost")))
    {
        return None;
    }
    // Beta hides /v/<id>/connections and has no outreach page: send those clicks to visible routes.
    let mapped = match parts.get(3).filter(|_| parts.len() == 4) {
        Some(&"connections") if !tools::BETA_COMPUTER_USE => "/connections".to_string(),
        Some(&"outreach") if !tools::BETA_COMPUTER_USE => format!("/v/{}", parts[2]),
        _ => path.to_string(),
    };
    let target = base.join(&mapped).ok()?;
    (target.origin() == base.origin()).then_some(target)
}

#[derive(Clone, serde::Serialize)]
struct DesktopNoticePayload {
    subject: String,
    device_id: String,
    auth_epoch: u64,
    id: String,
    title: String,
    body: String,
    target_path: Option<String>,
}

#[derive(Clone, serde::Serialize, PartialEq, Eq)]
pub(crate) struct NoticeCustody { subject: String, device_id: String, auth_epoch: u64 }
const MAX_ISSUED_NOTICES: usize = 256;
const MAX_NOTICE_RECEIPT_ATTEMPTS: u8 = 3;
#[derive(PartialEq, Eq)]
enum NoticeReceiptState { Pending, InFlight, Confirmed }
struct IssuedNotice { id: String, owner: NoticeCustody, state: NoticeReceiptState, attempts: u8 }
#[derive(Default)]
struct IssuedNotices { records: Vec<IssuedNotice> }
impl IssuedNotices {
    fn issue(&mut self, id: &str, custody: &NoticeCustody) -> bool {
        if id.is_empty() || id.len() > 100 { return false; }
        // Retired generations cannot qualify a callback. Within one current
        // generation retain unconfirmed ACKs. Only this idempotent delivery
        // receipt may retry; no device execution or native-channel result does.
        self.records.retain(|record| &record.owner == custody);
        if let Some(record) = self.records.iter().find(|record| record.id == id) {
            return record.state == NoticeReceiptState::Pending && record.attempts < MAX_NOTICE_RECEIPT_ATTEMPTS;
        }
        if self.records.len() >= MAX_ISSUED_NOTICES {
            let Some(oldest_confirmed) = self.records.iter().position(|record| record.state == NoticeReceiptState::Confirmed)
                else { return false; };
            self.records.remove(oldest_confirmed);
        }
        self.records.push(IssuedNotice {id: id.to_owned(), owner: custody.clone(), state: NoticeReceiptState::Pending, attempts: 0}); true
    }
    fn revoke_unemitted(&mut self, id: &str, custody: &NoticeCustody) {
        self.records.retain(|record| record.id != id || &record.owner != custody || record.attempts > 0);
    }
    fn claim_presented(&mut self, id: &str, custody: &NoticeCustody) -> bool {
        let Some(record) = self.records.iter_mut()
            .find(|record| record.id == id && &record.owner == custody) else { return false; };
        if record.state != NoticeReceiptState::Pending || record.attempts >= MAX_NOTICE_RECEIPT_ATTEMPTS { return false; }
        record.state = NoticeReceiptState::InFlight; record.attempts += 1; true
    }
    fn confirm_presented(&mut self, id: &str, custody: &NoticeCustody) {
        if let Some(record) = self.records.iter_mut().find(|record| record.id == id && &record.owner == custody) {
            if record.state == NoticeReceiptState::InFlight { record.state = NoticeReceiptState::Confirmed; }
        }
    }
    fn release_unconfirmed(&mut self, id: &str, custody: &NoticeCustody) {
        if let Some(record) = self.records.iter_mut().find(|record| record.id == id && &record.owner == custody) {
            if record.state == NoticeReceiptState::InFlight { record.state = NoticeReceiptState::Pending; }
        }
    }
}
fn issued_notices() -> &'static Mutex<IssuedNotices> {
    static ISSUED: OnceLock<Mutex<IssuedNotices>> = OnceLock::new();
    ISSUED.get_or_init(|| Mutex::new(IssuedNotices::default()))
}
fn notice_presented_payload(id: &str, custody: &NoticeCustody) -> serde_json::Value {
    serde_json::json!({"recipient_subject": custody.subject,
        "results": [{"id": id, "in_app": "in_app_shown"}]})
}
fn notice_presented_recorded(id: &str, payload: &serde_json::Value) -> bool {
    payload.get("statuses").and_then(|value| value.as_array()).is_some_and(|rows| rows.iter().any(|row|
        row.get("id").and_then(|value| value.as_str()) == Some(id)
        && row.get("in_app_status").and_then(|value| value.as_str()) == Some("in_app_shown")))
}
fn with_notice_custody<R>(app: &AppHandle, custody: &NoticeCustody, dispatch: impl FnOnce() -> R) -> Option<R> {
    app.state::<AppState>().auth.with_notice_custody(
        &(custody.subject.clone(), custody.device_id.clone(), custody.auth_epoch), dispatch)
}
fn notice_custody(app: &AppHandle) -> Option<NoticeCustody> {
    let state = app.state::<AppState>();
    let (subject, device_id, auth_epoch) = state.auth.notice_custody()?;
    Some(NoticeCustody { subject, device_id, auth_epoch })
}
#[tauri::command]
async fn desktop_notice_custody(window: tauri::WebviewWindow, state: State<'_, AppState>) -> Result<Option<NoticeCustody>, String> {
    require_native_account_window(&window, &state)?;
    let auth = state.auth.acquire().await?;
    require_native_account_window(&window, &state)?;
    Ok(auth.notice_custody().map(|(subject, device_id, auth_epoch)| NoticeCustody {subject, device_id, auth_epoch}))
}
#[tauri::command]
async fn desktop_notice_presented(window: tauri::WebviewWindow, state: State<'_, AppState>,
    notice_id: String, subject: String, device_id: String, auth_epoch: u64) -> Result<bool, String> {
    require_native_account_window(&window, &state)?;
    let auth = state.auth.acquire().await?;
    require_native_account_window(&window, &state)?;
    let custody = NoticeCustody {subject, device_id, auth_epoch};
    if auth.notice_custody() != Some((custody.subject.clone(), custody.device_id.clone(), custody.auth_epoch)) {
        return Ok(false);
    }
    let supervisor = state.supervisor.clone();
    tauri::async_runtime::spawn_blocking(move || {
        // The exact current account remains pinned through this bounded local
        // ACK. No body text or native-channel result is accepted from the page.
        let _auth = auth;
        if !supervisor.is_ready() { return Ok(false); }
        let client = reqwest::blocking::Client::builder().user_agent(crate::http::USER_AGENT)
            .timeout(Duration::from_secs(3)).redirect(reqwest::redirect::Policy::none())
            .no_proxy().build().map_err(|_| "notice_receipt_unavailable")?;
        // Definite no-dispatch preflight failures leave the receipt pending.
        let admitted = issued_notices().lock().map_err(|_| "notice_receipt_unavailable")?
            .claim_presented(&notice_id, &custody);
        if !admitted { return Ok(false); }
        let result = (|| {
            let response = client.post(format!("{}/native/notices/ack", supervisor.bridge_url()))
                .header("x-native-token", supervisor.native_bridge_token())
                .json(&notice_presented_payload(&notice_id, &custody)).send()
                .map_err(|_| "notice_receipt_unconfirmed")?;
            if !response.status().is_success() { return Ok(false); }
            let payload = response.json::<serde_json::Value>().map_err(|_| "notice_receipt_unconfirmed")?;
            Ok(notice_presented_recorded(&notice_id, &payload))
        })();
        let mut issued = issued_notices().lock().map_err(|_| "notice_receipt_unavailable")?;
        if result == Ok(true) { issued.confirm_presented(&notice_id, &custody); }
        else {
            // Exact status writes are idempotent. Permit a later same-custody
            // DOM commit to retry, bounded independently of all device effects.
            issued.release_unconfirmed(&notice_id, &custody);
        }
        result
    }).await.map_err(|_| "notice_receipt_unconfirmed".to_string())?
        .map_err(str::to_owned)
}
fn emit_in_app_notice(app: &AppHandle, id: &str, title: &str, body: &str, target_path: Option<&str>, custody: &NoticeCustody) -> Option<bool> {
    with_notice_custody(app, custody, || {
        if !issued_notices().lock().is_ok_and(|mut issued| issued.issue(id, custody)) { return None; }
        let emitted = app.emit(
        "desktop-notice",
        DesktopNoticePayload {
            subject: custody.subject.clone(), device_id: custody.device_id.clone(), auth_epoch: custody.auth_epoch,
            id: id.to_string(),
            title: title.to_string(),
            body: body.to_string(),
            target_path: target_path.map(str::to_string),
        },
    )
        .is_ok();
        if !emitted {
            if let Ok(mut issued) = issued_notices().lock() { issued.revoke_unemitted(id, custody); }
        }
        Some(emitted)
    }).flatten()
}

#[cfg(target_os = "macos")]
fn deliver_owned_native_notice(app: &AppHandle, id: &str, title: &str, body: &str,
                               target: Option<url::Url>, custody: &NoticeCustody) -> Result<(), &'static str> {
    // This may wait for notification permission, but never holds the account
    // lease. Re-admit the original generation before any private content send.
    mac_notice::prepare_then_dispatch(mac_notice::macos::prepare_delivery, |prepared| {
        with_notice_custody(app, custody, ||
            mac_notice::macos::deliver_prepared(app, id, title, body, target, Some(custody.clone()), prepared))
            .unwrap_or(Err("account_unavailable"))
    })
}

#[cfg(not(target_os = "macos"))]
fn deliver_owned_native_notice(app: &AppHandle, id: &str, title: &str, body: &str,
                               target: Option<url::Url>, custody: &NoticeCustody) -> Result<(), &'static str> {
    with_notice_custody(app, custody, || deliver_native_notice(app, id, title, body, target))
        .unwrap_or(Err("account_unavailable"))
}

#[cfg(target_os = "macos")]
fn deliver_native_notice(
    app: &AppHandle,
    id: &str,
    title: &str,
    body: &str,
    target: Option<url::Url>,
) -> Result<(), &'static str> {
    mac_notice::macos::deliver(app, id, title, body, target)
}

#[cfg(not(target_os = "macos"))]
fn deliver_native_notice(
    app: &AppHandle,
    _id: &str,
    title: &str,
    body: &str,
    _target: Option<url::Url>,
) -> Result<(), &'static str> {
    use tauri_plugin_notification::NotificationExt;
    app.notification()
        .builder()
        .title(title)
        .body(body)
        .show()
        .map_err(|_| "error")?;
    Ok(())
}

#[tauri::command]
fn mac_notice_diagnostic() -> mac_notice::NativeNoticeDiagnostic {
    mac_notice::current_diagnostic()
}

#[tauri::command]
fn mac_notice_test(app: AppHandle) -> Result<mac_notice::NativeNoticeDiagnostic, String> {
    #[cfg(target_os = "macos")]
    {
        mac_notice::macos::deliver_test(&app)?;
        Ok(mac_notice::current_diagnostic())
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = app;
        Err("Mac notifications are only available on macOS.".into())
    }
}

fn poll_notices(app: AppHandle, supervisor: Arc<Supervisor>) {
    thread::spawn(move || {
        let mut first = true;
        loop {
            if !first {
                thread::sleep(Duration::from_secs(20));
            }
            first = false;
            if !supervisor.is_ready() {
                continue;
            }
            let Some(custody) = notice_custody(&app) else { continue; };
            let url = format!("{}/native/notices", supervisor.bridge_url());
            let client = match reqwest::blocking::Client::builder()
                .user_agent(crate::http::USER_AGENT)
                .timeout(Duration::from_secs(3))
                .redirect(reqwest::redirect::Policy::none()).no_proxy()
                .build()
            {
                Ok(c) => c,
                Err(_) => continue,
            };
            let Ok(res) = client
                .get(&url)
                .header("x-native-token", supervisor.native_bridge_token())
                .send()
            else {
                continue;
            };
            if !res.status().is_success() {
                continue;
            }
            let Ok(body) = res.json::<serde_json::Value>() else {
                continue;
            };
            if notice_custody(&app).as_ref() != Some(&custody)
                || body.get("recipient_subject").and_then(|v| v.as_str()) != Some(custody.subject.as_str()) { continue; }
            let Some(items) = body.get("notices").and_then(|v| v.as_array()) else {
                continue;
            };
            if items.is_empty() {
                continue;
            }
            let mut results: Vec<serde_json::Value> = Vec::new();
            for item in items {
                if notice_custody(&app).as_ref() != Some(&custody) { break; }
                let title = item
                    .get("title")
                    .and_then(|v| v.as_str())
                    .unwrap_or("Supymem");
                let body_txt = item.get("body").and_then(|v| v.as_str()).unwrap_or("");
                let Some(id) = item.get("id").and_then(|v| v.as_str()) else {
                    continue;
                };
                let already_in_app = item
                    .get("in_app_status")
                    .and_then(|v| v.as_str())
                    .is_some_and(|s| s == "in_app_shown");
                let already_native = item
                    .get("native_status")
                    .and_then(|v| v.as_str())
                    .is_some_and(|s| s == "native_shown" || s.starts_with("native_failed:"));
                if item.get("kind").and_then(|v| v.as_str()) == Some("suggestions") {
                    // The overlay is the delivery; a banner only when it cannot open. Held notices
                    // (off, paused, quiet hours, too soon) stay in the Suggestions list and badge.
                    let (in_app, native) = match suggestions::handle_notice(&app, &supervisor.console_url(), &custody) {
                        suggestions::Delivery::Shown => (Some("in_app_shown"), Some("native_shown".to_string())),
                        suggestions::Delivery::Held(why) => (Some("in_app_failed"), Some(format!("native_failed:{why}"))),
                        suggestions::Delivery::Failed => {
                            let sent = deliver_owned_native_notice(&app, id, title, body_txt, None, &custody);
                            (Some("in_app_failed"), mac_notice::ChannelStatus::from_result(sent).as_ack_token())
                        }
                    };
                    results.push(serde_json::json!({ "id": id, "in_app": in_app, "native": native }));
                    continue;
                }
                let target_path = item.get("target_path").and_then(|v| v.as_str());
                let target = target_path
                    .and_then(|path| trusted_notice_target(&supervisor.console_url(), path));

                let mut delivery = mac_notice::NoticeDelivery::default();
                if already_in_app {
                    delivery.record_in_app(true);
                } else {
                    delivery.record_emitted(emit_in_app_notice(
                        &app, id, title, body_txt, target_path, &custody,
                    ));
                }
                if already_native {
                    // Preserve prior terminal native status; do not re-send.
                    if let Some(status) = item.get("native_status").and_then(|v| v.as_str()) {
                        if let Some(reason) = status.strip_prefix("native_failed:") {
                            delivery.native =
                                mac_notice::ChannelStatus::Failed(reason.to_string());
                        } else {
                            delivery.native = mac_notice::ChannelStatus::Shown;
                        }
                    } else {
                        delivery.native = mac_notice::ChannelStatus::Shown;
                    }
                } else {
                    delivery.record_native(deliver_owned_native_notice(
                        &app, id, title, body_txt, target.clone(), &custody,
                    ));
                }
                let (in_app, native) = delivery.ack_tokens();
                results.push(serde_json::json!({
                    "id": id,
                    "in_app": in_app,
                    "native": native,
                }));
            }
            if !results.is_empty() && notice_custody(&app).as_ref() == Some(&custody) {
                let _ = client
                    .post(format!("{}/native/notices/ack", supervisor.bridge_url()))
                    .header("x-native-token", supervisor.native_bridge_token())
                    .json(&serde_json::json!({ "results": results, "recipient_subject": custody.subject }))
                    .send();
            }
        }
    });
}

fn initialize_data_dir(data_dir: PathBuf) {
    #[cfg(unix)]
    if local_stack::is_local() {
        use std::os::unix::fs::DirBuilderExt;
        let _ = std::fs::DirBuilder::new().recursive(true).mode(0o700).create(&data_dir);
    }
    std::env::set_var("COMPANY_OS_APP_VERSION", env!("SUPYMEM_DESKTOP_VERSION"));
    crash::install_panic_hook(data_dir.clone());
    shell_events::init(data_dir.clone());
    let _ = std::fs::create_dir_all(&data_dir);
    let _ = std::fs::create_dir_all(data_dir.join("ventures"));
    let _ = std::fs::create_dir_all(data_dir.join("backups"));
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    #[cfg(feature = "local-stack")]
    std::env::set_var("SUPYMEM_MODE", "local");
    if local_stack::is_local() { let _ = LOCAL_STARTED.set(std::time::Instant::now()); }
    env_logger::Builder::from_env(env_logger::Env::default()
        .default_filter_or(if local_stack::is_local() { "info" } else { "error" })).init();

    if !local_stack::is_local() { initialize_data_dir(conventional_data_dir()); }

    if std::env::var("COMPANY_OS_REPO_ROOT").is_err() {
        std::env::set_var("COMPANY_OS_REPO_ROOT", resolve_repo_root());
    }

    // This must be the first plugin. A second copy would otherwise start a
    // second bridge and contend for the same macOS Keychain credential.
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(nav_guard::plugin())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(update::Controller::default())
        .setup(|app| {
            let local_data_error = if local_stack::is_local() {
                let data_dir = conventional_data_dir();
                let resource_dir = tauri::Manager::path(app).resource_dir().unwrap_or_default();
                match local_stack::prepare_data_dir(&data_dir, &resource_dir) {
                    Ok(()) => { initialize_data_dir(data_dir); None },
                    Err(error) => Some(error),
                }
            } else { None };
            // A prior process may have exited before its native sign-out handoff.
            // Only a fresh verified session may own newly queued crash receipts.
            if local_data_error.is_none() {
                let _ = std::fs::remove_file(conventional_data_dir().join("crash_owner.json"));
                error_capture::begin_session(&conventional_data_dir());
            }
            #[cfg(desktop)]
            {
                let _ = app.handle().plugin(tauri_plugin_autostart::init(
                    tauri_plugin_autostart::MacosLauncher::LaunchAgent,
                    Some(vec![]),
                ));
            }

            let resource_dir = app
                .path()
                .resource_dir()
                .unwrap_or_else(|_| PathBuf::from("."));
            let runtime_bin = resolve_runtime_bin(&resource_dir.join("binaries"));
            let supervisor = Arc::new(Supervisor::new(
                runtime_bin,
                conventional_data_dir(),
                resource_dir.clone(),
            ));
            if cfg!(debug_assertions) || channel::local_review() || channel::staging() || local_stack::is_local() {
                let console: url::Url = supervisor.console_url().parse()?;
                if native_security::console_origin_allowed(&console,
                    cfg!(debug_assertions) || channel::local_review() || local_stack::is_local(), channel::staging()) {
                    let mut capability: serde_json::Value = serde_json::from_str(include_str!("../capabilities/default.json"))?;
                    capability["identifier"] = serde_json::json!("review-console");
                    capability["windows"] = serde_json::json!(["main"]);
                    capability["remote"]["urls"] = serde_json::json!([console.origin().ascii_serialization(), format!("{}/*", console.origin().ascii_serialization())]);
                    app.add_capability(capability.to_string())?;
                }
            }
            let native_auth = Arc::new(auth::SharedAuth::new(auth::NativeAuth::new(supervisor.gateway_url())));
            let auth_events=app.handle().clone();
            native_auth.set_listener(move |status| {
                suggestions::retire_for_auth(&auth_events);
                let _=auth_events.emit("supymem-native-auth",status);
            });
            let boot_auth = native_auth.clone();
            let pty_hub = Arc::new(PtyHub::new());
            let stdio_hub = Arc::new(StdioHub::new());
            let sup = supervisor.clone();
            let handle = app.handle().clone();

            if let Some(main) = app.get_webview_window("main") {
                if local_stack::is_local() {
                    let origin = if cfg!(target_os = "windows") { "http://tauri.localhost" } else { "tauri://localhost" };
                    main.navigate(format!("{origin}/local-starting.html").parse::<url::Url>()?)?;
                }
                bring_window_forward(&main);
            }

            // Check once on every production launch. This native check cannot
            // be suppressed by a stale hosted console, and failures merely log
            // a diagnostic: the founder is never blocked by an update check.
            if channel::updater_enabled() && !local_stack::is_local() {
                let update_app = app.handle().clone();
                tauri::async_runtime::spawn(async move {
                    tokio::time::sleep(Duration::from_secs(3)).await;
                    let updates = update_app.state::<update::Controller>();
                    match run_update_check(&update_app, &updates, false).await {
                        Ok(result) if result.available => {
                            log::info!("desktop_update phase=launch_update_available version={:?} force={}", result.version, result.force);
                            if let Err(error) = show_update_window(&update_app) {
                                log::warn!("desktop_update phase=launch_window_failed error={error}");
                            }
                        }
                        Ok(_) => log::info!("desktop_update phase=launch_current"),
                        Err(error) => log::warn!("desktop_update phase=launch_check_failed error={error}"),
                    }
                });
            }

            std::thread::spawn(move || {
                if local_stack::is_local() {
                    let started = std::time::Instant::now();
                    let result = if let Some(reason) = local_data_error {
                        let error = sup.local_error(&reason).unwrap_or(reason);
                        show_local_error(&handle, &sup);
                        Err(error)
                    } else { run_local_startup(&handle, sup) };
                    log::info!("desktop_startup phase=local_console_navigated ok={} ms={}", result.is_ok(), started.elapsed().as_millis());
                    return;
                }
                let t0 = std::time::Instant::now();
                let started = sup.start_all();
                log::info!("desktop_startup phase=supervisor_started ms={}", t0.elapsed().as_millis());
                match started {
                    Ok(()) => {
                        let gw = sup.gateway_url();
                        let con = sup.console_url();
                        let _ = handle.emit("backend-ready", &gw);
                        let connection = show_main_with(
                            &handle,
                            &con,
                            &gw,
                            &sup.bridge_url(),
                            &sup.bridge_token(),
                            sup.runtime_mode(),
                        );
                        log::info!("desktop_startup phase=console_connection_checked ms={}", t0.elapsed().as_millis());
                        if connection.connected && !finish_main_navigation(&handle, &con) {
                            show_splash_error(&handle, "console_navigation_failed");
                            if let Some(main) = handle.get_webview_window("main") {
                                let origin = if cfg!(target_os="windows") { "http://tauri.localhost" } else { "tauri://localhost" };
                                if let Ok(url) = format!("{origin}/offline.html").parse::<url::Url>() { let _ = main.navigate(url); }
                            }
                        }
                        log::info!("desktop_startup phase=console_navigated ms={}", t0.elapsed().as_millis());
                        #[cfg(target_os = "macos")]
                        start_runtime_heartbeat(sup.clone(), boot_auth.clone());
                        // A protected Keychain prompt must never hold the hosted
                        // console on its splash screen. Restore on an independent
                        // worker after scheduling the usable console window.
                        let restore_auth=boot_auth.clone();
                        std::thread::spawn(move || {
                            let started = std::time::Instant::now();
                            if let Ok(mut auth)=restore_auth.try_lock() { auth.reconnect(None); }
                            log::info!("desktop_startup phase=session_restored ms={}", started.elapsed().as_millis());
                        });
                        let auth_sup=sup.clone();
                        let navigation_finished = std::time::Instant::now();
                        std::thread::spawn(move || {
                            let bridge_ok = auth_sup.wait_bridge();
                            log::info!("desktop_startup phase=bridge_ready ok={} ms={}", bridge_ok.is_ok(), t0.elapsed().as_millis());
                            venture_link::ready(&handle);
                            let mut bridge_reset = false;
                            loop {
                                std::thread::sleep(Duration::from_secs(1));
                                if !auth_sup.is_ready() { break; }
                                // ponytail: older hosted consoles have no paint signal; allow helper sync after 10s, remove the fallback once they all report paint.
                                if !handle.try_state::<AppState>().is_some_and(|state| state.product_painted.load(std::sync::atomic::Ordering::SeqCst))
                                    && navigation_finished.elapsed() < Duration::from_secs(10) { continue; }
                                if auth_sup.wait_bridge().is_ok() {
                                    bridge_reset |= auth_sup.ensure_bridge().unwrap_or(false);
                                    if let Ok(mut auth)=boot_auth.try_lock() {
                                        if bridge_reset { auth.bridge_access = None; auth.set_bridge_connected(false); bridge_reset = false; }
                                        let access = auth.bridge_session().and_then(|body| body.get("access_token").and_then(serde_json::Value::as_str).map(str::to_owned));
                                        if access != auth.bridge_access || !auth.status().bridge_connected && access.is_some() {
                                            connect_auth_bridge(&mut auth,&auth_sup);
                                        }
                                    }
                                }
                            }
                        });
                    }
                    Err(err) => {
                        let _ = handle.emit("backend-error", &err);
                        show_splash_error(&handle, "supervisor_start_failed");
                        if channel::packaged_local_review() {
                            // A local review artifact must never turn a failed
                            // local stack into a production session.
                            return;
                        }
                        // Don't leave the founder on a stuck splash if a console is already up.
                        if supervisor::http_url_ok("http://127.0.0.1:3100/api/health")
                            || supervisor::http_url_ok("http://127.0.0.1:3100/")
                        {
                            show_main(&handle, "http://127.0.0.1:3100", "http://127.0.0.1:3200");
                        } else {
                            show_main_with(
                                &handle,
                                channel::console(),
                                channel::gateway(),
                                "http://127.0.0.1:3500",
                                "",
                                "hosted",
                            );
                        }
                        venture_link::ready(&handle);
                    }
                }
            });

            #[cfg(target_os = "macos")]
            {
                mac_notice::macos::warm(app.handle());
            }
            poll_notices(app.handle().clone(), supervisor.clone());

            app.manage(AppState {
                supervisor,
                product_painted: std::sync::atomic::AtomicBool::new(false),
                auth: native_auth,
                streams: Arc::new(events::Hub::default()),
                pty: pty_hub,
                stdio: stdio_hub,
                active_venture: Mutex::new(None),
                active_native_order: Mutex::new(None),
                exit_waiting: std::sync::atomic::AtomicBool::new(false),
                exit_quiescence: Mutex::new(None),
                #[cfg(target_os = "macos")]
                cua_host: Mutex::new(None),
            });
            drain_pending_auth_callbacks(app.handle());

            #[cfg(target_os = "macos")]
            {
                // Retain the platform's About/Edit/Window/Services/Quit menu and
                // add the former tray actions to the Supymem app menu.
                let menu = tauri::menu::Menu::default(app.handle())?;
                if let Some(tauri::menu::MenuItemKind::Submenu(application)) = menu.items()?.into_iter().next() {
                    use tauri::menu::{MenuItem, PredefinedMenuItem};
                    let h = app.handle();
                    application.insert(&MenuItem::with_id(h, "app-updates", "Check for updates…", true, Some("CmdOrCtrl+U"))?, 1)?;
                    let items = [
                        MenuItem::with_id(h, "show", "Show window", true, None::<&str>)?,
                        MenuItem::with_id(h, "new_venture", "New venture", true, None::<&str>)?,
                        MenuItem::with_id(h, "setup", "Set up this Mac", true, None::<&str>)?,
                        MenuItem::with_id(h, "pause", "Pause the current venture", true, None::<&str>)?,
                        MenuItem::with_id(h, "resume", "Resume the current venture", true, None::<&str>)?,
                        MenuItem::with_id(h, "reveal", "Reveal data folder", true, None::<&str>)?,
                    ];
                    application.insert(&PredefinedMenuItem::separator(h)?, 2)?;
                    for (i, item) in items.iter().enumerate() {
                        application.insert(item, 3 + i)?;
                    }
                    application.insert(&PredefinedMenuItem::separator(h)?, 3 + items.len())?;
                    if local_stack::is_local() {
                        application.insert(&MenuItem::with_id(h, "charlie_visibility", "Show or hide Charlie", true, None::<&str>)?, 3 + items.len())?;
                    }
                }
                if let Some(tauri::menu::MenuItemKind::Submenu(help)) = menu.get(tauri::menu::HELP_SUBMENU_ID) {
                    use tauri::menu::MenuItem;
                    let h = app.handle();
                    help.append(&MenuItem::with_id(h, "help", "Supymem help", true, None::<&str>)?)?;
                    help.append(&MenuItem::with_id(h, "report", if local_stack::is_local() { "Download support bundle" }
                        else { "Report a problem" }, true, None::<&str>)?)?;
                    help.append(&MenuItem::with_id(h, "cursor-diagnostic", "Save cursor activity receipt", true, None::<&str>)?)?;
                    if tools::BETA_COMPUTER_USE {
                        help.append(&MenuItem::with_id(h, "permissions", "Set up macOS permissions", true, None::<&str>)?)?;
                    }
                    help.set_as_help_menu_for_nsapp()?;
                }
                app.set_menu(menu)?;
                app.on_menu_event(|app, event| handle_menu_action(app, event.id.as_ref()));
            }

            #[cfg(target_os = "macos")]
            if local_stack::is_local() {
                if let Err(error) = mascot::install(app.handle()) {
                    log::warn!("local mascot installation failed: {error}");
                }
            }

            #[cfg(target_os = "macos")]
            if let Err(error) = agent_cursor::install(app.handle(), &conventional_data_dir()) {
                log::error!("agent cursor overlay failed: {error}");
            }

            #[cfg(target_os = "macos")]
            if let Err(error) = agent_stop_pill::install(app.handle()) {
                log::error!("agent cursor stop pill failed: {error}");
            }
            emergency_stop::install(app.handle());

            #[cfg(all(desktop, not(target_os = "macos")))]
            {
                use tauri::{
                    image::Image,
                    menu::{Menu, MenuItem, PredefinedMenuItem},
                    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
                };
                let title = MenuItem::with_id(app, "about", "Supymem", false, None::<&str>)?;
                let show = MenuItem::with_id(app, "show", "Show window", true, None::<&str>)?;
                let new_venture =
                    MenuItem::with_id(app, "new_venture", "New venture", true, None::<&str>)?;
                let setup = MenuItem::with_id(app, "setup", "Set up this Mac", true, None::<&str>)?;
                let pause = MenuItem::with_id(app, "pause", "Pause the current venture", true, None::<&str>)?;
                let resume =
                    MenuItem::with_id(app, "resume", "Resume the current venture", true, None::<&str>)?;
                let updates = MenuItem::with_id(
                    app,
                    "updates",
                    "Check for updates",
                    true,
                    None::<&str>,
                )?;
                let reveal =
                    MenuItem::with_id(app, "reveal", "Reveal data folder", true, None::<&str>)?;
                let support = MenuItem::with_id(app, "report", "Download support bundle", true, None::<&str>)?;
                let local = local_stack::is_local();
                let quit = MenuItem::with_id(app, "quit", "Quit Supymem", true, None::<&str>)?;
                let sep1 = PredefinedMenuItem::separator(app)?;
                let sep2 = PredefinedMenuItem::separator(app)?;
                let sep3 = PredefinedMenuItem::separator(app)?;
                let menu = Menu::with_items(
                    app,
                    &[
                        &title,
                        &show,
                        &new_venture,
                        &setup,
                        &sep1,
                        &pause,
                        &resume,
                        &sep2,
                        &updates,
                        &support,
                        &reveal,
                        &sep3,
                        &quit,
                    ],
                )?;
                if !local {
                    menu.remove(&support)?;
                }
                let tray_icon = Image::new(
                    include_bytes!("../icons/tray-template@2x.rgba"),
                    44,
                    44,
                );
                let _tray = TrayIconBuilder::new()
                    .icon(tray_icon)
                    .icon_as_template(true)
                    .menu(&menu)
                    .tooltip("Supymem")
                    .on_menu_event(|app, event| handle_menu_action(app, event.id.as_ref()))
                    .on_tray_icon_event(|tray, event| {
                        if let TrayIconEvent::Click {
                            button: MouseButton::Left,
                            button_state: MouseButtonState::Up,
                            ..
                        } = event
                        {
                            let app = tray.app_handle();
                            shell_events::record("activate", "tray_show", None);
                            bring_main_forward(app);
                        }
                    })
                    .build(app)?;
            }

            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() == "main" {
                match event {
                    // Coming back from full screen / minimize / another Space.
                    tauri::WindowEvent::Focused(true) => {
                        if let Some(w) = window.app_handle().get_webview_window("main") { nudge_repaint(&w); }
                    }
                    tauri::WindowEvent::Resized(_) => {
                        if let (Ok(size), Ok(scale)) = (window.inner_size(), window.scale_factor()) {
                            if size.width as f64 / scale <= 600.0 { suggestions::hide(window.app_handle()); }
                        }
                        // Full-screen transitions resize; repaint only when that flag flips.
                        let fs = window.is_fullscreen().unwrap_or(false);
                        if MAIN_FULLSCREEN.swap(fs, std::sync::atomic::Ordering::Relaxed) != fs {
                            if let Some(w) = window.app_handle().get_webview_window("main") { nudge_repaint(&w); }
                        }
                    }
                    _ => {}
                }
            }
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "updates" {
                    if let Some(updates) = window.app_handle().try_state::<update::Controller>() {
                        if updates.prevent_close() { api.prevent_close(); }
                    }
                }
                if window.label() == "main" {
                    api.prevent_close();
                    let native_run_active = window
                        .app_handle()
                        .try_state::<AppState>()
                        .is_some_and(|state| native_run_is_active(&state));
                    if close_requested_should_hide_main(native_run_active) {
                        // Hiding a full-screen window strands its Space as an
                        // empty black screen. Leave full screen first, then hide.
                        if window.is_fullscreen().unwrap_or(false) {
                            let _ = window.set_fullscreen(false);
                            let w = window.clone();
                            thread::spawn(move || {
                                thread::sleep(Duration::from_millis(1000));
                                let h = w.clone();
                                let _ = w.run_on_main_thread(move || { let _ = h.hide(); });
                            });
                        } else {
                            let _ = window.hide();
                        }
                    } else {
                        // A reviewed Mac task is still running: keep the review
                        // surface on screen instead of close-to-tray.
                        let _ = window.unminimize();
                        let _ = window.show();
                    }
                }
                if window.label() == "setup" {
                    let _ = tools::mark_setup_seen();
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            charlie_mascot_state,
            dictation::dictation_available,
            dictation::dictation_start,
            dictation::dictation_stop,
            desktop_retry_console,
            desktop_auth_status,
            desktop_product_painted,
            webview_diagnostics::append_webview_error,
            desktop_notice_custody,
            desktop_notice_presented,
            desktop_auth_start,
            desktop_auth_reconnect,
            desktop_auth_sign_out,
            desktop_auth_complete_code,
            desktop_api_request,
            suggestions::suggestions_api_request,
            suggestions::suggestions_get_prefs,
            suggestions::suggestions_set_prefs,
            suggestions::suggestions_pause_today,
            suggestions::suggestions_resume,
            suggestions::suggestions_overlay_close,
            suggestions::suggestions_overlay_resize,
            suggestions::suggestions_overlay_custody,
            suggestions::suggestions_overlay_ready,
            suggestions::suggestions_overlay_request,
            suggestions::suggestions_open_venture,
            device_work_order_capabilities,
            inspect_device_work_order,
            desktop_app_inventory,
            permissions::permissions_status,
            permissions::permissions_request,
            permissions::permissions_open_settings,
            desktop_permission_status,
            desktop_permission_targets,
            desktop_permission_request,
            desktop_launch_installed_app,
            device_work_order_action,
            emergency_stop::emergency_stop_computer_use,
            emergency_stop::computer_use_status,
            desktop_http_request,
            desktop_events_subscribe,
            desktop_events_unsubscribe,
            backend_ready,
            gateway_url,
            console_url,
            backend_status,
            reveal_in_file_manager,
            open_composio_connect_link,
            store_passphrase,
            clear_passphrase,
            notify,
            mac_notice_diagnostic,
            mac_notice_test,
            data_dir_path,
            venture_workspace_path,
            set_active_venture,
            is_desktop,
            spawn_agent_session,
            spawn_coding_control,
            start_cursor_ide_session,
            read_venture_landing,
            open_coding_ide,
            list_local_clis,
            list_local_tools,
            connect_local_tool,
            macos_control_status,
            open_macos_privacy_pane,
            show_permission_coach,
            hide_permission_coach,
            confirm_macos_permission,
            relaunch_app,
            mark_mac_setup_seen,
            docker_status,
            start_docker,
            install_docker,
            open_setup_window,
            close_setup_window,
            install_local_builder,
            uninstall_local_builder,
            write_hermes_config,
            write_hermes_files,
            stdio_write,
            kill_coding_control,
            pty_write,
            pty_resize,
            kill_agent_session,
            list_agent_sessions,
            autostart_enabled,
            set_autostart,
            app_version,
            agent_cursor_prefs,
            agent_cursor_diagnostic_status,
            agent_cursor_rendered,
            set_agent_cursor_prefs,
            drain_native_events,
            check_for_update,
            request_update_check,
            defer_update,
            install_update,
        ])
        .build(tauri::generate_context!())
        .expect("error while running Supymem desktop")
        .run(|app, event| {
            // Dock/Cmd-Q/application Quit follows this path.
            // Tauri state may be retained by worker threads, so Drop alone does
            // not reliably stop the onefile sidecar before process exit.
            if matches!(event, tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit) {
                if let Some(state) = app.try_state::<AppState>() {
                    if let tauri::RunEvent::ExitRequested { ref api, code, .. } = event {
                        if code != Some(tauri::RESTART_EXIT_CODE) && state.exit_quiescence.lock().unwrap().is_none() {
                            api.prevent_exit();
                            if !state.exit_waiting.swap(true, std::sync::atomic::Ordering::SeqCst) {
                                let app = app.clone();
                                let auth = state.auth.clone();
                                tauri::async_runtime::spawn(async move {
                                    let result = auth.quiesce().await;
                                    if let Some(state) = app.try_state::<AppState>() {
                                        match result {
                                            Ok(lease) => *state.exit_quiescence.lock().unwrap() = Some(Some(lease)),
                                            Err(error) => {
                                                // refresh_in_flight is persisted before sending, so relaunch recovers.
                                                log::error!("Account quiesce before quit failed: {error}");
                                                *state.exit_quiescence.lock().unwrap() = Some(None);
                                            }
                                        }
                                        app.exit(code.unwrap_or(0));
                                    }
                                });
                            }
                            return;
                        }
                    }
                    if let Err(error) = state.stdio.kill_all() {
                        log::error!("Local process shutdown failed: {error}");
                        if let tauri::RunEvent::ExitRequested { ref api, .. } = event {
                            api.prevent_exit();
                            state.exit_quiescence.lock().unwrap().take();
                            state.exit_waiting.store(false, std::sync::atomic::Ordering::SeqCst);
                            return;
                        }
                    }
                    state.pty.kill_all();
                    state.streams.stop_all();
                    state.supervisor.stop_all();
                    #[cfg(target_os = "macos")]
                    dictation::shutdown();
                    if matches!(event, tauri::RunEvent::Exit) { error_capture::end_session(&conventional_data_dir()); }
                    #[cfg(target_os = "macos")]
                    if let Ok(mut host) = state.cua_host.lock() { host.take(); }
                }
            }
            #[cfg(any(target_os = "macos", target_os = "ios"))]
            if let tauri::RunEvent::Opened { ref urls } = event {
                for url in urls {
                    if url.scheme() != channel::callback_scheme() { continue; }
                    if url.host_str() == Some("open") {
                        venture_link::open(app, url);
                        continue;
                    }
                    if app.try_state::<AppState>().is_some() {
                        handle_auth_callback(app, url.to_string());
                    } else if let Ok(mut pending) = PENDING_AUTH_CALLBACKS.lock() {
                        // Cold launch from supymem://: macOS delivers the URL before setup()
                        // has managed AppState. Dropping it made sign-in return inconclusive.
                        pending.push(url.to_string());
                    }
                }
            }
            // Dock / Cmd-Tab of an already-running app. A hidden or minimized
            // window does not come back unless we unminimize + show + focus.
            #[cfg(target_os = "macos")]
            if let tauri::RunEvent::Reopen { .. } = event {
                bring_main_forward(app);
            }
        });
}

/// Auth callbacks that arrived before AppState existed (cold launch); drained at the end of setup().
static PENDING_AUTH_CALLBACKS: Mutex<Vec<String>> = Mutex::new(Vec::new());

fn drain_pending_auth_callbacks(app: &AppHandle) {
    let urls = PENDING_AUTH_CALLBACKS.lock().map(|mut p| std::mem::take(&mut *p)).unwrap_or_default();
    for url in urls { handle_auth_callback(app, url); }
}

fn handle_auth_callback(app: &AppHandle, callback: String) {
    let Some(state) = app.try_state::<AppState>() else { return };
    let shared = state.auth.clone(); let sup = state.supervisor.clone();
    let handle = app.clone();
    std::thread::spawn(move || {
        if let Ok(mut auth)=shared.try_lock() {
            let started = std::time::Instant::now();
            let result=auth.callback(&callback);
            sync_auth_bridge(&mut auth,&sup);
            log::info!("desktop_startup phase=device_approval_verified ok={} ms={}", result.is_ok(), started.elapsed().as_millis());
            let mut status=auth.status();
            if result.is_err() && status.error.is_none() { status.error=Some("Authorization callback was rejected. Start a new connection from this app.".into()); }
            drop(auth);
            let _=handle.emit("supymem-native-auth",status);
        } else {
            let mut status=shared.status();
            status.error=Some(if status.state=="secure_store_pending" {
                "Authorization callback could not be accepted while secure access is pending. Finish the macOS prompt, then restart browser sign-in."
            } else {
                "Authorization callback could not be accepted while another account request is finishing. Wait for it to complete, then restart browser sign-in."
            }.into());
            let _=handle.emit("supymem-native-auth",status);
        }
        bring_main_forward(&handle);
    });
}

#[cfg(test)]
mod tests {
    #[test]
    fn local_build_requests_route_to_core_and_bridge_with_distinct_credentials() {
        use super::{device_order, local_build_http_request, Supervisor};
        use reqwest::Method;

        let root = std::env::temp_dir().join(format!("build-route-{}", uuid::Uuid::new_v4()));
        let supervisor = Supervisor::new(root.join("python3"), root.join("data"), root.join("resources"));
        let client = reqwest::blocking::Client::new();
        let owner = "test-owner-secret";
        for venture in ["warehouse_inventory", "weekend_cycling"] {
            let review = device_order::review_path(venture, "dwo_review").unwrap();
            let core = local_build_http_request(&client, &supervisor, Method::GET, &review, owner).build().unwrap();
            assert_eq!(core.url().as_str(), format!("{}/ventures/{venture}/device-work-orders/dwo_review", supervisor.core_url()));
            assert!(core.headers()["x-local-proxy-secret"] == owner);
            assert!(!core.headers().contains_key("x-native-token"));

            let path = format!("/native/device-work-orders/{venture}/dwo_review/status");
            let native = local_build_http_request(&client, &supervisor, Method::GET, &path, owner).build().unwrap();
            assert_eq!(native.url().as_str(), format!("{}{path}", supervisor.bridge_url()));
            assert!(native.headers()["x-native-token"] == supervisor.native_bridge_token().as_str());
            assert!(!native.headers().contains_key("x-local-proxy-secret"));
            assert!(!native.headers().contains_key("origin"));
        }
    }

    #[test]
    fn charlie_mascot_validates_state_and_voice_level() {
        use super::{charlie_voice_level, CharlieMascotState};
        for (name, state) in [("idle", CharlieMascotState::Idle), ("listening", CharlieMascotState::Listening), ("speaking", CharlieMascotState::Speaking)] {
            assert_eq!(serde_json::from_value::<CharlieMascotState>(serde_json::json!(name)).unwrap(), state);
            assert_eq!(state.name(), name);
        }
        for value in [serde_json::json!("unknown"), serde_json::json!("Idle"), serde_json::json!(null), serde_json::json!(42)] {
            assert!(serde_json::from_value::<CharlieMascotState>(value).is_err());
        }
        assert_eq!(charlie_voice_level(None).unwrap(), None);
        assert_eq!(charlie_voice_level(Some(-1.0)).unwrap(), Some(0.0));
        assert_eq!(charlie_voice_level(Some(2.0)).unwrap(), Some(1.0));
        assert_eq!(charlie_voice_level(Some(0.5)).unwrap(), Some(0.5));
        for value in [f32::NAN, f32::INFINITY, f32::NEG_INFINITY] {
            assert!(charlie_voice_level(Some(value)).is_err());
        }
    }

    #[test]
    fn verified_session_and_console_requests_do_not_wait_for_bridge_sync() {
        let source = include_str!("lib.rs");
        let sync = source.split("fn sync_auth_bridge(").nth(1).unwrap()
            .split("fn connect_auth_bridge(").next().unwrap();
        assert!(sync.contains("if auth.status().state != \"connected\""));
        let startup = source.split("let restore_auth=boot_auth.clone();").nth(1).unwrap()
            .split("let auth_sup=sup.clone();").next().unwrap();
        assert!(startup.contains("auth.reconnect(None)"));
        assert!(!startup.contains("wait_bridge"));
        assert!(!startup.contains("sync_auth_bridge"));
        let device_start = source.split("if action == \"start\" {").nth(1).unwrap()
            .split("#[cfg(target_os = \"macos\")]").next().unwrap();
        assert!(device_start.contains("connect_auth_bridge(&mut auth, &sup)"));
        let local_startup = include_str!("supervisor.rs").split("pub fn start_all(&self)").nth(1).unwrap()
            .split("/// Block until the hosted bridge").next().unwrap();
        assert!(!local_startup.contains("spawn_role(\"bridge\""));
        let background = source.split("let mut bridge_reset = false;").nth(1).unwrap()
            .split("Err(err) => {").next().unwrap();
        assert!(background.find("state.product_painted.load").unwrap() < background.find("boot_auth.try_lock()").unwrap());
    }
    fn notice_owner(epoch: u64) -> super::NoticeCustody {
        super::NoticeCustody {subject: "usr_a".into(), device_id: "device_a".into(), auth_epoch: epoch}
    }
    #[test]
    fn notice_presented_receipt_requires_exact_issued_pending_id_and_owner() {
        let mut issued = super::IssuedNotices::default(); let owner = notice_owner(7);
        assert!(issued.issue("not_a", &owner));
        assert!(!issued.claim_presented("not_unissued", &owner));
        for wrong in [super::NoticeCustody {subject: "usr_b".into(), ..owner.clone()},
                      super::NoticeCustody {device_id: "device_b".into(), ..owner.clone()}, notice_owner(9)] {
            assert!(!issued.claim_presented("not_a", &wrong));
        }
        assert!(issued.claim_presented("not_a", &owner));
        assert!(!issued.claim_presented("not_a", &owner));
        assert!(!issued.issue("not_a", &owner), "an in-flight receipt cannot issue another effect");
        issued.confirm_presented("not_a", &owner);
        assert!(!issued.issue("not_a", &owner), "confirmed presentation is deduplicated");
    }
    #[test]
    fn notice_failed_emit_and_retired_generations_cannot_ack_a_banner() {
        let mut issued = super::IssuedNotices::default(); let old = notice_owner(7);
        assert!(issued.issue("not_failed", &old)); issued.revoke_unemitted("not_failed", &old);
        assert!(!issued.claim_presented("not_failed", &old));
        assert!(issued.issue("not_a", &old));
        let current = notice_owner(9); assert!(issued.issue("not_a", &current));
        assert!(!issued.claim_presented("not_a", &old));
        assert!(issued.claim_presented("not_a", &current));
    }
    #[test]
    fn notice_issued_ledger_is_bounded_and_capacity_refuses_without_widening() {
        let mut issued = super::IssuedNotices::default(); let owner = notice_owner(7);
        assert!(!issued.issue("", &owner)); assert!(!issued.issue(&"x".repeat(101), &owner));
        for i in 0..super::MAX_ISSUED_NOTICES { assert!(issued.issue(&format!("not_{i}"), &owner)); }
        assert!(!issued.issue("not_overflow", &owner));
        assert!(!issued.claim_presented("not_overflow", &owner));
        assert_eq!(issued.records.len(), super::MAX_ISSUED_NOTICES);
        assert!(issued.claim_presented("not_0", &owner));
    }
    #[test]
    fn notice_presented_ack_carries_only_exact_in_app_channel_and_requires_recorded_readback() {
        let payload = super::notice_presented_payload("not_a", &notice_owner(7));
        assert_eq!(payload, serde_json::json!({"recipient_subject":"usr_a",
            "results":[{"id":"not_a","in_app":"in_app_shown"}]}));
        for unconfirmed in [serde_json::json!({}), serde_json::json!({"acked":1}),
            serde_json::json!({"statuses":[{"id":"not_b","in_app_status":"in_app_shown"}]}),
            serde_json::json!({"statuses":[{"id":"not_a","native_status":"native_shown"}]})] {
            assert!(!super::notice_presented_recorded("not_a", &unconfirmed));
        }
        assert!(super::notice_presented_recorded("not_a", &serde_json::json!({
            "statuses":[{"id":"not_a","in_app_status":"in_app_shown","native_status":null}]})));
    }
    #[test]
    fn notice_event_acceptance_keeps_banner_pending_and_native_channel_independent() {
        let mut delivery = super::mac_notice::NoticeDelivery::default(); delivery.record_emitted(Some(true));
        assert_eq!(delivery.ack_tokens(), (None, None)); assert!(!delivery.settled());
        delivery.record_native(Ok(()));
        assert_eq!(delivery.ack_tokens(), (None, Some("native_shown".into())));
        delivery.record_emitted(None);
        assert_eq!(delivery.ack_tokens(), (None, Some("native_shown".into())));
        delivery.record_emitted(Some(false));
        assert_eq!(delivery.ack_tokens(), (Some("in_app_failed"), Some("native_shown".into())));
    }
    #[test]
    fn notice_successes_above_capacity_reclaim_only_confirmed_receipts() {
        let mut issued = super::IssuedNotices::default(); let owner = notice_owner(7);
        // An uncertain attempt survives capacity churn and cannot be replayed.
        assert!(issued.issue("not_uncertain", &owner));
        assert!(issued.claim_presented("not_uncertain", &owner));
        for i in 0..super::MAX_ISSUED_NOTICES * 2 {
            let id = format!("not_success_{i}"); assert!(issued.issue(&id, &owner));
            assert!(issued.claim_presented(&id, &owner)); issued.confirm_presented(&id, &owner);
        }
        assert_eq!(issued.records.len(), super::MAX_ISSUED_NOTICES);
        assert!(!issued.issue("not_uncertain", &owner));
        assert!(!issued.claim_presented("not_uncertain", &owner));
    }
    #[test]
    fn notice_capacity_cannot_evict_uncertain_attempts_or_unpresented_events() {
        let mut issued = super::IssuedNotices::default(); let owner = notice_owner(7);
        for i in 0..super::MAX_ISSUED_NOTICES {
            let id = format!("not_{i}"); assert!(issued.issue(&id, &owner));
            if i % 2 == 0 { assert!(issued.claim_presented(&id, &owner)); }
        }
        assert!(!issued.issue("not_overflow", &owner));
        assert!(issued.claim_presented("not_1", &owner));
        assert!(!issued.claim_presented("not_0", &owner));
    }
    #[test]
    fn notice_transport_refusal_and_unconfirmed_receipts_allow_only_bounded_idempotent_retry() {
        let mut issued = super::IssuedNotices::default(); let owner = notice_owner(7);
        assert!(issued.issue("not_a", &owner));
        // Not-ready/client-build failures happen before claim and consume none.
        assert_eq!(issued.records[0].attempts, 0); assert!(issued.issue("not_a", &owner));
        for _ in 0..super::MAX_NOTICE_RECEIPT_ATTEMPTS {
            assert!(issued.claim_presented("not_a", &owner));
            assert!(!issued.claim_presented("not_a", &owner), "no concurrent duplicate dispatch");
            issued.release_unconfirmed("not_a", &owner);
        }
        assert!(!issued.claim_presented("not_a", &owner));
        assert!(!issued.issue("not_a", &owner));
        assert!(!issued.claim_presented("not_a", &notice_owner(9)));
        assert_eq!(issued.records[0].attempts, super::MAX_NOTICE_RECEIPT_ATTEMPTS);
    }
    #[test]
    fn notice_commands_are_in_the_packaged_app_permission_allowlist() {
        let acl = include_str!("../permissions/app.toml");
        let allow = acl.split("commands.allow = [").nth(1).unwrap().split(']').next().unwrap();
        for command in ["desktop_notice_custody", "desktop_notice_presented"] {
            assert!(allow.contains(&format!("\"{command}\"")), "native notice command must be invocable: {command}");
        }
    }
    #[test]
    fn native_wire_capability_scope_requires_trusted_main_account_origin() {
        let trusted = url::Url::parse("https://app.example.invalid/v/ven_one/builds").unwrap();
        assert!(crate::auth::trusted_account_window("main", &trusted, "https://app.example.invalid"));
        for label in ["setup", "suggestions-overlay", "agent-cursor", "foreign"] {
            assert!(!crate::auth::trusted_account_window(label, &trusted, "https://app.example.invalid"));
        }
        let foreign = url::Url::parse("https://outside.test/v/ven_one/builds").unwrap();
        assert!(!crate::auth::trusted_account_window("main", &foreign, "https://app.example.invalid"));
    }
    #[test]
    fn tray_pause_message_reports_result() {
        use serde_json::json;
        assert!(super::tray_pause_message(true, 200, &json!({})).starts_with("Paused"));
        assert!(super::tray_pause_message(false, 200, &json!({})).starts_with("Resumed"));
        let taken = json!({ "detail": { "error": { "message": "This venture is already paused." } } });
        assert_eq!(super::tray_pause_message(true, 409, &taken), "This venture is already paused.");
        assert!(super::tray_pause_message(true, 401, &json!(null)).contains("signed in"));
    }

    use super::{
        close_requested_should_hide_main, native_action_reaps_cua_host,
        native_execution_is_terminal, native_order_auth_lock_error,
        native_order_error,
        native_watch_terminal, plan_error_message, should_focus_on_finish, should_restore_window,
        trusted_notice_target,
    };
    use std::time::Duration;

    #[test]
    fn repaint_is_queued_from_a_worker_not_a_native_window_callback() {
        let source = include_str!("lib.rs");
        let repaint = source.split("fn nudge_repaint(").nth(1).unwrap()
            .split("fn keep_window_visible(").next().unwrap();
        assert!(!repaint.contains("once(window)"));
        assert!(repaint.find("thread::spawn(move ||").unwrap()
            < repaint.find("w.run_on_main_thread(move || once(&again))").unwrap());
        assert!(repaint.contains("for ms in [0u64, 350, 900]"));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn runtime_heartbeat_payload_has_process_session_and_fresh_event() {
        let body = super::runtime_heartbeat_body(
            "8793811c-d724-46ca-b3e4-6b5d06b8be80",
            "0.1.48",
        );
        assert_eq!(body["session_id"], "8793811c-d724-46ca-b3e4-6b5d06b8be80");
        assert_eq!(body["app_version"], "0.1.48");
        assert!(uuid::Uuid::parse_str(body["event_id"].as_str().unwrap()).is_ok());
    }

    #[test]
    fn close_to_tray_is_deferred_while_a_mac_task_runs() {
        assert!(close_requested_should_hide_main(false));
        assert!(!close_requested_should_hide_main(true));
    }

    #[test]
    fn restore_only_when_hidden_or_minimized() {
        assert!(!should_restore_window(true, false));
        assert!(should_restore_window(false, false));
        assert!(should_restore_window(true, true));
        assert!(should_restore_window(false, true));
    }

    #[test]
    fn only_app_navigation_takes_focus_when_a_run_finishes() {
        assert!(should_focus_on_finish(true));
        assert!(!should_focus_on_finish(false));
    }

    #[test]
    fn native_execution_terminal_states_clear_visibility_guard() {
        for state in [
            "stopped",
            "not_started",
            "reconciliation_required",
            "interrupted_reconciliation_required",
        ] {
            assert!(native_execution_is_terminal(state), "{state}");
        }
        for state in ["starting", "claiming", "opening_browser", "observing", "acting"] {
            assert!(!native_execution_is_terminal(state), "{state}");
        }
    }

    #[test]
    fn native_watch_does_not_reap_a_new_cua_host_on_a_stale_not_started_receipt() {
        assert!(!native_watch_terminal("not_started", Duration::from_secs(0)));
        assert!(!native_watch_terminal("not_started", Duration::from_secs(19)));
        assert!(native_watch_terminal("not_started", Duration::from_secs(20)));
        assert!(native_watch_terminal("stopped", Duration::from_secs(0)));
    }

    #[test]
    fn status_poll_cannot_reap_host_during_startup_but_terminal_actions_can() {
        // A new Start and the page's immediate status poll may overlap. The
        // bridge can still return the preceding not_started receipt.
        assert!(native_execution_is_terminal("not_started"));
        assert!(!native_action_reaps_cua_host("status", "not_started"));
        assert!(!native_action_reaps_cua_host("status", "stopped"));
        assert!(!native_watch_terminal("not_started", Duration::from_secs(0)));
        assert!(!native_watch_terminal("not_started", Duration::from_secs(19)));
        // The watcher eventually closes a failed start. Explicit terminal
        // actions still release the host without waiting for that poll.
        assert!(native_watch_terminal("not_started", Duration::from_secs(20)));
        assert!(native_watch_terminal("stopped", Duration::ZERO));
        assert!(native_action_reaps_cua_host("cancel", "stopped"));
        assert!(native_action_reaps_cua_host("reconcile", "not_started"));
        assert!(!native_action_reaps_cua_host("cancel", "claiming"));
    }

    #[test]
    fn plan_error_extracts_fastapi_not_on_path() {
        let body = r#"{"detail":{"error":{"code":"not_on_path","message":"codex is not on PATH"}}}"#;
        assert_eq!(plan_error_message(404, body), "codex is not on PATH");
    }

    #[test]
    fn plan_error_falls_back_to_status() {
        assert_eq!(plan_error_message(500, ""), "session plan failed: 500");
    }

    #[test]
    fn hosted_and_packaged_review_legacy_sessions_refuse_before_planning_effects() {
        for (mode, review) in [("hosted", false), ("hosted", true), ("local", true)] {
            let mut planned = false;
            let result = super::admitted_legacy_session(mode, review, || {
                planned = true;
                Ok(())
            });
            assert!(!planned, "legacy planning ran in {mode}/review={review}");
            assert!(result.unwrap_err().contains("approved device work order"));
        }
    }

    #[test]
    fn developer_local_session_still_reaches_existing_provider_consent_planning() {
        let mut attempts = 0;
        assert_eq!(super::admitted_legacy_session("local", false, || {
            attempts += 1;
            Err::<(), _>("consent_required".into())
        }).unwrap_err(), "consent_required");
        assert_eq!(attempts, 1);
    }

    #[test]
    fn native_order_error_preserves_only_stable_codes() {
        assert_eq!(native_order_error(409, br#"{"detail":"native_journal_unavailable"}"#),
                   "native_journal_unavailable");
        assert_eq!(native_order_error(401, br#"{"detail":{"code":"device_authentication_required"}}"#),
                   "device_authentication_required");
        assert_eq!(native_order_error(409, br#"{"detail":{"error":{"code":"native_executor_busy_or_unreconciled"}}}"#),
                   "native_executor_busy_or_unreconciled");
        assert_eq!(native_order_error(409, br#"{"detail":{"error":{"code":"desktop_cua_host_invalid"}}}"#),
                   "desktop_cua_host_invalid");
        assert_eq!(native_order_error(401, br#"{"detail":{"error":{"code":"verified_account_required"}}}"#),
                   "verified_account_required");
        assert_eq!(native_order_error(503, br#"{"detail":{"error":{"code":"cu_reconcile_unavailable"}}}"#),
                   "cu_reconcile_unavailable");
        assert_eq!(native_order_error(400, br#"{"detail":"Bearer secret"}"#),
                   "native_order_request_rejected");
    }

    #[test]
    fn native_order_error_classifies_unstructured_bridge_rejection_without_leaking_body() {
        let secret = br#"Bearer secret from upstream"#;
        for (status, code) in [
            (401, "native_order_bridge_auth_rejected"),
            (404, "native_order_bridge_route_unavailable"),
            (405, "native_order_bridge_route_unavailable"),
            (409, "native_order_bridge_conflict_unclassified"),
            (422, "native_order_bridge_request_invalid"),
            (429, "native_order_bridge_rate_limited"),
            (500, "native_order_bridge_server_error"),
            (503, "native_order_bridge_server_error"),
        ] {
            assert_eq!(native_order_error(status, secret), code);
            assert_eq!(native_order_error(status, br#"{"detail":"Bearer secret"}"#), code);
        }
    }

    #[test]
    fn native_order_auth_lock_failure_distinguishes_before_and_after_post() {
        assert_eq!(native_order_auth_lock_error(false), "native_order_auth_unavailable_before_send");
        assert_eq!(native_order_auth_lock_error(true), "native_order_result_unconfirmed");
    }

    #[test]
    fn notice_targets_are_same_origin_and_route_bounded() {
        let good = trusted_notice_target("https://app.example.invalid", "/v/ven_abc-123/analytics").unwrap();
        assert_eq!(good.as_str(), "https://app.example.invalid/v/ven_abc-123/analytics");
        for path in [
            "https://evil.test/v/ven_1/now", "/v/ven_1/../settings", "/v/ven_1/builds",
            "/v/ven_1/now?next=https://evil.test", "/v/ven_1#other", "/admin/overview",
        ] {
            assert!(trusted_notice_target("https://app.example.invalid", path).is_none(), "{path}");
        }
        assert!(trusted_notice_target("https://evil.test", "/v/ven_1/now").is_none());
        assert_eq!(trusted_notice_target("https://app.example.invalid", "/v/ven_1/connections").unwrap().as_str(),
                   "https://app.example.invalid/connections");
        assert_eq!(trusted_notice_target("https://app.example.invalid", "/v/ven_1/outreach").unwrap().as_str(),
                   "https://app.example.invalid/v/ven_1");
        let task = trusted_notice_target("https://app.example.invalid", "/v/ven_1#attention-ht_exact").unwrap();
        assert_eq!(task.as_str(), "https://app.example.invalid/v/ven_1#attention-ht_exact");
        let review = trusted_notice_target(
            "https://app.example.invalid",
            "/v/ven_1/device-work-orders/dwo_exact-2",
        ).unwrap();
        assert_eq!(review.as_str(),
                   "https://app.example.invalid/v/ven_1/device-work-orders/dwo_exact-2");
        for path in [
            "/v/ven_1/device-work-orders/not-an-order",
            "/v/ven_1/device-work-orders/dwo_good/extra",
            "/v/ven_1/device-work-orders/dwo_bad?next=https://evil.test",
        ] {
            assert!(trusted_notice_target("https://app.example.invalid", path).is_none(), "{path}");
        }
    }

}
