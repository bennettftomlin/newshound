"""Two backends asked the same questions (triage/questions.py) about the same items.

Jev returns probabilities natively. gemma4 is asked each question as a one-token
answer (yes/no, a letter, a digit) and its probabilities are read from Ollama's
top_logprobs for that token, normalised over the valid answers -- so both backends
give a P(yes), a distribution over options, and a probability-weighted score, and
the comparison is like for like rather than a calibrated number against a label.
`valid_mass` records how much of gemma's probability landed on a valid answer at
all; when it is low, the model wanted to say something else and the answer is weak.
"""
import concurrent.futures as cf
import json
import math
import os
import pathlib
import sys
import time

from triage.questions import QUESTIONS

LETTERS = "ABCDEFGHIJ"


TRANSPORT_DIR = pathlib.Path(__file__).resolve().parents[2] / "tools" / "kb-graph"
RETRY_STATUS = (429, 500, 502, 503, 504)


def _transport():
    """claude_knowledge's shared HTTP transport, found from this file's own location (newshound/triage/)."""
    if str(TRANSPORT_DIR) not in sys.path:
        sys.path.append(str(TRANSPORT_DIR))
    import transport
    return transport


def _post(url, body, headers, timeout=120, tries=5):
    """POST json through the shared transport: one attempt per loop turn. 429 and 5xx are retried, honouring
    Retry-After in either form (delta-seconds or an HTTP date) capped at 30 s; a network error or timeout is retried
    with a short back-off; any other status (a 401, a 403) raises at once, unretried."""
    transport = _transport()
    # the bearer key is scrubbed from every error the transport raises
    secrets = [str(v).split()[-1] for k, v in headers.items() if k.lower() == "authorization" and str(v).split()]
    for attempt in range(tries):
        try:
            r = transport.send("POST", url, json=body, headers={"Content-Type": "application/json", **headers},
                               timeout=timeout, redact=[x for x in secrets if len(x) >= 4], what="newshound triage judge", agent="newshound-triage")
            return r.json()
        except transport.TransportError as e:
            if e.kind == "http_status":
                if e.status in RETRY_STATUS and attempt < tries - 1:
                    time.sleep(min(e.retry_after if e.retry_after is not None else 2 * (attempt + 1), 30)); continue
                raise RuntimeError(f"HTTP {e.status}: {e.body[:300]!r}")
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1)); continue
            raise


class Jev:
    name = "jev"

    def __init__(self, model=None):
        self.model = model or "jev-latest"
        key = os.environ.get("TYPESAFE_API_KEY")
        f = pathlib.Path.home() / ".config" / "typesafe" / "key"
        self.key = key or (f.read_text().strip() if f.exists() else "")
        if not self.key:
            raise SystemExit("no TypeSafe key: set TYPESAFE_API_KEY or ~/.config/typesafe/key")

    def judge(self, state):
        t0 = time.time()
        d = _post("https://api.typesafe.ai/v1/systemone",
                  {"state": state, "model": self.model, "questions": QUESTIONS},
                  {"Authorization": f"Bearer {self.key}"})
        out = {}
        for qid, a in d["answers"].items():
            if a["type"] == "noul":
                out[qid] = {"p": a["noul"]}
            elif a["type"] == "choice":
                out[qid] = {"choice": a["choice"], "probs": a["probabilities"],
                            "confidence": a.get("confidence")}
            else:
                out[qid] = {"score": a["score"], "probs": a["probabilities"],
                            "confidence": a.get("confidence")}
        return out, {"model": d.get("model"), "usage": d.get("usage"), "seconds": round(time.time() - t0, 3)}


