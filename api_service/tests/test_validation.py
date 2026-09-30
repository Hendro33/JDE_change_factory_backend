"""Real persistence/HTTP/worker integration; only ERP and story boundary fixtures are synthetic."""
import time
import pytest
from .conftest import headers
from jde_api_service.validation import service as s, worker, runners

H = headers('vdb')

def post(client, path, data):
    r = client.post('/validation' + path, headers=H, json=data)
    assert r.status_code == 200, r.text
    return r.json()

@pytest.fixture
def setup(client, monkeypatch):
    material = {'user_story': {'acceptance_criteria': [{'id': 'AC1', 'text': 'Correct total'}]}}
    monkeypatch.setattr(s, 'source', lambda company, key: {'id': key, 'title': 'Order totals', 'hash': s.digest(material), 'material': material})
    env = post(client, '/environments', {'name':'UAT', 'stage':'UAT','jde_environment':'PY920','enabled':True})
    scenario = post(client, '/scenarios', {'title':'Verify total','story_id':'ST1','criteria':['AC1'], 'steps':[{'id':'s1','action':'Read total','expected':'Correct total'}]})
    scenario = post(client, '/scenarios/'+scenario['id']+'/approve', {'revision':scenario['revision']})
    plan = post(client, '/plans', {'title':'Order acceptance','story_ids':['ST1'],'environment_ids':[env['id']], 'selections':[{'scenario_id':scenario['id'],'version':1}]})
    plan = post(client, '/plans/'+plan['id']+'/approve', {'revision':plan['revision']})
    dep = post(client, '/deployments', {'environment_id':env['id'],'story_ids':['ST1'],'build':'build-1','evidence':'CNC confirmed deployment in UAT'})
    return env, scenario, plan, dep, material

def start(client, setup, key='operation-0001'):
    env, scenario, plan, dep, _ = setup
    return post(client, '/runs', {'plan_id':plan['id'],'version':1,'environment_id':env['id'],'deployment_id':dep['id'],'idempotency_key':key})

def consume():
    item = worker.claim('test-worker')
    assert item and item[0] == 'runs'
    worker.execute(item[1])
    return s.get('runs', item[1]['id'], 'vdb')

def test_manual_to_signoff_release_and_staleness(client, setup):
    run = start(client, setup)
    assert start(client, setup)['id'] == run['id']
    run = consume()
    assert run['status'] == 'awaiting_input', run
    run = post(client, f"/runs/{run['id']}/attempts/{setup[1]['id']}/result", {'revision':run['revision'],'steps':{'s1':'passed'},'observations':{'s1':'Total verified 120 EUR'}})
    assert run['status'] == 'queued'
    run = consume()
    assert run['status'] == 'completed'
    post(client, f"/runs/{run['id']}/review", {'revision':run['revision'],'note':'Evidence checked'})
    summary = s.summary('vdb', setup[2]['id'])
    assert summary['outcome'] == 'passed'
    post(client, f"/plans/{setup[2]['id']}/signoff", {'version':1,'coverage_hash':summary['coverage_hash'],'note':'Ready for release review'})
    post(client, '/releases', {'plan_id':setup[2]['id'],'version':1,'coverage_hash':summary['coverage_hash'],'decision':'approve','note':'Approved','operational_readiness':'Rollback and support confirmed'})
    setup[4]['changed'] = True
    assert s.summary('vdb', setup[2]['id'])['coverage'][0]['outcome'] == 'stale'
    r = client.post('/validation/releases', headers=H, json={'plan_id':setup[2]['id'],'version':1,'coverage_hash':summary['coverage_hash'],'decision':'approve','note':'Old','operational_readiness':'Ready'})
    assert r.status_code == 409

def test_roles_and_tenant_isolation(client, viewer_client, ellen_client, setup):
    run = start(client, setup)
    assert viewer_client.get('/validation',headers=H).status_code == 403
    assert viewer_client.post('/validation/runs',headers=H,json={}).status_code == 403
    assert ellen_client.get('/validation',headers=headers('bwm')).status_code == 403
    assert client.get('/validation/runs/'+run['id'],headers=headers('bwm')).status_code == 404
    assert 'credential' not in client.get('/validation',headers=H).json()['environments'][0]

