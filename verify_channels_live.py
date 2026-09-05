"""Live Slack/Meta readiness checks. Secrets are never printed."""
from __future__ import annotations
import argparse, json, os, re, urllib.parse, urllib.request

def _json(url:str,headers:dict|None=None)->dict:
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme!="https" or parsed.hostname not in {"slack.com","graph.facebook.com"} or parsed.username or parsed.password:
        raise ValueError("channel verification URL is not an approved HTTPS provider origin")
    request=urllib.request.Request(url,headers=headers or {})
    with urllib.request.urlopen(request,timeout=15) as response:  # nosec B310
        return json.loads(response.read().decode("utf-8"))

def run()->dict:
    report={"slack":{"configured":False},"meta":{"configured":False},"webhook":{"configured":False}}
    slack=os.environ.get("FRONTDESK_SLACK_BOT_TOKEN","")
    if slack:
        payload=_json("https://slack.com/api/auth.test",{"Authorization":f"Bearer {slack}"})
        report["slack"]={"configured":True,"authenticated":bool(payload.get("ok")),
                         "team_matches":not os.environ.get("FRONTDESK_SLACK_TEAM_ID") or payload.get("team_id")==os.environ.get("FRONTDESK_SLACK_TEAM_ID")}
    meta=os.environ.get("FRONTDESK_META_PAGE_TOKEN","")
    if meta:
        version=os.environ.get("FRONTDESK_META_GRAPH_VERSION","v26.0")
        if not re.fullmatch(r"v\d{1,2}\.\d",version):raise ValueError("invalid Meta Graph API version")
        payload=_json(f"https://graph.facebook.com/{version}/me?"+urllib.parse.urlencode({"access_token":meta,"fields":"id,name"}))
        report["meta"]={"configured":True,"authenticated":bool(payload.get("id"))}
    base=os.environ.get("FRONTDESK_PUBLIC_BASE_URL","").rstrip("/")
    if base:
        parsed=urllib.parse.urlsplit(base)
        if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("public webhook URL must be HTTPS without embedded credentials")
        with urllib.request.urlopen(base+"/health",timeout=15) as response:  # nosec B310
            report["webhook"]={"configured":True,"healthy":response.status==200}
    return report

def main()->int:
    argparse.ArgumentParser().parse_args();report=run();print(json.dumps(report,indent=2))
    ready=all(item.get("configured") and all(value is True for key,value in item.items() if key!="configured") for item in report.values())
    return 0 if ready else 2
if __name__=="__main__":raise SystemExit(main())
