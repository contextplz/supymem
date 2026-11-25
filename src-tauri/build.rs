fn main() {
    println!("cargo:rerun-if-env-changed=SUPYMEM_UPDATE_REPO");
    let repository = std::env::var("SUPYMEM_UPDATE_REPO").ok()
        .filter(|_| std::env::var_os("CARGO_FEATURE_LOCAL_STACK").is_some())
        .unwrap_or_default();
    let parts: Vec<_> = repository.split('/').collect();
    assert!(repository.is_empty() || (parts.len() == 2 && parts.iter().all(|part| !part.is_empty() && *part != "." && *part != ".."
        && part.chars().all(|character| character.is_ascii_alphanumeric() || "-_.".contains(character)))),
        "Set SUPYMEM_UPDATE_REPO to your public GitHub owner/repository or unset it to turn off update checks, then rebuild.");
    println!("cargo:rustc-env=SUPYMEM_OSS_REPOSITORY={repository}");
    build_dictation();
    println!("cargo:rerun-if-changed=tauri.local.json");
    let local_config: serde_json::Value = serde_json::from_str(include_str!("tauri.local.json")).expect("Invalid local configuration");
    let data_name = local_config["productName"].as_str().expect("Set your local product name, then rebuild.");
    let hosted_config: serde_json::Value = serde_json::from_str(include_str!("tauri.conf.json")).expect("Invalid hosted configuration");
    assert!(!data_name.is_empty() && data_name != "." && data_name != ".."
        && !data_name.contains(['/', '\\', '\n', '\r'])
        && !data_name.eq_ignore_ascii_case(hosted_config["productName"].as_str().expect("Hosted product name")),
        "Use a distinct local product name without path separators, then rebuild.");
    println!("cargo:rustc-env=SUPYMEM_LOCAL_DATA_NAME={data_name}");
    let version = if std::env::var_os("CARGO_FEATURE_LOCAL_STACK").is_some() {
        let config = &local_config;
        assert_eq!(config["identifier"].as_str(), Some("com.supymem.local"), "Use your local desktop bundle identity, then rebuild.");
        config["version"].as_str().expect("Set your local app version in tauri.local.json, then rebuild.").to_owned()
    } else {
        std::env::var("CARGO_PKG_VERSION").expect("Cargo package version")
    };
    println!("cargo:rustc-env=SUPYMEM_DESKTOP_VERSION={version}");
    println!("cargo:rerun-if-env-changed=SUPYMEM_DESKTOP_CHANNEL");
    let channel = std::env::var("SUPYMEM_DESKTOP_CHANNEL").unwrap_or_else(|_| "production".into());
    assert!(
        matches!(channel.as_str(), "production" | "staging"),
        "Invalid public desktop channel"
    );
    println!("cargo:rerun-if-env-changed=SUPYMEM_STAGING_SESSION_ONLY");
    let session_only = std::env::var("SUPYMEM_STAGING_SESSION_ONLY").unwrap_or_else(|_| "disabled".into());
    assert!(matches!(session_only.as_str(), "enabled" | "disabled"), "Invalid staging session-storage option");
    assert!(session_only != "enabled" || channel == "staging", "Session-only acceptance storage requires the staging channel");
    println!("cargo:rustc-env=SUPYMEM_STAGING_SESSION_ONLY={session_only}");
    let mut staging_updater = false;
    let mut packaged_local_review = false;
    if let Ok(config) = std::env::var("TAURI_CONFIG") {
        let config: serde_json::Value = serde_json::from_str(&config).expect("Invalid Tauri override JSON");
        let staging_name = config.get("productName").and_then(serde_json::Value::as_str) == Some("Supymem Staging");
        assert_eq!(staging_name, channel == "staging", "Build channel and Tauri packaging override must match");
        packaged_local_review = config.pointer("/bundle/macOS/infoPlist").and_then(serde_json::Value::as_str) == Some("Info.local-review.plist");
        if packaged_local_review {
            assert_eq!(channel, "production", "Packaged local review uses the local-only production code path");
            assert_eq!(config.get("productName").and_then(serde_json::Value::as_str), Some("Supymem Local Review"), "Local review needs its own app name");
            assert_eq!(config.get("identifier").and_then(serde_json::Value::as_str), Some("com.companyos.localreview"), "Local review needs its own bundle identity");
            assert_eq!(config.pointer("/bundle/createUpdaterArtifacts").and_then(serde_json::Value::as_bool), Some(false), "Local review cannot create updater artifacts");
            assert!(config.pointer("/plugins/updater/endpoints").and_then(serde_json::Value::as_array).is_some_and(|items| items.is_empty()), "Local review cannot use an updater endpoint");
        }
        if channel == "staging" {
            let endpoints = config.pointer("/plugins/updater/endpoints").and_then(serde_json::Value::as_array);
            staging_updater = endpoints.is_some_and(|items| items.len() == 1 && items[0].as_str() == Some("https://supymem.com/downloads/staging/latest.json"));
            if endpoints.is_some_and(|items| !items.is_empty()) { assert!(staging_updater, "Unexpected staging update endpoint"); }
            if staging_updater {
                let key = config.pointer("/plugins/updater/pubkey").and_then(serde_json::Value::as_str).expect("Staging updater key required");
                let production: serde_json::Value = serde_json::from_str(include_str!("tauri.conf.json")).unwrap();
                assert_ne!(Some(key), production.pointer("/plugins/updater/pubkey").and_then(serde_json::Value::as_str), "Staging must not use production updater key");
            }
        }
    } else if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("linux")
        && std::env::var_os("CARGO_FEATURE_LOCAL_STACK").is_some()
    {
        // Bare Cargo has no Tauri CLI hook to prepare the runtime payload.
        let mut config = local_config;
        config["bundle"]["resources"] = serde_json::Value::Null;
        config["build"]["devUrl"] = serde_json::Value::Null;
        let config = config.to_string();
        std::env::set_var("TAURI_CONFIG", &config);
        println!("cargo:rustc-env=TAURI_CONFIG={config}");
    }
    println!("cargo:rustc-env=SUPYMEM_DESKTOP_CHANNEL={channel}");
    println!("cargo:rustc-env=SUPYMEM_STAGING_UPDATER={}", if staging_updater { "enabled" } else { "disabled" });
    println!("cargo:rustc-env=SUPYMEM_LOCAL_REVIEW={}", if packaged_local_review { "enabled" } else { "disabled" });
    tauri_build::build()
}

fn build_dictation() {
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("macos") { return; }
    println!("cargo:rerun-if-changed=src/dictation.m");
    let output = std::path::PathBuf::from(std::env::var_os("OUT_DIR").expect("build output"));
    let object = output.join("dictation.o");
    let library = output.join("libsupymem_dictation.a");
    let target = std::env::var("TARGET").expect("build target");
    let status = std::process::Command::new("xcrun")
        .args(["clang", "-fobjc-arc", "-Wall", "-Wextra", "-Werror", "-mmacosx-version-min=11.0", "-target", &target, "-c", "src/dictation.m", "-o"])
        .arg(&object).status().expect("macOS clang is required");
    assert!(status.success(), "Could not compile native dictation");
    let status = std::process::Command::new("xcrun").args(["ar", "crs"])
        .arg(&library).arg(&object).status().expect("macOS ar is required");
    assert!(status.success(), "Could not archive native dictation");
    println!("cargo:rustc-link-search=native={}", output.display());
    println!("cargo:rustc-link-lib=static=supymem_dictation");
    for framework in ["Foundation", "Speech", "AVFoundation"] {
        println!("cargo:rustc-link-lib=framework={framework}");
    }
}
