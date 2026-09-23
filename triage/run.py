"""Newshound triage experiment: collect recent alerts, ask two models, compare.

    python -m triage.run collect                     # -> triage/runs/<ts>/items.jsonl
    python -m triage.run judge  <run> --backend jev  # -> judgments-jev.jsonl
    python -m triage.run judge  <run> --backend gemma [--model gemma4:latest]
    python -m triage.run report <run>                # -> report.md + table.csv

Nothing here posts to Slack or writes to newshound's state DB.
"""
import argparse
import datetime as dt
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("collect")
    j = sub.add_parser("judge"); j.add_argument("run"); j.add_argument("--backend", required=True)
    j.add_argument("--model"); j.add_argument("--limit", type=int)
    r = sub.add_parser("report"); r.add_argument("run")
    a = ap.parse_args()
    if a.cmd == "collect":
        from triage.sources import collect
        run = HERE / "runs" / dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        run.mkdir(parents=True)
        items = collect()
        with open(run / "items.jsonl", "w") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        groups = {}
        for it in items:
            groups[it["group"]] = groups.get(it["group"], 0) + 1
        print(f"{len(items)} items -> {run}/items.jsonl  {groups}")
        return 0
    if a.cmd == "judge":
        from triage.judges import judge_run
        return judge_run(pathlib.Path(a.run), a.backend, model=a.model, limit=a.limit)
    if a.cmd == "report":
        from triage.report import report
        return report(pathlib.Path(a.run))


if __name__ == "__main__":
    sys.exit(main())
