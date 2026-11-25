"""The Mac bridge accepts only an exact, screenshot-bound input-edit proposal."""
import base64
import hashlib

import pytest

from coding_agents.jev_decision import Control, Observation
from desktop_runtime.device_gateway import WorkOrderGateway


@pytest.mark.parametrize("input_edit_id,accepted", [("c1", True), ("none", False), ("c2", False)])
def test_delete_visual_classification_is_bound_to_selected_control(input_edit_id, accepted):
    png = base64.b64encode(b"bounded-image").decode()
    observation = Observation(revision="ps1_abc", application="com.example.app",
        location="mac-window:10", goal="Clear the current input", context="",
        controls=[Control(id="c1", label="Delete", operations=["click"], visible=True, enabled=True),
                  Control(id="c2", label="Other", operations=["click"], visible=True, enabled=True)],
        permitted=True, confirmation_pending=False, prior_effect_uncertain=False)

    def request(method, path, body):
        if method == "GET" and path.endswith("/authority"):
            return {"current": True}
        if method == "GET":
            return {"id": "dwo_one", "venture_id": "ven_one", "device_id": "dev_one",
                    "state": "claimed", "execution_id": "dex_one", "review_digest": "a" * 64}
        assert method == "POST" and path.endswith("/desktop-vision")
        assert body["observation"]["goal"] == observation.goal
        return {"authority": "observation_only", "snapshot_id": "ps1_abc",
                "image_sha256": hashlib.sha256(b"bounded-image").hexdigest(),
                "observation_digest": observation.digest(),
                "proposal_invocation_id": "din_one",
                "report": {"verdict": "continue", "visible_facts": ["Input keypad visible"],
                           "next_step": "Clear input", "click_control_id": "c1",
                           "input_edit_control_id": input_edit_id}}

    gateway = WorkOrderGateway(venture_id="ven_one", order_id="dwo_one",
                               device_id="dev_one", request=request)
    gateway.claimed, gateway.execution_id = True, "dex_one"
    if accepted:
        assert gateway.review_desktop_image("din_one", "ps1_abc", png, observation)["report"][
            "input_edit_control_id"] == "c1"
    else:
        with pytest.raises(PermissionError, match="native_vision_response_invalid"):
            gateway.review_desktop_image("din_one", "ps1_abc", png, observation)
