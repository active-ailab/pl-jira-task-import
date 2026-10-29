#!/usr/bin/env python3
"""Build a local import preview from computed spreadsheet values. No network or writes to Jira/Lark."""
import argparse
import collections
import csv
import datetime
import hashlib
import json
import pathlib
import re

STAGES = {"Epic": "epic", "适配任务": "task", "适配子任务": "subtask", "新功能开发任务": "newtask"}
ERROR = re.compile(r"^#(?:REF!|N/A|VALUE!|DIV/0!|NAME\?|NUM!|SPILL!|ERROR!|NULL!)$")
KEY = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")
COMMON = {"Assignee": "assignee", "Issue Type": "issuetype", "Fix Version/s": "fixVersions",
          "Reporter": "reporter", "Summary": "summary", "Severity": "customfield_10401",
          "启动时间": "customfield_10702", "到期日": "duedate"}
MAPPINGS = {
    "epic": {**COMMON, "Epic Name": "customfield_10103"},
    "task": {**COMMON, "Epic Link": "customfield_10101"},
    "subtask": {**COMMON, "详细描述": "description", "父级ID": "subtask-parent-id"},
    "newtask": {**COMMON, "详细描述": "description", "Epic Name": "customfield_10103", "Epic Link": "customfield_10101"},
}


def text(v):
    if v is None:
        return ""
    if isinstance(v, list):
        return "".join(text(x) for x in v)
    if isinstance(v, dict):
        return text(v.get("text", v.get("value", "")))
    return str(v).strip()


def header(v):
    return re.sub(r"\s+", "", text(v))


def description(v):
    if isinstance(v, list):
        return "".join(description(x) for x in v)
    if isinstance(v, dict):
        label, link = text(v), v.get("link", "")
        return f"{label} ({link})" if link and link != label else label
    return text(v)


def rows(sheet):
    values = sheet["values"]
    if not values:
        return
    names = [header(v) for v in values[0]]
    for rownum, row in enumerate(values[1:], 2):
        if not any(text(v) for v in row):
            continue
        yield rownum, {name: (description(row[i]) if name == "详细描述" else text(row[i])) if i < len(row) else ""
                       for i, name in enumerate(names) if name}


def day(v):
    if not v:
        return ""
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(v, fmt).strftime("%Y/%m/%d")
        except ValueError:
            pass
    raise ValueError(f"非法日期: {v}")


def load_existing(path):
    result = {}
    for f in sorted(path.glob("*.json")):
        try:
            item = json.loads(f.read_text())
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("key") and item.get("fields", {}).get("issuetype"):
            result[item["key"]] = item
    return result


