#!/usr/bin/env python3
"""Read a Lark spreadsheet completely; never write remote data."""
import argparse
import concurrent.futures
import datetime
import json
import pathlib
import shutil
import subprocess

REQUIRED = {"项目人员信息", "Epic", "适配任务", "适配子任务", "新功能开发任务", "导入到Jira"}
TASK_SHEETS = {"Epic", "适配任务", "适配子任务", "新功能开发任务"}


def call(cli, args):
    p = subprocess.run([cli, *args], capture_output=True, text=True, timeout=120)
    try:
        result = json.loads(p.stdout)
    except ValueError as exc:
        raise RuntimeError(f"Invalid CLI JSON (exit {p.returncode})") from exc
    if p.returncode or result.get("ok") is False or result.get("code", 0) != 0:
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result["data"]


def col_name(n):
    result = ""
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def snapshot(cli, token, workers=4, titles=None):
    before = call(cli, ["sheets", "+info", "--as", "user", "--spreadsheet-token", token])
    sheets = before["sheets"]["sheets"]
    if titles:
        missing = set(titles) - {s["title"] for s in sheets}
        if missing:
            raise ValueError(f"Unknown sheet titles: {sorted(missing)}")
        sheets = [s for s in sheets if s["title"] in titles]
    def get_filter(sheet):
        data = call(cli, ["sheets", "spreadsheet.sheet.filters", "get", "--as", "user", "--params",
                          json.dumps({"spreadsheet_token": token, "sheet_id": sheet["sheet_id"]})])
        return sheet["sheet_id"], data.get("sheet_filter_info", {})
    task_sheets = [s for s in sheets if s["title"] in TASK_SHEETS]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        filters = dict(ex.map(get_filter, task_sheets))
    jobs = []
    for s in sheets:
        if s["title"] not in REQUIRED:
            continue
        grid = s["grid_properties"]
        for start in range(1, grid["row_count"] + 1, 200):
            end = min(start + 199, grid["row_count"])
            jobs.append((s["sheet_id"], start, f"A{start}:{col_name(grid['column_count'])}{end}"))

    def read(job):
        sid, start, rng = job
        data = call(cli, ["sheets", "+read", "--as", "user", "--spreadsheet-token", token,
                          "--sheet-id", sid, "--range", rng,
                          "--value-render-option", "FormattedValue"])
        if data.get("truncated"):
            raise RuntimeError(f"Truncated range: {sid}!{rng}")
        vr = data["valueRange"]
        return sid, start, vr["values"], vr.get("revision", data.get("revision"))

    pages = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for sid, start, rows, revision in ex.map(read, jobs):
            pages.setdefault(sid, []).append((start, rows, revision))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        if dict(ex.map(get_filter, task_sheets)) != filters:
            raise RuntimeError("Spreadsheet filter changed while reading; retry snapshot")
    result = {"source_token": token, "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "value_render_option": "FormattedValue", "sheets": []}
    for s in sheets:
        if s["title"] not in REQUIRED:
            continue
        values = []
        revisions = set()
        for start, rows, revision in sorted(pages[s["sheet_id"]]):
            values.extend([[]] * max(0, start - 1 - len(values)))
            values.extend(rows)
            revisions.add(revision)
        if len(revisions) != 1:
            raise RuntimeError(f"Spreadsheet changed while reading {s['title']}; retry snapshot")
        result["sheets"].append({**s, "values": values, "revision": revisions.pop(),
                                 "filter_checked": s["sheet_id"] in filters,
                                 "filter_info": filters.get(s["sheet_id"], {}),
                                 "filtered_out_rows": filters.get(s["sheet_id"], {}).get("filtered_out_rows", [])})
    revisions = {s["revision"] for s in result["sheets"]}
    if len(revisions) != 1:
        raise RuntimeError("Spreadsheet changed between sheets; retry snapshot")
    result["revision"] = revisions.pop()
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--token", required=True, help="Resolved spreadsheet token, not wiki token")
    ap.add_argument("--lark-bin", default=shutil.which("lark-cli") or str(pathlib.Path.home()/".npm-global/bin/lark-cli"))
    ap.add_argument("--output", required=True, type=pathlib.Path)
    ap.add_argument("--sheet-title", action="append", help="仅读取指定工作表；可重复，用于上一层回填后的刷新")
    args = ap.parse_args()
    result = snapshot(args.lark_bin, args.token, titles=args.sheet_title)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "revision": result["revision"],
                      "sheets": {s["title"]: len(s["values"]) for s in result["sheets"]}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
