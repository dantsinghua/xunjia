import importlib.util
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location("site_acceptance_check",ROOT/"deploy/site_acceptance.py")
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

@pytest.fixture
def installed(tmp_path,monkeypatch):
 prefix=tmp_path/"site";release=prefix/"releases/r1";release.mkdir(parents=True)
 (prefix/"current").symlink_to(release)
 monkeypatch.setattr(module,"load_json",lambda path:{"payload":{},"build_id":"a"*64,"release_id":"r1"})
 monkeypatch.setattr(module,"verify_payload",lambda *args:None)
 monkeypatch.setattr(module,"read_env",lambda path:{})
 monkeypatch.setattr(module,"effective_env",lambda *args:{"XUNJIA_HOST":"127.0.0.1","XUNJIA_PORT":"18770"})
 monkeypatch.setattr(module,"validate_config",lambda *args:[])
 return prefix

def response(**extra):return {"service":"xunjia","build_id":"a"*64,"release_id":"r1","boot_id":"boot1","alive":True,"ready":True,"automation_ready":False,**extra}

def test_site_check_does_not_confuse_http_200_with_full_delivery(installed):
 r=module.assess(installed,reader=lambda url:(200,response()))
 assert not r["passed"] and r["native_messages_sent"]==0
 assert "AUTOMATION_NOT_VERIFIED" in r["blockers"] and "REAL_MODEL_PROBE_NOT_RUN" in r["blockers"]
 assert next(c for c in r["checks"] if c["name"]=="business_readiness")["passed"]

@pytest.mark.parametrize("change",[{"build_id":"old"},{"release_id":"other"},{"service":"njcb-all"},{"boot_id":""}])
def test_site_rejects_wrong_application_or_release(installed,change):
 r=module.assess(installed,reader=lambda url:(200,response(**change)))
 assert not r["passed"] and "HEALTH_WRONG_RELEASE" in r["blockers"]

def test_site_reports_database_error_without_echoing_arbitrary_messages(installed):
 r=module.assess(installed,reader=lambda url:(503,response(ready=False,reasons=["DATABASE_UNAVAILABLE","secret-app-key=private"])))
 assert "DATABASE_UNAVAILABLE" in r["blockers"]
 assert "private" not in str(r)

def test_unconfigured_site_model_probe_fails_before_any_network(installed):
 r=module.assess(installed,probe_dify=True,reader=lambda url:(200,response()))
 assert "DIFY_V22_CHATFLOWS_NOT_CONFIGURED" in r["blockers"]

def test_missing_installation_has_precise_error(tmp_path):
 r=module.assess(tmp_path/"site")
 assert r["blockers"]==["CURRENT_RELEASE_INVALID"]