def test_approved_versions_are_immutable_and_source_change_blocks_approval(client, setup):
    env, scenario, plan, dep, material = setup
    original = s.version(scenario)
    payload = {**original['body'], 'revision':scenario['revision'], 'title':'New draft'}
    newer = client.put('/validation/scenarios/'+scenario['id'], headers=H, json=payload).json()
    assert newer['versions'][0] == original
    assert newer['versions'][1]['status'] == 'draft'
    draft = client.put('/validation/plans/'+plan['id'], headers=H, json={**s.version(plan)['body'],'revision':plan['revision']}).json()
    material['changed'] = True
    assert client.post('/validation/plans/'+plan['id']+'/approve',headers=H,json={'revision':draft['revision']}).status_code == 409

def test_expired_write_lease_requires_reconciliation_and_blocks_environment(client, setup):
    first = start(client, setup)
    claim = worker.claim('crashed')[1]
    claim.update(status='running', inflight_write=True, lease_until=time.time()-1)
    s.store('runs').put(claim['id'],claim)
    worker.recover()
    failed = s.get('runs',first['id'],'vdb')
    assert failed['status'] == 'execution_error' and failed['reconciliation_required']
    start(client,setup,'operation-0002')
    assert worker.claim('next') is None
    post(client,'/runs/'+first['id']+'/reconcile',{'revision':failed['revision'],'note':'Checked transaction and restored test data'})
    assert worker.claim('next') is not None

def test_no_false_pass_from_queued_stopped_or_missing_observations(client, setup):
    run = start(client, setup)
    assert s.summary('vdb',setup[2]['id'])['outcome'] == 'incomplete'
    post(client,'/runs/'+run['id']+'/stop',{'revision':run['revision']})
    assert worker.claim('worker') is None
    run = start(client,setup,'operation-0002');run=consume()
    response=client.post(f"/validation/runs/{run['id']}/attempts/{setup[1]['id']}/result",headers=H,json={'revision':run['revision'],'steps':{'s1':'passed'},'observations':{}})
    assert response.status_code == 422
    assert s.summary('vdb',setup[2]['id'])['outcome'] == 'incomplete'

def test_step_safety_and_assertions():
    from jde_api_service.validation.models import Step, Environment
    env=Environment(name='UAT',jde_environment='PY920').model_dump()
    with pytest.raises(runners.Blocked):
        runners.check_step(env,Step(id='s',action='Save',expected='Saved',operation='click',target='Save').model_dump())
    step=Step(id='s',action='Observe',expected='Total 12',operation='observe',assertion='equals',expected_value=12,result_path='total').model_dump()
    assert runners.assess(step,{'total':13})['outcome']=='failed'
    assert runners.assess(step,{'total':12})['outcome']=='passed'
    step['assertion']='human'
    assert runners.assess(step,{'total':12})['outcome']=='needs_review'

