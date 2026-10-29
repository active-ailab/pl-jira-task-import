#!/usr/bin/env python3
"""Import one reviewed spreadsheet layer, journal writes, and backfill real keys. Preview by default."""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import subprocess
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from plan_import import ERROR, KEY, day, header, rows, text
from snapshot_template import call

TITLES = {"epic": "Epic", "task": "适配任务", "subtask": "适配子任务"}
TYPES = {"epic": "Epic", "task": "Task", "subtask": "Sub-task"}


def select(snapshot, stage):
    sheet = next(s for s in snapshot["sheets"] if s["title"] == TITLES[stage])
    if not sheet.get("filter_checked") or snapshot.get("value_render_option") != "FormattedValue":
        raise ValueError("需要已核验筛选状态的 FormattedValue 快照")
    selected, skipped = [], []
    for num, row in rows(sheet):
        reason = ""
        if num in sheet["filtered_out_rows"]:
            reason = "被工作表筛选排除"
        elif stage == "task" and row.get("是否适用本项目") != "是":
            reason = "不适用本项目"
        elif stage == "subtask" and not KEY.fullmatch(row.get("父级ID", "")):
            reason = "父级未就绪或不适用: " + row.get("父级ID", "")
        if reason:
            skipped.append({"row": num, "summary": row.get("Summary", ""), "reason": reason})
        else:
            selected.append({"row": num, "values": row})
    return sheet, selected, skipped


def payload(row, stage, project, typ, actor, batch, source_label):
    if row.get("IssueType") != TYPES[stage]:
        raise ValueError("Issue Type 与当前导入层不符")
    for name in ["Summary", "Assignee", "Severity", "FixVersion/s"]:
        if not row.get(name) or ERROR.fullmatch(row[name]):
            raise ValueError(f"缺失或错误字段 {name}: {row.get(name)}")
    meta = typ["fields"]
    severity = next(x for x in meta["customfield_10401"]["allowedValues"] if x["value"] == row["Severity"])
    version = next(x for x in meta["fixVersions"]["allowedValues"] if x["name"] == row["FixVersion/s"])
    f = {"project":{"key":project}, "issuetype":{"id":typ["id"]}, "summary":row["Summary"],
         "assignee":{"name":row["Assignee"]}, "customfield_10401":{"id":severity["id"]},
         "fixVersions":[{"id":version["id"]}], "labels":[batch, source_label],
         "customfield_10702":day(row["启动时间"]).replace("/", "-")}
    if stage == "epic":
        f["customfield_10103"] = row["EpicName"]
        # Epic due depends on SUMIF against keys which have not been backfilled yet.
    else:
        f["duedate"] = day(row["到期日"]).replace("/", "-")
        parent = row["父级ID"] if stage == "subtask" else row["EpicLink"]
        if not KEY.fullmatch(parent) or not parent.startswith(project+"-"):
            raise ValueError("上层真实 Jira 编号尚未就绪")
        f["parent" if stage == "subtask" else "customfield_10101"] = {"key":parent} if stage == "subtask" else parent
    if row.get("详细描述"):
        f["description"] = row["详细描述"]
    supplied = set(f)
    for fid, field in meta.items():
        if field.get("required") and fid not in supplied and not field.get("hasDefaultValue") and field.get("defaultValue") is None:
            raise ValueError("未提供必填字段: "+fid)
    return f


