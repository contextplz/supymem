"""Freeze the actual published target in the confirmation; recheck on execution."""
from dataclasses import replace
from sqlalchemy import select
from app.chat.actions import get_action
from app.chat.confirm import ConfirmationCard
from app.db.supymem_models import Deployment, FounderApp
from app.state import db


def _reply(text, actions=None, kind="text"):
    return {"kind": kind, "body_md": text, "actions": actions or [], "citations": []}


def prepare(venture_id):
    with db()() as session:
        rows = session.execute(select(FounderApp, Deployment).join(Deployment, Deployment.app_id == FounderApp.id)
            .where(FounderApp.venture_id == venture_id, FounderApp.deleted_at.is_(None),
                   Deployment.state == "live", Deployment.target == "supymem_static")).all()
        if len(rows) != 1:
            return _reply("I could not identify one current Supymem-hosted website to unpublish. Nothing changed. Open Website to review the hosting target.",
                          [{"label": "Open Website", "href": f"/v/{venture_id}/site"}], "needs_you")
        app, dep = rows[0]
        card = replace(ConfirmationCard.for_action(get_action("site.unpublish"),
            interpretation=f"Take this public website offline: {dep.url}",
            consequences=["Public visitors will receive an unavailable page once hosting confirms the change.",
                          "The accepted source, private preview and saved waitlist submissions are retained.",
                          "The operation is queued; I will not claim the website is offline before provider and public readback verify it."],
            resulting_state={"app_id": app.id, "deployment_id": dep.id, "version": dep.version}),
            confirm_label="Unpublish landing page").as_action()
        return _reply("Confirm to unpublish this exact current website. Your saved source remains available to publish again.", [card], "confirmation")


def execute(venture_id, payload, actor):
    from app.hosting.unpublish import queue
    from app.hosting.service import HostingError
    if not actor:
        raise PermissionError("unpublish_chat_authentication_required")
    with db()() as session:
        app = session.get(FounderApp, payload.get("app_id"))
        dep = session.get(Deployment, payload.get("deployment_id"))
        if (not app or app.venture_id != venture_id or not dep or dep.app_id != app.id
            or dep.version != payload.get("version")):
            return _reply("This confirmation no longer matches this venture's website. Nothing changed. Ask again to review its current published landing page.")
    try:
        result = queue(app.id, dep.id, actor=actor)
    except HostingError:
        return _reply("The current website could not be queued for unpublishing. It may have changed or another publication is in progress. Open Website to review its current state.",
                      [{"label": "Open Website", "href": f"/v/{venture_id}/site"}], "needs_you")
    return _reply("This website is already verified offline." if result["state"] == "unpublished" else
                  "Unpublishing is queued. The website may still be public until hosting and public readback verify the change. Open Website to check completion or retry.",
                  [{"label": "Open Website", "href": f"/v/{venture_id}/site"}])
