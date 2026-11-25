"""Bind all existing chat subroutes to an authorized conversation for this request."""
from fastapi import HTTPException
from fastapi.routing import APIRoute
from app.chat.conversations import bind_conversation
from app.tenancy import bind, current_or_none


class ConversationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def scoped_handler(request):
            venture_id = request.path_params.get("venture_id")
            if not venture_id:
                return await handler(request)
            # Standalone signed router tests use the same authority as main middleware.
            scope = current_or_none()
            if scope is None or scope.venture_id != venture_id:
                from app.admin.authn import require_signed_caller, verify_caller
                from app.authz.workspace import assert_venture_access
                subject = request.headers.get("X-Subject")
                if require_signed_caller() or request.headers.get("X-COS-Assertion"):
                    subject = verify_caller(request).sub
                    if not subject:
                        raise HTTPException(401, detail={"code": "unauthenticated"})
                    from app.authz.http import bind_subject_header
                    bind_subject_header(request, subject)
                if subject:
                    _, wid = assert_venture_access(venture_id, subject)
                else:
                    from app.db.models import Venture
                    from app.state import db
                    with db()() as session:
                        venture = session.get(Venture, venture_id)
                        if venture is None:
                            raise HTTPException(404, "not_found")
                        wid = venture.workspace_id
                with bind(workspace_id=wid or "none", venture_id=venture_id, actor=subject or "anonymous"):
                    return await self._conversation_handler(request, handler, venture_id)
            return await self._conversation_handler(request, handler, venture_id)

        return scoped_handler

    async def _conversation_handler(self, request, handler, venture_id):
        # Catalog/create routes require actor scope but do not select a chat.
        if request.url.path.endswith("/chat/conversations"):
            return await handler(request)
        cid = request.path_params.get("conversation_id") or request.query_params.get("conversation_id")
        if request.method in {"POST", "PUT", "PATCH"}:
            try:
                value = await request.json()
            except ValueError:
                value = None
            if isinstance(value, dict) and value.get("conversation_id") is not None:
                if cid and cid != value["conversation_id"]:
                    raise HTTPException(422, detail={"error": {"code": "chat_conversation_scope_conflict"}})
                cid = value["conversation_id"]
        if cid is not None and (not isinstance(cid, str) or not 1 <= len(cid) <= 128):
            raise HTTPException(422, detail={"error": {"code": "chat_conversation_invalid"}})
        try:
            with bind_conversation(venture_id, cid):
                return await handler(request)
        except ValueError as exc:
            if str(exc) == "chat_conversation_not_found":
                raise HTTPException(404, detail={"error": {"code": str(exc)}}) from exc
            raise
