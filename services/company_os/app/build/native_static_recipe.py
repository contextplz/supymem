"""Review-visible static implementation recipe for the exact owned app."""
from app.core.local_mode import is_local
from app.db.supymem_models import FounderApp


# Founder directive 2026-10-05: every venture gets its own page, never a shared template look.
DESIGN_DIRECTION = (
    'Design direction: this page must look made for this one venture, not like a template. First decide a '
    'visual direction from the venture itself (its audience, tone, problem and the approved copy): pick a '
    'distinct layout, type pairing (bundled or system fonts), color palette and illustration style that fit '
    'it. Avoid the generic centered-hero, three-feature-cards, waitlist-band, footer pattern unless it truly '
    'suits this venture. Write the approved copy as the page\'s own voice. The early-access link is part of '
    'this design (styled and placed in the page\'s own way), not a bolted-on generic section. Stay honest: '
    'no invented features, numbers, logos, people or testimonials.\n'
)

STATIC_RENDER_RECIPE = DESIGN_DIRECTION + (
        'Static browser implementation recipe: render every required exact text and declared visual '
        'visibly on initial load, including a non-interactive preview of text an action later reveals. '
        'Keep concept/proposed workflows clearly labelled; do not claim working automation. '
        'Every declared control must have its exact DOM id, role, name and target. '
        'HTTPS external links must use target="_blank" and rel="noopener noreferrer". '
    )

STATIC_IMPLEMENTATION_RECIPE = STATIC_RENDER_RECIPE + (
        'Only approved managed-waitlist controls may emit cta_click; no fake local signup. '
        'The following app-bound public ingest-only platform beacon is the sole analytics script: '
        'include it exactly once before </body>, without replacement tracking code. '
        'It sends anonymous closed event names/path/referrer host, honors privacy/check markers, '
        'and grants no model, account, source-read, execution or publication authority.\n'
    )


def reviewed_recipe(session, snapshot, *, workspace_id, venture_id):
    if snapshot.get('artifact_profile') != 'static_landing_v1':
        return ''
    baseline = snapshot.get('publication_baseline') or {}
    app_id = baseline.get('app_id')
    app = session.get(FounderApp, app_id) if app_id else None
    if (app is None or app.deleted_at is not None
            or (app.workspace_id, app.venture_id) != (workspace_id, venture_id)):
        raise ValueError('device_static_analytics_app_unavailable')
    from app.build.subscription_static_baseline import assert_current
    assert_current(session, app, baseline)
    # This is the existing public ingest-only app key/beacon, never a secret
    # credential or provider key. The entire script is in the approved manifest.
    from app.hosting.revisions import _public_analytics_script
    beacon = _public_analytics_script(app.id).strip()
    if is_local():
        from app.product_analytics.service import enabled
        recipe = STATIC_RENDER_RECIPE + (
            'Implementation belongs to your connected coding tool: include the measurement contract '
            'before source is frozen; Supymem checks the result and must never rewrite frozen source. '
            'Use a light theme. '
        )
        if beacon:
            recipe += (
                'Include the following public ingest-only measurement reference exactly once before </body>. '
                'Preserve its privacy and check markers, anonymous closed events and consented signup. '
                'Never include provider keys, private app keys, cookies or personal form values.\n'
            )
        elif enabled('page_measurement'):
            recipe += (
                'For this private preview, omit measurement scripts because no public destination is configured. '
                'Before publishing, set your site URL in Settings > Keys and connections > Publishing, '
                'then ask your coding tool to add the saved measurement reference and check the new source. '
                'Do not claim that this preview records visits or signups.\n'
            )
        else:
            recipe += 'Page measurement is off: omit measurement scripts and replacement tracking code.\n'
        return recipe + beacon + '\n\n'
    return STATIC_IMPLEMENTATION_RECIPE + beacon + '\n\n'
