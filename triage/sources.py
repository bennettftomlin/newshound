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

Deployment note: resolve.py and webget are local to this machine and the
CourtListener token is read from ~/.config/courtlistener/token. A deployable
version needs both vendored and the token in the environment.
"""
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
def cl_token():
    t = os.environ.get("COURTLISTENER_TOKEN")
    f = pathlib.Path.home() / ".config" / "courtlistener" / "token"
    return t or (f.read_text().strip() if f.exists() else "")


def _pace():
    """claude_knowledge's shared CourtListener budget (newshound/clpace.py): every API attempt reserves a slot."""
    parent = str(pathlib.Path(__file__).resolve().parent.parent)
    if parent not in sys.path:
        sys.path.append(parent)
    import clpace
    clpace.pace()


def cl_get(path, **params):
    url = CL_API + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Token {cl_token()}",
                                               "User-Agent": "newshound-triage (Protos)"})
    for attempt in range(4):
        _pace()                               # each retry too: a 429's retry is a request like any other
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(3 * (attempt + 1)); continue
            raise
        except (urllib.error.URLError, TimeoutError, socket.timeout):
            if attempt < 3:
                time.sleep(3 * (attempt + 1)); continue
            raise


def cl_search_docs(query, pages=2):
    """The search feed's documents, from the API, newest entry first."""
    docs, cursor = [], None
    for _ in range(pages):
        params = {"type": "r", "q": query, "order_by": "entry_date_filed desc"}
        if cursor:
            params["cursor"] = cursor
        d = cl_get("search/", **params)
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


_docket_cache = {}


def cl_docket(docket_id):
    if docket_id not in _docket_cache:
        try:
            d = cl_get(f"dockets/{docket_id}/", fields="case_name,court_id,docket_number,date_filed,"
                                                       "nature_of_suit,cause")
            _docket_cache[docket_id] = {"name": d.get("case_name"), "court": d.get("court_id"),
                                        "docket_number": d.get("docket_number"),
                                        "nature_of_suit": d.get("nature_of_suit") or None,
                                        "cause": d.get("cause") or None, "filed": d.get("date_filed")}
        except Exception as e:
            _docket_cache[docket_id] = {"error": f"{type(e).__name__}"}
    return _docket_cache[docket_id]


def cl_document(link):
    """Docket text + opening text for one RECAP document URL, when search didn't carry it.

    The search API nests only a few documents under each docket, so a busy docket
    (Sun v. World Liberty Financial: 12 new documents in the feed) leaves most of
    its feed entries unmatched. recap-documents and docket-entries answer per document.
    """
    m = re.search(r"/docket/(\d+)/(\d+)/(?:(\d+)/)?", link or "")
    if not m:
        return None
    docket, num, att = m.group(1), int(m.group(2)), m.group(3)
    try:
        docs = cl_get("recap-documents/", docket_entry__docket__id=docket, document_number=num,
                      fields="description,short_description,attachment_number,page_count,"
                             "is_available,plain_text,absolute_url").get("results", [])
        entry = cl_get("docket-entries/", docket=docket, entry_number=num,
                       fields="description,date_filed").get("results", [])
    except Exception:
        return None
    want = int(att) if att else None
    doc = next((d for d in docs if (d.get("attachment_number") or None) == want), docs[0] if docs else {})
    e = entry[0] if entry else {}
    out = {"case": cl_docket(docket),
           "filing": {k: v for k, v in {
               "entry_date": e.get("date_filed"), "document_number": num,
               "attachment_number": want, "event": doc.get("short_description") or None,
               "description": strip_html(e.get("description") or doc.get("description"))[:1200] or None,
           }.items() if v is not None}}
    text = re.sub(r"\s+", " ", doc.get("plain_text") or "").strip()
    if text:
        out["text_excerpt"] = text[:EXCERPT]
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
                docs = cl_search_docs(q)
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
                    got = None if bare else cl_document(link)
                    if got:
                        state.update(got)
                        how = "courtlistener-document-api"
                    else:
                        snippet = strip_html(e.get("summary", "")).replace("Original document", "").strip()
                        if snippet:
                            state["text_excerpt"] = snippet[:EXCERPT]
            elif is_cl:
                m = re.search(r"/docket/(\d+)/", url)
                case = cl_docket(m.group(1)) if m else {}
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
    fixed = 0
    for it in items:
        st = it["state"]
        if it["enriched"] == "rss-only" and "/docket/" in (it["link"] or ""):
            got = cl_document(it["link"])
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