class Gemma:
    name = "gemma"
    SYSTEM = ("You are a news-desk assistant for an investigative crypto news outlet. You are shown one "
              "item from a monitoring feed as JSON, then asked one question about it. Reply with only the "
              "requested answer token and nothing else.")

    def __init__(self, model=None, host="http://localhost:11434"):
        self.model = model or "gemma4:latest"
        self.host = host

    def _ask(self, item_json, question):
        d = _post(f"{self.host}/api/chat", {
            "model": self.model, "stream": False, "think": False, "logprobs": True, "top_logprobs": 20,
            "options": {"temperature": 0, "num_predict": 1, "num_ctx": 8192},
            "messages": [{"role": "system", "content": self.SYSTEM},
                         {"role": "user", "content": f"ITEM:\n{item_json}\n\n{question}"}]}, {}, timeout=300)
        lp = (d.get("logprobs") or [{}])[0]
        tops = lp.get("top_logprobs") or [{"token": lp.get("token", ""), "logprob": lp.get("logprob", 0)}]
        return [(t["token"], math.exp(t["logprob"])) for t in tops], d.get("prompt_eval_count", 0)

    @staticmethod
    def _dist(tops, valid):
        mass = {v: 0.0 for v in valid}
        for tok, p in tops:
            k = tok.strip().strip(".,:;\"'*()").lower()
            if k in mass:
                mass[k] += p
        total = sum(mass.values())
        if total <= 0:
            return None, 0.0
        return {k: v / total for k, v in mass.items()}, total

    def judge(self, state):
        t0, tokens = time.time(), 0
        item = json.dumps(state, ensure_ascii=False, indent=1)
        out = {}
        for qid, q in QUESTIONS.items():
            instr = q["instructions"]
            if q["type"] == "noul":
                c = q.get("criteria") or {}
                prompt = (f"QUESTION: {instr}\nYes means: {c.get('true', 'yes')}\nNo means: {c.get('false', 'no')}\n"
                          "Answer with exactly one word: yes or no.")
                tops, n = self._ask(item, prompt)
                dist, mass = self._dist(tops, ["yes", "no"])
                out[qid] = {"p": dist["yes"] if dist else None, "valid_mass": round(mass, 4)}
            elif q["type"] == "choice":
                opts = list(q["criteria"].items())
                lines = "\n".join(f"{LETTERS[i]}. {k}: {v or k}" for i, (k, v) in enumerate(opts))
                prompt = f"QUESTION: {instr}\nOptions:\n{lines}\nAnswer with the single letter of the best option."
                tops, n = self._ask(item, prompt)
                dist, mass = self._dist(tops, [LETTERS[i].lower() for i in range(len(opts))])
                probs = {opts[i][0]: dist[LETTERS[i].lower()] for i in range(len(opts))} if dist else None
                out[qid] = {"choice": max(probs, key=probs.get) if probs else None, "probs": probs,
                            "valid_mass": round(mass, 4)}
            else:
                lines = "\n".join(f"{i}: {lvl}" for i, lvl in enumerate(q["criteria"]))
                prompt = f"QUESTION: {instr}\nLevels:\n{lines}\nAnswer with the single digit of the best level."
                tops, n = self._ask(item, prompt)
                dist, mass = self._dist(tops, [str(i) for i in range(len(q["criteria"]))])
                out[qid] = {"score": sum(int(k) * v for k, v in dist.items()) if dist else None,
                            "probs": dist, "valid_mass": round(mass, 4)}
            tokens += n
        return out, {"model": self.model, "prompt_tokens": tokens, "seconds": round(time.time() - t0, 3)}


BACKENDS = {"jev": Jev, "gemma": Gemma}


def judge_run(run, backend, model=None, limit=None, workers=None):
    judge = BACKENDS[backend](model)
    snap = run / "questions.json"
    if snap.exists() and json.loads(snap.read_text()) != QUESTIONS:
        raise SystemExit(f"{run} was judged with a different question set ({snap}); "
                         "judge a new run directory instead of mixing versions")
    snap.write_text(json.dumps(QUESTIONS, indent=1, ensure_ascii=False))
    items = [json.loads(l) for l in open(run / "items.jsonl")]
    out_path = run / f"judgments-{backend}{'-' + model.replace(':', '_') if model else ''}.jsonl"
    done = set()
    if out_path.exists():
        done = {json.loads(l)["key"] for l in open(out_path) if l.strip()}
    todo = [it for it in items if it["key"] not in done][:limit]
    workers = workers or (8 if backend == "jev" else 1)
    print(f"{backend} ({getattr(judge, 'model', '')}): {len(todo)} to judge, {len(done)} already done -> {out_path.name}")
    errors, t0 = 0, time.time()

    def one(it):
        try:
            answers, meta = judge.judge(it["state"])
            return {"key": it["key"], "backend": backend, "answers": answers, "meta": meta}
        except Exception as e:
            return {"key": it["key"], "backend": backend, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    with open(out_path, "a") as f, cf.ThreadPoolExecutor(workers) as ex:
        for i, rec in enumerate(ex.map(one, todo), 1):
            f.write(json.dumps(rec, ensure_ascii=False) + "\n"); f.flush()
            errors += "error" in rec
            if i % 25 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)} · errors {errors} · {time.time() - t0:.0f}s", flush=True)
    return 1 if errors == len(todo) and todo else 0