def verify(obj, row, stage, actor):
    f = obj["fields"]
    assert f["summary"] == row["Summary"]
    assert f["issuetype"]["name"] == TYPES[stage]
    assert (f.get("assignee") or {}).get("name") == row["Assignee"]
    assert f["customfield_10401"]["value"] == row["Severity"]
    assert [x["name"] for x in f["fixVersions"]] == [row["FixVersion/s"]]
    assert f["customfield_10702"] == day(row["启动时间"]).replace("/", "-")
    assert f["reporter"]["name"] == actor
    if stage == "epic":
        assert f["customfield_10103"] == row["EpicName"]
    else:
        assert f["duedate"] == day(row["到期日"]).replace("/", "-")
        assert (f.get("parent") or {}).get("key") == row["父级ID"] if stage == "subtask" else f["customfield_10101"] == row["EpicLink"]
    assert (f.get("description") or "") == row.get("详细描述", "")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", type=pathlib.Path, required=True)
    ap.add_argument("--stage", choices=TITLES, required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--batch", required=True, help="同一重新导入批次须保持稳定，防止重复")
    ap.add_argument("--state-dir", type=pathlib.Path, required=True)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--fresh-hierarchy", action="store_true", help="仅用于用户明确要求从空编号重建层级；旧任务不改动")
    ap.add_argument("--lark-bin", default=str(pathlib.Path.home()/".npm-global/bin/lark-cli"))
    ap.add_argument("--jira-bin", default=str(pathlib.Path.home()/".local/bin/jira"))
    args = ap.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.batch) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", args.project):
        raise ValueError("Invalid batch/project")
    snap = json.loads(args.snapshot.read_text())
    sheet, selected, skipped = select(snap, args.stage)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    (args.state_dir/(args.stage+"-selection.json")).write_text(json.dumps({"revision":snap["revision"],"selected":selected,"skipped":skipped},ensure_ascii=False,indent=2))
    print(json.dumps({"stage":args.stage,"selected":len(selected),"skipped":len(skipped),"execute":args.execute}),flush=True)
    if not args.execute:
        return
    spec = importlib.util.spec_from_file_location("jira_fields", pathlib.Path.home()/".codex/skills/active-jira/scripts/query_jira_field_options.py")
    m = importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    cfg = m.read_config(None);server = cfg["server"]
    _, headers = m.resolve_auth(server,cfg.get("login"),cfg.get("auth_type"))
    actor = m.jira_get(server,headers,"/rest/api/2/myself")["name"]
    meta = m.jira_get(server,headers,"/rest/api/2/issue/createmeta?"+urlencode({"projectKeys":args.project,"expand":"projects.issuetypes.fields"}))
    typ = next(t for p in meta["projects"] if p["key"]==args.project for t in p["issuetypes"] if t["name"]==TYPES[args.stage])
    current_filter = call(args.lark_bin,["sheets","spreadsheet.sheet.filters","get","--as","user","--params",json.dumps({"spreadsheet_token":snap["source_token"],"sheet_id":sheet["sheet_id"]})]).get("sheet_filter_info",{})
    if current_filter != sheet["filter_info"]:
        raise RuntimeError("当前筛选与快照不一致，停止本层导入")
    log = args.state_dir/"journal.jsonl"
    history = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    def journal(record):
        with log.open("a") as out:
            out.write(json.dumps({"at":datetime.datetime.now(datetime.timezone.utc).isoformat(),**record},ensure_ascii=False)+"\n");out.flush();os.fsync(out.fileno())
        history.append(record)
    def view(key):
        result = subprocess.run([args.jira_bin,"issue","view",key,"--raw"],capture_output=True,text=True,timeout=45)
        if result.returncode:
            raise RuntimeError("Cannot read Jira "+key)
        return json.loads(result.stdout)
    def mutate(method,path,data):
        req=Request(server.rstrip("/")+path,data=json.dumps(data).encode(),headers={**headers,"Content-Type":"application/json"},method=method)
        with urlopen(req,timeout=45) as response:
            raw=response.read()
            return json.loads(raw) if raw else {}
    # Validate every selected row's fields and owner before creating this layer.
    prepared=[]
    for item in selected:
        row=item["values"];num=item["row"]
        source=f"{snap['source_token']}:{sheet['sheet_id']}:{num}"
        label="pl-src-"+hashlib.sha256(source.encode()).hexdigest()[:16]
        f=payload(row,args.stage,args.project,typ,actor,args.batch,label)
        owners=m.jira_get(server,headers,"/rest/api/2/user/assignable/search?"+urlencode({"project":args.project,"username":row["Assignee"],"maxResults":1000}))
        if not any(x.get("name")==row["Assignee"] and x.get("active") for x in owners):
            raise ValueError("负责人不可指派: "+row["Assignee"])
        if args.stage != "epic":
            parent=row["父级ID"] if args.stage=="subtask" else row["EpicLink"]
            parent_obj=view(parent)
            expected="Task" if args.stage=="subtask" else "Epic"
            if parent_obj["fields"]["issuetype"]["name"]!=expected:
                raise ValueError("父级类型不符: "+parent)
            if args.fresh_hierarchy and args.batch not in parent_obj["fields"].get("labels",[]):
                raise ValueError("父级不属于本轮创建层级: "+parent)
        prepared.append((item,source,label,f))
    for item,source,label,f in prepared:
        row=item["values"];num=item["row"]
        base={"stage":args.stage,"source_id":source,"sheet_id":sheet["sheet_id"],"row":num,"summary":row["Summary"],"assignee":row["Assignee"],"batch":args.batch,"template_reporter":row.get("Reporter"),"actual_reporter":actor}
        known=[x for x in history if x.get("source_id")==source and x.get("key")]
        key=known[-1]["key"] if known else ""
        if not key:
            jql=f'project = {args.project} AND labels = "{args.batch}" AND labels = "{label}"'
            found=m.jira_get(server,headers,"/rest/api/2/search?"+urlencode({"jql":jql,"maxResults":2,"fields":"summary"}))
            if found["total"]>1:
                raise RuntimeError("同批次源行重复: "+source)
            if found["issues"]:
                key=found["issues"][0]["key"]
            elif any(x.get("source_id")==source and x.get("event") in ("attempt","uncertain","created") for x in history):
                raise RuntimeError("之前写入结果不明，禁止盲目重试: "+source)
            elif not args.fresh_hierarchy:
                raise ValueError("未启用明确授权的新层级模式；先核对已有任务再执行")
            else:
                existing_col="EpicLink(导入后填写)" if args.stage=="epic" else "Key(导入后填写)"
                if args.stage!="subtask" and row.get(existing_col):
                    raise ValueError("回填格已有编号，不能创建重复层级: "+source)
                journal({**base,"event":"attempt"})
                try:
                    key=mutate("POST","/rest/api/2/issue",{"fields":f})["key"]
                except HTTPError as exc:
                    journal({**base,"event":"rejected" if 400<=exc.code<500 else "uncertain","http":exc.code,"error":exc.read().decode(errors="replace")})
                    raise
                except Exception as exc:
                    journal({**base,"event":"uncertain","error":type(exc).__name__});raise
                journal({**base,"event":"created","key":key})
                print(json.dumps({"event":"created","stage":args.stage,"key":key,"row":num,"summary":row["Summary"]},ensure_ascii=False),flush=True)
        obj=view(key);verify(obj,row,args.stage,actor)
        (args.state_dir/(key+".json")).write_text(json.dumps(obj,ensure_ascii=False,indent=2))
        journal({**base,"event":"verified","key":key})
        if args.stage in ("epic","task"):
            cell=("G" if args.stage=="epic" else "J")+str(num)
            current=call(args.lark_bin,["sheets","+read","--as","user","--spreadsheet-token",snap["source_token"],"--range",sheet["sheet_id"]+"!"+cell,"--value-render-option","FormattedValue"])
            old=text(current["valueRange"]["values"][0][0])
            if old and old!=key:
                raise RuntimeError("回填格已有不同值: "+cell)
            if old!=key:
                call(args.lark_bin,["sheets","+write","--as","user","--spreadsheet-token",snap["source_token"],"--range",sheet["sheet_id"]+"!"+cell,"--values",json.dumps([[key]])])
            after=call(args.lark_bin,["sheets","+read","--as","user","--spreadsheet-token",snap["source_token"],"--range",sheet["sheet_id"]+f"!A{num}:"+("M" if args.stage=="epic" else "Q")+str(num),"--value-render-option","FormattedValue"])
            values=after["valueRange"]["values"][0]
            assert text(values[6 if args.stage=="epic" else 9])==key
            if args.stage=="epic":
                due=day(text(values[12])).replace("/","-")
                mutate("PUT","/rest/api/2/issue/"+key,{"fields":{"duedate":due}})
                obj=view(key);assert obj["fields"]["duedate"]==due
                (args.state_dir/(key+".json")).write_text(json.dumps(obj,ensure_ascii=False,indent=2))
            journal({**base,"event":"backfilled","key":key,"cell":cell,"revision":after.get("revision")})
            print(json.dumps({"event":"backfilled","key":key,"cell":cell}),flush=True)
    print(json.dumps({"stage":args.stage,"completed":len(prepared),"skipped":len(skipped)}),flush=True)


if __name__=="__main__":
    main()
