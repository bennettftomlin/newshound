"""Compare the backends on the same items, against labels neither model produced.

Labels (all derived in code, independent of both models -- a model graded by a
label another model made measures agreement, not accuracy):
  * substantive: from the COURT's own event name for the filing (CM/ECF short
    description): "Complaint", "Indictment", "Order on ... Sentencing" vs "Notice of
    Appearance", "Pro Hac Vice", "Extension of Time". Only clear cases are labelled.
  * party: whether the watched term appears in the case caption (for CourtListener
    search hits). A string test: "HTX" in "Housing Alliance HTX" counts, so it is a
    proxy for the Coinbase-cited-as-precedent case, not a judgement of identity.
  * followed: the docket is linked from a published Protos post or a vault draft --
    the closest thing to "Protos thought this case was news".
"""
import collections
import csv
import json
import math
import pathlib
import re
import statistics as st

KB = pathlib.Path(__file__).resolve().parents[2]          # claude_knowledge

SUBSTANTIVE = re.compile(r"\b(complaint|indictment|information|petition|judgment|verdict|plea|sentenc\w*|"
                         r"opinion|findings of fact|injunction|restraining order|consent (judgment|decree)|"
                         r"settlement|forfeiture|seizure|arrest|order (granting|denying|dismissing)|"
                         r"order on motion to dismiss|order on motion for summary)\b", re.I)
PROCEDURAL = re.compile(r"\b(appearance|pro hac vice|extension of time|assign\w*|scheduling|set hearings?|"
                        r"set (motion and )?deadlines|motion submitted|referred|certificate of service|summons|"
                        r"mailing|transcript|disclosure statement|notice of completion|clerk'?s notice|"
                        r"minute entry|stipulat\w*|opt-out|teleconference|status report|oral argument|"
                        r"reply|motion to continue|withdraw\w*|substitut\w*|civil cover sheet|"
                        r"text only)\b", re.I)


def label_substantive(it):
    f = it["state"].get("filing") or {}
    ev = f.get("event") or (f.get("description") or "")[:90]
    if not ev or "CourtListener" not in it["feed_label"]:
        return None
    sub, proc = bool(SUBSTANTIVE.search(ev)), bool(PROCEDURAL.search(ev))
    if sub and not proc:
        return 1
    if proc and not sub:
        return 0
    return None


EVENT_RULES = [   # (court event-name pattern, expected category) -- clear cases only
    (re.compile(r"^(criminal complaint|indictment|information)\b", re.I), "charges"),
    (re.compile(r"\b(plea agreement|consent judgment|settlement)\b", re.I), "settlement_or_plea"),
    (re.compile(r"\bsentenc\w*|^judgment\b", re.I), "sentencing"),
    (re.compile(r"^(complaint|petition|notice of removal)\b", re.I), "new_case"),
]


def label_event(it):
    f = it["state"].get("filing") or {}
    ev = f.get("event") or ""
    if not ev or "CourtListener" not in it["feed_label"]:
        return None
    for pat, cat in EVENT_RULES:
        if pat.search(ev):
            if cat == "new_case" and f.get("document_number") not in (1, None):
                return None          # a complaint deep in a docket is an amendment, not a new case
            return cat
    return "procedural" if label_substantive(it) == 0 else None


def label_party(it):
    if not it["feed_label"].startswith("CourtListener RECAP search"):
        return None
    term = (it["state"].get("watch_term") or "").lower()
    name = ((it["state"].get("case") or {}).get("name") or it["title"] or "").lower()
    desc = ((it["state"].get("filing") or {}).get("description") or "").lower()
    words = [w for w in re.split(r"\W+", term) if w]
    in_caption = all(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", name) for w in words)
    if in_caption:
        return 1
    if not any(w in desc for w in words):
        return 0
    return None


def followed_dockets():
    """Docket ids linked from Protos's published posts or the vault's Protos drafts."""
    ids = set()
    pat = re.compile(r"courtlistener\.com/docket/(\d+)")
    for p in (KB / "derived" / "headlines" / "pages").glob("*"):
        ids |= set(pat.findall(p.read_text(errors="replace")))
    for p in (KB / "Cryptocurrency").rglob("*.md"):
        try:
            ids |= set(pat.findall(p.read_text(errors="replace")))
        except OSError:
            pass
    return ids


def docket_of(it):
    m = re.search(r"/docket/(\d+)/", it["link"] or "") or re.search(r"/docket/(\d+)/", it["feed"])
    return m.group(1) if m else None


def auc(pairs):
    """P(score of a positive > score of a negative), ties half. None if one class is empty."""
    pos = [s for s, y in pairs if y == 1]; neg = [s for s, y in pairs if y == 0]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def pearson(xs, ys):
    if len(xs) < 3:
        return None
    mx, my = st.mean(xs), st.mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs)); sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy) if sx and sy else None


def fmt(x, nd=2):
    return "—" if x is None else f"{x:.{nd}f}"


