//! Cold start never depends on the hosted HTML being available.
use std::{io::Read, time::Duration};
use url::Url;
#[derive(Debug, Clone, serde::Serialize)]
pub struct ConnectionReport {
    pub connected: bool,
    pub code: &'static str,
    pub http_status: Option<u16>,
}
impl ConnectionReport {
    pub fn ready() -> Self {
        Self {
            connected: true,
            code: "connected",
            http_status: Some(200),
        }
    }
    fn failed(code: &'static str, http_status: Option<u16>) -> Self {
        Self {
            connected: false,
            code,
            http_status,
        }
    }
}

// Keep one diagnostic receipt, not an unbounded log of repeated failures. Never
// persist URLs, response bodies, query strings, credentials or request headers.
pub fn record(report: &ConnectionReport, data_dir: &std::path::Path) {
    let Ok(mut value) = serde_json::to_value(report) else {
        return;
    };
    value["checked_at"] = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|time| serde_json::json!(time.as_secs()))
        .unwrap_or(serde_json::Value::Null);
    if std::fs::create_dir_all(data_dir).is_err() {
        return;
    }
    let path = data_dir.join("connection-status.json");
    let _ = std::fs::write(path, value.to_string());
}

pub fn check(origin: &str) -> ConnectionReport {
    let Ok(base) = Url::parse(origin) else {
        return ConnectionReport::failed("invalid_endpoint", None);
    };
    if !matches!(base.scheme(), "http" | "https")
        || !base.username().is_empty()
        || base.password().is_some()
    {
        return ConnectionReport::failed("invalid_endpoint", None);
    }
    let expected = base.origin();
    let client = reqwest::blocking::Client::builder().user_agent(crate::http::USER_AGENT)
        .timeout(Duration::from_secs(8))
        .no_proxy()
        .redirect(reqwest::redirect::Policy::custom(move |attempt| {
            if attempt.previous().len() < 3 && attempt.url().origin() == expected {
                attempt.follow()
            } else {
                attempt.stop()
            }
        }))
        .build();
    let Ok(client) = client else {
        return ConnectionReport::failed("client_unavailable", None);
    };
    let response = client
        .get(origin)
        .header("Accept", "text/html")
        .header("Accept-Encoding", "identity")
        .send();
    let response = match response {
        Ok(response) => response,
        Err(error) => {
            return ConnectionReport::failed(
                if error.is_timeout() {
                    "timeout"
                } else {
                    "network_unavailable"
                },
                None,
            )
        }
    };
    let status = response.status().as_u16();
    if !response.status().is_success() {
        return ConnectionReport::failed("service_unavailable", Some(status));
    }
    if !response
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("text/html"))
    {
        return ConnectionReport::failed("unexpected_response", Some(status));
    }
    let mut prefix = vec![];
    if response.take(64 * 1024).read_to_end(&mut prefix).is_err() {
        return ConnectionReport::failed("incomplete_response", Some(status));
    }
    let text = String::from_utf8_lossy(&prefix).to_ascii_lowercase();
    if text.contains("<html") && !text.contains("503 service temporarily unavailable") {
        ConnectionReport {
            connected: true,
            code: "connected",
            http_status: Some(status),
        }
    } else {
        ConnectionReport::failed("unexpected_response", Some(status))
    }
}
#[cfg(test)]
mod tests {
    #[test]
    #[ignore = "Explicit real hosted endpoint diagnostic; no credentials or response contents"]
    fn live_staging_probe() {
        let report = super::check("https://app.staging.supymem.com");
        println!("{}", serde_json::to_string(&report).unwrap());
        assert!(report.connected, "{}", report.code);
    }

    #[test]
    fn unavailable_http_and_non_html_are_not_a_console() {
        use std::{
            io::{Read, Write},
            net::TcpListener,
        };
        for (status, kind, body, expected) in [
            (
                "503 Service Unavailable",
                "text/html",
                "<html>Unavailable</html>",
                false,
            ),
            ("200 OK", "application/json", "{}", false),
            (
                "200 OK",
                "text/html",
                "<!doctype html><html>Supymem</html>",
                true,
            ),
        ] {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let addr = listener.local_addr().unwrap();
            let server = std::thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                let mut buf = [0; 1024];
                let size = stream.read(&mut buf).unwrap();
                let headers = String::from_utf8_lossy(&buf[..size]).to_ascii_lowercase();
                assert!(headers.contains(&format!("user-agent: {}", crate::http::USER_AGENT.to_ascii_lowercase())));
                let response=format!("HTTP/1.1 {status}\r\nContent-Type: {kind}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len());
                stream.write_all(response.as_bytes()).unwrap();
            });
            let report = super::check(&format!("http://{addr}"));
            assert_eq!(report.connected, expected);
            if status.starts_with("503") {
                assert_eq!(report.code, "service_unavailable");
                assert_eq!(report.http_status, Some(503));
            }
            server.join().unwrap();
        }
    }
}