def test_live_ais_adapter_records_evidence_and_failure_without_retry(client, setup, monkeypatch):
    import httpx
    from jde_api_service.discovery import transport
    from jde_api_service.services import credential_crypto
    from jde_api_service.dependencies import AuthContext, Identity
    from jde_api_service.validation import models as m
    sent=[]
    def handle(request):
        sent.append(request.url.path)
        if request.url.path.endswith('/tokenrequest'):
            return httpx.Response(200,json={'environment':'PY920','role':'*ALL','userInfo':{'token':'SECRET-TOKEN'}})
        if request.url.path.endswith('/logout'):
            return httpx.Response(200,json={})
        return httpx.Response(200,json={'total':13,'token':'SECRET-TOKEN'})
    real=transport.LiveAisTransport
    monkeypatch.setattr(transport,'LiveAisTransport',lambda *a,**kw:real(*a,**kw,transport=httpx.MockTransport(handle)))
    ctx=AuthContext(Identity('u-hendro','Hendro'),'vdb',frozenset({'test_manager','admin','product_manager'}))
    env=s.get('environments',setup[0]['id'],'vdb')
    env.update(ais_url='https://ais.example/jderest',username='test-account',credential=credential_crypto.encrypt('TEST-ONLY'),routes=['ais'],allow_writes=True,side_effects_isolated=True,allowed_ais_paths=['/check'])
    s.store('environments').put(env['id'],env)
    scenario=s.save_scenario(ctx,m.Scenario(title='AIS total',story_id='ST1',criteria=['AC1'],route='ais',steps=[m.Step(id='check',action='Check total',expected='12',operation='ais',path='/check',assertion='equals',expected_value=12,result_path='total')]))
    s.approve(ctx,'scenarios',scenario['id'],m.Revision(revision=scenario['revision']))
    plan=s.save_plan(ctx,m.Plan(title='AIS plan',story_ids=['ST1'],environment_ids=[env['id']],selections=[m.Selection(scenario_id=scenario['id'],version=1)]))
    s.approve(ctx,'plans',plan['id'],m.Revision(revision=plan['revision']))
    run=s.start(ctx,m.Start(plan_id=plan['id'],version=1,environment_id=env['id'],deployment_id=setup[3]['id'],idempotency_key='actual-http-adapter'))
    done=consume()
    assert done['status']=='completed',done
    a=done['attempts'][0]
    assert a['outcome']=='failed' and a['evidence']
    assert sent.count('/jderest/check')==1
    evidence=client.get('/validation/evidence/'+a['evidence'][0],headers=H)
    assert evidence.status_code==200 and 'SECRET-TOKEN' not in evidence.text
    defect=post(client,'/defects',{'run_id':run['id'],'scenario_id':scenario['id'],'severity':'high'})
    assert post(client,'/defects',{'run_id':run['id'],'scenario_id':scenario['id'],'severity':'high'})['id']==defect['id']

def test_environment_locks_and_manual_dependencies(client,setup):
    from jde_api_service.dependencies import AuthContext,Identity
    from jde_api_service.validation import models as m
    ctx=AuthContext(Identity('u-hendro','Hendro'),'vdb',frozenset({'test_manager'}))
    second=s.save_scenario(ctx,m.Scenario(title='Dependent check',steps=[m.Step(id='s2',action='Read next',expected='Correct')]))
    s.approve(ctx,'scenarios',second['id'],m.Revision(revision=second['revision']))
    p=setup[2];body=s.version(p)['body'];body['selections'].append({'scenario_id':second['id'],'version':1,'depends_on':[setup[1]['id']]})
    p=s.save_plan(ctx,m.Plan(**body,revision=p['revision']),p['id']);s.approve(ctx,'plans',p['id'],m.Revision(revision=p['revision']))
    run=s.start(ctx,m.Start(plan_id=p['id'],version=2,environment_id=setup[0]['id'],deployment_id=setup[3]['id'],idempotency_key='dependency-test'))
    run=consume()
    assert run['status']=='awaiting_input'
    assert worker.claim('other') is None
    early=client.post(f"/validation/runs/{run['id']}/attempts/{second['id']}/result",headers=H,json={'revision':run['revision'],'steps':{'s2':'passed'},'observations':{'s2':'done'}})
    assert early.status_code==409
    post(client,f"/runs/{run['id']}/attempts/{setup[1]['id']}/result",{'revision':run['revision'],'steps':{'s1':'passed'},'observations':{'s1':'done'}})
    run=consume()
    post(client,f"/runs/{run['id']}/attempts/{second['id']}/result",{'revision':run['revision'],'steps':{'s2':'passed'},'observations':{'s2':'done'}})
    run=consume()
    assert run['status']=='completed' and all(a['outcome']=='passed' for a in run['attempts'])