def build(snapshot, project_info, metadata, existing, include_new=False, online_checks=None):
    if snapshot.get("value_render_option") != "FormattedValue":
        raise ValueError("需要 FormattedValue 计算结果，拒绝使用公式源码快照")
    project = project_info["key"]
    project_name = project_info["name"]
    if online_checks and online_checks.get("project") != project:
        raise ValueError("在线核验结果不属于目标项目")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", project):
        raise ValueError("Invalid project key")
    sheets = {s["title"]: s for s in snapshot["sheets"]}
    missing = {"Epic", "适配任务", "适配子任务", "项目人员信息"} - sheets.keys()
    if missing:
        raise ValueError(f"Missing sheets: {sorted(missing)}")
    unchecked = [title for title in STAGES if title in sheets and not sheets[title].get("filter_checked")]
    if unchecked:
        raise ValueError(f"缺少工作表筛选核验，拒绝全量导入: {unchecked}")
    type_fields = {}
    for p in metadata.get("projects", []):
        if p["key"] == project:
            type_fields.update({t["name"]: t["fields"] for t in p["issuetypes"]})
    if not {"Epic", "Task", "Sub-task"} <= type_fields.keys():
        raise ValueError("目标项目缺少 Epic/Task/Sub-task 创建元数据")
    people = {r.get("Jira中的Username") for _, r in rows(sheets["项目人员信息"]) if r.get("Jira中的Username")}
    plan = {"project": project, "project_name": project_name, "source_token": snapshot["source_token"],
            "revision": snapshot["revision"], "mode": "preview", "items": [], "excluded": [],
            "online_checks_remaining": ["逐一检查待创建 owner 在目标项目可指派", "无源编号的待创建行查询全项目同类型同父级同标题重复项",
                                         "确定正式导入范围和项目业务数据，提交前重新读取模板及已有 Jira"],
            "ignored_csv_fields": ["Component/s", "工作量（人小时）"], "protected_sheet_writes": 0,
            "online_checks": online_checks or {}}

    def append(sheet_title, rownum, row, stage):
        sheet = sheets[sheet_title]
        item = {"source": f"{sheet['sheet_id']}!{rownum}", "sheet": sheet_title, "row": rownum,
                "stage": stage, "summary": row.get("Summary", ""), "source_values": row,
                "problems": [], "warnings": [], "action": "create", "existing_key": ""}
        item["source_id"] = f"{snapshot['source_token']}:{sheet['sheet_id']}:{rownum}"
        item["fingerprint"] = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        item["csv"] = {c: row.get(header(c), "") for c in MAPPINGS[stage]}
        if stage == "epic":
            item["existing_key"] = row.get("EpicLink(导入后填写)", "")
        elif stage == "task":
            item["existing_key"] = row.get("Key(导入后填写)", "")
        if not item["summary"]:
            item["problems"].append("缺少 Summary")
        expected_type = "Epic" if stage == "epic" else "Sub-task" if stage == "subtask" else "Task"
        if row.get("IssueType") != expected_type:
            item["problems"].append(f"Issue Type 应为 {expected_type}")
        for col, value in item["csv"].items():
            if ERROR.fullmatch(value):
                item["problems"].append(f"{col} 公式错误: {value}")
            elif re.match(r"^=?\s*(?:VLOOKUP|HLOOKUP|INDEX|MATCH|IF|TODAY|SUMIF)\s*\(", value, re.I):
                item["problems"].append(f"{col} 仍为公式源码")
        for col in ("Assignee", "Reporter"):
            user = item["csv"].get(col, "")
            if not user:
                item["problems"].append(f"缺少 {col}")
            elif user not in people:
                item["warnings"].append(f"{col} 不在项目人员信息中: {user}")
        if stage == "epic":
            m = re.search(r"适配(.+?)项目", item["summary"])
            if m and m[1].casefold() != project_name.casefold():
                item["problems"].append(f"Epic 标题项目 {m[1]} 与目标 {project_name} 不一致，需确认")
        if re.search(r"(?:\bAAA\b|\bBBB\b|\bCCC\b)", item["summary"]):
            item["problems"].append("包含新功能模板占位符")
        for col in ("启动时间", "到期日"):
            try:
                item["csv"][col] = day(item["csv"].get(col, ""))
            except ValueError as exc:
                item["problems"].append(str(exc))
        try:
            start, due = (item["csv"].get(c) for c in ("启动时间", "到期日"))
            if start and due and datetime.datetime.strptime(due, "%Y/%m/%d") < datetime.datetime.strptime(start, "%Y/%m/%d"):
                item["problems"].append("到期日早于启动时间")
        except ValueError:
            pass
        fields = type_fields[expected_type]
        for col in ("Severity", "Fix Version/s"):
            field = fields.get(MAPPINGS[stage][col], {})
            value = item["csv"].get(col, "")
            legal = {text(x.get("value", x.get("name", ""))) for x in field.get("allowedValues", [])}
            if value and legal and value not in legal:
                item["problems"].append(f"{col} 不是目标项目的合法值: {value}")
        supplied = {field for col, field in MAPPINGS[stage].items() if item["csv"].get(col)} | {"project"}
        if stage == "subtask":
            supplied.add("parent")
        for fid, field in fields.items():
            if field.get("required") and fid not in supplied and not field.get("hasDefaultValue") and field.get("defaultValue") is None:
                item["problems"].append(f"缺少 Jira 必填字段: {field['name']} ({fid})")
        key = item["existing_key"]
        if key:
            obj = existing.get(key)
            if not key.startswith(project + "-"):
                item["problems"].append(f"已有编号属于其他项目: {key}")
            elif not obj:
                item["problems"].append(f"已有编号尚未在线核对: {key}")
            elif (obj["fields"]["issuetype"]["name"] != expected_type or obj["fields"]["summary"] != item["summary"]):
                item["problems"].append(f"已有编号类型/标题不匹配: {key}")
            else:
                item["action"] = "reuse"
        plan["items"].append(item)
        return item

    for title, stage in STAGES.items():
        if title not in sheets:
            continue
        for num, row in rows(sheets[title]):
            reason = ""
            if num in sheets[title].get("filtered_out_rows", []):
                reason = "被工作表筛选条件排除"
            elif stage == "newtask" and not include_new:
                reason = "可选新功能未纳入"
            elif stage == "task" and row.get("是否适用本项目") == "否":
                reason = "不适用本项目"
            elif stage == "task" and row.get("是否适用本项目") != "是":
                reason = "适用性未明确"
            elif stage == "subtask":
                parent = row.get("父级ID", "")
                if parent == "NG":
                    reason = "父级为NG"
                elif ERROR.fullmatch(parent):
                    reason = f"父级公式错误:{parent}"
                elif not KEY.fullmatch(parent):
                    reason = "父级编号未就绪"
                elif not parent.startswith(project + "-"):
                    reason = "父级不属于目标项目"
            if reason:
                plan["excluded"].append({"sheet": title, "row": num, "source": f"{sheets[title]['sheet_id']}!{num}",
                                         "summary": row.get("Summary", ""), "reason": reason})
                continue
            append(title, num, row, stage)
    for item in plan["items"]:
        if item["stage"] not in ("task", "subtask", "newtask"):
            continue
        col = "父级ID" if item["stage"] == "subtask" else "Epic Link"
        parent = item["csv"].get(col, "")
        expected_parent_type = "Task" if item["stage"] == "subtask" else "Epic"
        obj = existing.get(parent)
        if not KEY.fullmatch(parent) or not parent.startswith(project + "-"):
            item["problems"].append(f"{col} 缺少本项目真实编号；须完成上一阶段后重新读取")
        elif not obj:
            item["problems"].append(f"{col} 尚未在线核对: {parent}")
        elif obj["fields"]["issuetype"]["name"] != expected_parent_type:
            item["problems"].append(f"{col} 类型应为 {expected_parent_type}: {parent}")
        elif item["stage"] == "subtask":
            matches = [x for x in obj["fields"].get("subtasks", []) if x["fields"]["summary"] == item["summary"]]
            if len(matches) > 1:
                item["problems"].append("同父同标题存在多个 Jira 子任务")
            elif matches:
                item["action"] = "reuse"
                item["existing_key"] = matches[0]["key"]
        elif item["action"] == "reuse" and existing[item["existing_key"]]["fields"].get("customfield_10101") != parent:
            item["problems"].append("已有 Task 的 Epic Link 与模板不同")
    identities = collections.defaultdict(list)
    for item in plan["items"]:
        identities[(item["stage"], item["summary"], item["csv"].get("父级ID", item["csv"].get("Epic Link", "")))].append(item)
    for group in identities.values():
        if len(group) > 1:
            for item in group:
                item["problems"].append("模板中同阶段同父同标题存在重复行，需人工确认")
    if online_checks:
        checks = online_checks.get("assignee_assignable", {})
        for item in plan["items"]:
            user = item["csv"].get("Assignee", "")
            if item["action"] == "create" and checks.get(user) is False:
                item["problems"].append(f"负责人未通过目标项目可指派性检查: {user}")
        candidates = [x for x in plan["items"] if x["action"] == "create" and not x["problems"]]
        if all(checks.get(x["csv"].get("Assignee")) is True for x in candidates):
            plan["online_checks_remaining"].remove("逐一检查待创建 owner 在目标项目可指派")
    for item in plan["items"]:
        if item["action"] == "reuse":
            obj = existing.get(item["existing_key"])
            if obj:
                current = (obj["fields"].get("assignee") or {}).get("name", "")
                if current != item["csv"].get("Assignee", ""):
                    item["problems"].append("已有 Jira 的 owner 与模板不同，未自动更新")
            else:
                item["warnings"].append("复用候选仅核对了父级和标题；正式执行前读取详情核对 owner")
        item["status"] = "blocked" if item["problems"] else item["action"]
    plan["counts"] = dict(collections.Counter(x["status"] for x in plan["items"]))
    plan["stage_counts"] = {stage: dict(collections.Counter(x["status"] for x in plan["items"] if x["stage"] == stage)) for stage in STAGES.values()}
    plan["exclusion_counts"] = dict(collections.Counter(x["reason"] for x in plan["excluded"]))
    plan["ready_for_write"] = False  # Online checks and authorization belong to the invoking agent.
    return plan


