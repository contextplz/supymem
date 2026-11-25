"""Import an immutable generated product into a platform-owned image context.

This validates transport/layout, not behavior, ownership, approval or QA. The
controller must resolve the authorized artifact and approved runtime digest.
Generated source cannot replace the runtime, Dockerfile or dependency lock.
"""
import ast
from pathlib import Path, PurePosixPath

from .artifact_policy import ArtifactRejected
from .artifact_snapshot import import_source, validate_manifest

REQUIRED = {"product/__init__.py", "product/routes.py", "product/static/index.html"}
STATIC_EXTENSIONS = {".html", ".css", ".js", ".mjs", ".json", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff", ".woff2", ".txt"}
HTTP_RECORD_ENDPOINTS = {
    "product_records", "create_product_record", "update_product_record", "delete_product_record",
}
RECORD_SERVICES = {
    "list_records_for_subject", "create_record_for_subject", "update_record_for_subject",
}
RECORD_DICT_FIELDS = {"id", "kind", "payload", "created_at", "updated_at"}


def _reject_record_object_access(tree: ast.AST) -> None:
    """The platform record service returns dictionaries, never model objects.

    Follow the common local aliases and helper calls so a generated route cannot
    pass artifact preflight with ``record.id`` or ``record.payload`` and then
    fail its first authenticated request. This is a bounded contract check, not
    a substitute for authenticated runtime QA.
    """
    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module == "app.records"
        for alias in node.names if alias.name in RECORD_SERVICES
    }
    if not imported:
        return
    functions = {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    inspected: set[tuple[str, frozenset[str]]] = set()

    def service_call(value: ast.AST) -> bool:
        return isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in imported

    def uses_record(value: ast.AST, names: set[str]) -> bool:
        if isinstance(value, ast.Name):
            return value.id in names
        if isinstance(value, ast.Subscript):
            return uses_record(value.value, names)
        return service_call(value)

    def inspect_function(function: ast.FunctionDef | ast.AsyncFunctionDef, seeded: set[str]) -> None:
        key = (function.name, frozenset(seeded))
        if key in inspected:
            return
        inspected.add(key)
        names = set(seeded)
        # A short fixed point handles aliases created before/after their source.
        for _ in range(len(list(ast.walk(function))) + 1):
            before = len(names)
            for node in ast.walk(function):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    value = node.value
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    if value is not None and uses_record(value, names):
                        names.update(target.id for target in targets if isinstance(target, ast.Name))
                elif isinstance(node, (ast.For, ast.AsyncFor)) and uses_record(node.iter, names):
                    if isinstance(node.target, ast.Name):
                        names.add(node.target.id)
                elif isinstance(node, ast.comprehension) and uses_record(node.iter, names):
                    if isinstance(node.target, ast.Name):
                        names.add(node.target.id)
            if len(names) == before:
                break
        for node in ast.walk(function):
            if (isinstance(node, ast.Attribute) and node.attr in RECORD_DICT_FIELDS
                    and uses_record(node.value, names)):
                raise ArtifactRejected("artifact_product_record_contract_invalid")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions:
                callee = functions[node.func.id]
                parameters = [arg.arg for arg in callee.args.posonlyargs + callee.args.args]
                passed = {parameters[index] for index, arg in enumerate(node.args)
                          if index < len(parameters) and uses_record(arg, names)}
                if passed:
                    inspect_function(callee, passed)

    for function in functions.values():
        inspect_function(function, set())


def _reject_record_endpoint_imports(tree: ast.AST) -> None:
    """Generated routes must call the tenant-scoped service, not FastAPI handlers.

    FastAPI injects ``subject`` only during HTTP dispatch. A direct Python
    call instead receives a Depends sentinel and returns the wrong shape.
    """
    module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "app.records" and any(
                alias.name in HTTP_RECORD_ENDPOINTS for alias in node.names
            ):
                raise ArtifactRejected("artifact_product_route_contract_invalid")
            if node.module == "app" and any(alias.name == "records" for alias in node.names):
                module_names.update(alias.asname or alias.name for alias in node.names if alias.name == "records")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app.records":
                    module_names.add(alias.asname or "app")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in HTTP_RECORD_ENDPOINTS:
            continue
        receiver = node.value
        if isinstance(receiver, ast.Name) and receiver.id in module_names:
            raise ArtifactRejected("artifact_product_route_contract_invalid")
        if (isinstance(receiver, ast.Attribute) and receiver.attr == "records"
                and isinstance(receiver.value, ast.Name) and receiver.value.id in module_names):
            raise ArtifactRejected("artifact_product_route_contract_invalid")


def literal_product_routes(tree: ast.AST) -> dict[str, set[str]]:
    """Return literal router paths as mounted by the fixed product scaffold."""
    _reject_record_endpoint_imports(tree)
    _reject_record_object_access(tree)
    routes: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name) and decorator.func.value.id == "router"
                    and decorator.func.attr in {"get", "post", "put", "patch", "delete"}):
                continue
            if not decorator.args or not isinstance(decorator.args[0], ast.Constant) or not isinstance(decorator.args[0].value, str):
                continue
            path = decorator.args[0].value
            if not path.startswith("/") or path.startswith("//") or path.startswith("/api/") or "?" in path or "#" in path:
                raise ArtifactRejected("artifact_product_route_contract_invalid")
            routes.setdefault(decorator.func.attr, set()).add(path)
    return routes


def validate_product_manifest(manifest: list[dict]) -> None:
    validate_manifest(manifest)
    paths = {row["path"] for row in manifest}
    if not REQUIRED <= paths:
        raise ArtifactRejected("application_entrypoints_required")
    for name in paths:
        path = PurePosixPath(name)
        if path.parts[0] != "product" or len(path.parts) < 2:
            raise ArtifactRejected("application_platform_override_denied")
        if any(part.startswith(".") for part in path.parts):
            raise ArtifactRejected("application_hidden_source_denied")
        if path.parts[1] == "static":
            if len(path.parts) < 3 or path.suffix.lower() not in STATIC_EXTENSIONS:
                raise ArtifactRejected("application_public_asset_invalid")
        elif path.suffix != ".py" or any(not part.isidentifier() for part in path.parts[1:-1]) or not path.stem.isidentifier():
            raise ArtifactRejected("application_python_source_required")


def import_product(blob: bytes, destination: Path, *, manifest: list[dict], sha256: str, size_bytes: int,
                   exact_secrets=()):
    validate_product_manifest(manifest)
    return import_source(blob, destination, manifest=manifest, sha256=sha256,
                         size_bytes=size_bytes, exact_secrets=exact_secrets)
