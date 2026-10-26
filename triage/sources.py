"""Rebuild newshound's recent alerts as judge-ready items.

newshound's state DB keeps item IDs, not text, so history is reconstructed from
the feeds themselves: each feed in rss.md is re-read, each entry gets the ID
newshound gives it, and the live DB (opened read-only) says whether it was sent.
Only what newshound would send is kept -- keyword matches, plus everything from
CourtListener, which it trusts unfiltered.

Enrichment, because a feed entry is too thin to judge ("Arena v. Coinbase Global,
Inc. -- Original document"):
  * CourtListener search feeds -> the v4 search API for the same query: court,
    nature of suit, cause, the docket text, the court's own event name, and the
    opening text of the document when RECAP holds it.
  * CourtListener docket feeds -> the entry text the feed already carries, plus
    the docket's metadata from the API.
  * everything else -> source-to-lede/resolve.py's link handlers and fetchers
    (SEC contact User-Agent, PDF paging, bot-wall detection), via webget.

CourtListener API calls go through claude_knowledge's cl_match.cl_get (one shared budget; CLBudget when it is
spent, which the loops stop on) and are cached in derived/courtlistener/cache.sqlite, shared with the morning gather:
documents 30 days (6 hours when RECAP has no text yet), dockets 30 days, search pages 30 minutes.

Deployment note: resolve.py and webget are local to this machine and the
CourtListener token is read from ~/.config/courtlistener/token. A deployable
version needs both vendored and the token in the environment.
"""
import collections
import contextvars
import hashlib
import html
import json
import os
import pathlib
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import feedparser

socket.setdefaulttimeout(30)
HERE = pathlib.Path(__file__).resolve().parent
NH = HERE.parent
RESOLVE_DIR = NH.parent / ".claude" / "skills" / "source-to-lede"
CL_API = "https://www.courtlistener.com/api/rest/v4/"
BARE_CL = "https://www.courtlistener.com"
EXCERPT = 1800          # characters of document/page text shown to the models


# --- newshound's own rules, replicated: newshound.py cannot be imported because
# --- it builds a Slack client (and raises without a token) at import time. -----
def read_lines(name):
    with open(NH / name, encoding="utf-8-sig") as f:
        return [l.strip() for l in f if l.strip()]


def keyword_pattern(keywords):
    alternation = "|".join(re.escape(k) for k in keywords)
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.IGNORECASE)


def entry_text(entry):
    parts = [entry.get("title", ""), entry.get("summary", "")]
    for block in entry.get("content", []) or []:
        parts.append(block.get("value", "") if isinstance(block, dict) else block)
    return " ".join(str(p) for p in parts)


def entry_id(entry):
    if entry.get("id"):
        return str(entry["id"])
    return hashlib.sha256(f"{entry.get('link', '')}{entry.get('title', '')}".encode()).hexdigest()


def processed(db=NH / "last_processed.db"):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = {}
    for feed, item in con.execute("SELECT feed_source, item_id FROM processed_items"):
        out.setdefault(feed, set()).add(item)
    return out


