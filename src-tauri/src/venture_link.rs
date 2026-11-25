//! Email links stay HTTPS; an explicit browser action hands a route to this app.
use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Mutex,
};
use tauri::{AppHandle, Emitter, Manager};
use url::Url;

#[derive(Debug)]
struct Target {
    path: String,
    venture: Option<String>,
}

fn id(value: &str, prefix: &str) -> bool {
    value.starts_with(prefix)
        && value.len() > prefix.len()
        && value.len() <= 128
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
}

fn parse(url: &Url, scheme: &str) -> Option<Target> {
    if url.scheme() != scheme
        || url.host_str() != Some("open")
        || !matches!(url.path(), "" | "/")
        || !url.username().is_empty()
        || url.password().is_some()
        || url.port().is_some()
        || url.fragment().is_some()
    {
        return None;
    }
    let pairs: Vec<_> = url.query_pairs().collect();
    if pairs.len() != 1 || pairs[0].0 != "path" {
        return None;
    }
    let path = pairs[0].1.as_ref();
    // Reject before URL parsing can normalize traversal or reinterpret a host.
    if path.len() > 512
        || !path.starts_with('/')
        || path.starts_with("//")
        || path.contains("..")
        || path
            .bytes()
            .any(|c| !c.is_ascii_graphic() || matches!(c, b'%' | b'\\'))
    {
        return None;
    }
    let route = Url::parse(&format!("https://app.supymem.com{path}")).ok()?;
    let parts: Vec<_> = route.path().split('/').collect();
    let query: Vec<_> = route.query_pairs().collect();
    let mut venture = None;
    let valid = match parts.as_slice() {
        ["", ""] => query.is_empty() && route.fragment().is_none(),
        ["", "new"] | ["", "gtm"] => {
            if query.len() == 1 && query[0].0 == "venture" && id(&query[0].1, "ven_") {
                venture = Some(query[0].1.to_string());
                route.fragment().is_none()
            } else {
                parts[1] == "new" && query.is_empty() && route.fragment().is_none()
            }
        }
        ["", "v", owner, rest @ ..] if id(owner, "ven_") => {
            venture = Some((*owner).into());
            match rest {
                [] => {
                    (query.is_empty()
                        || (query.len() == 1 && query[0].0 == "chat" && query[0].1 == "1"))
                        && route.fragment().is_none()
                }
                ["site"] => {
                    (query.is_empty()
                        || (query.len() == 1 && query[0].0 == "create" && query[0].1 == "1"))
                        && route.fragment().is_none()
                }
                ["builds"] => {
                    (query.is_empty()
                        || (query.len() == 2
                            && query.iter().any(|(k, v)| k == "app" && id(v, ""))
                            && query.iter().any(|(k, v)| k == "run" && id(v, ""))))
                        && matches!(route.fragment(), None | Some("build-review"))
                }
                ["device-work-orders", order] => {
                    id(order, "dwo_") && query.is_empty() && route.fragment().is_none()
                }
                [page] => {
                    matches!(
                        *page,
                        "now"
                            | "work"
                            | "outreach"
                            | "connections"
                            | "analytics"
                            | "settings"
                            | "suggestions"
                            | "research"
                            | "idea"
                            | "plan"
                            | "tasks"
                            | "experiments"
                    ) && query.is_empty()
                        && route.fragment().is_none()
                }
                _ => false,
            }
        }
        _ => false,
    };
    valid.then(|| Target {
        path: path.into(),
        venture,
    })
}

fn destination(console: &str, target: &Target) -> Option<Url> {
    let base = Url::parse(console).ok()?;
    let trusted = base.as_str().trim_end_matches('/') == crate::channel::console()
        || (base.scheme() == "http" && matches!(base.host_str(), Some("localhost" | "127.0.0.1")));
    if !trusted
        || !base.username().is_empty()
        || base.password().is_some()
        || base.path() != "/"
        || base.query().is_some()
        || base.fragment().is_some()
    {
        return None;
    }
    base.join(&target.path).ok()
}

