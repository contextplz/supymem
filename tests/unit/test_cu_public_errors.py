"""Public failures admit exact source-owned codes, never naming conventions."""
import pytest
from desktop_runtime.cu_public_errors import public_exception_code, public_exception_type, public_failure_code

@pytest.mark.parametrize("code", ["native_order_identity_changed", "native_worker_upstream_rejected",
    "step_cap_reached", "time_cap_reached", "no_progress", "blocked_secure_text_field",
    "background_effect_refused", "ax_write_unconfirmed_verify_before_retry",
    "fresh_browser_observation_required", "route_retry_exhausted", "subgoal_reported_complete", "subgoal_completion_rejected"])
def test_known_authority_caps_and_effect_codes_survive(code):
    assert public_exception_code(RuntimeError(code), "failed") == code

@pytest.mark.parametrize("text", ["private_api_token", "native_private_account_id", "no_progress_private", "path/secret"])
def test_unknown_error_shapes_are_private(text):
    assert public_exception_code(RuntimeError(text), "native_action_failed") == "native_action_failed"
    assert public_failure_code(text, "failed") == "failed"

def test_private_exception_class_and_throwing_string_stay_private():
    from desktop_runtime.owned_coding import coding_failure_details
    class private_api_token(Exception):
        def __str__(self):
            raise RuntimeError("private")
    assert public_exception_code(private_api_token(), "failed") == "failed"
    assert public_exception_type(private_api_token()) == "Exception"
    details = coding_failure_details(private_api_token(), "check")
    assert details["failure_code"] == "native_execution_failed"
    assert details["exception_type"] == "Exception"


def test_fixed_single_mutation_receipt_remains_available():
    assert public_failure_code("applied", "withheld") == "applied"


@pytest.mark.parametrize("domain", ["Freight invoice review", "Family recipe planner"])
def test_exception_details_never_include_private_messages_paths_or_malformed_metadata(domain):
    from desktop_runtime.cu_public_errors import public_exception_details, safe_exception_details
    try:
        raise ValueError(domain + "/private-key")
    except ValueError as error:
        details = public_exception_details(error)
    assert {key: value for key, value in details.items() if key != "exception_frames"} == {
        "exception_type": "ValueError", "exception_code": "native_execution_failed"}
    assert len(details["exception_frames"]) == 1
    assert details["exception_frames"][0].startswith("external:")
    assert safe_exception_details({"exception_type": [domain], "exception_code": [domain],
        "exception_location": "/Users/" + domain + "/private-key:12", "message": domain}) == {}


@pytest.mark.parametrize("domain", ["Freight invoice review", "Family recipe planner"])
def test_exception_frames_keep_the_full_cause_chain_without_private_text(tmp_path, domain):
    from desktop_runtime.cu_public_errors import public_exception_details, safe_exception_details
    from core.artifact_policy import ArtifactRejected, write_file

    target = tmp_path / domain
    target.mkdir()
    alias = tmp_path / "untrusted"
    alias.symlink_to(target, target_is_directory=True)
    try:
        write_file(alias, "index.html", domain.encode())
    except ArtifactRejected as error:
        details = public_exception_details(error)
        cause = error.__cause__.__traceback__
        while cause.tb_next is not None:
            cause = cause.tb_next
        cause_location = f"{cause.tb_frame.f_globals['__name__']}:{cause.tb_lineno}"
    frames = details["exception_frames"]
    assert frames[-1] == cause_location
    assert len([frame for frame in frames if frame.startswith("core.artifact_policy:")]) >= 4
    assert any(frame.startswith("contextlib:") for frame in frames)
    assert safe_exception_details(details) == details
    assert domain not in str(details) and str(tmp_path) not in str(details)
    assert safe_exception_details({"exception_frames": [domain + ":12", "/Users/private:12"]}) == {}
