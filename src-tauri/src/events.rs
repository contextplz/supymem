//! Scoped read-only event streams, cancelled on account change and app shutdown.
use crate::auth::{SharedAuth, RequestIdentity};
use crate::supervisor::Supervisor;
use serde::Serialize;
use std::{
    collections::HashMap,
    io::Read,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tauri::{AppHandle, Emitter};

const MAX_FRAME: usize = 1024 * 1024;
#[derive(Clone, Serialize)]
pub struct Frame {
    pub id: String,
    pub kind: String,
    pub event: String,
    pub data: String,
    pub last_event_id: String,
    pub status: u16,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub origin: Option<String>,
}
#[derive(Default)]
pub struct Hub {
    entries: Mutex<HashMap<String, Arc<AtomicBool>>>,
}
impl Hub {
    pub fn subscribe(
        self: &Arc<Self>,
        app: AppHandle,
        auth: Arc<SharedAuth>,
        supervisor: Arc<Supervisor>,
        id: String,
        path: String,
        last_event_id: Option<String>,
    ) -> Result<(), String> {
        if uuid::Uuid::parse_str(&id).is_err()
            || !valid_path(&path)
            || last_event_id.as_ref().is_some_and(|v| !valid_id(v))
        {
            return Err("invalid_stream_request".into());
        }
        let cancel = Arc::new(AtomicBool::new(false));
        {
            let mut entries = self.entries.lock().map_err(|_| "stream_unavailable")?;
            if entries.len() >= 4 || entries.contains_key(&id) {
                return Err("stream_limit_reached".into());
            }
            entries.insert(id.clone(), cancel.clone());
        }
        let hub = self.clone();
        thread::spawn(move || {
            run(
                &app,
                &auth,
                &supervisor,
                &cancel,
                &id,
                &path,
                last_event_id.unwrap_or_default(),
            );
            emit(&app, &id, "closed", "", "", "", 0);
            if let Ok(mut entries) = hub.entries.lock() {
                entries.remove(&id);
            }
        });
        Ok(())
    }
    pub fn unsubscribe(&self, id: &str) {
        if let Ok(entries) = self.entries.lock() {
            if let Some(cancel) = entries.get(id) {
                cancel.store(true, Ordering::SeqCst);
            }
        }
    }
    pub fn stop_all(&self) {
        if let Ok(entries) = self.entries.lock() {
            for cancel in entries.values() {
                cancel.store(true, Ordering::SeqCst);
            }
        }
    }
}
fn valid_id(value: &str) -> bool {
    value.len() <= 200 && value.bytes().all(|c| c >= 32 && c < 127)
}
fn venture(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c))
}
fn valid_path(path: &str) -> bool {
    let Ok(url) = url::Url::parse(&format!("https://stream.invalid{path}")) else {
        return false;
    };
    if path
        .split('?')
        .next()
        .unwrap_or("")
        .split('/')
        .any(|p| matches!(p, "." | ".."))
    {
        return false;
    }
    if url.origin().ascii_serialization() != "https://stream.invalid"
        || url.fragment().is_some()
        || path.contains('%')
        || path.contains('\\')
    {
        return false;
    }
    if url.path() == "/events" {
        let ids: Vec<_> = url
            .query_pairs()
            .filter(|(k, _)| k == "venture_id")
            .collect();
        ids.len() == 1 && venture(&ids[0].1) && path.len() <= 2048
    } else {
        let parts: Vec<_> = url.path().split('/').collect();
        parts.len() == 6
            && parts[1] == "api"
            && parts[2] == "ventures"
            && venture(parts[3])
            && parts[4] == "activity"
            && parts[5] == "stream"
            && url.query().is_none()
    }
}
enum ProofState { Current, Busy, Invalid }
fn current(auth: &Arc<SharedAuth>, identity: &RequestIdentity) -> ProofState {
    if crate::local_stack::is_local() {
        return if crate::nav_guard::owner_secret(&std::path::Path::new(&identity.device_id).join("owner-session.secret"))
            .is_ok_and(|secret| secret == identity.access_token) { ProofState::Current } else { ProofState::Invalid };
    }
    // Brief ordinary account work may overlap a frame. Hold the frame for at
    // most one second, never wait behind an interactive credential-store call,
    // and never deliver it without rechecking the current account identity.
    for _ in 0..20 {
        if let Ok(a) = auth.try_lock() { return if a.matches_identity(identity) { ProofState::Current } else { ProofState::Invalid }; }
        if auth.store_pending() { return ProofState::Invalid; }
        thread::sleep(Duration::from_millis(50));
    }
    if auth.status().state=="native_auth_busy" { ProofState::Busy } else { ProofState::Invalid }
}
fn emit(app: &AppHandle, id: &str, kind: &str, event: &str, data: &str, last: &str, status: u16) {
    emit_with_origin(app, id, kind, event, data, last, status, None);
}
fn emit_with_origin(app: &AppHandle, id: &str, kind: &str, event: &str, data: &str, last: &str,
                    status: u16, origin: Option<&str>) {
    let _ = app.emit_to(
        "main",
        "supymem-native-sse",
        Frame {
            id: id.into(),
            kind: kind.into(),
            event: event.into(),
            data: data.into(),
            last_event_id: last.into(),
            status,
            origin: origin.map(str::to_owned),
        },
    );
}
fn pause(cancel: &AtomicBool, millis: u64) {
    for _ in 0..(millis / 100).max(1) {
        if cancel.load(Ordering::SeqCst) {
            break;
        }
        thread::sleep(Duration::from_millis(100));
    }
}
fn open_stream(
    identity: &RequestIdentity,
    path: &str,
    last: &str,
) -> Result<reqwest::blocking::Response, reqwest::Error> {
    let client = reqwest::blocking::Client::builder().user_agent(crate::http::USER_AGENT)
        .no_proxy()
        .timeout(Duration::from_secs(60))
        .connect_timeout(Duration::from_secs(10))
        .redirect(reqwest::redirect::Policy::none())
        .build()?;
    let mut request = client
        .get(format!("{}{path}", identity.gateway))
        .header("Accept", "text/event-stream")
        .header("Accept-Encoding", "identity");
    request = if crate::local_stack::is_local() {
        request.header("X-Local-Proxy-Secret", &identity.access_token)
    } else { request.bearer_auth(&identity.access_token) };
    if !last.is_empty() {
        request = request.header("Last-Event-ID", last);
    }
    request.send()
}
fn run(
    app: &AppHandle,
    auth: &Arc<SharedAuth>,
    supervisor: &Supervisor,
    cancel: &AtomicBool,
    id: &str,
    path: &str,
    mut last: String,
) {
    let mut backoff = 500;
    let mut auth_retried = false;
    let mut owner: Option<(String, String)> = None;
    while !cancel.load(Ordering::SeqCst) {
        let identity = if crate::local_stack::is_local() {
            let root = supervisor.data_dir();
            let Ok(secret) = crate::nav_guard::owner_secret(&root.join("owner-session.secret")) else {
                emit(app, id, "error", "", "", &last, 503);
                break;
            };
            RequestIdentity { gateway: supervisor.gateway_url(), access_token: secret, subject: String::new(),
                device_id: root.to_string_lossy().to_string(), expires_at: u64::MAX }
        } else {
            let Ok(mut locked) = auth.try_lock() else {
                // Page requests briefly hold the same mutex. The stream has
                // not failed, so keep waiting without telling the UI to
                // close and recreate its subscription.
                if auth.store_pending() { break; }
                pause(cancel, backoff);
                backoff = (backoff * 2).min(15000);
                continue;
            };
            match locked.request_identity() {
                Ok(i) => i,
                Err(r) => {
                    emit(app, id, "error", "", "", &last, r.status);
                    if r.status == 401 || r.status == 503 {
                        break;
                    }
                    drop(locked);
                    pause(cancel, backoff);
                    backoff = (backoff * 2).min(15000);
                    continue;
                }
            }
        };
        let key = (identity.subject.clone(), identity.device_id.clone());
        if owner.as_ref().is_some_and(|previous| previous != &key) {
            break;
        }
        owner = Some(key);
        if cancel.load(Ordering::SeqCst) {
            break;
        }
        let response = open_stream(&identity, path, &last);
        let mut response = match response {
            Ok(r) => r,
            Err(_) => {
                emit(app, id, "error", "", "", &last, 0);
                pause(cancel, backoff);
                backoff = (backoff * 2).min(15000);
                continue;
            }
        };
        let status = response.status().as_u16();
        if !response.status().is_success() {
            let mut reported_status = status;
            if status == 401 && !crate::local_stack::is_local() {
                if let Ok(mut a) = auth.try_lock() {
                    let checked = if auth_retried && a.matches_identity(&identity) { a.verify_session(false) }
                        else { a.confirm_feature_unauthorized(&identity) };
                    if let Err(reason) = checked {
                        reported_status = reason.status;
                    } else if !auth_retried && a.request_identity().is_ok_and(|renewed| renewed.access_token != identity.access_token) {
                        crate::sync_auth_bridge(&mut a, supervisor);
                        auth_retried = true;
                        drop(a);
                        continue;
                    }
                    crate::sync_auth_bridge(&mut a, supervisor);
                } else {
                    reported_status = 503;
                }
            }
            emit(app, id, "error", "", "", &last, reported_status);
            if matches!(reported_status, 400 | 401 | 403 | 404 | 422) {
                break;
            }
            pause(cancel, backoff);
            backoff = (backoff * 2).min(15000);
            continue;
        }
        auth_retried = false;
        if !response
            .headers()
            .get("content-type")
            .and_then(|h| h.to_str().ok())
            .is_some_and(|h| h.starts_with("text/event-stream"))
        {
            emit(app, id, "error", "", "", &last, 502);
            pause(cancel, backoff);
            backoff = (backoff * 2).min(15000);
            continue;
        }
        if cancel.load(Ordering::SeqCst) { break; }
        match current(auth, &identity) {
            ProofState::Current => (),
            ProofState::Invalid => break,
            ProofState::Busy => { pause(cancel,backoff);continue; }
        }
        emit(app, id, "open", "", "", &last, status);
        backoff = 500;
        let mut parser = Parser::default();
        let mut bytes = [0; 8192];
        let mut auth_busy = false;
        'read: loop {
            if cancel.load(Ordering::SeqCst) { return; }
            match current(auth, &identity) {
                ProofState::Current => (),
                ProofState::Invalid => return,
                ProofState::Busy => { auth_busy = true; break 'read; },
            }
            let now = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs();
            if now + 30 >= identity.expires_at {
                break;
            }
            let read = match response.read(&mut bytes) {
                Ok(0) | Err(_) => break,
                Ok(n) => n,
            };
            let frames = match parser.push(&bytes[..read]) {
                Ok(f) => f,
                Err(_) => {
                    emit(app, id, "error", "", "", &last, 502);
                    return;
                }
            };
            for frame in frames {
                if cancel.load(Ordering::SeqCst) { return; }
                match current(auth, &identity) {
                    ProofState::Current => (),
                    ProofState::Invalid => return,
                    // Retain `last` until a frame is delivered. Reconnect after
                    // ordinary contention so the server can replay that frame.
                    ProofState::Busy => { auth_busy = true; break 'read; },
                }
                if let Some(value) = frame.id {
                    last = value;
                }
                if !frame.data.is_empty() {
                    emit(app, id, "message", &frame.event, &frame.data, &last, 200);
                }
            }
        }
        if !cancel.load(Ordering::SeqCst) {
            // A local auth wait is a silent reconnect, not an SSE failure.
            // The last delivered ID remains unchanged for replay.
            if !auth_busy { emit(app, id, "error", "", "", &last, 0); }
            pause(cancel, backoff);
        }
    }
}
#[derive(Default)]
struct Parser {
    line: Vec<u8>,
    data: Vec<String>,
    event: String,
    id: Option<String>,
    size: usize,
    after_cr: bool,
}
struct Parsed {
    event: String,
    data: String,
    id: Option<String>,
}
impl Parser {
    fn push(&mut self, bytes: &[u8]) -> Result<Vec<Parsed>, ()> {
        let mut frames = vec![];
        for byte in bytes {
            if self.after_cr && *byte == b'\n' {
                self.after_cr = false;
                continue;
            }
            self.after_cr = false;
            if *byte == b'\r' || *byte == b'\n' {
                self.after_cr = *byte == b'\r';
                if let Some(frame) = self.finish_line()? {
                    frames.push(frame);
                }
            } else {
                self.line.push(*byte);
                self.size += 1;
                if self.size > MAX_FRAME {
                    return Err(());
                }
            }
        }
        Ok(frames)
    }
    fn finish_line(&mut self) -> Result<Option<Parsed>, ()> {
        let bytes = std::mem::take(&mut self.line);
        let line = String::from_utf8(bytes).map_err(|_| ())?;
        if line.is_empty() {
            let result = Parsed {
                event: if self.event.is_empty() {
                    "message".into()
                } else {
                    std::mem::take(&mut self.event)
                },
                data: self.data.join("\n"),
                id: self.id.take(),
            };
            self.data.clear();
            self.size = 0;
            return Ok(Some(result));
        }
        if line.starts_with(':') {
            return Ok(None);
        }
        let (key, value) = line.split_once(':').unwrap_or((&line, ""));
        let value = value.strip_prefix(' ').unwrap_or(value);
        match key {
            "data" => self.data.push(value.into()),
            "event" => {
                if value.len() > 100
                    || !value
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
                {
                    return Err(());
                }
                self.event = value.into();
            }
            "id" => {
                if !valid_id(value) {
                    return Err(());
                }
                self.id = Some(value.into());
            }
            _ => {}
        }
        Ok(None)
    }
}
#[cfg(test)]
mod tests {
    #[test]
    fn native_stream_origin_is_a_closed_optional_frame_field() {
        let frame = super::Frame { id: "id".into(), kind: "error".into(), event: "".into(),
            data: "".into(), last_event_id: "".into(), status: 503,
            origin: Some("native_auth_busy".into()) };
        let value = serde_json::to_value(frame).unwrap();
        assert_eq!(value["origin"], "native_auth_busy");
        assert!(value.get("path").is_none());
    }
    #[test]
    fn event_paths_require_an_explicit_venture() {
        for path in [
            "/events",
            "/events?venture_id=one&venture_id=two",
            "/api/ventures/../activity/stream",
            "/api/ventures/%2e%2e/activity/stream",
            "https://evil.test/events?venture_id=ven_a",
            "/api/admin/stream",
        ] {
            assert!(!super::valid_path(path), "{path}");
        }
        assert!(super::valid_path(
            "/events?venture_id=ven_a&topics=job.progress"
        ));
        assert!(super::valid_path("/api/ventures/ven_a/activity/stream"));
    }
    #[test]
    fn frames_handle_split_utf8_multiline_crlf_and_cursor() {
        let mut p = super::Parser::default();
        let data = "id: 12\r\nevent: activity\r\ndata: hello\r\ndata: 世界\r\n\r\n".as_bytes();
        let mut all = vec![];
        for chunk in data.chunks(3) {
            all.extend(p.push(chunk).ok().unwrap());
        }
        assert_eq!(all.len(), 1);
        assert_eq!(all[0].data, "hello\n世界");
        assert_eq!(all[0].id.as_deref(), Some("12"));
        assert_eq!(all[0].event, "activity");
    }
    #[test]
    fn oversized_unterminated_frames_and_invalid_ids_fail() {
        let mut p = super::Parser::default();
        assert!(p.push(&vec![b'x'; super::MAX_FRAME + 1]).is_err());
        let mut p = super::Parser::default();
        assert!(p.push(b"id: bad\0id\n\n").is_err());
    }
    #[test]
    fn stream_http_keeps_bearer_native_resumes_cursor_and_never_follows_redirects() {
        use std::{
            io::{Read, Write},
            net::TcpListener,
        };
        for redirected in [false, true] {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let address = listener.local_addr().unwrap();
            let server = std::thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                let mut bytes = [0; 4096];
                let count = stream.read(&mut bytes).unwrap();
                let request = String::from_utf8_lossy(&bytes[..count]).to_ascii_lowercase();
                assert!(request.starts_with("get /api/ventures/ven_a/activity/stream http/1.1"));
                if crate::local_stack::is_local() {
                    assert!(request.contains("x-local-proxy-secret: fixture-token\r\n"));
                    assert!(!request.contains("authorization:"));
                } else { assert!(request.contains("authorization: bearer fixture-token\r\n")); }
                assert!(request.contains("last-event-id: 41\r\n"));
                assert!(request.contains("accept: text/event-stream\r\n"));
                assert!(!request.contains("cookie:"));
                let (status, extra, body) = if redirected {
                    ("302 Found", "Location: http://127.0.0.1:1/leak\r\n", "")
                } else {
                    (
                        "200 OK",
                        "Content-Type: text/event-stream\r\n",
                        "id: 42\ndata: {\"state\":\"running\"}\n\n",
                    )
                };
                write!(stream, "HTTP/1.1 {status}\r\n{extra}Content-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
            });
            let identity = super::RequestIdentity {
                gateway: format!("http://{address}"),
                access_token: "fixture-token".into(),
                subject: "usr_fixture".into(),
                device_id: "device-fixture".into(),
                expires_at: u64::MAX,
            };
            let mut response =
                super::open_stream(&identity, "/api/ventures/ven_a/activity/stream", "41").unwrap();
            assert_eq!(
                response.status().as_u16(),
                if redirected { 302 } else { 200 }
            );
            if !redirected {
                let mut bytes = vec![];
                response.read_to_end(&mut bytes).unwrap();
                let frames = super::Parser::default().push(&bytes).ok().unwrap();
                assert_eq!(frames[0].id.as_deref(), Some("42"));
                assert_eq!(frames[0].data, "{\"state\":\"running\"}");
            }
            server.join().unwrap();
        }
    }
    #[test]
    fn unsubscribe_and_shutdown_cancel_registered_readers() {
        use std::sync::{
            atomic::{AtomicBool, Ordering},
            Arc,
        };
        let hub = super::Hub::default();
        let cancel = Arc::new(AtomicBool::new(false));
        hub.entries
            .lock()
            .unwrap()
            .insert("one".into(), cancel.clone());
        hub.unsubscribe("one");
        assert!(cancel.load(Ordering::SeqCst));
        cancel.store(false, Ordering::SeqCst);
        hub.stop_all();
        assert!(cancel.load(Ordering::SeqCst));
    }
}