// Only the newest click is retained during startup, never across sign-in/account changes.
static READY: AtomicBool = AtomicBool::new(false);
static LATEST: AtomicU64 = AtomicU64::new(0);
static PENDING: Mutex<Option<(Target, u64)>> = Mutex::new(None);

fn if_current<R>(generation: u64, action: impl FnOnce() -> R) -> Option<R> {
    (LATEST.load(Ordering::SeqCst) == generation).then(action)
}

pub(crate) fn open(app: &AppHandle, url: &Url) {
    let Some(target) = parse(url, crate::channel::callback_scheme()) else {
        return;
    };
    let Ok(mut pending) = PENDING.lock() else {
        return;
    };
    let generation = LATEST.fetch_add(1, Ordering::SeqCst) + 1;
    if !READY.load(Ordering::SeqCst) {
        *pending = Some((target, generation));
        return;
    }
    drop(pending);
    dispatch(app.clone(), target, generation);
}

/// Console boot and its initial account restoration have finished (including a signed-out result).
pub(crate) fn ready(app: &AppHandle) {
    let Ok(mut pending) = PENDING.lock() else {
        return;
    };
    READY.store(true, Ordering::SeqCst);
    let target = pending.take();
    drop(pending);
    if let Some((target, generation)) = target {
        dispatch(app.clone(), target, generation);
    }
}

fn failed(app: &AppHandle, generation: u64, code: &'static str) {
    let handle = app.clone();
    // OS clicks, failure presentation and navigation are serialized on the main thread.
    let _ = app.run_on_main_thread(move || {
        if_current(generation, || {
            let _ = handle.emit("supymem-link-error", code);
            crate::bring_main_forward(&handle);
        });
    });
}

fn dispatch(app: AppHandle, target: Target, generation: u64) {
    tauri::async_runtime::spawn(async move {
        if if_current(generation, || ()).is_none() {
            return;
        }
        let Some(state) = app.try_state::<crate::AppState>() else {
            return;
        };
        // Refresh the device session before checking resource access; use the existing native transport.
        let session =
            crate::native_api_request(&state, "GET".into(), "/auth/session".into(), None, None)
                .await;
        if !session.as_ref().is_ok_and(|reply| reply.status == 200) {
            let code = if session.is_ok_and(|reply| reply.status == 401) {
                "sign_in"
            } else {
                "unavailable"
            };
            failed(&app, generation, code);
            return;
        }
        if if_current(generation, || ()).is_none() {
            return;
        }
        let Some(owner) = crate::notice_custody(&app) else {
            failed(&app, generation, "unavailable");
            return;
        };
        if let Some(venture) = &target.venture {
            let reply = crate::native_api_request(
                &state,
                "GET".into(),
                format!("/api/ventures/{venture}"),
                None,
                None,
            )
            .await;
            if !reply.as_ref().is_ok_and(|reply| {
                reply.status == 200 && reply.data["id"].as_str() == Some(venture.as_str())
            }) {
                let code = if reply.is_ok_and(|reply| matches!(reply.status, 200 | 403 | 404)) {
                    "not_available"
                } else {
                    "unavailable"
                };
                failed(&app, generation, code);
                return;
            }
        }
        let Some(url) = destination(&state.supervisor.console_url(), &target) else {
            failed(&app, generation, "unavailable");
            return;
        };
        let handle = app.clone();
        let scheduled = app.run_on_main_thread(move || {
            // Account/device/epoch admission and navigation share the credential lease.
            if if_current(generation, || {
                crate::with_notice_custody(&handle, &owner, || {
                    if let Some(main) = handle.get_webview_window("main") {
                        if main.navigate(url).is_ok() {
                            crate::bring_window_forward(&main);
                            return true;
                        }
                    }
                    false
                })
            })
            .flatten()
                != Some(true)
            {
                failed(&handle, generation, "unavailable");
            }
        });
        if scheduled.is_err() {
            failed(&app, generation, "unavailable");
        }
    });
}

