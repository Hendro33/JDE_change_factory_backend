"""Reproducible browser proof using an isolated synthetic customer, never production data.

Install the backend test extra and Playwright Chromium; npm ci in the frontend.
Run: python scripts/prove_validation_ui.py --frontend ../JDE_change_factory_frontend
Optional JADE_VALIDATION_ARTIFACTS selects the screenshot/log directory.
"""
import os, sys, subprocess, tempfile, time, urllib.request, signal
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'api_service'))
if '--serve' in sys.argv:
 import pytest
 from tests.conftest import isolated_dirs, _create_member
 mp=pytest.MonkeyPatch()
 g=isolated_dirs.__wrapped__(Path(tempfile.mkdtemp(prefix='jade-validation-ui-')),mp)
 next(g)
 from tests.fixtures.customers import create_all
 create_all()
 _create_member('u-hendro','hendro@test.local','Hendro',['vdb'])
 from tests.test_stage1_execution_safeguards import _approved_story
 _approved_story('S-UI-1')
 # Isolated test tenant with the repository's synthetic source stories. No external writes.
 os.environ['JDE_VALIDATION_WORKER']='true'
 from jde_api_service.config import settings
 settings.repo_root=str(ROOT)
 settings.allowed_origins=['http://localhost:5173']
 from jde_api_service.main import app
 import uvicorn
 uvicorn.run(app,host="127.0.0.1",port=8000,log_level="warning")
 sys.exit(0)
from playwright.sync_api import sync_playwright
FRONTEND = Path(sys.argv[sys.argv.index('--frontend') + 1]) if '--frontend' in sys.argv else ROOT.parent / 'JDE_change_factory_frontend'
if not (FRONTEND / 'package.json').exists():
    raise SystemExit('Pass --frontend PATH to the frontend checkout')