# --- helpers ------------------------------------------------------------------
def strip_html(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    return re.sub(r"\s+", " ", s).strip()


def contact_ua():
    for p in (os.environ.get("FEED_USER_AGENT"), os.environ.get("EDGAR_UA")):
        if p:
            return p
    f = pathlib.Path.home() / ".config" / "edgar" / "ua"
    return f.read_text().strip() if f.exists() else ""


def parse_feed(url):
    ua = contact_ua() if "sec.gov" in url else ""
    return feedparser.parse(url, agent=ua) if ua else feedparser.parse(url)


def feed_label(url):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("q", [""])[0]
    if "courtlistener.com/feed/search" in url:
        return f"CourtListener RECAP search: {q}"
    m = re.search(r"courtlistener\.com/docket/(\d+)/feed", url)
    if m:
        return f"CourtListener docket {m.group(1)}"
    host = urllib.parse.urlparse(url).netloc.replace("www.", "")
    names = {"justice.gov": "DOJ Antitrust Division press releases", "cftc.gov": "CFTC enforcement",
             "occ.treas.gov": "OCC news", "public.govdelivery.com": "FDIC", "federalreserve.gov":
             "Federal Reserve press", "fca.org.uk": "UK FCA news", "consumerfinance.gov": "CFPB newsroom",
             "ecb.europa.eu": "ECB press", "whitehouse.gov": "White House news", "trumpstruth.org":
             "Trump's Truth Social posts (trumpstruth.org)", "ssf.gob.sv": "El Salvador financial regulator",
             "gov.uk": "UK Foreign Office", "feedproxy.feedly.com": "Feedly (custom feed)"}
    if "sec.gov" in url:
        return "SEC litigation (administrative)" if "litigation" in url else "SEC press releases"
    return names.get(host, host)


# --- CourtListener ------------------------------------------------------------
# One client, one budget, one cache (2026-10-08). On 2026-10-08 the account's 300-a-day cap ran out and a gather rerun
# read text for 3 of 30 documents. This module had its own retry loop: four tries, 3/6/9 s apart on a 429, never
# reading Retry-After, never telling other processes. Since then every call goes through claude_knowledge's
# cl_match.cl_get, so CourtListener's Retry-After is recorded (cl_block) and honoured by every process, and the shared
# 9/min, 70/h, 270/day window applies. Answers are kept in derived/courtlistener/cache.sqlite, which the morning
# gather (it imports this module) and the triage both use. A document costs one docket-entries call for the whole
# entry, exhibits included, where it used to cost recap-documents + docket-entries + dockets.
KB = NH.parent                               # claude_knowledge
if str(NH) not in sys.path:
    sys.path.append(str(NH))
import clpace                                # noqa: E402  (newshound/clpace.py: the short-wait rule, CLBudget)

CLBudget = clpace.CLBudget                   # what a caller catches to stop: the budget is spent for now
UA = "newshound-triage (Protos)"


def _ua():
    """UA with Bennett's contact appended when one is configured (tools/edgar/research_ua.py reads it through
    edgar_io.contact_ua and never prints it); UA as before otherwise."""
    try:
        edgar = str(KB / "tools" / "edgar")
        if edgar not in sys.path:
            sys.path.append(edgar)
        import research_ua
        return research_ua.user_agent("newshound-triage", fallback=UA)
    except Exception:                                  # noqa: BLE001 -- the header must never break a request
        return UA
# Module switches, read at call time, never bound as default arguments (memory: default-arg-binds-constant), so
# a check can point them at temporary files.
CACHE_DB = KB / "derived" / "courtlistener" / "cache.sqlite"
GRAPH_DB = KB / "derived" / "graph.sqlite"
DOC_TTL = 30 * 86400                         # a document RECAP holds text for: the text does not change
DOC_NOTEXT_TTL = 6 * 3600                    # no text yet: RECAP's OCR may land later in the day
DOCKET_TTL = 30 * 86400                      # case name, court, number, nature of suit, cause
SEARCH_TTL = 30 * 60                         # a search page: new filings arrive all day
# Every API call, cache hit and graph hit this process made; the gather reads the deltas to say what a run cost.
CL_STATS = collections.Counter()
ENTRY_FIELDS = ("entry_number,date_filed,description,recap_documents__id,recap_documents__document_number,"
                "recap_documents__attachment_number,recap_documents__description,"
                "recap_documents__short_description,recap_documents__page_count,recap_documents__is_available,"
                "recap_documents__plain_text,recap_documents__absolute_url")
# docket-entries' serializer is a NestedDynamicFieldsMixin (cl/api/utils.py, freelawproject/courtlistener): a nested
# field is selected as parent__child, split on "__" by get_fields_for_level_and_prefix (read in the source, not by
# calling the API).


def cl_token():
    t = os.environ.get("COURTLISTENER_TOKEN")
    f = pathlib.Path.home() / ".config" / "courtlistener" / "token"
    return t or (f.read_text().strip() if f.exists() else "")


def _cl_match():
    return clpace._cl_match()


_UA_ON = contextvars.ContextVar("newshound_triage_ua", default=False)


class _UAHandler(urllib.request.BaseHandler):
    """cl_match.cl_get builds its request with claude_knowledge's own User-Agent; a request made from here is
    re-labelled newshound-triage, so CourtListener can tell the two apart. Acts only while sources.cl_get is
    calling (a context variable), and only on courtlistener.com."""
    handler_order = 900

    def http_request(self, req):
        if _UA_ON.get() and (req.host or "").lower().endswith("courtlistener.com"):
            req.add_header("User-Agent", _ua())
        return req

    https_request = http_request


def _ensure_ua_handler():
    """Add _UAHandler to the opener urllib.request.urlopen uses (installing the default one first if none is),
    keeping any opener another module installed."""
    opener = urllib.request._opener
    if opener is None:
        opener = urllib.request.build_opener()
        urllib.request.install_opener(opener)
    if not any(type(h).__name__ == "_UAHandler" for h in opener.handlers):
        opener.add_handler(_UAHandler())


def cl_get(path, **params):
    """One CourtListener API GET, through cl_match.cl_get: the shared pace window and every process's record of
    CourtListener's Retry-After. A slot further off than clpace.MAX_WAIT seconds (two minutes; a launchd run or the
    morning gather never stalls for an hour), a long 429 or a throttle body raises CLBudget; other errors raise
    as they are."""
    cl = _cl_match()
    saved = cl.PACE_MAX_WAIT
    cl.PACE_MAX_WAIT = min(saved, clpace.MAX_WAIT)
    _ensure_ua_handler()
    tok = _UA_ON.set(True)
    try:
        out = cl.cl_get(CL_API + path, params, cl_token())
    except (cl.PaceExhausted, clpace.PaceWait) as e:
        CL_STATS["budget"] += 1
        raise CLBudget(str(e)) from e
    except RuntimeError as e:
        if str(e).startswith("throttled"):              # cl_get's throttle body in a 200
            CL_STATS["budget"] += 1
            raise CLBudget(str(e)) from e
        CL_STATS["api"] += 1
        raise
    except Exception:
        CL_STATS["api"] += 1
        raise
    finally:
        _UA_ON.reset(tok)
        cl.PACE_MAX_WAIT = saved
    CL_STATS["api"] += 1
    return out


# --- the shared cache -----------------------------------------------------------
def _cache():
    path = pathlib.Path(CACHE_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30)
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass                                  # another process holds it mid-switch; WAL is set once and persists
    con.executescript("""
        CREATE TABLE IF NOT EXISTS documents (docket_id TEXT NOT NULL, entry_number INTEGER NOT NULL,
            attachment_number INTEGER NOT NULL, payload TEXT NOT NULL, has_text INTEGER NOT NULL,
            fetched_at REAL NOT NULL, PRIMARY KEY (docket_id, entry_number, attachment_number));
        CREATE TABLE IF NOT EXISTS dockets (docket_id TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS searches (key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL NOT NULL);
    """)
    return con


def _cache_read(sql, args):
    """One row or None; a cache that cannot be read is a miss, never an error."""
    try:
        con = _cache()
        try:
            return con.execute(sql, args).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _cache_write(sql, rows):
    try:
        con = _cache()
        try:
            with con:
                con.executemany(sql, rows)
        finally:
            con.close()
    except sqlite3.Error:
        pass                                  # the answer is still returned; it just costs a call next time


def _att(n):
    """The cache's attachment key: 0 for the main document (CourtListener's attachment_number None)."""
    try:
        return int(n or 0)
    except (TypeError, ValueError):
        return 0


def _doc_cached(docket, num, att):
    row = _cache_read("SELECT payload, has_text, fetched_at FROM documents WHERE docket_id=? AND entry_number=? "
                      "AND attachment_number=?", (str(docket), int(num), _att(att)))
    if not row:
        return None
    ttl = DOC_TTL if row[1] else DOC_NOTEXT_TTL
    return json.loads(row[0]) if time.time() - row[2] < ttl else None


def _doc_store(docket, num, records):
    """records: {attachment_number or None: {"filing": ..., "text_excerpt"?: ...}}."""
    now = time.time()
    _cache_write("INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?)",
                 [(str(docket), int(num), _att(att), json.dumps(rec), 1 if rec.get("text_excerpt") else 0, now)
                  for att, rec in records.items()])


def _excerpt(text):
    return re.sub(r"\s+", " ", text or "").strip()[:EXCERPT]


def _record(entry, num, att, doc):
    """What cl_document returns for one document, less the case: the filing and the opening of its text."""
    rec = {"filing": {k: v for k, v in {
        "entry_date": entry.get("date_filed"), "document_number": num, "attachment_number": att,
        "event": doc.get("short_description") or None,
        "description": strip_html(entry.get("description") or doc.get("description"))[:1200] or None,
    }.items() if v is not None}}
    text = _excerpt(doc.get("plain_text"))
    if text:
        rec["text_excerpt"] = text
    return rec


def _nested_ok(entry):
    docs = entry.get("recap_documents") if entry else None
    return isinstance(docs, list) and all(isinstance(d, dict) and "plain_text" in d for d in docs)


def _fetch_entry(docket, num, want):
    """ONE docket-entries call for the entry, its documents nested; every attachment it returns is cached under
    its own key, so an entry's exhibits (USA Today v. OpenAI's 1, 1/1, 1/2) cost one call between them. When the
    response lacks the nested documents (the field selection not honoured, or no entry came back), the old
    recap-documents call answers for this document only."""
    res = cl_get("docket-entries/", docket=docket, entry_number=num, fields=ENTRY_FIELDS).get("results", [])
    entry = next((e for e in res if _nested_ok(e) and e.get("recap_documents")), None) or \
        next((e for e in res if _nested_ok(e)), None)
    if entry is None:
        CL_STATS["fallback"] += 1
        docs = cl_get("recap-documents/", docket_entry__docket__id=docket, document_number=num,
                      fields="description,short_description,attachment_number,page_count,"
                             "is_available,plain_text,absolute_url").get("results", [])
        entry = next(iter(res), None) or {}
        entry = {"date_filed": entry.get("date_filed"), "description": entry.get("description")}
    else:
        docs = entry["recap_documents"]
    records = {}
    for d in docs:
        att = d.get("attachment_number") or None
        records.setdefault(att, _record(entry, num, att, d))
    if want not in records:
        records[want] = _record(entry, num, want, {})         # RECAP has no such document yet: no text, short TTL
    _doc_store(docket, num, records)
    return records[want]


def cl_search_docs(query, pages=2):
    """The search feed's documents, from the API, newest entry first. A page is cached SEARCH_TTL seconds under
    its exact parameters, so the gather and a triage run in the same half hour share it."""
    docs, cursor = [], None
    for _ in range(pages):
        params = {"type": "r", "q": query, "order_by": "entry_date_filed desc"}
        if cursor:
            params["cursor"] = cursor
        key = "search/?" + urllib.parse.urlencode(sorted(params.items()))
        row = _cache_read("SELECT payload, fetched_at FROM searches WHERE key=?", (key,))
        if row and time.time() - row[1] < SEARCH_TTL:
            d = json.loads(row[0])
            CL_STATS["search_cache"] += 1
        else:
            d = cl_get("search/", **params)
            if isinstance(d, dict) and isinstance(d.get("results"), list):
                _cache_write("INSERT OR REPLACE INTO searches VALUES (?,?,?)", [(key, json.dumps(d), time.time())])
        for r in d.get("results", []):
            case = {"name": r.get("caseName"), "court": r.get("court_citation_string") or r.get("court_id"),
                    "docket_number": r.get("docketNumber"), "nature_of_suit": r.get("suitNature") or None,
                    "cause": r.get("cause") or None, "filed": r.get("dateFiled")}
            for doc in r.get("recap_documents") or []:
                docs.append({"case": case, "docket_id": r.get("docket_id"),
                             "url": doc.get("absolute_url") or "",
                             "entry_date": doc.get("entry_date_filed"),
                             "document_number": doc.get("document_number"),
                             "attachment_number": doc.get("attachment_number"),
                             "event": doc.get("short_description") or None,
                             "description": strip_html(doc.get("description"))[:1200] or None,
                             "text": strip_html(doc.get("snippet"))[:EXCERPT] or None,
                             "page_count": doc.get("page_count"), "available": doc.get("is_available")})
        nxt = d.get("next")
        cursor = urllib.parse.parse_qs(urllib.parse.urlparse(nxt).query).get("cursor", [None])[0] if nxt else None
        if not cursor:
            break
    docs.sort(key=lambda x: x["entry_date"] or "", reverse=True)
    return docs


_docket_cache = {}       # this process's answers (from any of the three places below)
_docket_failed = {}      # this process's non-budget failures: not asked again this run, never written to the cache


def _graph_docket(docket_id):
    """The docket from claude_knowledge's graph (cl_dockets, banked by the nightly CourtListener stages), or None.
    The graph keeps no nature of suit or cause; those stay None."""
    try:
        con = sqlite3.connect(f"file:{urllib.parse.quote(str(GRAPH_DB))}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return None
    try:
        row = con.execute("SELECT case_name, court, docket_number, date_filed FROM cl_dockets WHERE docket_id=?",
                          (int(docket_id),)).fetchone()
    except (sqlite3.Error, ValueError):
        return None
    finally:
        con.close()
    if not row or not row[0]:
        return None
    return {"name": row[0], "court": row[1], "docket_number": row[2], "nature_of_suit": None, "cause": None,
            "filed": row[3]}


def cl_docket(docket_id):
    """Case name, court, docket number, date filed, nature of suit and cause: from the graph, else the cache, else
    one API call (then cached DOCKET_TTL). CLBudget propagates; any other failure returns {"error": ...}, kept for
    this process only."""
    key = str(docket_id)
    if key in _docket_cache:
        return _docket_cache[key]
    if key in _docket_failed:
        return _docket_failed[key]
    row = _cache_read("SELECT payload, fetched_at FROM dockets WHERE docket_id=?", (key,))
    cached = json.loads(row[0]) if row and time.time() - row[1] < DOCKET_TTL else None
    case = _graph_docket(key)
    if case:
        CL_STATS["docket_graph"] += 1
        for k in ("nature_of_suit", "cause"):         # the graph has none; a cached API answer may
            if cached and cached.get(k):
                case[k] = cached[k]
    elif cached:
        CL_STATS["docket_cache"] += 1
        case = cached
    else:
        try:
            # the whole record (about 2 KB), no `fields`: the one request every caller makes for a docket by id
            # (cl_match.docket), so cl_get's response cache answers it for kbpy and the nightly alike, and theirs for us
            d = cl_get(f"dockets/{key}/")
        except CLBudget:
            raise
        except Exception as e:                        # noqa: BLE001
            _docket_failed[key] = {"error": type(e).__name__}
            return _docket_failed[key]
        case = {"name": d.get("case_name"), "court": d.get("court_id"), "docket_number": d.get("docket_number"),
                "nature_of_suit": d.get("nature_of_suit") or None, "cause": d.get("cause") or None,
                "filed": d.get("date_filed")}
        _cache_write("INSERT OR REPLACE INTO dockets VALUES (?,?,?)", [(key, json.dumps(case), time.time())])
    _docket_cache[key] = case
    return case


def cl_document(link, case=None):
    """Docket text + opening text for one RECAP document URL, when search didn't carry it.

    The search API nests only a few documents under each docket, so a busy docket
    (Sun v. World Liberty Financial: 12 new documents in the feed) leaves most of
    its feed entries unmatched. docket-entries answers per entry, every document of it at once.

    `case`: the case the caller already holds (the gather's search rows carry it from the search API); then no
    docket lookup is made. CLBudget propagates, so a caller's loop stops on it; any other failure returns None.
    """
    m = re.search(r"/docket/(\d+)/(\d+)/(?:(\d+)/)?", link or "")
    if not m:
        return None
    docket, num, att = m.group(1), int(m.group(2)), m.group(3)
    want = int(att) if att else None
    got = _doc_cached(docket, num, want)
    if got is not None:
        CL_STATS["doc_cache"] += 1
    else:
        try:
            got = _fetch_entry(docket, num, want)
        except CLBudget:
            raise
        except Exception:                             # noqa: BLE001
            return None
    out = {"case": case if case else cl_docket(docket), "filing": got["filing"]}
    if got.get("text_excerpt"):
        out["text_excerpt"] = got["text_excerpt"]
    return out


# --- everything else: resolve.py -------------------------------------------------
def page_text(url):
    """(text, how) for a non-CourtListener link, via resolve.py's handlers."""
    sys.path.insert(0, str(RESOLVE_DIR))
    import resolve
    man = {}
    for _name, pat, fn in resolve.HANDLERS:
        m = pat.search(url)
        if m:
            man = fn(m, url) or {}
            break
    if man.get("needs"):
        return None, f"resolve:{man.get('kind')} needs an authenticated step"
    target = man.get("fetch_url") or url
    try:
        if target.lower().endswith(".pdf") or man.get("kind") in ("recap-pdf",):
            data = resolve.fetch(target)
            if data[:4] == b"%PDF":
                return re.sub(r"\s+", " ", " ".join(resolve.pdf_pages(data)[:2]))[:EXCERPT], "resolve:pdf"
        if resolve._is_sec(target):
            text = strip_html(resolve.fetch(target).decode("utf-8", "replace"))
        else:
            out = subprocess.run([resolve.WEBGET, target], capture_output=True, text=True, timeout=120)
            text = re.sub(r"\s+", " ", out.stdout)
        if len(text) < 200 or resolve._is_wall(text):
            return None, "resolve:empty-or-wall"
        return text[:EXCERPT], f"resolve:{man.get('kind', 'generic')}"
    except Exception as e:
        return None, f"resolve:error {type(e).__name__}"


# --- collection -------------------------------------------------------------------
def collect(log=print):
    seen = processed()
    kws = read_lines("keywords.md")
    pat = keyword_pattern(kws)
    items = []
    budget = []          # set once the CourtListener budget is spent: no further API call this run

    def api(fn, *a, **k):
        """fn(*a, **k), or None once the budget is spent (said once; the items stay, thinner)."""
        if budget:
            return None
        try:
            return fn(*a, **k)
        except CLBudget as e:
            budget.append(str(e))
            log(f"  ! CourtListener budget spent, no further API calls this run: {e}")
            return None
    for url in read_lines("rss.md"):
        label = feed_label(url)
        try:
            feed = parse_feed(url)
        except Exception as e:
            log(f"  ! {label}: {type(e).__name__}"); continue
        done = seen.get(url, set())
        is_cl = "courtlistener.com" in url
        docs, used = [], set()
        if "courtlistener.com/feed/search" in url:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("q", [""])[0]
            try:
                docs = api(cl_search_docs, q) or []
            except Exception as e:
                log(f"  ! CourtListener API for {q!r}: {type(e).__name__}")
        kept = 0
        for e in feed.entries:
            text = entry_text(e)
            hits = sorted({m.group(0).lower() for m in pat.finditer(text)})
            if not is_cl and not hits:
                continue                                  # newshound never sends it
            iid = entry_id(e)
            link = e.get("link", "")
            bare = is_cl and iid in (BARE_CL, BARE_CL + "/")
            group = "id_collision" if bare else ("alerted" if iid in done else "pending")
            published = (e.get("published") or e.get("updated") or "")[:10]
            state = {"source": label, "headline": e.get("title", ""), "date": published}
            how = "rss-only"
            if "courtlistener.com/feed/search" in url:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("q", [""])[0]
                state["watch_term"] = q
                path = urllib.parse.urlparse(link).path
                doc = None
                if not bare:
                    doc = next((d for i, d in enumerate(docs) if i not in used and d["url"] and
                                d["url"].rstrip("/") == path.rstrip("/")), None)
                else:
                    doc = next((d for i, d in enumerate(docs) if i not in used and not d["url"] and
                                d["case"]["name"] == e.get("title") and d["entry_date"] == published), None)
                if doc:
                    used.add(docs.index(doc))
                    state["case"] = doc["case"]
                    state["filing"] = {k: doc[k] for k in ("entry_date", "document_number", "attachment_number",
                                                          "event", "description") if doc.get(k) is not None}
                    if doc.get("text"):
                        state["text_excerpt"] = doc["text"]
                    how = "courtlistener-search-api"
                else:
                    got = None if bare else api(cl_document, link)
                    if got:
                        state.update(got)
                        how = "courtlistener-document-api"
                    else:
                        snippet = strip_html(e.get("summary", "")).replace("Original document", "").strip()
                        if snippet:
                            state["text_excerpt"] = snippet[:EXCERPT]
            elif is_cl:
                m = re.search(r"/docket/(\d+)/", url)
                case = (api(cl_docket, m.group(1)) or {}) if m else {}
                state["watch_term"] = case.get("name") or e.get("title", "")
                if case and "error" not in case:
                    state["case"] = case
                    how = "courtlistener-docket-api"
                state["filing"] = {"description": strip_html(e.get("summary", ""))[:EXCERPT]}
            else:
                state["watch_term"] = ", ".join(hits)
                summary = strip_html(e.get("summary", ""))
                if summary:
                    state["summary"] = summary[:600]
                if link:
                    txt, how = page_text(link)
                    if txt:
                        state["text_excerpt"] = txt
            state["link"] = link
            items.append({"key": hashlib.sha1(f"{url}\x00{iid}\x00{e.get('title','')}\x00{published}".encode()).hexdigest()[:12],
                          "feed": url, "feed_label": label, "id": iid, "group": group,
                          "title": e.get("title", ""), "link": link, "published": published,
                          "enriched": how, "state": state})
            kept += 1
        log(f"  {label[:60]:60s} entries {len(feed.entries):3d} · kept {kept:3d}")
    return items


def fill_from_twins(items, log=print):
    """Docket text for search hits whose entry also arrived through a docket feed.

    A docket feed carries each entry's docket text; a search hit for the same entry
    (/docket/<id>/<n>/) may have none when the API is unavailable. Same court
    record, so borrow it -- no API call.
    """
    twins = {}
    for it in items:
        m = re.search(r"/docket/(\d+)/[^#]*#entry-(\d+)", it["link"] or "")
        if m and it["state"].get("filing", {}).get("description"):
            twins[(m.group(1), m.group(2))] = it["state"]
    filled = 0
    for it in items:
        m = re.search(r"/docket/(\d+)/(\d+)/", it["link"] or "")
        if it["enriched"] == "rss-only" and m and (m.group(1), m.group(2)) in twins:
            twin = twins[(m.group(1), m.group(2))]
            it["state"]["filing"] = dict(twin["filing"])
            if twin.get("case"):
                it["state"]["case"] = twin["case"]
            it["enriched"] = "docket-feed-twin"; filled += 1
    log(f"filled {filled} item(s) from their docket-feed twins")
    return items


def cap_state(items, limit=1200):
    """One docket description ran to 25,000 characters (collected before the cap)."""
    for it in items:
        f = it["state"].get("filing") or {}
        if f.get("description") and len(f["description"]) > limit:
            f["description"] = f["description"][:limit]
    return items


def reenrich(items, log=print):
    """Fill gaps in an existing item set without re-reading the feeds.

    The item set is the alert HISTORY, pinned to the feed list as it was when it was
    collected; re-reading rss.md after an edit would silently swap in different feeds.
    """
    fixed, budget = 0, None
    for it in items:
        st = it["state"]
        if it["enriched"] == "rss-only" and "/docket/" in (it["link"] or "") and not budget:
            try:
                got = cl_document(it["link"])
            except CLBudget as e:
                budget = e
                log(f"  ! CourtListener budget spent, no further documents this run: {e}")
                continue
            if got:
                st.update(got); it["enriched"] = "courtlistener-document-api"; fixed += 1
        elif it["enriched"].startswith("resolve:") and not st.get("text_excerpt") and it["link"]:
            txt, how = page_text(it["link"])
            if txt:
                st["text_excerpt"] = txt; it["enriched"] = how; fixed += 1
    log(f"re-enriched {fixed} item(s)")
    return items


def dedupe_documents(items, log=print):
    """One item per DOCUMENT, as newshound now sends it (cross-feed dedupe).

    The same filing reached through q=usdt, q=ethereum and q=coinbase was judged
    three times with three different `watch_term`s -- and scored 3.2, 3.1 and 1.5.
    Keep the richest copy and give it every term that matched.
    """
    sys.path.insert(0, str(NH))
    from newshound import canonical_key
    rank = {"rss-only": 0}
    groups = {}
    for it in items:
        groups.setdefault(canonical_key(it["id"], it["link"]), []).append(it)
    out = []
    for _key, group in groups.items():
        best = max(group, key=lambda i: (rank.get(i["enriched"], 1), len(json.dumps(i["state"]))))
        terms = sorted({t.strip() for i in group for t in (i["state"].get("watch_term") or "").split(",") if t.strip()})
        best = json.loads(json.dumps(best))
        best["state"]["watch_term"] = ", ".join(terms)
        best["feeds"] = sorted({i["feed_label"] for i in group})
        out.append(best)
    log(f"{len(items)} items -> {len(out)} documents")
    return out
