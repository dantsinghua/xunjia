"""Read-only installed-site acceptance. Never enables sending, creates DDL or changes services."""
from __future__ import annotations
import argparse,json,re,sys,urllib.request,urllib.error
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from common import DeployError,read_env,effective_env,validate_config,load_json,verify_payload,health_url,safe_prefix

def health_read(url):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:
        try:response=opener.open(url,timeout=5)
        except urllib.error.HTTPError as error:response=error
        with response:
            raw=response.read(65537)
            if len(raw)>65536:raise DeployError("HEALTH_RESPONSE_TOO_LARGE")
            payload=json.loads(raw)
            if not isinstance(payload,dict) or not isinstance(payload.get("data"),dict):raise DeployError("HEALTH_CONTRACT_INVALID")
            return response.code,payload["data"]
    except DeployError:raise
    except (OSError,ValueError,TypeError):raise DeployError("HEALTH_UNAVAILABLE") from None

def assess(prefix,*,probe_dify=False,reader=health_read):
    checks=[]
    def add(name,passed,code,**details):
        checks.append({"name":name,"passed":bool(passed),"code":code,**details})
    result={"passed":False,"checks":checks,"read_only":True,"native_messages_sent":0,"model_probe_requested":probe_dify}
    try:
        prefix=safe_prefix(prefix);release=(prefix/"current").resolve()
        if not (prefix/"current").is_symlink() or not release.is_relative_to(prefix/"releases"):raise DeployError("CURRENT_RELEASE_INVALID")
        meta=load_json(release/"RELEASE.json");app=release/"app"
        verify_payload(app,meta["payload"])
        values=effective_env(read_env(prefix/"config/xunjia.env"),prefix,app)
        missing=validate_config(values)
        add("installed_files",True,"RELEASE_FILES_VERIFIED",build_id=meta["build_id"],release_id=meta["release_id"])
        add("configuration",not missing,"CONFIG_INCOMPLETE" if missing else "CONFIG_VALID",missing=missing)
        boot=None
        for name,path,flag in (("liveness","/api/health/live","alive"),("business_readiness","/api/health/ready","ready"),("automation","/api/health/automation","automation_ready")):
            try:
                status,data=reader(health_url(values,path))
                identity=(data.get("service")=="xunjia" and data.get("build_id")==meta["build_id"] and data.get("release_id")==meta["release_id"] and bool(data.get("boot_id")))
                if boot is not None:identity=identity and data.get("boot_id")==boot
                if name=="liveness" and identity:boot=data["boot_id"]
                reasons=[c for c in data.get("reasons",[]) if isinstance(c,str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}",c)]
                ok=status==200 and identity and data.get(flag) is True
                code="VERIFIED" if ok else "HEALTH_WRONG_RELEASE" if not identity else reasons[0] if reasons else "AUTOMATION_NOT_VERIFIED" if name=="automation" else "HEALTH_NOT_READY"
                add(name,ok,code,http_status=status,reasons=reasons)
            except DeployError as exc:add(name,False,exc.code)
        if probe_dify:
            if not all(values.get(k) for k in ("XUNJIA_DIFY_URL","XUNJIA_DIFY_ANALYSIS_APP_KEY","XUNJIA_DIFY_REPLY_APP_KEY")):
                add("dify_probe",False,"DIFY_V22_CHATFLOWS_NOT_CONFIGURED")
            else:
                sys.path.insert(0,str(app))
                from scripts.measure_dify_v22 import run_case
                from dify.negotiation_v22.cases import review_cases
                cfg={"source":{"base_url":values["XUNJIA_DIFY_URL"]},"apps":{stage:{"key":values["XUNJIA_DIFY_"+stage.upper()+"_APP_KEY"]} for stage in ("analysis","reply")}}
                for case in review_cases():
                    case["policy_revision"]="latest_quote_confirmation_v1"
                    record=run_case(case,cfg,int(values.get("XUNJIA_DIFY_TIMEOUT_SECONDS","90")),stage_budget_seconds=float(values.get("XUNJIA_DIFY_STAGE_BUDGET_SECONDS","150")))
                    ok=record.get("success") is True and record.get("semantic_ok") is True
                    error=record.get("error_code","DIFY_PROBE_FAILED")
                    code=error if isinstance(error,str) and re.fullmatch(r"DIFY_[A-Z_]{1,70}",error) else "DIFY_SEMANTIC_PROBE_FAILED"
                    add("dify:"+case["id"],ok,"VERIFIED" if ok else code)
                    if not ok:break  # Retain failure; do not spend calls repeating a broken dependency.
        else:add("dify_probe",False,"REAL_MODEL_PROBE_NOT_RUN")
        result["passed"]=bool(checks) and all(row["passed"] for row in checks)
    except DeployError as exc:add("installed_site",False,exc.code)
    except (OSError,ValueError,KeyError,TypeError,ImportError) as exc:add("installed_site",False,"SITE_CHECK_FAILED",error_type=type(exc).__name__)
    result["blockers"]=[row["code"] for row in checks if not row["passed"]]
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix",type=Path,required=True)
    parser.add_argument("--probe-dify",action="store_true",help="Call ordinary App APIs on six synthetic review messages; no IM is sent")
    args=parser.parse_args();result=assess(args.prefix,probe_dify=args.probe_dify)
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result["passed"] else 2
if __name__=="__main__":raise SystemExit(main())