ARTIFACTS = Path(os.environ.get('JADE_VALIDATION_ARTIFACTS') or tempfile.mkdtemp(prefix='jade-validation-proof-'))
ARTIFACTS.mkdir(parents=True, exist_ok=True)
env = {**os.environ, 'NO_PROXY':'127.0.0.1,localhost', 'VITE_USE_MOCK_API':'false', 'VITE_API_BASE_URL':'http://localhost:8000'}
api = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--serve'], cwd=ROOT, env=env, stdout=open(ARTIFACTS/'api.log','w'), stderr=subprocess.STDOUT, start_new_session=True)
ui = subprocess.Popen(['npm','run','dev','--','--host','127.0.0.1','--port','5173','--strictPort'], cwd=FRONTEND, env=env, stdout=open(ARTIFACTS/'ui.log','w'), stderr=subprocess.STDOUT, start_new_session=True)
try:
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 for _ in range(60):
  try:
   opener.open('http://127.0.0.1:5173',timeout=1);opener.open('http://127.0.0.1:8000/health',timeout=1);break
  except Exception:time.sleep(.5)
 with sync_playwright() as p:
  b=p.chromium.launch(headless=True)
  page=b.new_page(viewport={'width':1440,'height':1000})
  page.on('pageerror',lambda e:print('PAGEERROR',e))
  page.goto('http://localhost:5173')
  page.get_by_label('Email',exact=True).fill('hendro@test.local')
  page.get_by_label('Password',exact=True).fill('test-password-not-real-0000')
  page.get_by_role('button',name='Sign in',exact=True).click()
  page.get_by_role('link',name='Validation',exact=True).wait_for()
  page.goto('http://localhost:5173/admin/validation')
  page.get_by_role('button',name='Add environment').click()
  page.get_by_label('Display name',exact=True).fill('Acceptance UAT')
  page.get_by_label('JDE environment identity').fill('PY920')
  page.get_by_label('Environment enabled for validation').check()
  page.get_by_role('button',name='Save environment').click()
  page.get_by_role('heading',name='Acceptance UAT · UAT').wait_for()
  page.screenshot(path=str(ARTIFACTS / "jade-validation-admin.png"),full_page=True)
  page.goto('http://localhost:5173/validation')
  page.get_by_role('button',name='Test Library',exact=True).click()
  page.get_by_role('button',name='Create scenario',exact=True).click()
  page.get_by_label('Title',exact=True).fill('Verify default order type')
  page.screenshot(path=str(ARTIFACTS / "jade-scenario.png"),full_page=True)
  page.get_by_label('Source story',exact=True).select_option('S-UI-1')
  page.get_by_label('Action',exact=True).fill('Inspect the new sales order default')
  page.get_by_label('Expected business result',exact=True).fill('Order type is SO')
  page.get_by_role('button',name='Save draft',exact=True).click()
  page.get_by_role('button',name='Approve version',exact=True).click()
  page.get_by_text('Scenario approved',exact=True).wait_for()
  page.get_by_role('button',name='Plans',exact=True).click()
  page.get_by_role('button',name='Create plan',exact=True).click()
  page.get_by_label('Plan title',exact=True).fill('Sales order acceptance')
  page.get_by_label('As a clerk I want SO as the default order type',exact=True).check()
  page.get_by_label('Acceptance UAT · UAT',exact=True).check()
  page.get_by_label('Verify default order type',exact=True).check()
  page.get_by_role('button',name='Save draft',exact=True).click()
  page.get_by_role('button',name='Approve plan',exact=True).click()
  page.get_by_text('Plan approved',exact=True).wait_for()
  page.goto('http://localhost:5173/am/validation')
  page.get_by_label('Validation plan',exact=True).select_option(label='Sales order acceptance')
  page.get_by_label('Target environment',exact=True).select_option(label='Acceptance UAT · UAT')
  page.get_by_label('Build / package identifier',exact=True).fill('UAT-SYNTHETIC-1')
  page.get_by_label('Deployment confirmation and evidence reference',exact=True).fill('Synthetic UI test deployment confirmation')
  page.get_by_role('button',name='Record deployment',exact=True).click()
  page.get_by_text('Deployment confirmation recorded',exact=True).wait_for()
  page.goto('http://localhost:5173/validation')
  page.get_by_role('button',name='Plans',exact=True).click()
  page.get_by_label('Run in environment',exact=True).select_option(label='Acceptance UAT · UAT')
  page.get_by_role('button',name='Start validation',exact=True).click()
  page.get_by_label('Outcome — Inspect the new sales order default',exact=True).wait_for(timeout=20000)
  page.get_by_label('Outcome — Inspect the new sales order default',exact=True).select_option('passed')
  page.get_by_label('Actual observation — Inspect the new sales order default',exact=True).fill('SO shown for the synthetic order')
  page.get_by_role('button',name='Submit UAT observations',exact=True).click()
  page.get_by_role('button',name='Review recorded results',exact=True).wait_for(timeout=20000)
  page.get_by_label('Review / reconciliation note',exact=True).fill('Checked the recorded observations')
  page.get_by_role('button',name='Review recorded results',exact=True).click()
  page.get_by_text('Results reviewed',exact=True).wait_for()
  page.get_by_role('button',name='Dashboard',exact=True).click()
  page.get_by_label('Test Manager conclusion and exceptions',exact=True).fill('All mandatory checks passed')
  page.get_by_role('button',name='Record testing conclusion',exact=True).click()
  page.get_by_text('Testing conclusion recorded',exact=True).wait_for()
  page.screenshot(path=str(ARTIFACTS / "jade-validation-dashboard.png"),full_page=True)
  page.goto('http://localhost:5173/am/validation')
  page.get_by_label('Validation plan',exact=True).select_option(label='Sales order acceptance')
  page.get_by_label('Decision rationale',exact=True).fill('Reviewed passing test evidence')
  page.get_by_label('Operational readiness, rollback and support arrangements',exact=True).fill('Synthetic rollback and support confirmed')
  page.get_by_role('button',name='Approve release',exact=True).click()
  page.get_by_text('Release decision recorded',exact=True).wait_for()
  page.screenshot(path=str(ARTIFACTS / "jade-validation-release.png"),full_page=True)
  print('PASS: Browser UI journey from administrator setup through scenario, plan, deployment, manual UAT, review, sign-off and release.')
  b.close()
except Exception:
 print('API LOG',open(ARTIFACTS / 'api.log').read()[-1500:])
 try:
  print(page.locator('body').inner_text()[-5000:]);page.screenshot(path=str(ARTIFACTS / "jade-ui-error.png"),full_page=True)
 except Exception:pass
 raise
finally:
 os.killpg(api.pid, signal.SIGTERM);os.killpg(ui.pid, signal.SIGTERM)

print("Proof artifacts:", ARTIFACTS)