def test_jira_uncertain_create_is_reconciled_without_duplicate(client,setup,monkeypatch):
    import httpx
    from types import SimpleNamespace
    from jde_api_service.validation import jira
    from jde_api_service.services.jira_gateway import JiraHttpGateway
    run=start(client,setup);run=consume()
    post(client,f"/runs/{run['id']}/attempts/{setup[1]['id']}/result",{'revision':run['revision'],'steps':{'s1':'failed'},'observations':{'s1':'Incorrect total'}})
    consume()
    d=post(client,'/defects',{'run_id':run['id'],'scenario_id':setup[1]['id']})
    policy=s.policy('vdb');policy['jira_enabled']=True;s.store('policy').put('vdb',policy)
    created=[]
    def handle(request):
        if request.url.path.endswith('/search/jql'):
            return httpx.Response(200,json={'issues':[{'key':'TEST-1'}] if created else []})
        if request.method=='POST':
            created.append(1)
            raise httpx.ReadTimeout('uncertain response')
        return httpx.Response(200,json={'fields':{'status':{'name':'Open'}}})
    monkeypatch.setattr(jira,'get_jira_integration_service',lambda:SimpleNamespace(get_for_customer=lambda _:SimpleNamespace(base_url='https://jira.example',project_key='TEST')))
    monkeypatch.setattr(jira,'get_jira_gateway',lambda _:JiraHttpGateway(email='test@example.com',api_token='test-only',transport=httpx.MockTransport(handle)))
    response=client.post('/validation/defects/'+d['id']+'/sync',headers=H,json={'revision':d['revision']})
    assert response.status_code==409
    d=s.get('defects',d['id'],'vdb')
    synced=post(client,'/defects/'+d['id']+'/sync',{'revision':d['revision']})
    assert synced['jira_key']=='TEST-1' and len(created)==1

def test_story_handoff_requires_current_release_and_holds_when_source_changes(client,setup):
    run=start(client,setup);run=consume()
    post(client,f"/runs/{run['id']}/attempts/{setup[1]['id']}/result",{'revision':run['revision'],'steps':{'s1':'passed'},'observations':{'s1':'Verified'}})
    run=consume();post(client,'/runs/'+run['id']+'/review',{'revision':run['revision'],'note':'Reviewed'})
    summary=s.summary('vdb',setup[2]['id'])
    post(client,'/plans/'+setup[2]['id']+'/signoff',{'version':1,'coverage_hash':summary['coverage_hash'],'note':'Passed'})
    assert not s.story_handoff('vdb','ST1')[0]['release_current']
    post(client,'/releases',{'plan_id':setup[2]['id'],'version':1,'coverage_hash':summary['coverage_hash'],'decision':'approve','note':'Ready','operational_readiness':'Ready'})
    assert s.story_handoff('vdb','ST1')[0]['release_current']
    setup[4]['changed']=True
    assert not s.story_handoff('vdb','ST1')[0]['release_current']

def test_export_report_contains_verified_evidence_and_storage_probe(client,setup):
    import io,json,zipfile
    assert post(client,'/storage/test',{})['status']=='verified'
    run=start(client,setup);run=consume()
    post(client,f"/runs/{run['id']}/attempts/{setup[1]['id']}/result",{'revision':run['revision'],'steps':{'s1':'passed'},'observations':{'s1':'Verified'}})
    consume()
    response=client.get('/validation/plans/'+setup[2]['id']+'/report',headers=H)
    assert response.status_code==200
    with zipfile.ZipFile(io.BytesIO(response.content)) as report:
        assert 'README.md' in report.namelist() and 'report.json' in report.namelist()
        data=json.loads(report.read('report.json'))
        assert data['evidence'] and data['summary']['outcome']=='incomplete' # not reviewed
        assert any(n.startswith('evidence/') for n in report.namelist())
    assert client.get('/validation/plans/'+setup[2]['id']+'/report',headers=headers('bwm')).status_code==404

