"""The first local product profile, described honestly without a model call."""
import json

from pydantic import BaseModel, ConfigDict


class ProductScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    profile: str = "web_app_vercel_v1"
    in_scope: list[str]
    not_in_this_build: list[str]
    reasons: list[str]


class UnsupportedProductScope(ValueError):
    def __init__(self, scope: ProductScope):
        super().__init__("device_product_scope_unsupported")
        self.reasons = scope.not_in_this_build[1:]


def assert_supported(scope: ProductScope) -> None:
    if len(scope.not_in_this_build) > 1:
        raise UnsupportedProductScope(scope)


def assess(snapshot: dict) -> ProductScope:
    # shortcut: keyword checks conservatively restrict this dependency-free profile;
    # upgrade when a richer executable profile has a reviewed contract.
    text = json.dumps({"request": snapshot.get("request"),
        "tasks": (snapshot.get("plan") or {}).get("tasks", [])}, ensure_ascii=False).lower()
    excluded = ["Data stays in this demo; it is not saved between devices."]
    for words, reason in (
        (("database", "postgres", "mysql", "persist", "saved", "saves", "between devices"),
         "A shared database and durable storage are not included in this build."),
        (("login", "log in", "sign in", "authentication", "accounts"),
         "User accounts and secure sign-in are not included in this build."),
        (("payment", "checkout", "stripe", "billing"),
         "Payments and billing need a separately connected provider."),
        (("ios", "android", "native mobile", "hardware", "sensor"),
         "Native mobile apps and physical hardware are not supported by this local web build."),
        (("paid api", "licensed data", "openai", "external api", "third-party api"),
         "Paid APIs and licensed external data are not included in this build."),
    ):
        if any(word in text for word in words):
            excluded.append(reason)
    return ProductScope(in_scope=["A working web page and a small API, including /api/health.",
        "Local tests and demo data, using Node with no installed dependencies.",
        "A local preview; nothing is deployed or published."],
        not_in_this_build=excluded,
        reasons=["This local web build runs without package downloads or paid services."])
