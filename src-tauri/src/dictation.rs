//! On-device macOS dictation. Transcripts go only to the calling composer.
use serde::Serialize;
use tauri::{State, WebviewWindow};

const UNAVAILABLE: &str = "Dictation is unavailable on this device. Type your message instead.";

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct Transcript {
    session_id: String,
    text: String,
    is_final: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

fn validate_session(session_id: &str) -> Result<(), String> {
    uuid::Uuid::parse_str(session_id)
        .map(|_| ())
        .map_err(|_| "Dictation could not start. Try Dictate again, or type your message.".into())
}

fn locale_text(locale: Option<String>) -> Result<String, String> {
    let locale = locale.unwrap_or_default();
    if locale.len() > 64
        || !locale
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'-' | b'_'))
    {
        return Err(
            "This language is unavailable for dictation. Type your message instead.".into(),
        );
    }
    Ok(locale)
}

fn trusted(window: &WebviewWindow, state: &super::AppState) -> Result<(), String> {
    super::require_native_account_window(window, state)
        .map_err(|_| "Open Supymem to use Dictate.".into())
}

#[tauri::command]
pub async fn dictation_available(
    window: WebviewWindow,
    state: State<'_, super::AppState>,
    locale: Option<String>,
) -> Result<bool, String> {
    trusted(&window, &state)?;
    let locale = locale_text(locale)?;
    #[cfg(target_os = "macos")]
    {
        let (sender, receiver) = tokio::sync::oneshot::channel();
        window
            .run_on_main_thread(move || {
                let locale = std::ffi::CString::new(locale).expect("validated locale");
                let _ = sender.send(unsafe { macos::supymem_dictation_available(locale.as_ptr()) });
            })
            .map_err(|_| UNAVAILABLE.to_string())?;
        receiver.await.map_err(|_| UNAVAILABLE.to_string())
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = locale;
        Ok(false)
    }
}

#[tauri::command]
pub async fn dictation_start(
    window: WebviewWindow,
    state: State<'_, super::AppState>,
    session_id: String,
    locale: Option<String>,
) -> Result<(), String> {
    trusted(&window, &state)?;
    validate_session(&session_id)?;
    let locale = locale_text(locale)?;
    #[cfg(target_os = "macos")]
    {
        macos::start(window, session_id, locale).await
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = locale;
        Err(UNAVAILABLE.into())
    }
}

#[tauri::command]
pub fn dictation_stop(
    window: WebviewWindow,
    state: State<'_, super::AppState>,
    session_id: String,
) -> Result<(), String> {
    trusted(&window, &state)?;
    validate_session(&session_id)?;
    #[cfg(target_os = "macos")]
    {
        let session = std::ffi::CString::new(session_id).expect("validated session");
        unsafe { macos::supymem_dictation_stop(session.as_ptr()) };
    }
    Ok(())
}

#[cfg(target_os = "macos")]
pub fn shutdown() {
    // RunEvent::Exit runs on the main thread, where the native session lives.
    unsafe { macos::supymem_dictation_shutdown() };
}

#[cfg(target_os = "macos")]
mod macos {
    use super::Transcript;
    use std::ffi::{c_char, c_int, c_void, CStr, CString};
    use tauri::{Emitter, WebviewWindow};
    use tokio::sync::oneshot;

    extern "C" {
        pub fn supymem_dictation_available(locale: *const c_char) -> bool;
        fn supymem_dictation_start(
            session: *const c_char,
            locale: *const c_char,
            context: *mut c_void,
            callback: extern "C" fn(*mut c_void, c_int, *const c_char, *const c_char),
        );
        pub fn supymem_dictation_stop(session: *const c_char);
        pub fn supymem_dictation_shutdown();
    }

    struct Context {
        window: WebviewWindow,
        session_id: String,
        started: Option<oneshot::Sender<Result<(), String>>>,
    }

    fn string(pointer: *const c_char) -> Option<String> {
        if pointer.is_null() {
            None
        } else {
            Some(
                unsafe { CStr::from_ptr(pointer) }
                    .to_string_lossy()
                    .into_owned(),
            )
        }
    }

