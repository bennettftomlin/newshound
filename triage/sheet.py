"""Build the labelling sheet for one run: a stratified sample of its documents with
both models' answers, embedded in sheet.html (beside this file) for publishing.

    python -m triage.run sheet <run> [--n 60] [--seed 20260923]   # -> <run>/sheet.html

Strata, drawn in this order and never twice: the widest Jev-gemma interest gaps,
the highest interest from either model, AI-company searches, filings the court's
own event name marks procedural, and a random remainder. The first two are picked
BY the models' scores, so agreement measured on the sheet describes the sample,
not the feed; the page says so.

The page stores 0-4 ratings, on the same interest scale the models answered, in its own
db (collection `labels`, one document per item, id = the run's item key, `label` 0-4), so re-running with the same run and seed reproduces the
same sample and the labels line up.
"""
import json
import pathlib
import random
import re
import urllib.parse

from triage.report import label_substantive

HERE = pathlib.Path(__file__).resolve().parent
AI = re.compile(r"sam altman|dario amodei|elon musk|openai|spacex|anthropic", re.I)
STRATA = (("disagree", 1 / 4), ("top", 1 / 4), ("ai", 1 / 6), ("procedural", 1 / 6), ("random", 1 / 6))
BARE = "https://www.courtlistener.com"


def _load(run):
    items = {}
    for line in open(run / "items.jsonl"):
        it = json.loads(line)
        items[it["key"]] = it
    judged, models = {}, {}
    for backend in ("jev", "gemma"):
        rows = [json.loads(l) for l in open(run / f"judgments-{backend}.jsonl")]
        judged[backend] = {r["key"]: r["answers"] for r in rows if "answers" in r}
        models[backend] = next((r["meta"]["model"] for r in rows if r.get("meta", {}).get("model")), backend)
    return items, judged, models


def _interest(a):
    return a["protos_interest"]["score"]


def sample(items, judged, n, seed):
    J, G = judged["jev"], judged["gemma"]
    keys = [k for k in items if k in J and k in G]
    rnd = random.Random(seed)
    quota = {name: round(n * share) for name, share in STRATA}
    quota["random"] = n - sum(v for k, v in quota.items() if k != "random")
    pools = {
        "disagree": sorted(keys, key=lambda k: -abs(_interest(J[k]) - _interest(G[k]))),
        "top": sorted(keys, key=lambda k: -max(_interest(J[k]), _interest(G[k]))),
        "ai": [k for k in keys if AI.search(items[k]["state"].get("watch_term", ""))],
        "procedural": [k for k in keys if label_substantive(items[k]) == 0],
        "random": keys[:],
    }
    for name in ("ai", "procedural", "random"):
        rnd.shuffle(pools[name])
    stratum = {}
    for name, _ in STRATA:
        taken = 0
        for k in pools[name]:
            if taken == quota[name]:
                break
            if k not in stratum:
                stratum[k] = name
                taken += 1
    chosen = list(stratum)
    rnd.shuffle(chosen)
    return chosen, stratum


def _model(a):
    return {"interest": round(_interest(a), 2), "substantive": round(a["substantive"]["p"], 2),
            "party": round(a["subject_is_party"]["p"], 2), "event": a["event"]["choice"],
            "buried": round(a["buried_detail"]["p"], 2)}


def card(it, stratum, jev, gemma):
    st = it["state"]
    case, f = st.get("case") or {}, st.get("filing") or {}
    link = it["link"]
    if link.rstrip("/") == BARE:
        link = BARE + "/?" + urllib.parse.urlencode({"q": f'caseName:("{it["title"]}")', "type": "r"})
    text = st.get("text_excerpt") or ""
    return {
        "key": it["key"], "stratum": stratum, "title": it["title"], "link": link,
        "source": it["feed_label"],
        "terms": [t.strip() for t in (st.get("watch_term") or "").split(",") if t.strip()],
        "court": case.get("court"), "docket": case.get("docket_number"), "filed": case.get("filed"),
        "nature": case.get("nature_of_suit"), "cause": case.get("cause"),
        "entry_date": f.get("entry_date") or st.get("date"), "doc": f.get("document_number"),
        "event": f.get("event"),
        "description": (f.get("description") or st.get("summary") or "")[:450],
        "excerpt": text[:600],
        # what the models were shown beyond the headline
        "seen": "text" if (text or st.get("summary")) else "docket" if case else "title",
        "jev": _model(jev), "gemma": _model(gemma),
    }


def build(run, n=60, seed=20260923, out=None):
    items, judged, models = _load(run)
    chosen, stratum = sample(items, judged, n, seed)
    data = {
        "meta": {"run": run.name, "n": len(chosen), "total": len(items), "seed": seed, "models": models,
                 # the rubric as THIS run asked it, so a rating and a score mean the same thing
                 "scale": json.load(open(run / "questions.json"))["protos_interest"]["criteria"]},
        "items": [card(items[k], stratum[k], judged["jev"][k], judged["gemma"][k]) for k in chosen],
    }
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    page = (HERE / "sheet.html").read_text().replace("/*__DATA__*/{}", blob, 1)
    if "/*__DATA__*/" in page:
        raise SystemExit("sheet.html: data placeholder not replaced")
    out = out or run / "sheet.html"
    out.write_text(page)
    counts = {}
    for k in chosen:
        counts[stratum[k]] = counts.get(stratum[k], 0) + 1
    print(f"{len(chosen)} of {len(items)} documents -> {out}  {counts}")
    return 0
