"""Actual Chromium + local HTTPS UI. External JDE and AI providers are not contacted."""
import asyncio
import http.server
import ssl
import threading
from pathlib import Path

import pytest
from jde_api_service.validation import models as m, runners, service as s
from jde_api_service.dependencies import AuthContext, Identity
from .test_jde_live_readiness import _cert

@pytest.mark.parametrize('route',['browser','computer_use'])
def test_browser_executes_approved_step_and_masks_observations(client,tmp_path,monkeypatch,route):
    if not runners.available()[0]:
        pytest.skip('Install Playwright Chromium to run the real browser adapter test')
    screen='<span id="env">PY920</span><span id="role">JADEREAD</span><div id="private">SENSITIVE-TEST-DATA</div><button onclick="document.getElementById(\'result\').textContent=\'Saved order SO\'">Save</button><p id="result">Ready</p>'
    import json
    html=('<form onsubmit="event.preventDefault();document.body.innerHTML='+json.dumps(screen).replace('"','&quot;')+'"><input name="User"><input name="Environment"><input name="Role"><input type="password"><button>Sign in</button></form>').encode()
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(html)));self.end_headers();self.wfile.write(html)
    cert,key=_cert(tmp_path,'browser','127.0.0.1')
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(cert,key)
    server.socket=tls.wrap_socket(server.socket,server_side=True)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    ctx=AuthContext(Identity('u-hendro','Hendro'),'vdb',frozenset({'admin','test_manager'}))
    public=s.save_environment(ctx,m.Environment(name='Browser fixture',jde_environment='PY920',jde_role='JADEREAD',enabled=True,
        username='test-user',password='TEST-PASSWORD',web_url=f'https://127.0.0.1:{server.server_address[1]}',ca_pem=Path(cert).read_text(),
        browser_environment_selector='#env',browser_role_selector='#role',allow_writes=True,side_effects_isolated=True,redaction_selectors=['#private'],routes=[route]))
    env=s.get('environments',public['id'],'vdb')
    observed=[]
    async def choose(company,actor,observation,step):
        assert 'SENSITIVE-TEST-DATA' not in json.dumps(observation)
        observed.append(observation)
        return {'result':{'ref':next(c['ref'] for c in observation['controls'] if c['text']=='Save'),'reason':'Exact Save control'}}
    monkeypatch.setattr(runners.agents,'choose_control',choose)
    scenario=m.Scenario(title='Save fixture',route=route,steps=[m.Step(id='save',action='Save the order',expected='Saved order SO',operation='click',target='Save',assertion='contains',expected_value='Saved order SO')])
    events=[]
    try:
        asyncio.run(runners.browser_scenario(env,{'id':'fixture-run','initiated_by':'u-hendro'},{'scenario_id':'fixture-scenario','body':scenario.model_dump()},lambda:None,events.append))
        result=next(e for e in events if e['event']=='step')
        assert result['outcome']=='passed' and result['evidence']
        assert 'SENSITIVE-TEST-DATA' not in json.dumps(result)
        assert bool(observed)==(route=='computer_use')
        env['jde_environment']='WRONG-ENVIRONMENT'
        with pytest.raises(runners.Blocked,match='does not match'):
            asyncio.run(runners.browser_scenario(env,{'id':'fixture-wrong','initiated_by':'u-hendro'},{'scenario_id':'fixture-scenario','body':scenario.model_dump()},lambda:None,events.append))
    finally:
        server.shutdown();server.server_close()