#[cfg(test)]
mod tests {
    #[test]
    fn an_older_click_cannot_navigate_or_present_a_failure_after_a_newer_click() {
        use std::sync::atomic::Ordering;
        let first = super::LATEST.fetch_add(1, Ordering::SeqCst) + 1;
        let second = super::LATEST.fetch_add(1, Ordering::SeqCst) + 1;
        let mut effects = Vec::new();
        for effect in ["navigate", "error", "show", "focus"] {
            assert!(super::if_current(first, || effects.push(effect)).is_none());
        }
        assert!(effects.is_empty());
        assert!(super::if_current(second, || effects.push("newest_navigation")).is_some());
        assert_eq!(effects, ["newest_navigation"]);
    }
    fn link(path: &str) -> url::Url {
        let mut url = url::Url::parse("supymem://open").unwrap();
        url.query_pairs_mut().append_pair("path", path);
        url
    }
    #[test]
    fn email_destinations_retain_their_venture_query_and_review_anchor() {
        for path in [
            "/",
            "/new",
            "/new?venture=ven_a",
            "/gtm?venture=ven_a",
            "/v/ven_a?chat=1",
            "/v/ven_a/now",
            "/v/ven_a/idea",
            "/v/ven_a/research",
            "/v/ven_a/plan",
            "/v/ven_a/tasks",
            "/v/ven_a/site?create=1",
            "/v/ven_a/analytics",
            "/v/ven_a/settings",
            "/v/ven_a/experiments",
            "/v/ven_a/device-work-orders/dwo_a",
            "/v/ven_a/builds?app=app_a&run=build_a#build-review",
            "/v/ven_a/builds?run=build_a&app=app_a",
        ] {
            let target = super::parse(&link(path), "supymem").expect(path);
            assert_eq!(target.path, path);
            assert_eq!(
                target.venture.as_deref(),
                if path == "/" || path == "/new" {
                    None
                } else {
                    Some("ven_a")
                }
            );
        }
    }
    #[test]
    fn handoff_rejects_external_redirects_ambiguous_queries_and_unknown_routes() {
        for path in [
            "https://evil.test/v/ven_a",
            "//evil.test/v/ven_a",
            "/v/ven_a/../account",
            "/v/ven_a/%2e%2e/account",
            "/v/ven_a\\now",
            "/v/ven_a/now\n",
            "/account",
            "/gtm",
            "/new?next=https://evil.test",
            "/new?venture=ven_a&venture=ven_b",
            "/v/ven_a?chat=1#unexpected",
            "/v/ven_a/site?create=2",
            "/v/ven_a/builds?app=a&app=b",
            "/v/ven_a/builds?app=a&run=b&next=/account",
            "/v/not_a_venture/now",
            "/v/ven_a/unknown",
            "/v/ven_a/device-work-orders/not_an_order",
            "/new#fragment",
        ] {
            assert!(super::parse(&link(path), "supymem").is_none(), "{path}");
        }
        for raw in [
            "supymem-staging://open?path=/new",
            "supymem://evil?path=/new",
            "supymem://user@open?path=/new",
            "supymem://open:90?path=/new",
            "supymem://open/other?path=/new",
            "supymem://open?path=/new&path=/new",
            "supymem://open?path=/new#x",
        ] {
            assert!(
                super::parse(&url::Url::parse(raw).unwrap(), "supymem").is_none(),
                "{raw}"
            );
        }
    }
    #[test]
    fn only_the_build_channel_or_local_console_can_receive_a_route() {
        let target = super::parse(&link("/v/ven_a/now"), "supymem").unwrap();
        assert_eq!(
            super::destination(crate::channel::console(), &target)
                .unwrap()
                .path(),
            "/v/ven_a/now"
        );
        assert!(super::destination("http://127.0.0.1:3100", &target).is_some());
        for origin in [
            "https://evil.test",
            "https://app.supymem.com.evil.test",
            "https://user@app.supymem.com",
            "https://app.supymem.com/path",
            "https://app.supymem.com?next=x",
            "http://app.supymem.com",
        ] {
            assert!(super::destination(origin, &target).is_none(), "{origin}");
        }
    }
}
