"""Reviewed native sub-goal budgets and observation-bound recovery."""
from dataclasses import dataclass, field
import hashlib
import json
import math
import time

from desktop_runtime.cu_ai_retries import semantic_state


@dataclass
class Recovery:
    postconditions: list[dict] = field(default_factory=list)
    started_steps: int = 0
    started_elapsed: float = 0
    alternate_used: bool = False
    alternate_pending: bool = False
    pending_screen: str = ""
    last_action: str = ""
    failed_action: str = ""
    last_method: str = ""
    hint: str = ""

    def plan(self, goals, state):
        if not isinstance(goals, list) or not 1 <= len(goals) <= 8:
            raise ValueError("subgoal_postcondition_invalid")
        names, conditions = [], []
        for goal in goals:
            condition = goal.get("postcondition") if isinstance(goal, dict) else None
            if (not isinstance(goal, dict) or set(goal) != {"goal", "postcondition"}
                    or not isinstance(goal["goal"], str) or not 0 < len(goal["goal"]) <= 300
                    or not isinstance(condition, dict) or not condition
                    or not set(condition) <= {"window_title", "role", "label", "value"}
                    or any(not isinstance(value, str) or not 0 < len(value) <= 320 for value in condition.values())
                    or set(condition) == {"role"}
                    or ("label" in condition or "value" in condition) and "role" not in condition):
                raise ValueError("subgoal_postcondition_invalid")
            names.append(goal["goal"])
            conditions.append(condition)
        self.postconditions = conditions
        self.advance(state)
        return names

    def advance(self, state):
        self.started_steps = state.steps
        self.started_elapsed = int(time.monotonic() - state.started)
        self.alternate_used = False
        self.alternate_pending = False
        self.pending_screen = self.last_action = self.failed_action = self.last_method = self.hint = ""

    def snapshot(self):
        return dict(self.__dict__)

    def restore(self, value, state):
        if not isinstance(value, dict) or set(value) != set(self.__dict__):
            raise ValueError("checkpoint_invalid_or_task_changed")
        conditions = value.get("postconditions")
        if not isinstance(conditions, list) or len(conditions) != len(state.subgoals):
            raise ValueError("checkpoint_invalid_or_task_changed")
        self.plan([{"goal": name, "postcondition": condition}
                   for name, condition in zip(state.subgoals, conditions)], state)
        if (type(value["started_steps"]) is not int or not 0 <= value["started_steps"] <= state.steps
                or type(value["started_elapsed"]) not in {int, float}
                or not math.isfinite(value["started_elapsed"]) or not 0 <= value["started_elapsed"] <= time.monotonic() - state.started
                or type(value["alternate_used"]) is not bool
                or type(value["alternate_pending"]) is not bool
                or value["alternate_pending"] and not value["alternate_used"]
                or any(not isinstance(value[key], str) or len(value[key]) > 700
                       for key in ("pending_screen", "last_action", "failed_action", "last_method", "hint"))):
            raise ValueError("checkpoint_invalid_or_task_changed")
        self.__dict__.update(value)

    def budget(self, state):
        count = max(1, len(self.postconditions))
        return (max(1, math.ceil(state.caps.max_steps / count)), state.caps.max_seconds / count)

    def expired(self, state):
        if state.subgoal >= len(self.postconditions):
            return None
        steps, seconds = self.budget(state)
        if state.steps - self.started_steps >= steps:
            return "subgoal_step_cap_reached"
        if time.monotonic() - state.started - self.started_elapsed >= seconds:
            return "subgoal_time_cap_reached"
        return None

    def observe(self, observation):
        screen = hashlib.sha256(observation.png).hexdigest() if observation.png else semantic_state(observation)
        stalled = self.pending_screen == screen
        self.pending_screen = ""
        return stalled

    def alternate(self):
        if self.alternate_used:
            return False
        self.alternate_used = True
        self.alternate_pending = True
        self.failed_action = self.last_action
        self.pending_screen = ""
        self.hint = ("The last method made no progress. Try one different authorized method, not the same action route again. "
                     "Use a scoped keyboard action or screenshot fallback if accessibility targeting failed. Never raise the window or replay an uncertain input.")
        return True

    @staticmethod
    def accessibility_available(observation):
        return any((element.get("ax_role") or element.get("role", "")).startswith("AX")
                   and element.get("id") and element.get("enabled") is not False and element.get("is_enabled") is not False
                   and (element.get("ax_role") or element.get("role")) not in {"AXWindow", "AXStaticText", "AXHeading", "AXTitle"}
                   for element in observation.elements)

    def admit(self, action, observation, state):
        expired = self.expired(state)
        if expired:
            return expired
        if state.subgoal >= len(self.postconditions):
            return "subgoal_postcondition_unmet"
        target = next((element for element in observation.elements if element.get("id") == action.element_id), {})
        identity = {key: target.get(key) for key in ("role", "ax_role", "label", "value")}
        signature = hashlib.sha256(json.dumps([action.op, identity, action.text, action.chord,
                                               action.x, action.y, action.direction, action.amount], sort_keys=True).encode()).hexdigest()
        method = "accessibility" if action.element_id else "keyboard" if action.op in {"key", "type"} else "screenshot"
        if signature in {self.last_action, self.failed_action} or self.alternate_pending and method == self.last_method:
            return "no_progress"
        controls = self.accessibility_available(observation)
        if action.op in {"click", "double_click", "right_click", "drag"} and not action.element_id and controls and not self.alternate_used:
            self.hint = "Use the current accessibility element ID first. Screenshot coordinates are a fallback only when accessibility targeting is unavailable or has failed."
            return "accessibility_target_required"
        self.last_action, self.last_method = signature, method
        self.alternate_pending = False
        self.pending_screen = hashlib.sha256(observation.png).hexdigest() if observation.png else semantic_state(observation)
        return None

    def satisfied(self, observation, state):
        if not observation.snapshot_id or observation.pid is None or state.subgoal >= len(self.postconditions):
            return False
        condition = self.postconditions[state.subgoal]
        if "window_title" in condition and observation.window_title != condition["window_title"]:
            return False
        fields = {key: value for key, value in condition.items() if key != "window_title"}
        return not fields or any(all((element.get("ax_role") or element.get("role")) == value if key == "role"
                                    else element.get(key) == value for key, value in fields.items())
                                 and not any(element.get(key) is True for key in ("label_truncated", "value_truncated"))
                                 for element in observation.elements)