    extern "C" fn receive(
        context: *mut c_void,
        kind: c_int,
        text: *const c_char,
        error: *const c_char,
    ) {
        // Native callbacks are serialized on the main queue. Native owns this
        // Box until its single terminal callback; no transcripts are logged.
        let context = unsafe { &mut *context.cast::<Context>() };
        if kind == 0 {
            if let Some(started) = context.started.take() {
                let _ = started.send(Ok(()));
            }
            return;
        }
        let error = string(error);
        let terminal = kind == 2;
        let _ = context.window.emit_to(
            context.window.label(),
            "dictation-transcript",
            Transcript {
                session_id: context.session_id.clone(),
                text: string(text).unwrap_or_default(),
                is_final: terminal,
                error: error.clone(),
            },
        );
        if terminal {
            if let Some(started) = context.started.take() {
                let _ = started.send(Err(error.unwrap_or_else(|| {
                    "Dictation stopped. Try Dictate again, or type your message.".into()
                })));
            }
            unsafe {
                drop(Box::from_raw(context as *mut Context));
            }
        }
    }

    pub async fn start(
        window: WebviewWindow,
        session_id: String,
        locale: String,
    ) -> Result<(), String> {
        let (sender, receiver) = oneshot::channel();
        let session = CString::new(session_id.as_str()).expect("validated session");
        let locale = CString::new(locale).expect("validated locale");
        let context = Box::new(Context {
            window,
            session_id,
            started: Some(sender),
        });
        unsafe {
            supymem_dictation_start(
                session.as_ptr(),
                locale.as_ptr(),
                Box::into_raw(context).cast(),
                receive,
            )
        };
        receiver.await.map_err(|_| super::UNAVAILABLE.to_string())?
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn final_transcript_matches_the_composer_event_contract() {
        let transcript = Transcript {
            session_id: "d5dd86db-5647-4025-8d81-b6070046319a".into(),
            text: "an idea".into(),
            is_final: true,
            error: None,
        };
        let value = serde_json::to_value(transcript).unwrap();
        assert_eq!(
            value,
            serde_json::json!({
                "sessionId": "d5dd86db-5647-4025-8d81-b6070046319a",
                "text": "an idea",
                "isFinal": true,
            })
        );
    }

    #[test]
    fn locale_and_session_inputs_are_bounded_and_c_string_safe() {
        assert!(validate_session("not a session").is_err());
        assert!(validate_session("d5dd86db-5647-4025-8d81-b6070046319a").is_ok());
        assert_eq!(locale_text(Some("en-IN".into())).unwrap(), "en-IN");
        assert_eq!(locale_text(None).unwrap(), "");
        assert!(locale_text(Some("en\0US".into())).is_err());
        assert!(locale_text(Some("a".repeat(65))).is_err());
    }

    #[test]
    fn every_bundle_has_speech_and_microphone_purpose_strings() {
        for source in [
            include_str!("../Info.plist"),
            include_str!("../Info.staging.plist"),
            include_str!("../Info.local-review.plist"),
        ] {
            let info = plist::Value::from_reader_xml(source.as_bytes()).unwrap();
            let info = info.as_dictionary().unwrap();
            for key in [
                "NSSpeechRecognitionUsageDescription",
                "NSMicrophoneUsageDescription",
            ] {
                assert!(!info[key].as_string().unwrap().trim().is_empty());
            }
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn native_lifecycle_preserves_final_text_and_ignores_late_callbacks() {
        let executable =
            std::env::temp_dir().join(format!("supymem-dictation-test-{}", std::process::id()));
        let status = std::process::Command::new("xcrun")
            .args([
                "clang",
                "-fobjc-arc",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-framework",
                "Foundation",
                "-framework",
                "Speech",
                "-framework",
                "AVFoundation",
                "src/dictation_test.m",
                "-o",
            ])
            .arg(&executable)
            .current_dir(env!("CARGO_MANIFEST_DIR"))
            .status()
            .unwrap();
        assert!(
            status.success(),
            "native bridge lifecycle test must compile"
        );
        let result = std::process::Command::new(&executable).status().unwrap();
        let _ = std::fs::remove_file(executable);
        assert!(result.success(), "native bridge lifecycle checks must pass");
    }
}