def report(run):
    items = {json.loads(l)["key"]: json.loads(l) for l in open(run / "items.jsonl")}
    backends = {}
    for f in sorted(run.glob("judgments-*.jsonl")):
        name = f.stem.replace("judgments-", "")
        backends[name] = {r["key"]: r for r in map(json.loads, open(f)) if "answers" in r}
    names = list(backends)
    followed = followed_dockets()
    for it in items.values():
        it["labels"] = {"substantive": label_substantive(it), "party": label_party(it),
                        "event": label_event(it),
                        "followed": int(docket_of(it) in followed) if docket_of(it) else None}

    L = [f"# Newshound triage test — {run.name}", "",
         f"{len(items)} items rebuilt from the feeds as newshound saw them "
         f"({dict(collections.Counter(i['group'] for i in items.values()))}); "
         f"judged by: {', '.join(f'{n} ({len(b)})' for n, b in backends.items())}.", ""]

    # --- cost and speed
    L += ["## Speed and cost", "", "| backend | items | median s/item | median input tokens |", "|---|---|---|---|"]
    for n, b in backends.items():
        secs = [r["meta"]["seconds"] for r in b.values()]
        toks = [(r["meta"].get("usage") or {}).get("input_tokens") or r["meta"].get("prompt_tokens") or 0 for r in b.values()]
        L.append(f"| {n} | {len(b)} | {fmt(st.median(secs))} | {st.median(toks):.0f} |")
    L.append("")

    # --- against independent labels
    L += ["## Against labels neither model made", "",
          "AUC: chance 0.50, perfect 1.00. Accuracy at a 0.5 cut.", "",
          "| question | label (n pos/neg) | " + " | ".join(f"{n} AUC | {n} acc" for n in names) + " |",
          "|---|---|" + "---|---|" * len(names)]
    for q, lab in (("substantive", "substantive"), ("subject_is_party", "party")):
        rows = [(k, it["labels"][lab]) for k, it in items.items() if it["labels"][lab] is not None]
        npos = sum(y for _, y in rows); cells = []
        for n in names:
            pairs = [(backends[n][k]["answers"][q]["p"], y) for k, y in rows
                     if k in backends[n] and backends[n][k]["answers"][q].get("p") is not None]
            acc = sum((p >= 0.5) == bool(y) for p, y in pairs) / len(pairs) if pairs else None
            cells += [fmt(auc(pairs)), fmt(acc)]
        L.append(f"| {q} | {lab} ({npos}/{len(rows) - npos}) | " + " | ".join(cells) + " |")
    rows = [(k, it["labels"]["followed"]) for k, it in items.items() if it["labels"]["followed"] is not None]
    cells = []
    for n in names:
        pairs = [(backends[n][k]["answers"]["protos_interest"]["score"], y) for k, y in rows
                 if k in backends[n] and backends[n][k]["answers"]["protos_interest"].get("score") is not None]
        cells += [fmt(auc(pairs)), "—"]
    npos = sum(y for _, y in rows)
    L.append(f"| protos_interest | case Protos wrote about ({npos}/{len(rows) - npos}) | " + " | ".join(cells) + " |")
    ev_rows = [(k, it["labels"]["event"]) for k, it in items.items() if it["labels"]["event"]]
    cells = []
    for n in names:
        got = [(backends[n][k]["answers"]["event"]["choice"], y) for k, y in ev_rows if k in backends[n]]
        cells += ["—", fmt(sum(c == y for c, y in got) / len(got) if got else None)]
    L.append(f"| event (category) | court's event name ({len(ev_rows)}: "
             + ", ".join(f"{c} {n}" for c, n in collections.Counter(y for _, y in ev_rows).most_common()) + ") | "
             + " | ".join(cells) + " |")
    L.append("")
    for n in names:
        miss = collections.Counter((y, backends[n][k]["answers"]["event"]["choice"]) for k, y in ev_rows
                                   if k in backends[n] and backends[n][k]["answers"]["event"]["choice"] != y)
        if miss:
            L.append(f"{n} event misses (court label → model): " + "; ".join(f"{a}→{b} ×{c}" for (a, b), c in miss.most_common(6)))
    L.append("")

    # --- agreement between backends
    if len(names) >= 2:
        a, b = names[0], names[1]
        both = [k for k in items if k in backends[a] and k in backends[b]]
        L += [f"## {a} vs {b} on the same {len(both)} items", "", "| question | agreement |", "|---|---|"]
        for q, spec in __import__("triage.questions", fromlist=["QUESTIONS"]).QUESTIONS.items():
            if spec["type"] == "noul":
                xs = [(backends[a][k]["answers"][q]["p"], backends[b][k]["answers"][q]["p"]) for k in both
                      if backends[b][k]["answers"][q].get("p") is not None]
                agree = sum((x >= .5) == (y >= .5) for x, y in xs) / len(xs) if xs else None
                L.append(f"| {q} (yes/no) | same side of 0.5 in {fmt(100 * agree if agree is not None else None, 0)}% · "
                         f"r = {fmt(pearson(*zip(*xs)) if xs else None)} · mean P(yes) {a} {fmt(st.mean(x for x, _ in xs))}, {b} {fmt(st.mean(y for _, y in xs))} |")
            elif spec["type"] == "choice":
                xs = [(backends[a][k]["answers"][q]["choice"], backends[b][k]["answers"][q]["choice"]) for k in both]
                same = sum(x == y for x, y in xs) / len(xs)
                L.append(f"| {q} (category) | same pick in {100 * same:.0f}% |")
            else:
                xs = [(backends[a][k]["answers"][q]["score"], backends[b][k]["answers"][q]["score"]) for k in both
                      if backends[b][k]["answers"][q].get("score") is not None]
                L.append(f"| {q} (0–4) | r = {fmt(pearson(*zip(*xs)))} · mean {a} {fmt(st.mean(x for x, _ in xs))}, "
                         f"{b} {fmt(st.mean(y for _, y in xs))} · mean gap {fmt(st.mean(abs(x - y) for x, y in xs))} |")
        L.append("")
        # category confusion
        conf = collections.Counter((backends[a][k]["answers"]["event"]["choice"], backends[b][k]["answers"]["event"]["choice"]) for k in both)
        L += [f"Most common category pairs ({a} → {b}): " + "; ".join(f"{x}→{y} ×{n}" for (x, y), n in conf.most_common(8)), ""]

    # --- how interest spreads, by independent label
    L += ["## Interest score by label", "", "| group | n | " + " | ".join(f"{n} mean" for n in names) + " |",
          "|---|---|" + "---|" * len(names)]
    groups = [("court says substantive", lambda it: it["labels"]["substantive"] == 1),
              ("court says procedural", lambda it: it["labels"]["substantive"] == 0),
              ("term in caption", lambda it: it["labels"]["party"] == 1),
              ("term not in caption or docket text", lambda it: it["labels"]["party"] == 0),
              ("case Protos wrote about", lambda it: it["labels"]["followed"] == 1),
              ("hidden by the ID-collision bug", lambda it: it["group"] == "id_collision"),
              # Scores here rest on a caption alone: no docket text, no document. A high
              # number on one of these rates the CASE, not the filing.
              ("title and link only (not enriched)", lambda it: it["enriched"] == "rss-only")]
    for label, pred in groups:
        ks = [k for k, it in items.items() if pred(it)]
        cells = []
        for n in names:
            vals = [backends[n][k]["answers"]["protos_interest"]["score"] for k in ks if k in backends[n]
                    and backends[n][k]["answers"]["protos_interest"].get("score") is not None]
            cells.append(fmt(st.mean(vals)) if vals else "—")
        L.append(f"| {label} | {len(ks)} | " + " | ".join(cells) + " |")
    L.append("")

    # --- lists a person can check
    def row(k):
        it = items[k]
        vals = " · ".join(f"{n} {fmt(backends[n][k]['answers']['protos_interest']['score'], 1)}"
                          for n in names if k in backends[n])
        return f"- **{it['title'][:90]}** — {it['feed_label'].replace('CourtListener RECAP search: ', 'q=')} · {vals}  \n  {((it['state'].get('filing') or {}).get('event') or it['state'].get('summary') or '')[:120]} · <{it['link']}>"
    for n in names:
        top = sorted(backends[n], key=lambda k: -(backends[n][k]["answers"]["protos_interest"]["score"] or 0))[:12]
        L += [f"## Top 12 by {n}", ""] + [row(k) for k in top] + [""]
    if len(names) >= 2:
        a, b = names[0], names[1]
        both = [k for k in items if k in backends[a] and k in backends[b]]
        gap = sorted(both, key=lambda k: -abs((backends[a][k]["answers"]["protos_interest"]["score"] or 0) -
                                              (backends[b][k]["answers"]["protos_interest"]["score"] or 0)))[:12]
        L += [f"## Biggest disagreements on interest ({a} vs {b})", ""] + [row(k) for k in gap] + [""]

    (run / "report.md").write_text("\n".join(L), encoding="utf-8")
    with open(run / "table.csv", "w", newline="") as f:
        w = csv.writer(f)
        head = ["key", "group", "feed", "title", "link", "event_by_court", "label_substantive", "label_party", "label_followed"]
        for n in names:
            head += [f"{n}_{q}" for q in ("substantive", "subject_is_party", "buried_detail", "event", "protos_interest")]
        w.writerow(head)
        for k, it in items.items():
            r = [k, it["group"], it["feed_label"], it["title"], it["link"], (it["state"].get("filing") or {}).get("event"),
                 it["labels"]["substantive"], it["labels"]["party"], it["labels"]["followed"]]
            for n in names:
                a = (backends[n].get(k) or {}).get("answers") or {}
                r += [fmt((a.get(q) or {}).get("p")) for q in ("substantive", "subject_is_party", "buried_detail")]
                r += [(a.get("event") or {}).get("choice"), fmt((a.get("protos_interest") or {}).get("score"))]
            w.writerow(r)
    print(f"{run / 'report.md'}\n{run / 'table.csv'}")
    return 0
