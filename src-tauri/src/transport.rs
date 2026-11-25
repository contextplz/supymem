//! Bounded authenticated file transport. No credential reaches the WebView.
use crate::auth::{allowed_request, RequestIdentity};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{collections::HashMap, io::Read, time::Duration};

pub const MAX_BYTES: usize = 32 * 1024 * 1024;
const MAX_ENCODED: usize = ((MAX_BYTES + 2) / 3) * 4;
#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum Body {
    Json {
        value: Value,
    },
    Bytes {
        #[serde(rename = "contentType")]
        content_type: String,
        base64: String,
    },
    Multipart {
        fields: Vec<Field>,
    },
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Field {
    name: String,
    value: Option<String>,
    filename: Option<String>,
    content_type: Option<String>,
    base64: Option<String>,
}
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RawReply {
    pub status: u16,
    pub body_base64: String,
    pub headers: HashMap<String, String>,
}
impl RawReply {
    pub fn sent_unconfirmed(code: &str) -> Self {
        Self { status: 503,
            body_base64: STANDARD.encode(crate::auth::Reply::sent_unconfirmed(code).data.to_string()),
            headers: HashMap::from([("content-type".into(), "application/json".into())]) }
    }
    pub fn error(status: u16, code: &str) -> Self {
        Self {
            status,
            body_base64: STANDARD
                .encode(json!({"error":{"code":code,"message":code.replace('_'," ")}}).to_string()),
            headers: HashMap::from([("content-type".into(), "application/json".into())]),
        }
    }
}
fn decode(value: &str) -> Result<Vec<u8>, RawReply> {
    if value.len() > MAX_ENCODED {
        return Err(RawReply::error(413, "payload_too_large"));
    }
    let bytes = STANDARD
        .decode(value)
        .map_err(|_| RawReply::error(400, "invalid_base64_payload"))?;
    if bytes.len() > MAX_BYTES {
        return Err(RawReply::error(413, "payload_too_large"));
    }
    Ok(bytes)
}
fn content_type(value: &str) -> Result<&str, RawReply> {
    if value.is_empty()
        || value.len() > 120
        || !value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"/.-+_;= ".contains(&c))
    {
        return Err(RawReply::error(400, "invalid_content_type"));
    }
    Ok(value)
}
fn field_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 100
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_-[]".contains(&c))
}
fn safe_filename(raw: &str) -> String {
    let base = raw.rsplit(['/', '\\']).next().unwrap_or("upload");
    let sanitized: String = base
        .chars()
        .filter(|c| !c.is_control())
        .map(|c| {
            if c.is_ascii_alphanumeric() || "._- ".contains(c) {
                c
            } else {
                '_'
            }
        })
        .take(180)
        .collect();
    if sanitized.trim_matches(['.', ' ']).is_empty() {
        "upload".into()
    } else {
        sanitized
    }
}
fn append(out: &mut Vec<u8>, bytes: &[u8]) -> Result<(), RawReply> {
    if out.len().saturating_add(bytes.len()) > MAX_BYTES {
        return Err(RawReply::error(413, "payload_too_large"));
    }
    out.extend_from_slice(bytes);
    Ok(())
}
fn encode_body(body: Option<Body>) -> Result<(Option<String>, Vec<u8>), RawReply> {
    match body {
        None => Ok((None, vec![])),
        Some(Body::Json { value }) => {
            let bytes =
                serde_json::to_vec(&value).map_err(|_| RawReply::error(400, "invalid_json"))?;
            if bytes.len() > MAX_BYTES {
                return Err(RawReply::error(413, "payload_too_large"));
            }
            Ok((Some("application/json".into()), bytes))
        }
        Some(Body::Bytes {
            content_type: kind,
            base64,
        }) => Ok((Some(content_type(&kind)?.into()), decode(&base64)?)),
        Some(Body::Multipart { fields }) => {
            if fields.is_empty() || fields.len() > 32 {
                return Err(RawReply::error(400, "invalid_multipart_fields"));
            }
            let boundary = format!("supymem-{}", uuid::Uuid::new_v4().simple());
            let mut out = vec![];
            for field in fields {
                if !field_name(&field.name) {
                    return Err(RawReply::error(400, "invalid_multipart_name"));
                }
                append(
                    &mut out,
                    format!(
                        "--{boundary}\r\nContent-Disposition: form-data; name=\"{}\"",
                        field.name
                    )
                    .as_bytes(),
                )?;
                match (field.value, field.base64) {
                    (Some(value), None) => {
                        if field.filename.is_some() {
                            return Err(RawReply::error(400, "invalid_multipart_field"));
                        }
                        append(&mut out, b"\r\n\r\n")?;
                        append(&mut out, value.as_bytes())?;
                    }
                    (None, Some(encoded)) => {
                        let filename = safe_filename(field.filename.as_deref().unwrap_or("upload"));
                        let kind = field
                            .content_type
                            .unwrap_or_else(|| "application/octet-stream".into());
                        append(
                            &mut out,
                            format!(
                                "; filename=\"{filename}\"\r\nContent-Type: {}\r\n\r\n",
                                content_type(&kind)?
                            )
                            .as_bytes(),
                        )?;
                        append(&mut out, &decode(&encoded)?)?;
                    }
                    _ => return Err(RawReply::error(400, "invalid_multipart_field")),
                }
                append(&mut out, b"\r\n")?;
            }
            append(&mut out, format!("--{boundary}--\r\n").as_bytes())?;
            Ok((
                Some(format!("multipart/form-data; boundary={boundary}")),
                out,
            ))
        }
    }
}
#[derive(Clone)]
pub struct Prepared {
    pub method: String,
    pub path: String,
    pub headers: HashMap<String, String>,
    kind: Option<String>,
    bytes: Vec<u8>,
}
pub fn prepare(
    method: String,
    path: String,
    headers: HashMap<String, String>,
    body: Option<Body>,
) -> Result<Prepared, RawReply> {
    if !path.starts_with("/api/") || !allowed_request(&method, &path, &headers) {
        return Err(RawReply::error(400, "native_request_denied"));
    }
    if matches!(method.as_str(), "GET" | "HEAD") && body.is_some() {
        return Err(RawReply::error(400, "unexpected_request_body"));
    }
    let (kind, bytes) = encode_body(body)?;
    Ok(Prepared {
        method,
        path,
        headers,
        kind,
        bytes,
    })
}
pub fn send(identity: &RequestIdentity, request: Prepared) -> RawReply {
    let client = match reqwest::blocking::Client::builder().user_agent(crate::http::USER_AGENT)
        .timeout(Duration::from_secs(60))
        .connect_timeout(Duration::from_secs(10))
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .build()
    {
        Ok(c) => c,
        Err(_) => return RawReply::error(0, "network_client_unavailable"),
    };
    let mut outgoing = client
        .request(
            reqwest::Method::from_bytes(request.method.as_bytes()).unwrap(),
            format!("{}{}", identity.gateway, request.path),
        )
        .bearer_auth(&identity.access_token)
        .header("Accept-Encoding", "identity")
        .header("Cache-Control", "no-store");
    for (name, value) in request.headers {
        outgoing = outgoing.header(name, value);
    }
    if let Some(kind) = request.kind {
        outgoing = outgoing.header("Content-Type", kind).body(request.bytes);
    }
    let response = match outgoing.send() {
        Ok(r) => r,
        Err(_) => return RawReply::error(0, "request_outcome_unknown"),
    };
    let status = response.status().as_u16();
    if response
        .content_length()
        .is_some_and(|n| n > MAX_BYTES as u64)
    {
        return RawReply::error(502, "response_too_large");
    }
    if response
        .headers()
        .get("content-encoding")
        .and_then(|h| h.to_str().ok())
        .is_some_and(|h| h != "identity")
    {
        return RawReply::error(502, "unsupported_response_encoding");
    }
    let headers = ["content-type", "content-disposition", "x-request-id"]
        .into_iter()
        .filter_map(|name| {
            response
                .headers()
                .get(name)
                .and_then(|v| v.to_str().ok())
                .filter(|v| v.len() <= 1024)
                .map(|v| (name.to_string(), v.to_string()))
        })
        .collect();
    let mut bytes = vec![];
    if response
        .take(MAX_BYTES as u64 + 1)
        .read_to_end(&mut bytes)
        .is_err()
    {
        return RawReply::error(0, "response_outcome_unknown");
    }
    if bytes.len() > MAX_BYTES {
        return RawReply::error(502, "response_too_large");
    }
    RawReply {
        status,
        body_base64: STANDARD.encode(bytes),
        headers,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn identity(origin: String) -> RequestIdentity {
        RequestIdentity {
            gateway: origin,
            access_token: "fixture-access".into(),
            subject: "usr_a".into(),
            device_id: "device_a".into(),
            expires_at: u64::MAX,
        }
    }
    fn request() -> Prepared {
        prepare("GET".into(), "/api/report".into(), HashMap::new(), None)
            .ok()
            .unwrap()
    }
    #[test]
    fn multipart_names_filenames_and_types_cannot_inject_headers() {
        let fields = vec![Field {
            name: "file".into(),
            value: None,
            filename: Some("../../a\"\r\nInjected: bad.png".into()),
            content_type: Some("image/png".into()),
            base64: Some(STANDARD.encode([0, 255, 1])),
        }];
        let (kind, bytes) = encode_body(Some(Body::Multipart { fields })).ok().unwrap();
        assert!(kind.unwrap().starts_with("multipart/form-data; boundary="));
        let text = String::from_utf8_lossy(&bytes);
        assert!(!text.contains("\r\nInjected:"));
        assert!(!text.contains("../"));
        assert!(text.contains("filename=\"a_Injected_ bad.png\""));
        assert!(bytes.windows(3).any(|b| b == [0, 255, 1]));
        assert!(!field_name("file\r\nInjected"));
        assert!(content_type("text/plain\r\nX-Extra: bad").is_err());
        assert_eq!(safe_filename(".."), "upload");
    }
    #[test]
    fn decoded_and_aggregate_payload_size_are_hard_bounded() {
        assert!(decode(&"A".repeat(MAX_ENCODED + 1)).is_err());
        let fields = vec![
            Field {
                name: "first".into(),
                value: Some("a".repeat(MAX_BYTES / 2)),
                filename: None,
                content_type: None,
                base64: None,
            },
            Field {
                name: "second".into(),
                value: Some("b".repeat(MAX_BYTES / 2)),
                filename: None,
                content_type: None,
                base64: None,
            },
        ];
        assert!(encode_body(Some(Body::Multipart { fields })).is_err());
        assert!(decode("invalid base64").is_err());
    }
    #[test]
    fn paths_auth_headers_and_body_on_get_are_rejected_before_http() {
        for path in [
            "https://evil.test/api/one",
            "/api/../auth/session",
            "/auth/session",
        ] {
            assert!(prepare("GET".into(), path.into(), HashMap::new(), None).is_err());
        }
        assert!(prepare(
            "GET".into(),
            "/api/test".into(),
            HashMap::from([("Authorization".into(), "unsafe".into())]),
            None
        )
        .is_err());
        assert!(prepare(
            "GET".into(),
            "/api/test".into(),
            HashMap::new(),
            Some(Body::Json { value: json!({}) })
        )
        .is_err());
    }
    fn server(response: Vec<u8>) -> (String, std::thread::JoinHandle<String>) {
        use std::{io::Write, net::TcpListener};
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut bytes = [0; 4096];
            let n = stream.read(&mut bytes).unwrap();
            let request = String::from_utf8_lossy(&bytes[..n]).to_string();
            let _ = stream.write_all(&response);
            request
        });
        (format!("http://{addr}"), handle)
    }
    #[test]
    fn real_http_keeps_binary_and_error_body_without_forwarding_cookies() {
        for (status, body) in [
            ("200 OK", vec![0, 255, 12]),
            ("401 Unauthorized", b"{\"error\":\"revoked\"}".to_vec()),
            ("400 Bad Request", b"plain error".to_vec()),
        ] {
            let mut bytes=format!("HTTP/1.1 {status}\r\nContent-Type: application/octet-stream\r\nSet-Cookie: secret=never-export\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",body.len()).into_bytes();
            bytes.extend(&body);
            let (origin, thread) = server(bytes);
            let reply = send(&identity(origin), request());
            assert_eq!(STANDARD.decode(reply.body_base64).unwrap(), body);
            assert_eq!(reply.headers.len(), 1);
            let request = thread.join().unwrap().to_ascii_lowercase();
            assert!(request.contains("authorization: bearer fixture-access"));
            assert!(!request.contains("cookie:"));
        }
    }
    #[test]
    fn redirect_is_returned_without_sending_bearer_to_next_origin() {
        let(origin,thread)=server(b"HTTP/1.1 302 Found\r\nLocation: https://evil.invalid/steal\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_vec());
        assert_eq!(send(&identity(origin), request()).status, 302);
        thread.join().unwrap();
    }
    #[test]
    fn oversized_content_length_and_chunked_body_are_rejected() {
        let (origin, thread) = server(
            format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                MAX_BYTES + 1
            )
            .into_bytes(),
        );
        assert_eq!(send(&identity(origin), request()).status, 502);
        thread.join().unwrap();
        let mut bytes =
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n".to_vec();
        let chunk = vec![b'x'; 1024 * 1024];
        for _ in 0..33 {
            bytes.extend(b"100000\r\n");
            bytes.extend(&chunk);
            bytes.extend(b"\r\n");
        }
        bytes.extend(b"0\r\n\r\n");
        let (origin, thread) = server(bytes);
        assert_eq!(send(&identity(origin), request()).status, 502);
        thread.join().unwrap();
    }
}
