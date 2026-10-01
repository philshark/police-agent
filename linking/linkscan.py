#!/usr/bin/env python3
"""heypolo internal-link scanner. Stdlib only, fetches pages with curl.

Run weekly by GitHub Actions. It never calls an AI model, so it costs nothing.
What it does:
  1. Reads https://heypolo.com/sitemap.xml.
  2. Fetches every /blog/ article plus every page we may link to
     (/blog/, /features, /for, /comparisons).
  3. Records which internal links sit inside each article's body
     (header, nav, footer, share buttons and the like are ignored).
  4. Works out which articles are new or changed since the last run.
  5. For those articles, ranks the pages they do NOT link to by plain text
     similarity and keeps only the best few. Claude scores that short list later.
  6. Writes linking/data/pages.json (the cache) and linking/data/pending.json
     (the short list the Monday routine reads).
"""
import argparse
import datetime
import difflib
import hashlib
import html
import json
import math
import os
import re
import subprocess
import sys
import time
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

SITE_HOST = "heypolo.com"
SITEMAP_URL = "https://heypolo.com/sitemap.xml"
UA = "Mozilla/5.0 (compatible; heypolo-linkscan/1.0)"
TARGET_SEGMENTS = {"blog", "features", "for", "comparisons"}

DELAY_SECONDS = 1.0          # pause between real page fetches
TIMEOUT = "40"
RETRIES = 2
MAX_FAIL_SHARE = 0.25        # abort if more than this share of fetches fail

EXCERPT_WORDS_BLOG = 1500    # stored in the cache, used for change detection
EXCERPT_WORDS_OTHER = 150    # non-blog link targets only need a short summary
SCORING_EXCERPT_WORDS = 350  # what Claude reads for each article being scored
FORWARD_CANDIDATES = 15      # pages an article could link out to, per article
REVERSE_CANDIDATES = 10      # older articles that could link to a new article
MAX_CHANGED_PER_RUN = 10     # guard against dynamic page content
CHANGE_RATIO = 0.85          # text similarity below this counts as "changed"
DEFAULT_BACKFILL_LIMIT = 15

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
FIXTURES = os.environ.get("LINKSCAN_FIXTURES")  # test mode: read pages from disk


def sha(s):
    return hashlib.sha256((s or "").encode("utf-8", "replace")).hexdigest()


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch(url):
    """Return (body or None, error or None)."""
    if FIXTURES:
        name = "sitemap.xml" if url == SITEMAP_URL else hashlib.sha1(url.encode()).hexdigest() + ".html"
        path = os.path.join(FIXTURES, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read(), None
        return None, "fixture missing"
    last_err = None
    for _ in range(RETRIES + 1):
        try:
            p = subprocess.run(
                ["curl", "-sS", "-L", "--compressed", "-A", UA, "--max-time", TIMEOUT,
                 "-w", "\n__STATUS__%{http_code}", url],
                capture_output=True, timeout=90)
        except subprocess.TimeoutExpired:
            last_err = "curl timeout"
            continue
        out = (p.stdout or b"").decode("utf-8", "replace")
        m = re.search(r"\n__STATUS__(\d+)\s*\Z", out)
        status = int(m.group(1)) if m else 0
        body = out[:m.start()] if m else out
        if status == 200 and body.strip():
            return body, None
        last_err = f"HTTP {status}" if status else "curl error"
    return None, last_err


def sitemap_urls(url, seen=None):
    seen = seen or set()
    body, err = fetch(url)
    if body is None:
        raise RuntimeError(f"cannot fetch sitemap {url}: {err}")
    locs = [html.unescape(x.strip()) for x in re.findall(r"<loc>\s*(.*?)\s*</loc>", body, re.I | re.S)]
    if "<sitemapindex" in body.lower():
        out = []
        for sm in locs:
            if sm not in seen:
                seen.add(sm)
                out.extend(sitemap_urls(sm, seen))
        return out
    return locs


# --------------------------------------------------------------------------
# URL handling
# --------------------------------------------------------------------------

def norm(u, base=None):
    """Return a lowercase canonical key for an internal URL, or None for anything else."""
    if not u:
        return None
    u = html.unescape(u.strip())
    if base:
        u = urljoin(base, u)
    p = urlsplit(u)
    if p.scheme not in ("http", "https"):
        return None
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host != SITE_HOST:
        return None
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return f"https://{host}{path}".lower()


def segment(key):
    parts = urlsplit(key).path.split("/")
    return parts[1] if len(parts) > 1 else ""


# --------------------------------------------------------------------------
# HTML parsing (a tiny DOM built on the standard library)
# --------------------------------------------------------------------------

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}
BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "li", "ul", "ol",
         "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr", "nav", "figure",
         "figcaption", "blockquote", "pre", "dd", "dt"}