def test_test_manager_cannot_release_or_administer(client,setup):
    from fastapi.testclient import TestClient
    from jde_api_service.main import app
    from jde_api_service.services import auth_service,membership_service
    from .conftest import TEST_PASSWORD,_apply_csrf_header
    user=auth_service.create_user('test-manager@test.local',TEST_PASSWORD,'Test Manager')
    membership_service.create_membership(user.id,'vdb',['test_manager'],created_by='u-hendro')
    other=TestClient(app)
    assert other.post('/auth/login',json={'email':'test-manager@test.local','password':TEST_PASSWORD}).status_code==200
    _apply_csrf_header(other)
    assert other.get('/validation',headers=H).status_code==200
    assert other.get('/validation/settings',headers=H).status_code==403
    assert other.post('/validation/releases',headers=H,json={}).status_code==403
    assert other.post('/validation/deployments',headers=H,json={}).status_code==403

def test_production_requires_bound_unexpired_application_manager_approval(client,setup):
    from jde_api_service.validation import models as m
    from jde_api_service.dependencies import AuthContext,Identity
    ctx=AuthContext(Identity('u-hendro','Hendro'),'vdb',frozenset({'test_manager','product_manager'}))
    env=s.get('environments',setup[0]['id'],'vdb');env['stage']='PROD';s.store('environments').put(env['id'],env)
    payload=m.Start(plan_id=setup[2]['id'],version=1,environment_id=env['id'],deployment_id=setup[3]['id'],idempotency_key='production-only')
    from fastapi import HTTPException
    with pytest.raises(HTTPException,match='Production validation is disabled'):
        s.start(ctx,payload)
    policy=s.policy('vdb');policy['production_enabled']=True;s.store('policy').put('vdb',policy)
    approval=s.approve_production(ctx,m.ProductionApproval(plan_id=setup[2]['id'],version=1,environment_id=env['id'],deployment_id=setup[3]['id'],note='Bound smoke approval'))
    payload.production_approval_id=approval['id']
    approval['expires_at']='2000-01-01T00:00:00+00:00';s.store('production_approvals').put(approval['id'],approval)
    with pytest.raises(HTTPException,match='expired'):
        s.start(ctx,payload)

def test_manual_ambiguity_finishes_without_false_pass(client,setup):
    run=start(client,setup);run=consume()
    post(client,f"/runs/{run['id']}/attempts/{setup[1]['id']}/result",{'revision':run['revision'],'steps':{'s1':'needs_review'},'observations':{'s1':'Cannot determine expected amount'}})
    run=consume()
    assert run['status']=='completed' and run['attempts'][0]['outcome']=='needs_review'
    assert s.summary('vdb',setup[2]['id'])['outcome']=='incomplete'

def test_design_agent_creates_reviewable_drafts_not_passed_tests(client,setup,monkeypatch):
    import asyncio
    from jde_api_service.validation import agents
    from . import _ai
    _ai.configure('vdb')
    async def answer(*args,**kwargs):
        return {'result':{'scenarios':[{'title':'Proposed check','story_id':'ST1','criteria':['AC1'],'steps':[{'id':'s','action':'Read amount','expected':'Correct total'}]}],'questions':[]},'model':'test-provider','ai_run_id':'scripted-test-only','pack':'test-pack'}
    monkeypatch.setattr(agents,'ask',answer)
    job=post(client,'/agents',{'role':'test-designer','story_id':'ST1'})
    kind,claimed=worker.claim('agent-worker')
    assert kind=='agent_jobs'
    asyncio.run(agents.execute(claimed))
    finished=s.get('agent_jobs',job['id'],'vdb')
    assert finished['status']=='completed'
    scenario=s.get('scenarios',finished['result']['result']['created_scenario_ids'][0],'vdb')
    assert s.version(scenario)['status']=='draft'
    assert s.rows('runs','vdb')==[]

def test_blank_assertions_cannot_create_vacuous_passes():
    from pydantic import ValidationError
    from jde_api_service.validation.models import Step
    for assertion,expected in [('contains',''),('contains','   '),('equals',None)]:
        with pytest.raises(ValidationError):
            Step(id='s',action='Check',expected='A real value',assertion=assertion,expected_value=expected)
