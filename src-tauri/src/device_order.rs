//! Native interpretation of a server-fetched work order, never a WebView grant.
//! Reading a record does not claim it or authorize a process or desktop effect.
use serde::{Deserialize, Serialize};
use std::collections::HashSet;

#[derive(Debug, Serialize)]
pub struct NativeWorkOrderCapabilities {
    pub browser_cuai_text_v1: bool,
    pub pagebuilder_reference_v1: bool,
    pub browser_cuai_text_v2: bool,
    pub browser_cuai_read_only_v1: bool,
    pub browser_qualification_v1: bool,
    pub product_build_v1: bool,
}

/// Compiled closed-wire support only; no worker, model or execution authority.
pub fn native_wire_capabilities() -> NativeWorkOrderCapabilities {
    NativeWorkOrderCapabilities { browser_cuai_text_v1: true, pagebuilder_reference_v1: true, browser_cuai_text_v2: true, browser_cuai_read_only_v1: true, browser_qualification_v1: true, product_build_v1: true }
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum OrderState { Pending, Approved, Claimed, WaitingForDevice, Cancelled, Stopped }

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BrowserVerification {
    pub expected_locations: Vec<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct GeneralBrowserVerification {
    pub operation: String,
    pub start_url: String,
    #[serde(default = "default_allow_type")]
    pub allow_type: bool,
    #[serde(default = "default_max_steps")]
    pub max_steps: u64,
    #[serde(default)]
    pub fill_values: std::collections::BTreeMap<String, String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub read_urls: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub execution_profile: Option<BrowserAIProfile>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BrowserAIRole {
    pub provider: String,
    pub model: String,
    pub service_tier: String,
    pub reasoning: String,
    pub max_output_tokens: u64,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BrowserAIRoles {
    pub plan: BrowserAIRole,
    pub actor: BrowserAIRole,
    pub verify: BrowserAIRole,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BrowserAIProfile {
    pub version: String,
    pub backend: String,
    pub policy_version: u64,
    pub roles: BrowserAIRoles,
    pub pricing_digest: String,
    pub max_provider_calls: u64,
    pub max_request_bytes: u64,
    pub completion_policy: String,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ReviewedDesktopSurface {
    pub application_bundle: String,
    pub process_id: u32,
    pub process_start_identity: String,
    pub window_id: u32,
    pub window_title: String,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ReviewedModalParent {
    pub application_bundle: String,
    pub process_id: u32,
    pub process_start_identity: String,
    pub window_id: u32,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ReviewedModalPolicy {
    pub operation: String,
    pub parents: Vec<ReviewedModalParent>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DesktopVerification {
    pub operation: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub modal_policy: Option<ReviewedModalPolicy>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reviewed_surfaces: Option<Vec<ReviewedDesktopSurface>>,
    pub application_bundle: String,
    pub process_id: u32,
    pub process_start_identity: String,
    pub window_id: u32,
    pub window_title: String,
    pub delivery_mode: String,
    pub max_steps: u64,
    pub reviewed_text: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub document_text_index: Option<u8>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub founder_goal: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expected_visible_text: Option<String>,
    #[serde(default)]
    pub visual_review: bool,
}

fn default_allow_type() -> bool { true }
fn default_max_steps() -> u64 { 32 }

fn browser_ai_profile_valid(profile: &BrowserAIProfile) -> bool {
    // Parsing a saved profile does not select a model or grant execution. Core
    // configuration and the exact advertised profile still gate admission.
    ((profile.version == "browser_cuai_text_v1" && profile.policy_version == 1)
        || (profile.version == "browser_cuai_text_v2" && profile.policy_version == 2))
        && profile.backend == "cuai"
        && profile.completion_policy == "browser_semantic_review_v1"
        && profile.pricing_digest.len() == 64
        && profile.pricing_digest.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        && (1..=128).contains(&profile.max_provider_calls)
        && (1024..=65536).contains(&profile.max_request_bytes)
        && [&profile.roles.plan, &profile.roles.actor, &profile.roles.verify].iter().enumerate().all(|(phase, role)| {
            role.provider == "openai" && role.service_tier == "default"
                && ((role.reasoning == "none" && role.model != "gpt-6.1-sol")
                    || (phase == 1 && role.model == "gpt-6.1-sol" && role.reasoning == "low"))
                && (1..=2500).contains(&role.max_output_tokens)
                && (1..=100).contains(&role.model.len())
                && role.model.bytes().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_' | b'-'))
        })
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TerminalVerification {
    pub operation: String,
    pub workspace_slug: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CodingVerification {
    pub operation: String,
    pub provider: String,
    pub workspace_slug: String,
    pub read_scope: String,
    pub write_scope: String,
    pub command_network: String,
    // Historical reviews may omit this field, but execution still requires
    // the server's current saved contract and a separately approved claim.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub offline_profile: Option<CodingOfflineProfile>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pagebuilder_candidate: Option<PagebuilderCandidate>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CodingLoopVerification {
    pub operation: String,
    pub provider: String,
    pub workspace_slug: String,
    pub profile: String,
    pub check_recipe: String,
    pub network: String,
    pub read_scope: String,
    pub write_scope: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub phase: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub plan_order_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub plan_digest: Option<String>,
    #[serde(default = "default_loop_iterations")]
    pub max_iterations: u64,
    #[serde(default = "default_loop_wall_seconds")]
    pub max_wall_seconds: u64,
    #[serde(default = "default_loop_call_seconds")]
    pub per_call_seconds: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_turns: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pagebuilder_candidate: Option<PagebuilderCandidate>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DeployVerification {
    pub operation: String,
    pub provider: String,
    pub target: String,
    pub workspace_slug: String,
    pub source_order_id: String,
    pub source_commit: String,
    pub file_manifest_sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub project_link_sha256: Option<String>,
}

fn default_loop_iterations() -> u64 { 6 }
fn default_loop_wall_seconds() -> u64 { 2700 }
fn default_loop_call_seconds() -> u64 { 720 }

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PagebuilderCandidate {
    pub schema_version: u8,
    pub job_id: String,
    pub variant: u8,
    pub revision_id: String,
    pub workspace_id: String,
    pub venture_id: String,
    pub app_id: String,
    pub source_sha256: String,
    pub source_manifest_digest: String,
    pub source_size_bytes: u64,
    pub spec_sha256: String,
    pub reference_sha256: String,
    pub reference_size_bytes: u64,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CodingOfflineProfile { StaticHtmlV1 }

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct InspectionVerification {
    pub operation: String,
    pub provider: String,
    pub workspace_slug: String,
    pub read_scope: String,
    pub write_scope: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AppNavigationVerification {
    pub operation: String,
    pub route: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub task_id: String,
    pub title: String,
    pub instruction: String,
    pub applications: Vec<String>,
    pub origins: Vec<String>,
    pub actions: Vec<String>,
    pub browser_verification: Option<BrowserVerification>,
    pub general_browser_verification: Option<GeneralBrowserVerification>,
    pub desktop_verification: Option<DesktopVerification>,
    pub terminal_verification: Option<TerminalVerification>,
    pub coding_verification: Option<CodingVerification>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub coding_loop_verification: Option<CodingLoopVerification>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deploy_verification: Option<DeployVerification>,
    pub inspection_verification: Option<InspectionVerification>,
    pub app_navigation_verification: Option<AppNavigationVerification>,
    pub credit_ceiling_micro: u64,
    pub duration_seconds: u64,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct WorkOrder {
    pub id: String,
    pub workspace_id: String,
    pub venture_id: String,
    pub actor_id: String,
    pub device_id: String,
    pub state: OrderState,
    pub review_digest: String,
    pub expires_at: String,
    pub claimed_at: Option<String>,
    pub manifest: Manifest,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub browser_qualification_grant: Option<OwnedFixtureGrant>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub browser_qualification_trial_binding: Option<QualificationTrialBinding>,
}

// A parsed controller scope is reviewed metadata, never execution authority.
// Core, the owned browser and the fixture handler separately admit each effect.
#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureField {
    pub name: String,
    pub value: serde_json::Value,
    pub control_id: String,
    pub kind: String,
    #[serde(default)]
    pub input_value: Option<String>,
    #[serde(default)]
    pub input_type: Option<String>,
}
#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureRule {
    pub rule_id: String, pub method: String, pub path: String, pub query: String,
    pub document_path: String, pub document_id: String, pub form_id: String, pub control_id: String,
    pub fields: Vec<FixtureField>, pub max_requests: u16, pub max_commits: u8,
    pub prerequisites: Vec<String>,
    #[serde(default)]
    pub rejected_retry_limit: u8,
    #[serde(default = "qualification_form_body")]
    pub body_kind: String,
}
fn qualification_form_body() -> String { "form".into() }
#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureFrame { pub document_path: String, pub parent_document_path: String, pub frame_id: String }
#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureDownload { pub rule_id: String, pub filename: String, pub media_type: String, pub max_bytes: u64, pub sha256: String }
#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct QualificationTrialBinding {
    pub schema_version: u8,
    pub actor_id: String,
    pub device_id: String,
    pub workspace_id: String,
    pub venture_id: String,
    pub trial_id: String,
    pub catalog_digest: String,
    pub task_id: String,
    pub seed_digest: String,
    pub order_id: String,
    pub review_digest: String,
    pub profile_digest: String,
    pub expires_at: u64,
    pub binding_digest: String,
}
#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct OwnedFixtureGrant {
    pub schema_version: u8,
    pub actor_id: String,
    pub device_id: String,
    pub workspace_id: String,
    pub venture_id: String,
    pub trial_id: String,
    pub catalog_digest: String,
    pub task_id: String,
    pub seed_digest: String,
    pub order_id: String,
    pub review_digest: String,
    pub profile_digest: String,
    pub expires_at: u64,
    pub nonce: String, pub origin: String, pub rules: Vec<FixtureRule>,
    #[serde(default)]
    pub allowed_operations: Vec<String>,
    #[serde(default = "qualification_one_tab")]
    pub max_tabs: u8,
    #[serde(default)]
    pub frames: Vec<FixtureFrame>,
    #[serde(default)]
    pub downloads: Vec<FixtureDownload>,
    pub grant_digest: String,
}
fn qualification_one_tab() -> u8 { 1 }
fn qualification_id(v: &str) -> bool { (1..=128).contains(&v.len()) && v.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b,b'_'|b'-')) }
fn qualification_hash(v: &str) -> bool { v.len()==64 && v.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) }
fn qualification_text(v: &str, min: usize) -> bool { (min..=600).contains(&v.chars().count()) && !v.chars().any(char::is_control) }
fn qualification_path(v: &str) -> bool {
    v.len()<=512 && v.starts_with("/public/browser-qualification/")
        && v.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b,b'/'|b'_'|b'-'|b'.'))
        && v.split('/').skip(1).all(|part| !matches!(part,""|"."|".."))
}
fn qualification_task(v: &str) -> bool { matches!(v,
    "read_fact"|"search_price"|"multitab"|"form_valid"|"form_recover"|"download"|"paginate"|"todos"|"counter"|"delayed"|"iframe"|"new_tab_link"|"scroll_footer"|"cookie_banner"|"select_prefs"|"cheapest_item"|"load_more"|"table_max"|"wizard"|"faq_details"|"library_all"|"directory_email"|"login_creds"|"login_wall"|"captcha_wall"|"pay_wall"|"l01_research_synthesis"|"l02_wizard_back_forward_validation"|"l03_mini_shop_stop_at_payment"|"l04_ten_page_collection"|"l05_multitab_compare_and_fill"|"l06_recover_from_error_page"|"qualify_compare_create"|"refresh_replan"|"bundle_exact_progress"|"explore_current_reference") }
fn qualification_operation(v: &str) -> bool { matches!(v,"click"|"type"|"wait"|"navigate"|"scroll"|"extract"|"select"|"check"|"new_tab"|"switch_tab"|"close_tab"|"download"|"hover") }
fn canonical_digest(mut v: serde_json::Value, excluded: Option<&str>) -> Option<String> {
    use sha2::{Digest,Sha256};
    if let Some(key)=excluded { v.as_object_mut()?.remove(key); }
    fn sorted(v: serde_json::Value) -> serde_json::Value {
        match v {
            serde_json::Value::Object(m) => serde_json::Value::Object(m.into_iter().collect::<std::collections::BTreeMap<_,_>>().into_iter().map(|(k,v)|(k,sorted(v))).collect()),
            serde_json::Value::Array(a) => serde_json::Value::Array(a.into_iter().map(sorted).collect()),
            other => other,
        }
    }
    Some(format!("{:x}",Sha256::digest(serde_json::to_vec(&sorted(v)).ok()?)))
}
fn qualification_field_valid(f: &FixtureField) -> bool {
    let sensitive=["password","passwd","otp","captcha","card","cvc","cvv","terms","token","secret","account"];
    if !qualification_id(&f.name) || !qualification_id(&f.control_id)
        || sensitive.iter().any(|s| f.name.to_lowercase().contains(s))
        || !matches!(f.kind.as_str(),"text"|"email"|"synthetic_code"|"synthetic_billing_contact"|"select"|"radio"|"checkbox"|"json_literal") { return false; }
    match (&f.input_value,&f.input_type) {
        (None,None) => {},
        (Some(v),Some(t)) if qualification_text(v,1) && matches!(t.as_str(),"text"|"email"|"tel"|"textarea") => {
            if matches!(f.kind.as_str(),"email"|"synthetic_billing_contact"|"synthetic_code")
                && (f.value.as_str()!=Some(v) || if f.kind=="synthetic_code" { t!="text" } else { !matches!(t.as_str(),"text"|"email") }) { return false; }
        },
        _ => return false,
    }
    if f.kind=="json_literal" {
        return match &f.value {
            serde_json::Value::Bool(_) => true,
            serde_json::Value::Number(n) => n.as_i64().is_some_and(|v| (-1_000_000..=1_000_000).contains(&v)),
            serde_json::Value::String(s) => qualification_text(s,0),
            serde_json::Value::Array(a) => a.len()<=16 && a.iter().all(|v| v.as_str().is_some_and(|s| qualification_text(s,0))),
            _ => false,
        };
    }
    let Some(v)=f.value.as_str() else { return false };
    if !qualification_text(v,1) { return false; }
    if matches!(f.kind.as_str(),"email"|"synthetic_billing_contact") {
        return v.strip_suffix("@example.invalid").is_some_and(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b,b'.'|b'_'|b'+'|b'-')));
    }
    if f.kind=="synthetic_code" { return matches!(f.name.as_str(),"code"|"prev") && (1..=64).contains(&v.len()) && qualification_id(v); }
    f.kind!="checkbox" || matches!(v,"yes"|"no")
}
fn qualification_rule_valid(r: &FixtureRule) -> bool {
    let pairs: Vec<_>=url::form_urlencoded::parse(r.query.as_bytes()).collect();
    let query=url::form_urlencoded::Serializer::new(String::new()).extend_pairs(pairs.iter().map(|(k,v)|(k.as_ref(),v.as_ref()))).finish();
    if !qualification_id(&r.rule_id) || !matches!(r.method.as_str(),"GET"|"HEAD"|"POST")
        || !qualification_path(&r.path) || !qualification_path(&r.document_path) || !qualification_id(&r.document_id)
        || r.query.len()>512 || query!=r.query || pairs.iter().map(|(k,_)|k).collect::<HashSet<_>>().len()!=pairs.len()
        || !matches!(r.body_kind.as_str(),"form"|"json") || r.fields.len()>16 || !r.fields.iter().all(qualification_field_valid)
        || r.fields.iter().map(|f|&f.name).collect::<HashSet<_>>().len()!=r.fields.len()
        || r.fields.iter().map(|f|&f.control_id).collect::<HashSet<_>>().len()!=r.fields.len()
        || !(1..=128).contains(&r.max_requests) || r.max_commits>16 || u16::from(r.max_commits)>r.max_requests || r.rejected_retry_limit>1
        || r.prerequisites.len()>36 || !distinct(&r.prerequisites) || !r.prerequisites.iter().all(|v| qualification_id(v) && v!=&r.rule_id) { return false; }
    if r.method=="POST" {
        qualification_id(&r.form_id) && qualification_id(&r.control_id) && (r.max_commits>0 || r.rejected_retry_limit==1)
            && (r.body_kind=="json" || r.fields.iter().all(|f| f.value.is_string()))
    } else {
        r.max_commits==0 && r.rejected_retry_limit==0 && if r.fields.is_empty() { r.form_id.is_empty() && r.control_id.is_empty() }
            else { qualification_id(&r.form_id) && qualification_id(&r.control_id) && r.fields.iter().all(|f|
                f.value.as_str().is_some_and(|v| pairs.iter().any(|(k,p)| k==&f.name && p==v))) }
    }
}
fn qualification_common_valid(v: &serde_json::Value, record: &WorkOrder, device: &str) -> bool {
    let ids=["actor_id","device_id","workspace_id","venture_id","trial_id","order_id"];
    let hashes=["catalog_digest","seed_digest","review_digest","profile_digest"];
    let Some(profile)=record.manifest.general_browser_verification.as_ref().and_then(|s|s.execution_profile.as_ref()) else { return false };
    let Some(profile_digest)=serde_json::to_value(profile).ok().and_then(|v| canonical_digest(v,None)) else { return false };
    let now=std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d|d.as_secs()).unwrap_or(u64::MAX);
    let expires=v["expires_at"].as_u64().unwrap_or(0);
    v["schema_version"]==1 && ids.iter().all(|k| v[k].as_str().is_some_and(qualification_id))
        && hashes.iter().all(|k| v[k].as_str().is_some_and(qualification_hash))
        && v["actor_id"]==record.actor_id && v["device_id"]==record.device_id && v["device_id"]==device
        && v["workspace_id"]==record.workspace_id && v["venture_id"]==record.venture_id
        && v["order_id"]==record.id && v["review_digest"]==record.review_digest && v["profile_digest"]==profile_digest
        && v["task_id"].as_str().is_some_and(qualification_task) && (1..=32_503_680_000).contains(&expires)
        && (matches!(record.state,OrderState::Stopped|OrderState::Cancelled) || now<expires)
}
fn qualification_valid(record: &WorkOrder, device: &str) -> bool {
    let binding=record.browser_qualification_trial_binding.as_ref();
    let grant=record.browser_qualification_grant.as_ref();
    if let Some(b)=binding {
        let Ok(v)=serde_json::to_value(b) else { return false };
        if !qualification_common_valid(&v,record,device) || canonical_digest(v,Some("binding_digest")).as_deref()!=Some(&b.binding_digest) { return false; }
    }
    let Some(g)=grant else { return true };
    let Ok(v)=serde_json::to_value(g) else { return false };
    if !qualification_common_valid(&v,record,device) || canonical_digest(v.clone(),Some("grant_digest")).as_deref()!=Some(&g.grant_digest)
        || !qualification_id(&g.nonce) || g.nonce.len()<32 || g.origin!="https://api.supymem.com"
        || !record.manifest.origins.contains(&g.origin)
        || !record.manifest.general_browser_verification.as_ref().is_some_and(|spec|
            g.rules.iter().any(|r| r.method=="GET" && spec.start_url==format!("{}{}{}",g.origin,r.path,if r.query.is_empty() { String::new() } else { format!("?{}",r.query) })))
        || !(1..=128).contains(&g.rules.len()) || !g.rules.iter().all(qualification_rule_valid)
        || !distinct(&g.allowed_operations) || !g.allowed_operations.iter().all(|s| qualification_operation(s))
        || !(1..=4).contains(&g.max_tabs) || g.max_tabs>1 && !g.allowed_operations.iter().any(|s|s=="new_tab")
        || g.frames.len()>2 || g.downloads.len()>1 { return false; }
    if let Some(b)=binding {
        let Ok(mut bv)=serde_json::to_value(b) else { return false };
        bv.as_object_mut().unwrap().remove("binding_digest");
        if bv.as_object().unwrap().iter().any(|(k,x)| &v[k]!=x) { return false; }
    }
    let prefix=format!("/public/browser-qualification/{}/",g.nonce);
    let mut ids=HashSet::new(); let mut routes=HashSet::new();
    for r in &g.rules {
        if !r.path.starts_with(&prefix) || !r.document_path.starts_with(&prefix)
            || !r.prerequisites.iter().all(|id|ids.contains(id)) || !ids.insert(r.rule_id.clone())
            || !routes.insert((r.method.clone(),r.path.clone(),r.query.clone(),serde_json::to_string(&r.fields).unwrap())) { return false; }
    }
    if !g.frames.iter().all(|f| qualification_path(&f.document_path) && qualification_path(&f.parent_document_path)
        && f.document_path.starts_with(&prefix) && f.parent_document_path.starts_with(&prefix)
        && f.document_path!=f.parent_document_path && qualification_id(&f.frame_id)) { return false; }
    if !g.downloads.iter().all(|d| d.filename.strip_suffix(".csv").is_some_and(|s| (1..=80).contains(&s.len()) && qualification_id(s))
        && d.media_type=="text/csv" && (1..=1_048_576).contains(&d.max_bytes) && qualification_hash(&d.sha256)
        && g.rules.iter().any(|r|r.rule_id==d.rule_id && r.method=="GET") && g.allowed_operations.iter().any(|s|s=="download")) { return false; }
    !matches!(g.task_id.as_str(),"login_creds"|"login_wall"|"captcha_wall"|"pay_wall")
        || g.rules.iter().all(|r|r.method!="POST") && g.downloads.is_empty()
            && !g.allowed_operations.iter().any(|s|matches!(s.as_str(),"click"|"type"|"select"|"check"|"download"))
}

fn bounded(value: &str, max: usize) -> bool {
    !value.is_empty() && value.chars().count() <= max && !value.contains('\0')
}

fn reviewed_desktop_surfaces_valid(v: &DesktopVerification, applications: &[String]) -> bool {
    reviewed_desktop_surfaces_valid_with_gate(v, applications,
        std::env::var("SUPYMEM_REVIEWED_DESKTOP_V2").as_deref() == Ok("1"))
}

fn reviewed_modal_policy_valid(v: &DesktopVerification, enabled: bool) -> bool {
    let Some(policy) = v.modal_policy.as_ref() else { return true };
    if !enabled || v.delivery_mode != "background" || policy.operation != "exact_parent_children_v1"
        || !(1..=8).contains(&policy.parents.len()) { return false; }
    let mut pairs = HashSet::new();
    policy.parents.iter().all(|parent| {
        if !pairs.insert((parent.process_id,parent.window_id)) { return false; }
        let matches = |bundle: &str,pid: u32,start: &str,wid: u32| {
            parent.application_bundle == bundle && parent.process_id == pid
                && parent.process_start_identity == start && parent.window_id == wid
        };
        if v.operation == "desktop_goal_v1" {
            matches(&v.application_bundle,v.process_id,&v.process_start_identity,v.window_id)
        } else if v.operation == "desktop_goal_v2" {
            v.reviewed_surfaces.as_ref().is_some_and(|rows| rows.iter().any(|row|
                matches(&row.application_bundle,row.process_id,&row.process_start_identity,row.window_id)))
        } else { false }
    })
}

fn reviewed_desktop_surfaces_valid_with_gate(v: &DesktopVerification, applications: &[String], enabled: bool) -> bool {
    if !reviewed_modal_policy_valid(v,
        std::env::var("SUPYMEM_REVIEWED_MODAL_CHILDREN").as_deref() == Ok("1")) { return false; }
    if v.operation == "desktop_goal_v1" {
        return v.reviewed_surfaces.is_none() && applications == [v.application_bundle.as_str()];
    }
    if v.operation != "desktop_goal_v2" || v.delivery_mode != "background"
        || !enabled { return false; }
    let Some(rows) = v.reviewed_surfaces.as_ref() else { return false };
    if !(1..=8).contains(&rows.len()) { return false; }
    let first = &rows[0];
    if first.application_bundle != v.application_bundle || first.process_id != v.process_id
        || first.process_start_identity != v.process_start_identity || first.window_id != v.window_id
        || first.window_title != v.window_title { return false; }
    let mut pairs = HashSet::new();
    let mut bundles = Vec::new();
    for row in rows {
        if !bundle_id(&row.application_bundle) || row.process_id == 0 || row.window_id == 0
            || !(1..=24).contains(&row.process_start_identity.len())
            || !row.process_start_identity.bytes().all(|b| b.is_ascii_digit())
            || row.window_title.len() > 200
            || !pairs.insert((row.process_id, row.window_id)) { return false; }
        if rows.iter().any(|other| (other.process_id == row.process_id || other.application_bundle == row.application_bundle)
            && (other.process_id != row.process_id || other.application_bundle != row.application_bundle
                || other.process_start_identity != row.process_start_identity)) { return false; }
        if !bundles.contains(&row.application_bundle) { bundles.push(row.application_bundle.clone()); }
    }
    applications == bundles
}

fn reviewed_desktop_text(verification: &DesktopVerification) -> bool {
    if let Some(index) = verification.document_text_index {
        if index != 0 || verification.application_bundle != "com.apple.TextEdit"
            || verification.reviewed_text.is_empty() {
            return false;
        }
    }
    verification.reviewed_text.iter().enumerate().all(|(index, value)| {
        if !bounded(value, 320) { return false; }
        if verification.document_text_index == Some(index as u8) {
            let lines: Vec<_> = value.split('\n').collect();
            lines.len() == 3 && lines.iter().all(|line| !line.trim().is_empty() && *line == line.trim())
                && !value.chars().any(|c| (c.is_control() && c != '\n') || matches!(c, '\u{2028}' | '\u{2029}'))
        } else {
            !value.chars().any(char::is_control)
        }
    })
}

fn distinct(values: &[String]) -> bool {
    values.iter().collect::<HashSet<_>>().len() == values.len()
}

fn bundle_id(value: &str) -> bool {
    let parts: Vec<_> = value.split('.').collect();
    parts.len() >= 2 && parts.iter().all(|part| !part.is_empty()
        && part.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-'))
}

fn exact_origin(value: &str) -> bool {
    if value.contains('*') || value.contains('\\') || value.chars().any(|c| c.is_control() || c.is_whitespace()) {
        return false;
    }
    let Ok(url) = url::Url::parse(value) else { return false; };
    // url normalizes an absent path to '/'; compare the raw authority as well.
    url.scheme() == "https" && url.host_str().is_some() && url.username().is_empty()
        && url.password().is_none() && url.query().is_none() && url.fragment().is_none()
        && url.path() == "/" && value.strip_prefix("https://").is_some_and(|tail|
            !tail.is_empty() && !tail.contains('/') && !tail.contains('@'))
}

fn exact_location(value: &str) -> Option<url::Url> {
    if value.len() > 500 || value.contains('\\')
        || value.chars().any(|c| c.is_control() || c.is_whitespace()) {
        return None;
    }
    let url = url::Url::parse(value).ok()?;
    if url.scheme() != "https" || url.host_str().is_none() || !url.username().is_empty()
        || url.password().is_some() || url.fragment().is_some() {
        return None;
    }
    Some(url)
}

fn workspace_slug(value: &str) -> bool {
    !value.is_empty() && value.len() <= 64 && !value.starts_with('-') && !value.ends_with('-')
        && value.bytes().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'-')
        && !value.contains("--")
}

fn hex_digest(value: &str, length: usize) -> bool {
    value.len() == length && value.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

fn product_order_id(value: &str) -> bool {
    value.len() <= 128 && value.strip_prefix("dwo_").is_some_and(|tail|
        !tail.is_empty() && tail.bytes().all(|c| c.is_ascii_alphanumeric()))
}

fn candidate_binding_valid(candidate: &PagebuilderCandidate, record: &WorkOrder, venture: &str) -> bool {
    candidate.schema_version == 1 && candidate.variant <= 2
        && candidate.workspace_id == record.workspace_id && candidate.venture_id == venture
        && [&candidate.job_id, &candidate.revision_id, &candidate.workspace_id,
            &candidate.venture_id, &candidate.app_id].iter().all(|value| {
                (1..=128).contains(&value.len()) && value.bytes().all(|c|
                    c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-'))
            })
        && [&candidate.source_sha256, &candidate.source_manifest_digest,
            &candidate.spec_sha256, &candidate.reference_sha256].iter().all(|value|
                value.len() == 64 && value.bytes().all(|c|
                    c.is_ascii_digit() || (b'a'..=b'f').contains(&c)))
        && (1..=32 * 1024 * 1024).contains(&candidate.source_size_bytes)
        && (1..=32 * 1024 * 1024).contains(&candidate.reference_size_bytes)
}

pub fn review_path(venture: &str, order: &str) -> Result<String, String> {
    let safe_id = |value: &str| bounded(value, 128)
        && value.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-');
    if !safe_id(venture) || !safe_id(order) { return Err("device_order_identifier_invalid".into()); }
    Ok(format!("/api/ventures/{venture}/device-work-orders/{order}"))
}

pub fn validate_review(value: serde_json::Value, venture: &str, order: &str, device: &str) -> Result<WorkOrder, String> {
    if value.to_string().len() > 262144 { return Err("device_order_response_bounds".into()); }
    let record: WorkOrder = serde_json::from_value(value).map_err(|_| "device_order_response_invalid")?;
    let rotated_recovery_view = record.device_id != device
        && (record.claimed_at.is_none()
            && matches!(&record.state, OrderState::Pending | OrderState::Approved)
            || record.claimed_at.is_some()
            && matches!(&record.state, OrderState::Cancelled | OrderState::Stopped));
    if record.id != order || record.venture_id != venture
        || (record.device_id != device && !rotated_recovery_view) {
        return Err("device_order_identity_mismatch".into());
    }
    let m = &record.manifest;
    let verification_valid = m.browser_verification.as_ref().is_none_or(|verification| {
        !verification.expected_locations.is_empty()
            && verification.expected_locations.len() <= 20
            && distinct(&verification.expected_locations)
            && m.actions.iter().any(|value| value == "browser.read")
            && verification.expected_locations.iter().all(|value| {
                exact_location(value).is_some_and(|location| {
                    m.origins.iter().any(|approved| exact_location(approved).is_some_and(
                        |origin| origin.origin() == location.origin()))
                })
            })
    });
    let general_valid = m.general_browser_verification.as_ref().is_none_or(|verification| {
        verification.operation == "general_goal_v1"
            && (1..=64).contains(&verification.max_steps)
            && verification.execution_profile.as_ref().is_none_or(browser_ai_profile_valid)
            && verification.fill_values.len() <= 8
            && verification.fill_values.keys().all(|key| matches!(key.as_str(), "email" | "name" | "text" | "tel"))
            && verification.fill_values.values().all(|value| !value.is_empty() && value.len() <= 320)
            && verification.read_urls.len() <= 6 && distinct(&verification.read_urls)
            && (verification.read_urls.is_empty() || (m.actions == ["browser.read"]
                && verification.execution_profile.is_some() && !verification.allow_type && verification.fill_values.is_empty()
                && verification.read_urls.iter().all(|value| exact_location(value).is_some_and(|location|
                    location.as_str() == value.as_str() && location.query().is_none()
                    && m.origins.iter().any(|approved| exact_location(approved)
                        .is_some_and(|origin| origin.origin() == location.origin()))))))
            && (m.actions == ["browser.read", "browser.interact"]
                || (m.actions == ["browser.read"] && verification.execution_profile.is_some()
                    && !verification.allow_type && verification.fill_values.is_empty()))
            && m.applications == ["com.google.Chrome"]
            && !m.origins.is_empty()
            && exact_location(&verification.start_url).is_some_and(|location| {
                m.origins.iter().any(|approved| exact_location(approved).is_some_and(
                    |origin| origin.origin() == location.origin()))
            })
            && m.browser_verification.is_none()
    });
    let desktop_valid = m.desktop_verification.as_ref().is_none_or(|verification| {
        reviewed_desktop_surfaces_valid(verification, &m.applications)
            && bundle_id(&verification.application_bundle)
            && verification.process_id > 0 && verification.window_id > 0
            && (1..=24).contains(&verification.process_start_identity.len())
            && verification.process_start_identity.bytes().all(|byte| byte.is_ascii_digit())
            && verification.window_title.len() <= 200
            && matches!(verification.delivery_mode.as_str(), "background" | "foreground_fallback")
            && (1..=48).contains(&verification.max_steps)
            && verification.reviewed_text.len() <= 8
            && reviewed_desktop_text(verification)
            && verification.founder_goal.as_ref().is_none_or(|goal| bounded(goal, 3000)
                && !goal.trim().is_empty())
            && verification.expected_visible_text.as_ref().is_none_or(|text| bounded(text, 80)
                && !text.contains('@') && !text.chars().any(char::is_control))
            && m.origins.is_empty()
            && m.actions == ["desktop.read", "desktop.interact"]
            && m.browser_verification.is_none()
            && m.general_browser_verification.is_none()
    });
    let terminal_valid = m.terminal_verification.as_ref().is_none_or(|verification| {
        verification.operation == "git_status_v1"
            && workspace_slug(&verification.workspace_slug)
            && m.actions == ["terminal.execute"]
            && m.applications == ["com.companyos.app"]
            && m.origins.is_empty()
            && m.browser_verification.is_none()
            && m.general_browser_verification.is_none()
            && m.coding_verification.is_none()
    });
    let coding_valid = m.coding_verification.as_ref().is_none_or(|verification| {
        verification.operation == "workspace_edit_v1"
            && matches!(verification.provider.as_str(), "cursor" | "codex" | "claude" | "opencode")
            && workspace_slug(&verification.workspace_slug)
            && verification.read_scope == "device_filesystem"
            && verification.write_scope == "venture_workspace"
            && verification.command_network == "disabled"
            && verification.pagebuilder_candidate.as_ref().is_none_or(|candidate| {
                matches!(verification.provider.as_str(), "codex" | "claude")
                    && verification.offline_profile == Some(CodingOfflineProfile::StaticHtmlV1)
                    && candidate_binding_valid(candidate, &record, venture)
            })
            && m.actions == ["editor.edit", "terminal.execute"]
            && m.applications == ["com.companyos.app"]
            && m.origins.is_empty()
            && m.browser_verification.is_none()
            && m.general_browser_verification.is_none()
            && m.terminal_verification.is_none()
    });
    let coding_loop_valid = m.coding_loop_verification.as_ref().is_none_or(|verification| {
        verification.operation == "workspace_loop_v1"
            && matches!(verification.provider.as_str(), "claude" | "codex" | "cursor" | "opencode")
            && workspace_slug(&verification.workspace_slug)
            && matches!((verification.profile.as_str(), verification.check_recipe.as_str()),
                ("web_app_static_v1", "web_app_static_checks_v1") | ("static_html_v1", "static_html_checks_v1")
                | ("web_app_vercel_v1", "web_app_vercel_checks_v1")
                | ("static_landing_v1", "static_landing_checks_v1")
                | ("workspace_utility_v1", "workspace_utility_checks_v1"))
            && if verification.profile == "web_app_vercel_v1" {
                verification.pagebuilder_candidate.is_none()
                    && (verification.provider == "claude" || verification.max_turns.is_none())
                    && match verification.phase.as_deref() {
                        Some("plan") => verification.plan_order_id.is_none() && verification.plan_digest.is_none()
                            && verification.max_iterations <= 2,
                        Some("implement") => verification.plan_order_id.as_ref().is_some_and(|id|
                            product_order_id(id))
                            && verification.plan_digest.as_ref().is_some_and(|digest| hex_digest(digest, 64)),
                        _ => false,
                    }
            } else if matches!(verification.profile.as_str(), "static_landing_v1" | "workspace_utility_v1") {
                verification.phase.as_deref() == Some("implement")
                    && verification.plan_order_id.is_none() && verification.plan_digest.is_none()
                    && verification.pagebuilder_candidate.is_none()
                    && verification.per_call_seconds <= verification.max_wall_seconds
                    && verification.max_iterations <= if verification.profile == "workspace_utility_v1" { 1 } else { 4 }
            } else {
                verification.phase.is_none() && verification.plan_order_id.is_none() && verification.plan_digest.is_none()
            }
            && verification.network == "none" && verification.read_scope == "venture_workspace"
            && verification.write_scope == "venture_workspace"
            && (1..=10).contains(&verification.max_iterations)
            && (1..=5400).contains(&verification.max_wall_seconds)
            && (1..=1200).contains(&verification.per_call_seconds)
            && (verification.profile != "web_app_vercel_v1" || verification.per_call_seconds <= verification.max_wall_seconds)
            && verification.max_turns.is_none_or(|turns| (1..=60).contains(&turns))
            && (verification.provider != "claude" || verification.max_turns.is_some())
            && (verification.provider != "codex" || verification.max_turns.is_none())
            && verification.pagebuilder_candidate.as_ref().is_none_or(|candidate|
                candidate_binding_valid(candidate, &record, venture))
            && m.actions == ["editor.edit", "terminal.execute"]
            && m.applications == ["com.companyos.app"] && m.origins.is_empty()
            && m.credit_ceiling_micro == 0 && m.duration_seconds == verification.max_wall_seconds
    });
    let deploy_valid = m.deploy_verification.as_ref().is_none_or(|verification| {
        verification.operation == "workspace_deploy_v1" && verification.provider == "vercel"
            && matches!(verification.target.as_str(), "preview" | "production")
            && workspace_slug(&verification.workspace_slug)
            && product_order_id(&verification.source_order_id)
            && hex_digest(&verification.source_commit, 40)
            && hex_digest(&verification.file_manifest_sha256, 64)
            && verification.project_link_sha256.as_ref().is_none_or(|digest| hex_digest(digest, 64))
            && (verification.target != "production" || verification.project_link_sha256.is_some())
            && m.actions == ["terminal.execute"] && m.applications == ["com.companyos.app"]
            && m.origins.is_empty() && m.credit_ceiling_micro == 0 && m.duration_seconds == 600
    });
    let app_navigation_valid = m.app_navigation_verification.as_ref().is_none_or(|verification| {
        verification.operation == "open_venture_screen_v1"
            && matches!(verification.route.as_str(), "builds" | "settings")
            && m.actions == ["app.navigate"]
            && m.applications == ["com.companyos.app"]
            && m.origins.is_empty()
            && m.browser_verification.is_none()
            && m.general_browser_verification.is_none()
            && m.terminal_verification.is_none()
            && m.coding_verification.is_none()
            && m.inspection_verification.is_none()
    });
    let inspection_valid = m.inspection_verification.as_ref().is_none_or(|verification| {
        verification.operation == "workspace_inspect_v1"
            && verification.provider == "cursor"
            && workspace_slug(&verification.workspace_slug)
            && verification.read_scope == "venture_workspace"
            && verification.write_scope == "none"
            && m.actions == ["editor.read"]
            && m.applications == ["com.companyos.app"]
            && m.origins.is_empty()
            && m.browser_verification.is_none()
            && m.general_browser_verification.is_none()
            && m.terminal_verification.is_none()
            && m.coding_verification.is_none()
            && m.app_navigation_verification.is_none()
    });
    let terminal_scope_complete = !m.actions.iter().any(|value| value == "terminal.execute")
        || m.terminal_verification.is_some() || m.coding_verification.is_some() || m.coding_loop_verification.is_some()
        || m.deploy_verification.is_some();
    let editor_scope_complete = !m.actions.iter().any(|value| value == "editor.edit")
        || m.coding_verification.is_some() || m.coding_loop_verification.is_some();
    let read_scope_complete = !m.actions.iter().any(|value| value == "editor.read")
        || m.inspection_verification.is_some();
    let app_scope_complete = !m.actions.iter().any(|value| value == "app.navigate")
        || m.app_navigation_verification.is_some();
    let desktop_scope_complete = !m.actions.iter().any(|value| value.starts_with("desktop."))
        || m.desktop_verification.is_some();
    if !bounded(&record.workspace_id, 128) || !bounded(&record.actor_id, 256)
        || !bounded(&record.expires_at, 64)
        || record.review_digest.len() != 64
        || !record.review_digest.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        || !bounded(&m.task_id, 128) || !bounded(&m.title, 160) || !bounded(&m.instruction, 12000)
        || m.applications.is_empty() || m.applications.len() > 8 || !distinct(&m.applications)
        || !m.applications.iter().all(|value| bundle_id(value))
        || m.origins.len() > 20 || !distinct(&m.origins) || !m.origins.iter().all(|value| exact_origin(value))
        || m.actions.is_empty() || m.actions.len() > 4
        || !m.actions.iter().all(|value| matches!(value.as_str(), "browser.read" | "browser.interact" | "desktop.read" | "desktop.interact" | "editor.read" | "editor.edit" | "terminal.execute" | "app.navigate"))
        || !qualification_valid(&record, device) || !verification_valid || !general_valid || !desktop_valid || !terminal_valid || !coding_valid || !coding_loop_valid || !deploy_valid || !app_navigation_valid || !inspection_valid
        || !terminal_scope_complete || !editor_scope_complete || !read_scope_complete || !app_scope_complete || !desktop_scope_complete
        || [m.browser_verification.is_some(), m.general_browser_verification.is_some(),
            m.desktop_verification.is_some(),
            m.terminal_verification.is_some(), m.coding_verification.is_some(), m.coding_loop_verification.is_some(), m.deploy_verification.is_some(),
            m.inspection_verification.is_some(), m.app_navigation_verification.is_some()]
            .iter().filter(|value| **value).count() > 1
        || m.credit_ceiling_micro > 1000000 || !(1..=if m.coding_loop_verification.is_some() { 5400 } else { 1800 }).contains(&m.duration_seconds) {
        return Err("device_order_scope_invalid".into());
    }
    Ok(record)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    // Emitted by the actual shared Python DTO; pins cross-language canonical bytes.
    fn qualification_record() -> serde_json::Value {
        serde_json::from_str(r###"{"actor_id":"usr_synthetic","browser_qualification_grant":{"actor_id":"usr_synthetic","allowed_operations":["navigate","type","click","extract"],"catalog_digest":"1111111111111111111111111111111111111111111111111111111111111111","device_id":"dev_synthetic","downloads":[],"expires_at":2000000000,"frames":[],"grant_digest":"5760753356c9d2e3850e2da87bc1271759ba5ea7f9fe763d060c3a2a48fbb262","max_tabs":1,"nonce":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","order_id":"dwo_one","origin":"https://api.supymem.com","profile_digest":"83fc3faf8461bc761f0cbc3a514629de88afa8573c2b0c7010fc76bcf99bc501","review_digest":"3333333333333333333333333333333333333333333333333333333333333333","rules":[{"body_kind":"form","control_id":"","document_id":"document-form","document_path":"/public/browser-qualification/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/form","fields":[],"form_id":"","max_commits":0,"max_requests":4,"method":"GET","path":"/public/browser-qualification/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/form","prerequisites":[],"query":"","rejected_retry_limit":0,"rule_id":"read_form"},{"body_kind":"form","control_id":"form-submit","document_id":"document-form","document_path":"/public/browser-qualification/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/form","fields":[{"control_id":"project","input_type":"text","input_value":"Atlas","kind":"text","name":"project","value":"Atlas"},{"control_id":"email","input_type":"text","input_value":"qa@example.invalid","kind":"email","name":"email","value":"qa@example.invalid"},{"control_id":"code","input_type":"text","input_value":"ABC123","kind":"synthetic_code","name":"code","value":"ABC123"}],"form_id":"form-project","max_commits":1,"max_requests":1,"method":"POST","path":"/public/browser-qualification/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/form","prerequisites":["read_form"],"query":"","rejected_retry_limit":0,"rule_id":"submit_form"}],"schema_version":1,"seed_digest":"2222222222222222222222222222222222222222222222222222222222222222","task_id":"form_valid","trial_id":"bqt_one","venture_id":"ven_one","workspace_id":"ws_one"},"browser_qualification_trial_binding":{"actor_id":"usr_synthetic","binding_digest":"a6dd18d2046a63f2c6cd073427ae68ff3658c4cb4ca77f2b701bf9dd3de02bc1","catalog_digest":"1111111111111111111111111111111111111111111111111111111111111111","device_id":"dev_synthetic","expires_at":2000000000,"order_id":"dwo_one","profile_digest":"83fc3faf8461bc761f0cbc3a514629de88afa8573c2b0c7010fc76bcf99bc501","review_digest":"3333333333333333333333333333333333333333333333333333333333333333","schema_version":1,"seed_digest":"2222222222222222222222222222222222222222222222222222222222222222","task_id":"form_valid","trial_id":"bqt_one","venture_id":"ven_one","workspace_id":"ws_one"},"device_id":"dev_synthetic","expires_at":"2033-05-18T03:33:20+00:00","id":"dwo_one","manifest":{"actions":["browser.read","browser.interact"],"applications":["com.google.Chrome"],"credit_ceiling_micro":100000,"duration_seconds":120,"general_browser_verification":{"allow_type":true,"execution_profile":{"backend":"cuai","completion_policy":"browser_semantic_review_v1","max_provider_calls":64,"max_request_bytes":65536,"policy_version":1,"pricing_digest":"deb0f50f682218f1462632140b85e7d1764c72ab760a1dda51c880d81b286e90","roles":{"actor":{"max_output_tokens":700,"model":"gpt-5.6-luna","provider":"openai","reasoning":"none","service_tier":"default"},"plan":{"max_output_tokens":700,"model":"gpt-5.6-luna","provider":"openai","reasoning":"none","service_tier":"default"},"verify":{"max_output_tokens":700,"model":"gpt-5.6-luna","provider":"openai","reasoning":"none","service_tier":"default"}},"version":"browser_cuai_text_v1"},"fill_values":{},"max_steps":32,"operation":"general_goal_v1","start_url":"https://api.supymem.com/public/browser-qualification/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/form"},"instruction":"Enter exact synthetic project details","origins":["https://api.supymem.com"],"task_id":"task_one","title":"Reviewed fixture"},"review_digest":"3333333333333333333333333333333333333333333333333333333333333333","state":"approved","venture_id":"ven_one","workspace_id":"ws_one"}"###).unwrap()
    }
    fn repin_qualification(value: &mut serde_json::Value) {
        let g=&mut value["browser_qualification_grant"];
        g["grant_digest"]=json!(canonical_digest(g.clone(),Some("grant_digest")).unwrap());
    }
    #[test]
    fn qualification_wire_roundtrips_shared_digests_without_changing_profile_or_manifest() {
        let value=qualification_record();
        let parsed=validate_review(value.clone(),"ven_one","dwo_one","dev_synthetic").unwrap();
        let wire=serde_json::to_value(parsed).unwrap();
        for key in ["browser_qualification_grant","browser_qualification_trial_binding"] { assert_eq!(wire[key],value[key]); }
        let baseline: Manifest=serde_json::from_value(value["manifest"].clone()).unwrap();
        assert_eq!(wire["manifest"],serde_json::to_value(baseline).unwrap());
        assert_eq!(wire["manifest"]["general_browser_verification"]["execution_profile"],value["manifest"]["general_browser_verification"]["execution_profile"]);
        let mut budget_only=value.clone(); budget_only.as_object_mut().unwrap().remove("browser_qualification_grant");
        assert!(validate_review(budget_only,"ven_one","dwo_one","dev_synthetic").is_ok());
        let mut stripped=value.clone(); stripped.as_object_mut().unwrap().remove("browser_qualification_trial_binding");
        assert!(validate_review(stripped,"ven_one","dwo_one","dev_synthetic").is_ok());
        let mut changed=value; changed["browser_qualification_grant"]["rules"][1]["fields"][0]["value"]=json!("other");
        assert!(validate_review(changed,"ven_one","dwo_one","dev_synthetic").is_err());
    }
    #[test]
    fn qualification_refuses_rehashed_foreign_scope_secret_fields_and_invalid_bodies() {
        for (path,v) in [
            ("/actor_id",json!("usr_other")),("/device_id",json!("dev_other")),("/workspace_id",json!("ws_other")),
            ("/review_digest",json!("f".repeat(64))),("/profile_digest",json!("f".repeat(64))), ("/origin",json!("https://127.0.0.1")),
            ("/expires_at",json!(1)),("/max_tabs",json!(5)),("/task_id",json!("pay_wall")),
            ("/rules/1/fields/0/name",json!("password")),("/rules/1/fields/1/value",json!("actual@example.com")),
            ("/rules/1/fields/2/input_type",json!("password")),("/rules/1/prerequisites",json!(["future_rule"])),
            ("/rules/1/max_commits",json!(2)),("/rules/1/fields/0/value",json!(true)),
            ("/rules/1/fields/0/input_value",json!(true)),("/allowed_operations",json!(["script"]))
        ] {
            let mut value=qualification_record(); *value["browser_qualification_grant"].pointer_mut(path).unwrap()=v;
            repin_qualification(&mut value);
            assert!(validate_review(value,"ven_one","dwo_one","dev_synthetic").is_err(),"{path}");
        }
        let mut value=qualification_record(); value["browser_qualification_grant"]["owned"]=json!(true); repin_qualification(&mut value);
        assert!(validate_review(value,"ven_one","dwo_one","dev_synthetic").is_err());
        let mut value=qualification_record(); value["browser_qualification_trial_binding"]["expires_at"]=json!(true);
        assert!(validate_review(value,"ven_one","dwo_one","dev_synthetic").is_err());
    }
    #[test]
    fn qualification_terminal_expiry_is_readable_without_reopening_authority() {
        let mut value=qualification_record(); value["browser_qualification_grant"]["expires_at"]=json!(1); repin_qualification(&mut value);
        let b=&mut value["browser_qualification_trial_binding"]; b["expires_at"]=json!(1);
        b["binding_digest"]=json!(canonical_digest(b.clone(),Some("binding_digest")).unwrap());
        assert!(validate_review(value.clone(),"ven_one","dwo_one","dev_synthetic").is_err());
        value["state"]=json!("stopped"); assert!(validate_review(value,"ven_one","dwo_one","dev_synthetic").is_ok());
    }

    #[test]
    fn native_wire_capabilities_are_closed_parser_support_markers() {
        assert_eq!(serde_json::to_value(native_wire_capabilities()).unwrap(),
            json!({"browser_cuai_text_v1": true, "pagebuilder_reference_v1": true, "browser_cuai_text_v2": true, "browser_cuai_read_only_v1": true, "browser_qualification_v1": true, "product_build_v1": true}));
    }

    #[test]
    fn native_wire_capability_command_requires_trusted_main_window_and_registration() {
        let source = include_str!("lib.rs");
        let command = source.split("fn device_work_order_capabilities(").nth(1).unwrap()
            .split("#[tauri::command]").next().unwrap();
        let guard = command.find("require_native_account_window(&window, &state)?").unwrap();
        let result = command.find("device_order::native_wire_capabilities()").unwrap();
        assert!(guard < result);
        assert!(!command.contains("desktop_api_request") && !command.contains("reqwest"));
        let handler = source.split("tauri::generate_handler![").nth(1).unwrap()
            .split(']').next().unwrap();
        assert!(handler.split(',').any(|name| name.trim() == "device_work_order_capabilities"));
        let acl = include_str!("../permissions/app.toml");
        let allowed = acl.split("commands.allow = [").nth(1).unwrap().split(']').next().unwrap();
        assert!(allowed.contains("\"device_work_order_capabilities\""));
    }

    fn record() -> serde_json::Value {
        json!({"id":"dwo_one","workspace_id":"w_one","venture_id":"ven_one","actor_id":"actor_one",
            "device_id":"device_one","state":"approved","review_digest":"a".repeat(64),
            "expires_at":"2026-09-20T00:00:00","manifest":{"task_id":"task_one","title":"Review",
                "instruction":"Review the approved page","applications":["com.apple.Safari"],
                "origins":["https://example.test"],"actions":["browser.read"],
                "credit_ceiling_micro":8000,"duration_seconds":60}})
    }

    fn coding_loop_record() -> serde_json::Value {
        let mut value=record();
        value["manifest"]=serde_json::from_str(include_str!("../../tests/fixtures/coding_loop_manifest.json")).unwrap();
        value
    }

    #[test]
    fn product_plan_run_and_each_deployment_preserve_the_actual_core_wire() {
        for fixture in [include_str!("../../tests/fixtures/product_build_plan_manifest.json"),
            include_str!("../../tests/fixtures/product_build_run_manifest.json"),
            include_str!("../../tests/fixtures/product_deploy_preview_manifest.json"),
            include_str!("../../tests/fixtures/product_deploy_production_manifest.json")] {
            let mut value=record(); value["manifest"]=serde_json::from_str(fixture).unwrap();
            let expected=value["manifest"].clone();
            let wire=serde_json::to_value(validate_review(value,"ven_one","dwo_one","device_one").unwrap()).unwrap();
            let key=if expected.get("coding_loop_verification").is_some() { "coding_loop_verification" } else { "deploy_verification" };
            assert_eq!(wire["manifest"][key],expected[key]);
            assert_eq!(wire["manifest"]["duration_seconds"],expected["duration_seconds"]);
        }
        let mut plan=record(); plan["manifest"]=serde_json::from_str(include_str!("../../tests/fixtures/product_build_plan_manifest.json")).unwrap();
        for (key,bad) in [("phase",json!("implement")),("max_iterations",json!(3)),
            ("provider",json!("unknown")),("plan_digest",json!("b".repeat(64))),
            ("max_wall_seconds",json!(1))] {
            let mut invalid=plan.clone(); invalid["manifest"]["coding_loop_verification"][key]=bad;
            if key=="max_wall_seconds" { invalid["manifest"]["duration_seconds"]=json!(1); }
            assert!(validate_review(invalid,"ven_one","dwo_one","device_one").is_err(),"{key}");
        }
        let mut run=record(); run["manifest"]=serde_json::from_str(include_str!("../../tests/fixtures/product_build_run_manifest.json")).unwrap();
        for (key,bad) in [("plan_digest",json!("b".repeat(63))), ("plan_order_id",json!("dwo_other/path")),
            ("phase",json!("plan")), ("max_turns",json!(40)), ("network",json!("enabled"))] {
            let mut invalid=run.clone(); invalid["manifest"]["coding_loop_verification"][key]=bad;
            assert!(validate_review(invalid,"ven_one","dwo_one","device_one").is_err(),"{key}");
        }
        let mut deploy=record(); deploy["manifest"]=serde_json::from_str(include_str!("../../tests/fixtures/product_deploy_preview_manifest.json")).unwrap();
        for (key,bad) in [("target",json!("all")), ("provider",json!("aws")),
            ("source_commit",json!("c".repeat(39))), ("file_manifest_sha256",json!("D".repeat(64))),
            ("source_order_id",json!("dwo_source_extra"))] {
            let mut invalid=deploy.clone(); invalid["manifest"]["deploy_verification"][key]=bad;
            assert!(validate_review(invalid,"ven_one","dwo_one","device_one").is_err(),"{key}");
        }
        deploy["manifest"]["coding_loop_verification"]=run["manifest"]["coding_loop_verification"].clone();
        assert!(validate_review(deploy,"ven_one","dwo_one","device_one").is_err());
        let mut production=record();
        production["manifest"]=serde_json::from_str(include_str!("../../tests/fixtures/product_deploy_production_manifest.json")).unwrap();
        for hash in [serde_json::Value::Null,json!("e".repeat(63)),json!("E".repeat(64))] {
            let mut invalid=production.clone(); invalid["manifest"]["deploy_verification"]["project_link_sha256"]=hash;
            assert!(validate_review(invalid,"ven_one","dwo_one","device_one").is_err());
        }
    }

    #[test]
    fn coding_loop_fixture_preserves_exact_caps_and_default_empty_cu_wire() {
        use sha2::{Digest,Sha256};
        let fixture=include_str!("../../tests/fixtures/coding_loop_manifest.json");
        assert_eq!(format!("{:x}",Sha256::digest(fixture.as_bytes())),"022cb7a98129585cc4c9f78a16b25f52ea8142ae98dc99b2b100e39b0752f59c");
        let value=coding_loop_record();
        let reviewed=validate_review(value.clone(),"ven_one","dwo_one","device_one").unwrap();
        let wire=serde_json::to_value(reviewed).unwrap();
        assert_eq!(wire["manifest"]["coding_loop_verification"],value["manifest"]["coding_loop_verification"]);
        assert_eq!(wire["manifest"]["duration_seconds"],json!(2700));
        let mut defaults=value;
        let spec=defaults["manifest"]["coding_loop_verification"].as_object_mut().unwrap();
        for key in ["max_iterations","max_wall_seconds","per_call_seconds"] { spec.remove(key); }
        let defaults=serde_json::to_value(validate_review(defaults,"ven_one","dwo_one","device_one").unwrap()).unwrap();
        assert_eq!(defaults["manifest"]["coding_loop_verification"],wire["manifest"]["coding_loop_verification"]);
        let legacy=serde_json::to_value(validate_review(record(),"ven_one","dwo_one","device_one").unwrap()).unwrap();
        assert!(legacy["manifest"].get("coding_loop_verification").is_none());
        // Existing reviewed_read_urls golden hashes independently guard the CU DTO.
    }

    #[test]
    fn coding_loop_review_accepts_only_closed_profiles_providers_and_caps() {
        for provider in ["claude","codex","cursor"] {
            let mut value=coding_loop_record(); value["manifest"]["coding_loop_verification"]["provider"]=json!(provider);
            if provider=="codex" { value["manifest"]["coding_loop_verification"]["max_turns"]=serde_json::Value::Null; }
            assert!(validate_review(value,"ven_one","dwo_one","device_one").is_ok());
        }
        let mut static_html=coding_loop_record();
        static_html["manifest"]["coding_loop_verification"]["profile"]=json!("static_html_v1");
        static_html["manifest"]["coding_loop_verification"]["check_recipe"]=json!("static_html_checks_v1");
        assert!(validate_review(static_html,"ven_one","dwo_one","device_one").is_ok());
        let mut maximum=coding_loop_record();
        maximum["manifest"]["duration_seconds"]=json!(5400);
        maximum["manifest"]["coding_loop_verification"]["max_wall_seconds"]=json!(5400);
        maximum["manifest"]["coding_loop_verification"]["max_iterations"]=json!(10);
        maximum["manifest"]["coding_loop_verification"]["per_call_seconds"]=json!(1200);
        maximum["manifest"]["coding_loop_verification"]["max_turns"]=json!(60);
        assert!(validate_review(maximum,"ven_one","dwo_one","device_one").is_ok());
        for (key,invalid) in [("max_iterations",json!(11)),("per_call_seconds",json!(1201)),
            ("max_wall_seconds",json!(5401)),("max_turns",json!(61)),("max_iterations",json!(0)),
            ("max_turns",json!(true)),("network",json!("allow")),("read_scope",json!("device_filesystem")),
            ("write_scope",json!("device_filesystem")),("check_recipe",json!("static_html_checks_v1")),
            ("operation",json!("workspace_edit_v1")),("provider",json!("unknown")),
            ("workspace_slug",json!("test--venture")),("workspace_slug",json!("a".repeat(65))),
            ("extra",json!(true))] {
            let mut value=coding_loop_record(); value["manifest"]["coding_loop_verification"][key]=invalid;
            assert!(validate_review(value,"ven_one","dwo_one","device_one").is_err(),"{key}");
        }
    }

    #[test]
    fn build_native_product_review_accepts_four_tools_without_relaxing_owned_scope() {
        for (venture, title) in [("ven_operations", "Supplier audit workspace"),
                                  ("ven_hobby", "Community garden planner")] {
            for provider in ["claude", "codex", "cursor", "opencode"] {
                let mut value = record();
                value["venture_id"] = json!(venture);
                value["manifest"] = serde_json::from_str(include_str!("../../tests/fixtures/product_build_plan_manifest.json")).unwrap();
                value["manifest"]["title"] = json!(title);
                value["manifest"]["coding_loop_verification"]["provider"] = json!(provider);
                if provider != "claude" {
                    value["manifest"]["coding_loop_verification"].as_object_mut().unwrap().remove("max_turns");
                }
                let reviewed = validate_review(value.clone(), venture, "dwo_one", "device_one").unwrap();
                assert_eq!(reviewed.manifest.coding_loop_verification.unwrap().provider, provider);
                for (key, invalid) in [("network", json!("allow")), ("read_scope", json!("device_filesystem")),
                    ("write_scope", json!("device_filesystem")), ("phase", json!("deploy")),
                    ("provider", json!("unknown"))] {
                    let mut changed = value.clone();
                    changed["manifest"]["coding_loop_verification"][key] = invalid;
                    assert!(validate_review(changed, venture, "dwo_one", "device_one").is_err(), "{provider}: {key}");
                }
                if provider != "claude" {
                    value["manifest"]["coding_loop_verification"]["max_turns"] = json!(20);
                    assert!(validate_review(value, venture, "dwo_one", "device_one").is_err());
                }
            }
        }
    }

    #[test]
    fn coding_loop_codex_null_or_omitted_turns_preserve_actual_core_saved_wire() {
        use sha2::{Digest,Sha256};
        let fixture=include_str!("../../tests/fixtures/coding_loop_codex_manifest.json");
        assert_eq!(format!("{:x}",Sha256::digest(fixture.as_bytes())),"90f0053b0c686ffb67bc2201957c0f8369947ee52bfd5a7f0c4e88d36cb19736");
        let mut value=record(); value["manifest"]=serde_json::from_str(fixture).unwrap();
        let reviewed=validate_review(value.clone(),"ven_one","dwo_one","device_one").unwrap();
        let wire=serde_json::to_value(reviewed).unwrap();
        let mut expected=value["manifest"]["coding_loop_verification"].clone();
        expected.as_object_mut().unwrap().remove("max_turns");
        // Core preparation saves Manifest.model_dump(exclude_none=True): nullable
        // input is accepted, but None is omitted from the actual reviewed wire.
        assert_eq!(wire["manifest"]["coding_loop_verification"],expected);
        value["manifest"]["coding_loop_verification"].as_object_mut().unwrap().remove("max_turns");
        let omitted=serde_json::to_value(validate_review(value.clone(),"ven_one","dwo_one","device_one").unwrap()).unwrap();
        assert_eq!(omitted["manifest"]["coding_loop_verification"],expected);
        for turns in [json!(1),json!(40),json!(60)] {
            let mut invalid=value.clone(); invalid["manifest"]["coding_loop_verification"]["max_turns"]=turns;
            assert!(validate_review(invalid,"ven_one","dwo_one","device_one").is_err());
        }
        let mut claude=coding_loop_record();
        claude["manifest"]["coding_loop_verification"].as_object_mut().unwrap().remove("max_turns");
        assert!(validate_review(claude.clone(),"ven_one","dwo_one","device_one").is_err());
        claude["manifest"]["coding_loop_verification"]["max_turns"]=serde_json::Value::Null;
        assert!(validate_review(claude,"ven_one","dwo_one","device_one").is_err());
    }

    #[test]
    fn coding_loop_requires_exact_one_verifier_and_zero_credit_owned_workspace_scope() {
        for (key,invalid) in [("duration_seconds",json!(1800)),("credit_ceiling_micro",json!(1)),
            ("applications",json!(["com.apple.Terminal"])),("origins",json!(["https://example.test"])),
            ("actions",json!(["terminal.execute","editor.edit"]))] {
            let mut value=coding_loop_record(); value["manifest"][key]=invalid;
            assert!(validate_review(value,"ven_one","dwo_one","device_one").is_err(),"{key}");
        }
        let mut second=coding_loop_record();
        second["manifest"]["duration_seconds"]=json!(1800);
        second["manifest"]["coding_loop_verification"]["max_wall_seconds"]=json!(1800);
        second["manifest"]["coding_verification"]=json!({"operation":"workspace_edit_v1","provider":"claude",
            "workspace_slug":"test-venture","read_scope":"device_filesystem","write_scope":"venture_workspace",
            "command_network":"disabled"});
        assert!(validate_review(second.clone(),"ven_one","dwo_one","device_one").is_err());
        second["manifest"].as_object_mut().unwrap().remove("coding_loop_verification");
        assert!(validate_review(second,"ven_one","dwo_one","device_one").is_ok());
        let mut legacy=record(); legacy["manifest"]["duration_seconds"]=json!(1801);
        assert!(validate_review(legacy,"ven_one","dwo_one","device_one").is_err());
    }

    #[test]
    fn coding_loop_preserves_existing_candidate_binding_and_rejects_foreign_or_unbounded_input() {
        let binding=json!({"schema_version":1,"job_id":"job_one","variant":0,"revision_id":"rev_one",
            "workspace_id":"w_one","venture_id":"ven_one","app_id":"app_one","source_sha256":"b".repeat(64),
            "source_manifest_digest":"c".repeat(64),"source_size_bytes":1200,"spec_sha256":"d".repeat(64),
            "reference_sha256":"e".repeat(64),"reference_size_bytes":300});
        let mut value=coding_loop_record();
        value["manifest"]["coding_loop_verification"]["pagebuilder_candidate"]=binding.clone();
        let reviewed=validate_review(value.clone(),"ven_one","dwo_one","device_one").unwrap();
        assert_eq!(serde_json::to_value(reviewed).unwrap()["manifest"]["coding_loop_verification"]["pagebuilder_candidate"],binding);
        for (key,invalid) in [("workspace_id",json!("w_other")),("venture_id",json!("ven_other")),
            ("source_size_bytes",json!(0)),("reference_size_bytes",json!(33*1024*1024)),("variant",json!(3)),
            ("source_sha256",json!("bad")),("extra",json!(true))] {
            let mut changed=value.clone(); changed["manifest"]["coding_loop_verification"]["pagebuilder_candidate"][key]=invalid;
            assert!(validate_review(changed,"ven_one","dwo_one","device_one").is_err(),"{key}");
        }
    }
    #[test]
    fn reviewed_modal_grant_requires_gate_and_exact_existing_parent_identity() {
        let mut value=document_record();
        value["manifest"]["desktop_verification"]["modal_policy"]=json!({
            "operation":"exact_parent_children_v1","parents":[{
                "application_bundle":"com.apple.TextEdit","process_id":101,
                "process_start_identity":"123456789","window_id":207}]});
        let parsed: WorkOrder=serde_json::from_value(value.clone()).unwrap();
        let spec=parsed.manifest.desktop_verification.as_ref().unwrap();
        assert!(!reviewed_modal_policy_valid(spec,false));
        assert!(reviewed_modal_policy_valid(spec,true));
        for (key,val) in [("process_id",json!(999)),("window_id",json!(999)),
                          ("process_start_identity",json!("999")),("application_bundle",json!("com.example.Decoy"))] {
            let mut changed=value.clone(); changed["manifest"]["desktop_verification"]["modal_policy"]["parents"][0][key]=val;
            let parsed: WorkOrder=serde_json::from_value(changed).unwrap();
            assert!(!reviewed_modal_policy_valid(parsed.manifest.desktop_verification.as_ref().unwrap(),true));
        }
        let mut extra=value;
        extra["manifest"]["desktop_verification"]["modal_policy"]["parents"][0]["all_windows"]=json!(true);
        assert!(serde_json::from_value::<WorkOrder>(extra).is_err());
    }

    #[test]
    fn native_review_preserves_exact_scope_without_claiming() {
        let r = validate_review(record(), "ven_one", "dwo_one", "device_one").unwrap();
        assert!(matches!(r.state, OrderState::Approved));
        assert_eq!(r.manifest.credit_ceiling_micro, 8000);
        assert_eq!(review_path("ven_one", "dwo_one").unwrap(), "/api/ventures/ven_one/device-work-orders/dwo_one");
    }
    #[test]
    fn waiting_order_preserves_device_custody_without_new_claim() {
        let mut value = record();
        value["state"] = json!("waiting_for_device");
        value["claimed_at"] = json!("2026-10-09T00:01:00+00:00");
        let parsed = validate_review(value.clone(), "ven_one", "dwo_one", "device_one").unwrap();
        assert!(matches!(parsed.state, OrderState::WaitingForDevice));
        assert_eq!(serde_json::to_value(parsed).unwrap()["state"], json!("waiting_for_device"));
        assert!(validate_review(value, "ven_one", "dwo_one", "device_two").is_err());
    }
    #[test]
    fn rotated_device_can_inspect_only_closed_or_cancelled_claim() {
        let mut r = record();
        r["claimed_at"] = json!("2026-09-20T00:01:00+00:00");
        assert!(validate_review(r.clone(), "ven_one", "dwo_one", "device_two").is_err());
        r["state"] = json!("cancelled");
        assert!(validate_review(r.clone(), "ven_one", "dwo_one", "device_two").is_ok());
        r["claimed_at"] = serde_json::Value::Null;
        assert!(validate_review(r, "ven_one", "dwo_one", "device_two").is_err());
    }
    #[test]
    fn browser_verification_is_preserved_only_inside_reviewed_scope() {
        let mut value = record();
        value["manifest"]["browser_verification"] = json!({"expected_locations": [
            "https://example.test/", "https://example.test/waitlist?source=review"
        ]});
        let valid = validate_review(value.clone(), "ven_one", "dwo_one", "device_one").unwrap();
        assert_eq!(valid.manifest.browser_verification.unwrap().expected_locations.len(), 2);

        for locations in [json!([]), json!(["http://example.test/"]),
            json!(["https://outside.test/"]), json!(["https://example.test/#fragment"])] {
            let mut invalid = value.clone();
            invalid["manifest"]["browser_verification"] = json!({"expected_locations": locations});
            assert!(validate_review(invalid, "ven_one", "dwo_one", "device_one").is_err());
        }
        let mut no_read = value;
        no_read["manifest"]["actions"] = json!(["browser.interact"]);
        assert!(validate_review(no_read, "ven_one", "dwo_one", "device_one").is_err());
    }

    #[test]
    fn self_navigation_requires_one_exact_supymem_screen() {
        let mut value = record();
        value["manifest"]["applications"] = json!(["com.companyos.app"]);
        value["manifest"]["origins"] = json!([]);
        value["manifest"]["actions"] = json!(["app.navigate"]);
        value["manifest"]["app_navigation_verification"] = json!({
            "operation": "open_venture_screen_v1", "route": "builds"
        });
        assert!(validate_review(value.clone(), "ven_one", "dwo_one", "device_one").is_ok());
        for route in ["people", "../settings", "https://outside.test/"] {
            let mut invalid = value.clone();
            invalid["manifest"]["app_navigation_verification"]["route"] = json!(route);
            assert!(validate_review(invalid, "ven_one", "dwo_one", "device_one").is_err());
        }
        let mut missing = value;
        missing["manifest"].as_object_mut().unwrap().remove("app_navigation_verification");
        assert!(validate_review(missing, "ven_one", "dwo_one", "device_one").is_err());
    }
    #[test]
    fn identity_and_path_confusion_are_rejected() {
        for (v, o, d) in [("ven_other", "dwo_one", "device_one"), ("ven_one", "dwo_other", "device_one")] {
            assert!(validate_review(record(), v, o, d).is_err());
        }
        // A rotated device may read a pending review for recovery, but the
        // server still denies its approve/claim transition under that id.
        assert!(validate_review(record(), "ven_one", "dwo_one", "device_other").is_ok());
        for v in ["..", "ven/one", "ven%2fone", "ven?next=1", ""] { assert!(review_path(v, "dwo_one").is_err()); }
    }
    #[test]
    fn unsupported_scope_and_browser_origin_escapes_are_rejected() {
        for origin in ["http://example.test", "https://example.test/path", "https://example.test/", "https://user@example.test",
            "https://*.example.test", "https://example.test?key=x", "https://example.test#fragment", "https://example.test\\evil"] {
            let mut value = record(); value["manifest"]["origins"] = json!([origin]);
            assert!(validate_review(value, "ven_one", "dwo_one", "device_one").is_err(), "{origin}");
        }
        for (key, item) in [("actions", json!(["shell.anything"])), ("duration_seconds", json!(1801)),
            ("credit_ceiling_micro", json!(-1)), ("argv", json!(["sh", "-c", "unexpected"]))] {
            let mut value = record(); value["manifest"][key] = item;
            assert!(validate_review(value, "ven_one", "dwo_one", "device_one").is_err());
        }
    }
    #[test]
    fn cancelled_and_expired_reviews_remain_readable_but_are_not_execution_grants() {
        let mut value = record(); value["state"] = json!("cancelled"); value["expires_at"] = json!("2000-01-01T00:00:00");
        assert!(matches!(validate_review(value, "ven_one", "dwo_one", "device_one").unwrap().state, OrderState::Cancelled));
    }
    #[test]
    fn stopped_browser_review_with_receipts_remains_readable() {
        let mut value = record();
        value["state"] = json!("stopped");
        value["claimed_at"] = json!("2026-09-23T02:11:10+00:00");
        value["closed_at"] = json!("2026-09-23T02:12:04+00:00");
        value["settled_micro"] = json!(448);
        value["native_stop"] = json!({
            "execution_id": "dex_example", "evidence_digest": "b".repeat(64),
            "processes_stopped": true
        });
        value["manifest"]["actions"] = json!(["browser.read", "browser.interact"]);
        value["manifest"]["origins"] = json!(["https://product.example", "https://gateway.example"]);
        value["manifest"]["browser_verification"] = json!({"expected_locations": [
            "https://product.example/", "https://gateway.example/public/waitlist"
        ]});
        value["manifest"]["credit_ceiling_micro"] = json!(1_000_000);
        value["manifest"]["duration_seconds"] = json!(600);
        let stopped = validate_review(value, "ven_one", "dwo_one", "device_one").unwrap();
        assert!(matches!(stopped.state, OrderState::Stopped));
        assert!(serde_json::to_value(stopped).is_ok());
    }
    #[test]
    fn terminal_review_requires_one_fixed_owned_workspace_operation() {
        let mut value = record();
        value["manifest"]["applications"] = json!(["com.companyos.app"]);
        value["manifest"]["origins"] = json!([]);
        value["manifest"]["actions"] = json!(["terminal.execute"]);
        value["manifest"]["terminal_verification"] = json!({
            "operation": "git_status_v1", "workspace_slug": "labhire"
        });
        assert!(validate_review(value.clone(), "ven_one", "dwo_one", "device_one").is_ok());
        for terminal in [json!({"operation":"shell_anything","workspace_slug":"labhire"}),
            json!({"operation":"git_status_v1","workspace_slug":"../other"})] {
            let mut invalid = value.clone();
            invalid["manifest"]["terminal_verification"] = terminal;
            assert!(validate_review(invalid, "ven_one", "dwo_one", "device_one").is_err());
        }
        let mut missing = value;
        missing["manifest"].as_object_mut().unwrap().remove("terminal_verification");
        assert!(validate_review(missing, "ven_one", "dwo_one", "device_one").is_err());
    }

    #[test]
    fn coding_review_requires_one_allowlisted_provider_and_owned_workspace() {
        let mut value = record();
        value["manifest"]["applications"] = json!(["com.companyos.app"]);
        value["manifest"]["origins"] = json!([]);
        value["manifest"]["actions"] = json!(["editor.edit", "terminal.execute"]);
        value["manifest"]["coding_verification"] = json!({
            "operation": "workspace_edit_v1", "provider": "codex", "workspace_slug": "labhire",
            "read_scope": "device_filesystem", "write_scope": "venture_workspace",
            "command_network": "disabled"
        });
        assert!(validate_review(value.clone(), "ven_one", "dwo_one", "device_one").is_ok());
        for verifier in [
            json!({"operation":"shell_anything","provider":"codex","workspace_slug":"labhire",
                "read_scope":"device_filesystem","write_scope":"venture_workspace","command_network":"disabled"}),
            json!({"operation":"workspace_edit_v1","provider":"unknown","workspace_slug":"labhire",
                "read_scope":"device_filesystem","write_scope":"venture_workspace","command_network":"disabled"}),
            json!({"operation":"workspace_edit_v1","provider":"codex","workspace_slug":"../other",
                "read_scope":"device_filesystem","write_scope":"venture_workspace","command_network":"disabled"}),
            json!({"operation":"workspace_edit_v1","provider":"codex","workspace_slug":"labhire",
                "read_scope":"venture_workspace","write_scope":"venture_workspace","command_network":"disabled"}),
        ] {
            let mut invalid = value.clone();
            invalid["manifest"]["coding_verification"] = verifier;
            assert!(validate_review(invalid, "ven_one", "dwo_one", "device_one").is_err());
        }
        let mut missing = value;
        missing["manifest"].as_object_mut().unwrap().remove("coding_verification");
        assert!(validate_review(missing, "ven_one", "dwo_one", "device_one").is_err());
    }

    fn static_coding_server_record() -> serde_json::Value {
        // Exact current CodingVerification/Manifest.model_dump(exclude_none=True)
        // wire shape, inside a saved pending order with its task snapshot.
        json!({
            "id":"dwo_one", "workspace_id":"w_one", "venture_id":"ven_one",
            "actor_id":"actor_one", "device_id":"device_one", "state":"pending",
            "review_digest":"a".repeat(64), "expires_at":"2026-10-04T01:00:00+00:00",
            "claimed_at":null,
            "task_snapshot":{"task_id":"task_one", "status":"pending", "plan_version":2,
                "capability_code":"coding.subscription_static"},
            "manifest":{
                "task_id":"task_one", "title":"Build private landing page",
                "instruction":"Use Codex to build the saved static landing page privately.",
                "applications":["com.companyos.app"], "origins":[],
                "actions":["editor.edit","terminal.execute"],
                "credit_ceiling_micro":0, "duration_seconds":600,
                "coding_verification":{
                    "operation":"workspace_edit_v1", "provider":"codex", "workspace_slug":"audit-qa",
                    "read_scope":"device_filesystem", "write_scope":"venture_workspace",
                    "command_network":"disabled", "offline_profile":"static_html_v1"
                }
            }
        })
    }

    #[test]
    fn current_server_static_coding_review_preserves_profile_and_zero_credit_without_claiming() {
        let record = validate_review(static_coding_server_record(), "ven_one", "dwo_one", "device_one").unwrap();
        assert!(matches!(record.state, OrderState::Pending));
        assert!(record.claimed_at.is_none());
        assert_eq!(record.manifest.credit_ceiling_micro, 0);
        assert_eq!(record.manifest.coding_verification.as_ref().unwrap().offline_profile,
            Some(CodingOfflineProfile::StaticHtmlV1));
        let wire = serde_json::to_value(record).unwrap();
        assert_eq!(wire["manifest"]["coding_verification"]["offline_profile"], json!("static_html_v1"));
        assert_eq!(wire["state"], json!("pending"));
    }

    #[test]
    fn unsupported_coding_profiles_and_unknown_verification_fields_are_rejected() {
        for profile in [json!("native_product_v1"), json!("static_html_v2"), json!(""),
            json!(true), json!(1), json!({"profile":"static_html_v1"})] {
            let mut value = static_coding_server_record();
            value["manifest"]["coding_verification"]["offline_profile"] = profile;
            assert_eq!(validate_review(value, "ven_one", "dwo_one", "device_one").unwrap_err(),
                "device_order_response_invalid");
        }
        let mut value = static_coding_server_record();
        value["manifest"]["coding_verification"]["allow_network"] = json!(true);
        assert_eq!(validate_review(value, "ven_one", "dwo_one", "device_one").unwrap_err(),
            "device_order_response_invalid");
    }

    #[test]
    fn pagebuilder_reference_preserves_saved_binding_and_rejects_foreign_or_unbounded_scope() {
        let binding = json!({"schema_version":1, "job_id":"job_one", "variant":0,
            "revision_id":"rev_one", "workspace_id":"w_one", "venture_id":"ven_one", "app_id":"app_one",
            "source_sha256":"a".repeat(64), "source_manifest_digest":"b".repeat(64),
            "source_size_bytes":100, "spec_sha256":"c".repeat(64),
            "reference_sha256":"d".repeat(64), "reference_size_bytes":200});
        let mut value = static_coding_server_record();
        value["manifest"]["coding_verification"]["pagebuilder_candidate"] = binding.clone();
        let record = validate_review(value.clone(), "ven_one", "dwo_one", "device_one").unwrap();
        assert_eq!(serde_json::to_value(record).unwrap()["manifest"]["coding_verification"]["pagebuilder_candidate"], binding);
        for (key, replacement) in [("venture_id", json!("ven_other")), ("workspace_id", json!("w_other")),
            ("job_id", json!("../foreign")), ("variant", json!(3)), ("schema_version", json!(2)),
            ("source_sha256", json!("bad")), ("reference_size_bytes", json!(33554433)),
            ("source_size_bytes", json!(0)), ("unknown", json!(true))] {
            let mut invalid = value.clone();
            invalid["manifest"]["coding_verification"]["pagebuilder_candidate"][key] = replacement;
            assert!(validate_review(invalid, "ven_one", "dwo_one", "device_one").is_err(), "{key}");
        }
        value["manifest"]["coding_verification"].as_object_mut().unwrap().remove("offline_profile");
        assert!(validate_review(value, "ven_one", "dwo_one", "device_one").is_err());
    }

    #[test]
    fn historical_coding_review_without_profile_remains_readable_and_cancelled() {
        for missing in [false, true] {
            let mut value = static_coding_server_record();
            value["state"] = json!("cancelled");
            if missing {
                value["manifest"]["coding_verification"].as_object_mut().unwrap().remove("offline_profile");
            } else {
                value["manifest"]["coding_verification"]["offline_profile"] = serde_json::Value::Null;
            }
            let record = validate_review(value, "ven_one", "dwo_one", "device_one").unwrap();
            assert!(matches!(record.state, OrderState::Cancelled));
            assert!(record.manifest.coding_verification.unwrap().offline_profile.is_none());
        }
    }

    #[test]
    fn inspection_review_is_read_only_and_cursor_only() {
        let mut value = record();
        value["manifest"]["applications"] = json!(["com.companyos.app"]);
        value["manifest"]["origins"] = json!([]);
        value["manifest"]["actions"] = json!(["editor.read"]);
        value["manifest"]["inspection_verification"] = json!({
            "operation": "workspace_inspect_v1", "provider": "cursor", "workspace_slug": "labhire",
            "read_scope": "venture_workspace", "write_scope": "none"
        });
        assert!(validate_review(value.clone(), "ven_one", "dwo_one", "device_one").is_ok());
        let mut edit = value.clone();
        edit["manifest"]["actions"] = json!(["editor.edit"]);
        assert!(validate_review(edit, "ven_one", "dwo_one", "device_one").is_err());
        let mut other = value;
        other["manifest"]["inspection_verification"]["provider"] = json!("codex");
        assert!(validate_review(other, "ven_one", "dwo_one", "device_one").is_err());
    }

    #[test]
    fn general_browser_goal_requires_owned_chrome_and_start_in_origins() {
        let mut value = record();
        value["manifest"]["actions"] = json!(["browser.read", "browser.interact"]);
        value["manifest"]["applications"] = json!(["com.google.Chrome"]);
        value["manifest"]["origins"] = json!(["https://resend.com", "https://www.resend.com"]);
        value["manifest"].as_object_mut().unwrap().remove("browser_verification");
        value["manifest"]["general_browser_verification"] = json!({
            "operation": "general_goal_v1",
            "start_url": "https://resend.com/",
            "allow_type": true,
            "max_steps": 32,
            "fill_values": {"email": "founder@example.com"}
        });
        assert!(validate_review(value.clone(), "ven_one", "dwo_one", "device_one").is_ok());
        let mut bad = value.clone();
        bad["manifest"]["general_browser_verification"]["start_url"] = json!("https://evil.example/");
        assert!(validate_review(bad, "ven_one", "dwo_one", "device_one").is_err());
        let mut loopback = value;
        loopback["manifest"]["origins"] = json!(["http://127.0.0.1:8765"]);
        loopback["manifest"]["general_browser_verification"]["start_url"] =
            json!("http://127.0.0.1:8765/signup.html");
        assert!(validate_review(loopback, "ven_one", "dwo_one", "device_one").is_err());
    }

    fn cuai_browser_record() -> serde_json::Value {
        let mut value = record();
        value["manifest"]["actions"] = json!(["browser.read", "browser.interact"]);
        value["manifest"]["applications"] = json!(["com.google.Chrome"]);
        value["manifest"]["origins"] = json!(["https://example.test"]);
        value["manifest"].as_object_mut().unwrap().remove("browser_verification");
        let role = |output| json!({"provider":"openai", "model":"gpt-6-sol",
            "service_tier":"default", "reasoning":"none", "max_output_tokens":output});
        value["manifest"]["general_browser_verification"] = json!({
            "operation":"general_goal_v1", "start_url":"https://example.test/", "max_steps":32,
            "execution_profile": {"version":"browser_cuai_text_v1", "backend":"cuai",
                "policy_version":1, "roles":{"plan":role(700), "actor":role(1600), "verify":role(300)},
                "pricing_digest":"a".repeat(64), "max_provider_calls":128, "max_request_bytes":65536,
                "completion_policy":"browser_semantic_review_v1"}
        });
        value
    }

    #[test]
    fn cuai_saved_profile_roundtrips_exact_roles_and_limits_without_granting_execution() {
        let value = cuai_browser_record();
        let expected = value["manifest"]["general_browser_verification"]["execution_profile"].clone();
        let reviewed = validate_review(value, "ven_one", "dwo_one", "device_one").unwrap();
        assert_eq!(serde_json::to_value(reviewed).unwrap()["manifest"]
            ["general_browser_verification"]["execution_profile"], expected);
    }

    #[test]
    fn cuai_read_only_uses_existing_read_action_and_false_empty_fill_policy() {
        let mut value = cuai_browser_record();
        value["manifest"]["actions"] = json!(["browser.read"]);
        value["manifest"]["general_browser_verification"]["allow_type"] = json!(false);
        value["manifest"]["general_browser_verification"]["fill_values"] = json!({});
        let parsed = validate_review(value.clone(), "ven_one", "dwo_one", "device_one").unwrap();
        let manifest = serde_json::to_value(parsed).unwrap()["manifest"].clone();
        assert_eq!(manifest["actions"], value["manifest"]["actions"]);
        assert_eq!(manifest["general_browser_verification"], value["manifest"]["general_browser_verification"]);
        for (key, invalid) in [("allow_type", json!(true)), ("fill_values", json!({"name":"Ada"})),
                               ("execution_profile", serde_json::Value::Null)] {
            let mut bad = value.clone();
            bad["manifest"]["general_browser_verification"][key] = invalid;
            assert!(validate_review(bad, "ven_one", "dwo_one", "device_one").is_err());
        }
    }

    fn read_urls_record() -> serde_json::Value {
        let mut value=cuai_browser_record();
        value["manifest"]["actions"]=json!(["browser.read"]);
        value["manifest"]["origins"]=json!(["https://pypi.org","https://github.com"]);
        let spec=&mut value["manifest"]["general_browser_verification"];
        spec["allow_type"]=json!(false); spec["fill_values"]=json!({}); spec["start_url"]=json!("https://pypi.org/");
        spec["read_urls"]=json!(["https://pypi.org/project/requests/","https://pypi.org/project/httpx/",
            "https://pypi.org/project/aiohttp/","https://github.com/psf/requests",
            "https://github.com/encode/httpx","https://github.com/aio-libs/aiohttp"]);
        value
    }

    #[test]
    fn reviewed_read_urls_roundtrip_exact_readonly_pages_and_omit_empty_default() {
        let value=read_urls_record();
        let parsed=validate_review(value.clone(),"ven_one","dwo_one","device_one").unwrap();
        let manifest=serde_json::to_value(parsed).unwrap()["manifest"].clone();
        assert_eq!(manifest["general_browser_verification"],value["manifest"]["general_browser_verification"]);
        let mut empty=value.clone(); empty["manifest"]["general_browser_verification"]["read_urls"]=json!([]);
        let mut absent=value; absent["manifest"]["general_browser_verification"].as_object_mut().unwrap().remove("read_urls");
        let empty=serde_json::to_value(validate_review(empty,"ven_one","dwo_one","device_one").unwrap()).unwrap();
        let absent=serde_json::to_value(validate_review(absent,"ven_one","dwo_one","device_one").unwrap()).unwrap();
        assert_eq!(empty["manifest"],absent["manifest"]);
        assert!(absent["manifest"]["general_browser_verification"].get("read_urls").is_none());
        // Independently computed with Python's sorted ASCII manifest serializer.
        assert_eq!(canonical_digest(absent["manifest"].clone(),None).unwrap(),"62f5106b562f41075b71b21540a6bb0a6573effbe4a51f4a4d7cd729b464a0ce");
        assert_eq!(canonical_digest(manifest,None).unwrap(),"6d58e6f1f6413bc1a4f200b207e5ba64253d111f2e148541a6300c34ed4a55ec");
    }

    #[test]
    fn reviewed_read_urls_refuse_unreviewed_or_mutation_scope_and_invalid_literal_urls() {
        for url in ["http://pypi.org/project/requests/","https://unreviewed.test/page","https://user:password@pypi.org/page",
            "https://pypi.org/project/requests/?page=2","https://pypi.org/project/requests/#secret",
            "https://pypi.org\\unreviewed.test/page","https://pypi.org/project/requests/ ",
            "HTTPS://pypi.org/project/requests/","https://PyPI.org/project/requests/","https://pypi.org",
            "https://pypi.org:443/project/requests/","https://pypi.org/project/../requests/",
            "https://pypi.org/project/%2e%2e/requests/","https://pypi.org/project/café/"] {
            let mut value=read_urls_record(); value["manifest"]["general_browser_verification"]["read_urls"]=json!([url]);
            assert!(validate_review(value,"ven_one","dwo_one","device_one").is_err(),"{url}");
        }
        for urls in [json!(["https://pypi.org/project/requests/","https://pypi.org/project/requests/"]),
            json!(["https://pypi.org/1","https://pypi.org/2","https://pypi.org/3","https://pypi.org/4",
                "https://pypi.org/5","https://pypi.org/6","https://pypi.org/7"]),json!(true),serde_json::Value::Null] {
            let mut value=read_urls_record(); value["manifest"]["general_browser_verification"]["read_urls"]=urls;
            assert!(validate_review(value,"ven_one","dwo_one","device_one").is_err());
        }
        let mut input=read_urls_record(); input["manifest"]["actions"]=json!(["browser.read","browser.interact"]);
        assert!(validate_review(input,"ven_one","dwo_one","device_one").is_err());
        let mut legacy=read_urls_record(); legacy["manifest"]["general_browser_verification"]["execution_profile"]=serde_json::Value::Null;
        assert!(validate_review(legacy,"ven_one","dwo_one","device_one").is_err());
    }

    #[test]
    fn cuai_actor_low_is_the_only_added_model_effort_pair() {
        let prefix = "/manifest/general_browser_verification/execution_profile";
        for (version, policy) in [("browser_cuai_text_v1", 1), ("browser_cuai_text_v2", 2)] {
            let mut value = cuai_browser_record();
            value.pointer_mut(prefix).unwrap()["version"] = json!(version);
            value.pointer_mut(prefix).unwrap()["policy_version"] = json!(policy);
            value.pointer_mut(prefix).unwrap()["roles"]["actor"]["model"] = json!("gpt-6.1-sol");
            value.pointer_mut(prefix).unwrap()["roles"]["actor"]["reasoning"] = json!("low");
            let expected = value.pointer(prefix).unwrap().clone();
            let parsed = validate_review(value.clone(), "ven_one", "dwo_one", "device_one").unwrap();
            assert_eq!(serde_json::to_value(parsed).unwrap()["manifest"]["general_browser_verification"]["execution_profile"], expected);
            for (phase, model, effort) in [("actor", "gpt-6.1-sol", "none"), ("actor", "gpt-6-sol", "low"),
                ("actor", "gpt-6-luna", "low"), ("plan", "gpt-6.1-sol", "low"), ("verify", "gpt-6.1-sol", "low"),
                ("plan", "gpt-6.1-sol", "none"), ("verify", "gpt-6.1-sol", "none")] {
                let mut bad = value.clone();
                bad.pointer_mut(prefix).unwrap()["roles"][phase]["model"] = json!(model);
                bad.pointer_mut(prefix).unwrap()["roles"][phase]["reasoning"] = json!(effort);
                assert!(validate_review(bad, "ven_one", "dwo_one", "device_one").is_err());
            }
        }
    }

    #[test]
    fn cuai_v2_is_a_separate_exact_policy_without_model_or_budget_changes() {
        let mut value = cuai_browser_record();
        value["manifest"]["general_browser_verification"]["execution_profile"]["version"] = json!("browser_cuai_text_v2");
        assert!(validate_review(value.clone(), "ven_one", "dwo_one", "device_one").is_err());
        value["manifest"]["general_browser_verification"]["execution_profile"]["policy_version"] = json!(2);
        let expected = value["manifest"]["general_browser_verification"]["execution_profile"].clone();
        let reviewed = validate_review(value, "ven_one", "dwo_one", "device_one").unwrap();
        assert_eq!(serde_json::to_value(reviewed).unwrap()["manifest"]["general_browser_verification"]["execution_profile"], expected);
    }

    #[test]
    fn cuai_saved_profile_refuses_changed_policy_provider_tier_and_unbounded_calls() {
        let prefix = "/manifest/general_browser_verification/execution_profile/";
        for (field, replacement) in [
            ("version", json!("browser_cuai_text_v2")), ("backend", json!("jev")),
            ("policy_version", json!(2)), ("policy_version", json!(true)),
            ("pricing_digest", json!("A".repeat(64))), ("pricing_digest", json!("a".repeat(63))),
            ("max_provider_calls", json!(0)), ("max_provider_calls", json!(129)),
            ("max_request_bytes", json!(1023)), ("max_request_bytes", json!(65537)),
            ("completion_policy", json!("model_says_done")),
            ("roles/plan/provider", json!("typesafe")), ("roles/actor/service_tier", json!("fast")),
            ("roles/verify/reasoning", json!("high")), ("roles/actor/model", json!("model\n")),
            ("roles/verify/model", json!("m".repeat(101))), ("roles/plan/model", json!("")),
            ("roles/plan/max_output_tokens", json!(0)), ("roles/actor/max_output_tokens", json!(2501)),
            ("roles/verify/max_output_tokens", json!("300")),
        ] {
            let mut value = cuai_browser_record();
            *value.pointer_mut(&format!("{prefix}{field}")).unwrap() = replacement;
            assert!(validate_review(value, "ven_one", "dwo_one", "device_one").is_err(), "{field}");
        }
    }

    #[test]
    fn cuai_saved_profile_is_closed_and_has_no_partial_role_fallback() {
        for path in ["", "roles", "roles/actor"] {
            let mut value = cuai_browser_record();
            let pointer = format!("/manifest/general_browser_verification/execution_profile/{path}");
            let pointer = pointer.trim_end_matches('/');
            value.pointer_mut(pointer).unwrap().as_object_mut().unwrap()
                .insert("provider_endpoint".into(), json!("https://unexpected.example"));
            assert!(validate_review(value, "ven_one", "dwo_one", "device_one").is_err());
        }
        let mut value = cuai_browser_record();
        value["manifest"]["general_browser_verification"]["execution_profile"]["roles"]
            .as_object_mut().unwrap().remove("verify");
        assert!(validate_review(value, "ven_one", "dwo_one", "device_one").is_err());
    }

    #[test]
    fn desktop_review_pins_one_app_and_reviewed_delivery_mode() {
        let mut value = record();
        value["manifest"]["applications"] = json!(["com.google.Chrome"]);
        value["manifest"]["origins"] = json!([]);
        value["manifest"]["actions"] = json!(["desktop.read", "desktop.interact"]);
        value["manifest"]["desktop_verification"] = json!({
            "operation": "desktop_goal_v1", "application_bundle": "com.google.Chrome",
            "process_id": 101, "process_start_identity": "123456789", "window_id": 207,
            "window_title": "Search",
            "delivery_mode": "background", "max_steps": 24,
            "reviewed_text": ["websites that sell pets"]
        });
        assert!(validate_review(value.clone(), "ven_one", "dwo_one", "device_one").is_ok());
        value["manifest"]["desktop_verification"]["founder_goal"] =
            json!("Open Chrome and inspect the selected window.");
        assert!(validate_review(value.clone(), "ven_one", "dwo_one", "device_one").is_ok());
        value["manifest"]["desktop_verification"]["founder_goal"] = json!(" ");
        assert!(validate_review(value.clone(), "ven_one", "dwo_one", "device_one").is_err());
        value["manifest"]["desktop_verification"]["founder_goal"] =
            json!("Open Chrome and inspect the selected window.");
        value["manifest"]["desktop_verification"]["expected_visible_text"] = json!("Completed");
        assert!(validate_review(value.clone(), "ven_one", "dwo_one", "device_one").is_ok());
        value["manifest"]["desktop_verification"]["expected_visible_text"] = json!("private@example.test");
        assert!(validate_review(value.clone(), "ven_one", "dwo_one", "device_one").is_err());
        value["manifest"]["desktop_verification"]["expected_visible_text"] = json!("Completed");
        value["manifest"]["desktop_verification"]["application_bundle"] = json!("com.apple.Safari");
        assert!(validate_review(value.clone(), "ven_one", "dwo_one", "device_one").is_err());
        value["manifest"]["desktop_verification"]["application_bundle"] = json!("com.google.Chrome");
        value["manifest"]["desktop_verification"]["delivery_mode"] = json!("foreground_fallback");
        assert!(validate_review(value.clone(), "ven_one", "dwo_one", "device_one").is_ok());
        value["manifest"]["desktop_verification"]["delivery_mode"] = json!("foreground");
        assert!(validate_review(value, "ven_one", "dwo_one", "device_one").is_err());
    }

    fn document_record() -> serde_json::Value {
        let mut value = record();
        value["manifest"]["applications"] = json!(["com.apple.TextEdit"]);
        value["manifest"]["origins"] = json!([]);
        value["manifest"]["actions"] = json!(["desktop.read", "desktop.interact"]);
        value["manifest"]["desktop_verification"] = json!({
            "operation":"desktop_goal_v1", "application_bundle":"com.apple.TextEdit",
            "process_id":101, "process_start_identity":"123456789", "window_id":207,
            "window_title":"Untitled", "delivery_mode":"background", "max_steps":48,
            "document_text_index":0, "reviewed_text":["Supymem test note\nReviewed second line\nReviewed third line", "note.txt"]
        });
        value
    }

    #[test]
    fn native_document_review_preserves_exact_bounded_three_line_profile() {
        let r = validate_review(document_record(), "ven_one", "dwo_one", "device_one").unwrap();
        let serialized = serde_json::to_value(r).unwrap();
        assert_eq!(serialized["manifest"]["desktop_verification"]["document_text_index"], json!(0));
        assert_eq!(serialized["manifest"]["desktop_verification"]["reviewed_text"],
                   document_record()["manifest"]["desktop_verification"]["reviewed_text"]);
    }

    #[test]
    fn native_document_review_refuses_other_apps_indices_controls_and_shapes() {
        for text in ["one\ntwo", "one\ntwo\nthree\nfour", "one\n\nthree", " one\ntwo\nthree",
                     "one\ntwo \nthree", "one\r\ntwo\nthree", "one\ntw\to\nthree", "one\ntwo\nthree\u{7f}",
                     "one\ntwo\nthree\u{2028}"] {
            let mut r = document_record();
            r["manifest"]["desktop_verification"]["reviewed_text"][0] = json!(text);
            assert!(validate_review(r, "ven_one", "dwo_one", "device_one").is_err(), "{text:?}");
        }
        for index in [json!(1), json!(-1), json!("0"), json!(true)] {
            let mut r=document_record(); r["manifest"]["desktop_verification"]["document_text_index"]=index;
            assert!(validate_review(r, "ven_one", "dwo_one", "device_one").is_err());
        }
        let mut r=document_record(); r["manifest"]["desktop_verification"]["reviewed_text"][0]=json!(format!("{}\nb\nc", "a".repeat(317)));
        assert!(validate_review(r, "ven_one", "dwo_one", "device_one").is_err());
        let mut r=document_record(); r["manifest"]["desktop_verification"]["reviewed_text"][1]=json!("unreviewed\nnewline");
        assert!(validate_review(r, "ven_one", "dwo_one", "device_one").is_err());
        let mut r=document_record(); r["manifest"]["desktop_verification"]["reviewed_text"]=json!([]);
        assert!(validate_review(r, "ven_one", "dwo_one", "device_one").is_err());
        let mut r=document_record(); r["manifest"]["desktop_verification"].as_object_mut().unwrap().remove("document_text_index");
        assert!(validate_review(r, "ven_one", "dwo_one", "device_one").is_err());
        let mut r=document_record(); r["manifest"]["applications"]=json!(["com.apple.Safari"]); r["manifest"]["desktop_verification"]["application_bundle"]=json!("com.apple.Safari");
        assert!(validate_review(r, "ven_one", "dwo_one", "device_one").is_err());
        let mut r=document_record(); r["manifest"]["desktop_verification"]["unreviewed_grant"]=json!(true);
        assert!(validate_review(r, "ven_one", "dwo_one", "device_one").is_err());
    }
    #[test]
    fn explicit_multi_surface_contract_preserves_primary_and_rejects_unreviewed_expansion() {
        let mut value = document_record();
        let first = value["manifest"]["desktop_verification"].clone();
        let surface = json!({"application_bundle":first["application_bundle"],
            "process_id":first["process_id"], "process_start_identity":first["process_start_identity"],
            "window_id":first["window_id"], "window_title":first["window_title"]});
        value["manifest"]["desktop_verification"]["reviewed_surfaces"] = json!([surface]);
        // v1 must never silently gain the new authority list, even if gate is enabled.
        assert!(validate_review(value.clone(), "ven_one", "dwo_one", "device_one").is_err());
        value["manifest"]["desktop_verification"]["operation"] = json!("desktop_goal_v2");
        let mut verification: super::DesktopVerification = serde_json::from_value(
            value["manifest"]["desktop_verification"].clone()).unwrap();
        let apps = vec![verification.application_bundle.clone()];
        assert!(super::reviewed_desktop_surfaces_valid_with_gate(&verification, &apps, true));
        assert!(!super::reviewed_desktop_surfaces_valid_with_gate(&verification, &apps, false));
        verification.reviewed_surfaces.as_mut().unwrap()[0].window_id += 1;
        assert!(!super::reviewed_desktop_surfaces_valid_with_gate(&verification, &apps, true));
        // Default process environment is off; opt-in full-chain validation is root QA.
        if std::env::var("SUPYMEM_REVIEWED_DESKTOP_V2").as_deref() != Ok("1") {
            assert!(!super::reviewed_desktop_surfaces_valid(&verification, &apps));
        }
        value["manifest"]["desktop_verification"]["reviewed_surfaces"][0]["cold_launch"] = json!(true);
        assert!(serde_json::from_value::<super::DesktopVerification>(
            value["manifest"]["desktop_verification"].clone()).is_err());
    }

}