def write_outputs(plan, project_info, output, batch_size=250):
    if not 1 <= batch_size <= 250:
        raise ValueError("CSV batch_size 必须为 1..250")
    output.mkdir(parents=True, exist_ok=True)
    if list(output.glob("*-batch-*.csv")):
        raise ValueError("输出目录已有批次 CSV，请使用新的预览目录避免混入旧文件")
    (output/"plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    files = []
    for stage, mapping in MAPPINGS.items():
        selected = [x for x in plan["items"] if x["stage"] == stage and x["status"] == "create"]
        for start in range(0, len(selected), batch_size):
            name = f"{stage}-batch-{start//batch_size+1:03}.csv"
            with (output/name).open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(mapping))
                writer.writeheader()
                writer.writerows(x["csv"] for x in selected[start:start+batch_size])
            files.append((name, len(selected[start:start+batch_size])))
        config = {"config.version": "2.0", "config.project.from.csv": "false", "config.encoding": "UTF-8",
                  "config.email.suffix": "@", "config.field.mappings": {}, "config.value.mappings": {},
                  "config.delimiter": ",", "config.date.format": "yyyy/MM/dd",
                  "config.project": {"project.key": project_info["key"], "project.name": project_info["name"],
                                     "project.lead": project_info.get("lead", {}).get("name")}}
        for column, field in mapping.items():
            config["config.field.mappings"][column] = {"existing.custom.field": field[len("customfield_"):]} if field.startswith("customfield_") else {"jira.field": field}
        (output/f"{stage}-configuration.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    lines = [f"# {plan['project']} PL 任务导入预检", "", f"来源表格：{plan['source_token']}，revision {plan['revision']}。",
             "", "这是只读快照和本地导入预览，未创建或修改 Jira，未回填飞书。待创建 CSV 仍须完成在线可指派性、查重和授权检查。", "",
             "| 阶段 | 候选总数 | 已有可复用 | 待创建候选 | 阻塞 |", "| --- | ---: | ---: | ---: | ---: |"]
    for stage, counts in plan["stage_counts"].items():
        lines.append(f"| {stage} | {sum(counts.values())} | {counts.get('reuse',0)} | {counts.get('create',0)} | {counts.get('blocked',0)} |")
    lines += ["", "## 过滤情况", ""] + [f"- {reason}：{n}" for reason, n in plan["exclusion_counts"].items()]
    lines += ["", "## CSV 预览批次", ""] + ([f"- [{name}]({name})：{n} 条" for name, n in files] or ["暂无无阻塞的待创建行。"])
    lines += ["", "## 阻塞明细", "", "| 来源 | 标题 | 原因 |", "| --- | --- | --- |"]
    for x in plan["items"]:
        if x["problems"]:
            vals = [x["source"], x["summary"], "；".join(x["problems"])]
            lines.append("| " + " | ".join(v.replace("|", "\\|").replace("\n", " ") for v in vals) + " |")
    lines += ["", "父级公式错误等被排除行的逐行位置见 plan.json/excluded；未作为正常的不适用项忽略。", "",
              "## 复用记录", "", "| 来源 | Jira | 标题 |", "| --- | --- | --- |"]
    for x in plan["items"]:
        if x["status"] == "reuse":
            lines.append(f"| {x['source']} | {x['existing_key']} | {x['summary']} |")
    lines += ["", "## 仍需在线检查", ""] + [f"- {s}" for s in plan["online_checks_remaining"]]
    if plan["online_checks"]:
        lines += ["", "已纳入本地在线核验快照：" + plan["online_checks"].get("fetched_at", "时间未提供") + "。凭据未写入计划。"]
    lines += ["", "原附件未映射 Component/s 和人小时估算，本次保留在源行信息中，不自动新增字段映射。", ""]
    (output/"report.md").write_text("\n".join(lines))
    return files


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", type=pathlib.Path, required=True)
    ap.add_argument("--project-info", type=pathlib.Path, required=True)
    ap.add_argument("--metadata", type=pathlib.Path, required=True)
    ap.add_argument("--existing", type=pathlib.Path, required=True)
    ap.add_argument("--output", type=pathlib.Path, required=True)
    ap.add_argument("--include-new-features", action="store_true")
    ap.add_argument("--online-checks", type=pathlib.Path, help="可选在线核验结果 JSON；需包含 project、assignee_assignable")
    args = ap.parse_args()
    snap, proj, meta = [json.loads(p.read_text()) for p in (args.snapshot, args.project_info, args.metadata)]
    checks = json.loads(args.online_checks.read_text()) if args.online_checks else None
    plan = build(snap, proj, meta, load_existing(args.existing), args.include_new_features, checks)
    files = write_outputs(plan, proj, args.output)
    print(json.dumps({"counts": plan["counts"], "stages": plan["stage_counts"], "csv": files,
                      "ready_for_write": False, "report": str(args.output/"report.md")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
