"""Explicit defect sync with reconciliation after uncertain creates; never blind retry."""
import json
from fastapi import HTTPException
from ..dependencies import require_current_role
from ..persistence.db import connection
from ..services.registry import get_jira_gateway, get_jira_integration_service
from ..services.jira_gateway import _adf_paragraph, _jql_quote
from . import service as s


def sync(ctx, key, payload):
    with connection(immediate=True):
        require_current_role(ctx, 'test_manager')
        d = s.get('defects', key, ctx.customer_id)
        s.check_revision(d, payload.revision)
        if not s.policy(ctx.customer_id)['jira_enabled']:
            raise HTTPException(409, 'Enable defect sync in Administration > Validation first')
        config = get_jira_integration_service().get_for_customer(ctx.customer_id)
        if not config:
            raise HTTPException(409, 'Configure the customer Jira connection first')
        # Gateway construction is local and validates saved configuration.
        try:
            gateway = get_jira_gateway(ctx.customer_id)
        except Exception:
            raise HTTPException(409, 'The customer Jira connection is not available') from None
        previous = d['sync_state']
        d.update(sync_state='syncing', revision=d['revision'] + 1)
        s.store('defects').put(key, d)
    uncertain = previous in ("syncing", "needs_reconciliation")
    try:
        jira_key = d['jira_key']
        if not jira_key:
            label = 'jade-' + key
            found = gateway._post(config.base_url, '/rest/api/3/search/jql', json={
                'jql': f'project = "{_jql_quote(config.project_key)}" AND labels = "{label}"',
                'fields': ['summary'], 'maxResults': 2}).json().get('issues', [])
            if len(found) > 1:
                raise RuntimeError('Multiple matching issues require administrator review')
            if found:
                jira_key = found[0]['key']
            elif previous in ('syncing', 'needs_reconciliation'):
                raise RuntimeError('An earlier create may have reached Jira. No duplicate will be created. Check Jira and retry reconciliation after indexing completes')
            else:
                with connection(immediate=True):
                    # Persist uncertainty BEFORE the external side effect.
                    current = s.get('defects', key, ctx.customer_id)
                    current['sync_state'] = 'needs_reconciliation'
                    s.store('defects').put(key, current)
                    require_current_role(ctx, 'test_manager')
                body = (f"JADE defect {key}\nPlan: {d['plan_id']}\nBuild: {d['build']}\n"
                        f"Scenario: {d['scenario_id']}\nRun: {d['run_id']}\nSeverity: {d['severity']}\n"
                        f"{d['note']}\nExpected/actual observations: {json.dumps(d['steps'])[:8000]}\nEvidence IDs (access in JADE): {', '.join(d['evidence'])}")
                uncertain = True
                result = gateway._post(config.base_url, '/rest/api/3/issue', json={'fields': {
                    'project': {'key': config.project_key}, 'issuetype': {'name': s.policy(ctx.customer_id)['jira_issue_type']},
                    'summary': ('[JADE validation] ' + d['title'])[:255], 'description': _adf_paragraph(body), 'labels': [label]}}).json()
                jira_key = result['key']
        status = gateway._get(config.base_url, '/rest/api/3/issue/' + jira_key, params={'fields':'status'}).json()
        with connection(immediate=True):
            current = s.get('defects', key, ctx.customer_id)
            current.update(jira_key=jira_key, jira_status=status['fields']['status']['name'], sync_state='synced',
                           sync_error='', synced_at=s.now(), revision=current['revision'] + 1)
            s.store('defects').put(key, current)
            s.audit(ctx.customer_id, ctx.identity.id, 'defect_synced', key, jira_key)
            return current
    except Exception:
        with connection(immediate=True):
            current = s.get('defects', key, ctx.customer_id)
            current.update(sync_state='needs_reconciliation' if uncertain else 'pending',
                           sync_error='Sync could not be confirmed. Check Jira; retry searches for the existing issue without creating a duplicate.',
                           revision=current['revision'] + 1)
            s.store('defects').put(key, current)
        raise HTTPException(409, current['sync_error']) from None
    finally:
        gateway._http.close()