DROP = {"script", "style", "noscript", "svg", "template", "iframe", "head"}
BOILER_TAGS = {"nav", "header", "footer", "aside", "form", "button", "dialog"}
BOILER_ROLES = {"navigation", "complementary", "contentinfo", "banner"}
BOILER_RE = re.compile(
    r"(?:^|[\s_-])(?:related|recommended|sidebar|share|sharing|social|breadcrumbs?|toc|"
    r"table-of-contents|newsletter|subscribe|comments?|author-box|more-posts|read-next|"
    r"cookie|navbar|menu)(?:$|[\s_-])", re.I)


class Node:
    __slots__ = ("tag", "attrs", "kids", "parent")

    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.kids, self.parent = tag, attrs, [], parent


class TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.cur)
        self.cur.kids.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.kids.append(Node(tag, {k: (v or "") for k, v in attrs}, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.kids.append(data)


def parse(markup):
    tb = TreeBuilder()
    tb.feed(markup)
    tb.close()
    return tb.root


def walk(node):
    yield node
    for k in node.kids:
        if isinstance(k, Node):
            yield from walk(k)


def is_boiler(node):
    if node.tag in BOILER_TAGS:
        return True
    if node.attrs.get("role", "").lower() in BOILER_ROLES:
        return True
    return bool(BOILER_RE.search(node.attrs.get("class", "") + " " + node.attrs.get("id", "")))


def collect(node, base):
    """Return (text, links) for a subtree, skipping boilerplate blocks.
    links is a list of (internal_url_key, anchor_text)."""
    pieces, links = [], []

    def text_only(n):
        out = []
        for k in n.kids:
            if isinstance(k, str):
                out.append(k)
            elif k.tag not in DROP:
                out.append(text_only(k))
        return "".join(out)

    def rec(n):
        for k in n.kids:
            if isinstance(k, str):
                pieces.append(k)
                continue
            if k.tag in DROP or is_boiler(k):
                continue
            if k.tag == "a":
                key = norm(k.attrs.get("href", ""), base)
                if key:
                    links.append((key, re.sub(r"\s+", " ", text_only(k)).strip()))
            blk = k.tag in BLOCK
            if blk:
                pieces.append("\n")
            rec(k)
            if blk:
                pieces.append("\n")

    rec(node)
    text = "".join(pieces)
    text = "\n".join(re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines())
    text = "\n".join(ln for ln in text.splitlines() if ln)
    return text, links


def clean(s):
    return html.unescape(re.sub(r"\s+", " ", s or "").strip())


def extract_page(markup, url):
    root = parse(markup)
    title = meta = ""
    h1s = []
    for n in walk(root):
        if n.tag == "title" and not title:
            title = clean("".join(k for k in n.kids if isinstance(k, str)))
        elif n.tag == "meta" and n.attrs.get("name", "").lower() == "description" and not meta:
            meta = clean(n.attrs.get("content", ""))
        elif n.tag == "h1":
            h1s.append(clean(collect(n, url)[0]))
    h1s = [h for h in h1s if h]

    best, label = None, "body"
    for tag in ("article", "main"):
        found = [n for n in walk(root) if n.tag == tag and not is_boiler(n)]
        if found:
            scored = [(len(collect(n, url)[0].split()), n) for n in found]
            words, node = max(scored, key=lambda t: t[0])
            if words >= 150:
                best, label = node, tag
                break
    if best is None:
        bodies = [n for n in walk(root) if n.tag == "body"]
        best = bodies[0] if bodies else root
    text, links = collect(best, url)
    return {"title": title, "h1": h1s[0] if h1s else "", "meta": meta,
            "text": text, "links": links, "container": label}


# --------------------------------------------------------------------------
# Text similarity (plain TF-IDF, no AI)
# --------------------------------------------------------------------------

STOP = set("""a about above after again all also am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had has have
having he her here hers him his how i if in into is it its just me more most my no nor not now of off on
once only or other our out over own same she should so some such than that the their them then there these
they this those through to too under until up us very was we were what when where which while who whom why
will with would you your yours get got make makes using use used one way ways new best guide need needs""".split())


def tokens(text):
    return [t for t in re.findall(r"[a-z0-9']+", (text or "").lower()) if len(t) >= 3 and t not in STOP]


def build_vectors(docs):
    """docs: {key: text}. Returns {key: {term: weight}} with unit length."""
    toks = {k: Counter(tokens(v)) for k, v in docs.items()}
    n = len(docs)
    df = Counter()
    for c in toks.values():
        df.update(c.keys())
    vecs = {}
    for k, c in toks.items():
        v = {t: (1 + math.log(f)) * (math.log((n + 1) / (df[t] + 1)) + 1) for t, f in c.items()}
        norm_ = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs[k] = {t: x / norm_ for t, x in v.items()}
    return vecs


def cosine(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(w * b.get(t, 0.0) for t, w in a.items())


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


def words_of(text):
    return re.findall(r"\S+", text or "")


def summary_of(rec, n=35):
    return rec["meta"] or " ".join(words_of(rec["excerpt"])[:n])


def changed(old, cur):
    if old.get("title") != cur["title"] or old.get("h1") != cur["h1"]:
        return 0.0
    if old.get("text_hash") == cur["text_hash"]:
        return None
    ow, nw = old.get("words", 0), cur["words"]
    if abs(nw - ow) / max(ow, 1) > 0.15:
        return 0.0
    ratio = difflib.SequenceMatcher(None, words_of(old.get("excerpt", "")),
                                    words_of(cur["excerpt"]), autojunk=False).ratio()
    return ratio if ratio < CHANGE_RATIO else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto", choices=["auto", "weekly", "backfill"])
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--limit", type=int, default=DEFAULT_BACKFILL_LIMIT)
    ap.add_argument("--data-dir", default=DATA_DIR)
    args = ap.parse_args()

    pages_path = os.path.join(args.data_dir, "pages.json")
    pending_path = os.path.join(args.data_dir, "pending.json")
    prev = load_json(pages_path, {}).get("pages", {})

    # 1. Sitemap
    all_urls = sitemap_urls(SITEMAP_URL)
    originals, order = {}, []
    for u in all_urls:
        k = norm(u)
        if k and segment(k) in TARGET_SEGMENTS and k not in originals:
            originals[k] = u
            order.append(k)
    blog_keys = [k for k in order if urlsplit(k).path.startswith("/blog/")]
    sitemap_keys = {norm(u) for u in all_urls if norm(u)}
    if not blog_keys:
        print("No /blog/ URLs found in the sitemap. Aborting.", file=sys.stderr)
        return 1

    # 2. Fetch and extract
    pages, failures, extracts = {}, [], {}
    for i, k in enumerate(order):
        if i and not FIXTURES:
            time.sleep(DELAY_SECONDS)
        markup, err = fetch(originals[k])
        if markup is None:
            failures.append({"url": originals[k], "error": err})
            if k in prev:
                pages[k] = prev[k]
            continue
        ex = extract_page(markup, originals[k])
        extracts[k] = ex
        is_blog = k in blog_keys
        wl = words_of(ex["text"])
        excerpt = " ".join(wl[:EXCERPT_WORDS_BLOG if is_blog else EXCERPT_WORDS_OTHER])
        rec = {"url": originals[k], "type": segment(k), "title": ex["title"], "h1": ex["h1"],
               "meta": ex["meta"], "words": len(wl), "container": ex["container"],
               "excerpt": excerpt, "text_hash": sha(re.sub(r"\s+", " ", ex["text"].lower())),
               "fetched_at": now_iso()}
        if is_blog:
            rec["outlinks"] = sorted({lk for lk, _ in ex["links"] if lk != k})
            anchors = {}
            for lk, at in ex["links"]:
                at = at[:80]
                if lk != k and at and at not in anchors.setdefault(lk, []):
                    anchors[lk].append(at)
            rec["anchors"] = anchors
        pages[k] = rec

    if len(failures) > MAX_FAIL_SHARE * len(order):
        print(f"{len(failures)} of {len(order)} fetches failed. Not writing anything.", file=sys.stderr)
        return 2

    # 3. Which articles need scoring?
    mode = args.mode
    if mode == "auto":
        mode = "weekly" if prev else "backfill"
    warnings = []
    reasons = {}
    if mode == "backfill":
        for k in blog_keys[args.offset:args.offset + args.limit]:
            reasons[k] = "backfill"
    else:
        changed_list = []
        for k in blog_keys:
            if k not in pages:
                continue
            if k not in prev:
                reasons[k] = "new"
                continue
            r = changed(prev[k], pages[k])
            if r is not None:
                changed_list.append((r, k))
        changed_list.sort()
        if len(changed_list) > MAX_CHANGED_PER_RUN:
            warnings.append(f"{len(changed_list)} articles looked edited; only the {MAX_CHANGED_PER_RUN} "
                            "most changed are included. Check whether page content is dynamic.")
        for _, k in changed_list[:MAX_CHANGED_PER_RUN]:
            reasons[k] = "changed"

    # 4. Rank candidates by plain text similarity
    docs = {}
    for k, rec in pages.items():
        n = 300 if k in blog_keys else EXCERPT_WORDS_OTHER
        docs[k] = " ".join([(rec["title"] + " ") * 3, (rec["h1"] + " ") * 3, (rec["meta"] + " ") * 2,
                            " ".join(words_of(rec["excerpt"])[:n])])
    vecs = build_vectors(docs)

    # Site-wide link stats, so the report can vary anchors and favour pages that get few links.
    inbound, anchors_in_use = Counter(), {}
    for k in blog_keys:
        if k not in pages:
            continue
        for lk in pages[k].get("outlinks", []):
            inbound[lk] += 1
        for lk, ats in pages[k].get("anchors", {}).items():
            for at in ats:
                anchors_in_use.setdefault(lk, Counter())[at.lower()] += 1

    def best_passage(source_key, target_key):
        """The sentence in the source text that overlaps most with the target's key terms.
        Gives Claude (and the reader) a real spot to place the link. Empty if nothing fits."""
        text = pages[source_key]["excerpt"]
        top = sorted(vecs[target_key].items(), key=lambda kv: -kv[1])[:25]
        weights = dict(top)
        best, best_score = "", 0.0
        for sent in re.split(r"(?<=[.!?])\s+", text):
            if not 40 <= len(sent) <= 400:
                continue
            sc = sum(weights.get(t, 0.0) for t in set(tokens(sent)))
            if sc > best_score:
                best, best_score = sent, sc
        return best[:260] if best_score >= 0.15 else ""

    def cand(k, sim, n_words, text_key=None, target_key=None):
        r = pages[k]
        c = {"url": r["url"], "type": r["type"], "title": r["title"] or r["h1"],
             "summary": " ".join(words_of(summary_of(r, n_words))[:n_words + 15]),
             "similarity": round(sim, 3), "inbound_links": inbound.get(k, 0),
             "anchors_in_use": [a for a, _ in anchors_in_use.get(k, Counter()).most_common(4)]}
        if text_key:
            # forward: text from the article, terms from the target page.
            # reverse: text from the older article, terms from the new article.
            c["passage"] = best_passage(text_key, target_key)
        return c

    articles = []
    for k in blog_keys:
        if k not in reasons or k not in pages:
            continue
        rec = pages[k]
        linked = set(rec.get("outlinks", []))
        fwd = [(cosine(vecs[k], vecs[t]), t) for t in order
               if t != k and t not in linked and t in pages]
        fwd.sort(reverse=True)
        entry = {"url": rec["url"], "reason": reasons[k], "title": rec["title"], "h1": rec["h1"],
                 "meta": rec["meta"], "excerpt": " ".join(words_of(rec["excerpt"])[:SCORING_EXCERPT_WORDS]),
                 "existing_internal_links": len(linked),
                 "forward_candidates": [cand(t, s, 35, text_key=k, target_key=t) for s, t in fwd[:FORWARD_CANDIDATES]],
                 "reverse_candidates": []}
        if reasons[k] == "new" and mode == "weekly":
            rev = [(cosine(vecs[k], vecs[s]), s) for s in blog_keys
                   if s != k and s not in reasons and s in pages and k not in set(pages[s].get("outlinks", []))]
            rev.sort(reverse=True)
            entry["reverse_candidates"] = [cand(s, v, 40, text_key=s, target_key=k) for v, s in rev[:REVERSE_CANDIDATES]]
        articles.append(entry)

    # 5. Health checks
    blog_pages = [pages[k] for k in blog_keys if k in pages]
    unknown = []
    for k in blog_keys:
        if k not in pages:
            continue
        for lk in pages[k].get("outlinks", []):
            if segment(lk) in TARGET_SEGMENTS and lk not in sitemap_keys:
                unknown.append({"article": pages[k]["url"], "link": lk})
    samples = []
    for k in blog_keys[:3]:
        if k in pages:
            samples.append({"url": pages[k]["url"], "words": pages[k]["words"],
                            "container": pages[k]["container"],
                            "body_links": pages[k].get("outlinks", [])[:12]})
    diagnostics = {
        "articles_scanned": len(blog_pages),
        "other_targets_scanned": len(order) - len(blog_keys),
        "mode": mode,
        "fetch_failures": failures,
        "average_words": round(sum(p["words"] for p in blog_pages) / max(len(blog_pages), 1)),
        "articles_under_300_words": [p["url"] for p in blog_pages if p["words"] < 300],
        "articles_with_no_body_links": [p["url"] for p in blog_pages if not p.get("outlinks")],
        "containers_used": dict(Counter(p["container"] for p in blog_pages)),
        "links_to_urls_missing_from_sitemap": unknown[:15],
        "sample_extractions": samples,
        "warnings": warnings,
    }

    pending = {"generated_at": now_iso(), "mode": mode,
               "backfill_range": [args.offset, args.offset + args.limit] if mode == "backfill" else None,
               "total_blog_articles": len(blog_keys), "articles": articles, "diagnostics": diagnostics}

    # 6. Write
    save_json(pages_path, {"generated_at": now_iso(), "pages": pages})
    save_json(pending_path, pending)
    print(f"mode={mode} scanned={len(blog_pages)} pending={len(articles)} failures={len(failures)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
