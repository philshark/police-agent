#!/usr/bin/env python3
"""heypolo.com sitemap change monitor.

Stdlib only. Uses `curl` for fetching (handles redirects, gzip, TLS).
Crawls every URL in the sitemap, normalises visible text + key SEO tags,
diffs against the last stored snapshot, writes a report and state files.
The scheduled agent reads reports/latest.json and sends the email.
"""
import os
import re
import sys
import json
import html
import hashlib
import subprocess
import datetime
import difflib

SITEMAP_URL = "https://heypolo.com/sitemap.xml"
UA = "Mozilla/5.0 (compatible; heypolo-change-monitor/1.0)"

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "state")
TEXT_DIR = os.path.join(STATE_DIR, "text")
HIST_DIR = os.path.join(STATE_DIR, "history")
REPORT_DIR = os.path.join(ROOT, "reports")
SNAP_PATH = os.path.join(STATE_DIR, "snapshot.json")
LATEST_REPORT_JSON = os.path.join(REPORT_DIR, "latest.json")

TIMEOUT = "40"
RETRIES = 2

BLOCK_TAGS = (r"(?:p|div|section|article|header|footer|main|li|ul|ol|tr|table"
              r"|h[1-6]|br|hr|nav|figure|figcaption|blockquote|pre|dd|dt)")


def sha(s):
    return hashlib.sha256((s or "").encode("utf-8", "replace")).hexdigest()


def slug(url):
    return hashlib.sha1(url.encode()).hexdigest()


def fetch(url):
    """Return (body:str|None, status:int, error:str|None).

    curl output is captured as raw bytes and decoded UTF-8 (errors replaced) so
    the crawler does not depend on the host locale encoding.
    """
    status = 0
    last_err = None
    for _ in range(RETRIES + 1):
        try:
            p = subprocess.run(
                ["curl", "-sS", "-L", "--compressed", "-A", UA,
                 "--max-time", TIMEOUT,
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
            return body, status, None
        if status and status != 200:
            last_err = f"HTTP {status}"
        elif p.returncode != 0:
            err = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            last_err = err[-1] if err else f"curl exit {p.returncode}"
        else:
            last_err = "empty body"
    return None, status, last_err


def sitemap_urls(url, seen=None):
    seen = seen or set()
    body, _, err = fetch(url)
    if body is None:
        raise RuntimeError(f"cannot fetch sitemap {url}: {err}")
    locs = [html.unescape(x.strip())
            for x in re.findall(r"<loc>\s*(.*?)\s*</loc>", body, re.I | re.S)]
    if "<sitemapindex" in body.lower():
        out = []
        for sm in locs:
            if sm not in seen:
                seen.add(sm)
                out.extend(sitemap_urls(sm, seen))
        return out
    return locs


def visible_text(h):
    h = re.sub(r"<!--.*?-->", " ", h, flags=re.S)
    h = re.sub(r"<(script|style|noscript|svg|template|iframe)\b[^>]*>.*?</\1>",
               " ", h, flags=re.S | re.I)
    h = re.sub(rf"</{BLOCK_TAGS}\s*>", "\n", h, flags=re.I)
    h = re.sub(rf"<{BLOCK_TAGS}(?:\s[^>]*)?/?>", "\n", h, flags=re.I)
    h = re.sub(r"<[^>]+>", "", h)
    h = html.unescape(h)
    lines = []
    for ln in h.splitlines():
        ln = re.sub(r"[ \t ]+", " ", ln).strip()
        if ln:
            lines.append(ln)
    return "\n".join(lines)


def extract_seo(h):
    def clean(s):
        return html.unescape(re.sub(r"\s+", " ", s or "").strip())

    tm = re.search(r"<title[^>]*>(.*?)</title>", h, re.I | re.S)
    title = clean(tm.group(1)) if tm else ""

    desc = ""
    dm = re.search(r'<meta[^>]+?name=["\']description["\'][^>]*>', h, re.I)
    if dm:
        cm = re.search(r'content=["\'](.*?)["\']', dm.group(0), re.I | re.S)
        desc = clean(cm.group(1)) if cm else ""

    canon = ""
    cl = re.search(r'<link[^>]+?rel=["\']canonical["\'][^>]*>', h, re.I)
    if cl:
        hm = re.search(r'href=["\'](.*?)["\']', cl.group(0), re.I)
        canon = hm.group(1).strip() if hm else ""

    robots = ""
    rm = re.search(r'<meta[^>]+?name=["\']robots["\'][^>]*>', h, re.I)
    if rm:
        cm = re.search(r'content=["\'](.*?)["\']', rm.group(0), re.I | re.S)
        robots = clean(cm.group(1)) if cm else ""

    h1s = [clean(visible_text(x))
           for x in re.findall(r"<h1\b[^>]*>(.*?)</h1>", h, re.I | re.S)]
    h1s = [x for x in h1s if x]

    jsonld = re.findall(
        r'<script[^>]+?type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        h, re.I | re.S)
    jsonld_norm = "\n".join(re.sub(r"\s+", " ", j).strip() for j in jsonld)

    return {"title": title, "meta_description": desc, "canonical": canon,
            "robots": robots, "h1": h1s, "jsonld_hash": sha(jsonld_norm)}


def seo_signature(seo):
    return sha(json.dumps(
        {k: seo[k] for k in
         ("title", "meta_description", "canonical", "robots", "h1", "jsonld_hash")},
        ensure_ascii=False, sort_keys=True))


def load_json(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def window_label(prev_date, today):
    if not prev_date:
        return "since last run"
    try:
        pd = datetime.date.fromisoformat(prev_date)
    except ValueError:
        return "since last run"
    delta = (today - pd).days
    if delta == 1:
        return "since yesterday"
    if today.weekday() == 0 and delta == 3:
        return "since Friday"
    return f"since {prev_date}"


def main():
    for d in (STATE_DIR, TEXT_DIR, HIST_DIR, REPORT_DIR):
        os.makedirs(d, exist_ok=True)

    today = datetime.date.today()
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    prev = load_json(SNAP_PATH, None)
    prev_pages = (prev or {}).get("pages", {})
    prev_date = (prev or {}).get("captured_at", "")[:10]

    raw_urls = sitemap_urls(SITEMAP_URL)
    seen, urls = set(), []
    for u in raw_urls:
        if u not in seen:
            seen.add(u)
            urls.append(u)
    total = len(urls)

    pages, errors, changed = {}, [], []

    for u in urls:
        body, status, err = fetch(u)
        if body is None:
            errors.append((u, err or f"HTTP {status}"))
            if u in prev_pages:            # keep old entry: transient error != change
                pages[u] = prev_pages[u]
            continue

        vt = visible_text(body)
        seo = extract_seo(body)
        entry = {
            "text_hash": sha(vt),
            "text_len": len(vt),
            "seo_hash": seo_signature(seo),
            "title": seo["title"],
            "meta_description": seo["meta_description"],
            "canonical": seo["canonical"],
            "robots": seo["robots"],
            "h1": seo["h1"],
            "jsonld_hash": seo["jsonld_hash"],
            "http_status": status,
            "fetched_at": now_iso,
        }
        pages[u] = entry

        tpath = os.path.join(TEXT_DIR, slug(u) + ".txt")
        old_text = ""
        if os.path.exists(tpath):
            with open(tpath, encoding="utf-8") as f:
                old_text = f.read()
        with open(tpath, "w", encoding="utf-8") as f:
            f.write(vt)

        p = prev_pages.get(u)
        if not p:
            continue                       # brand-new URL -> handled as "added"

        reasons = []
        if p.get("text_hash") != entry["text_hash"]:
            reasons.append("visible text")
        if p.get("title", "") != entry["title"]:
            reasons.append("title")
        if p.get("meta_description", "") != entry["meta_description"]:
            reasons.append("meta description")
        if p.get("canonical", "") != entry["canonical"]:
            reasons.append("canonical")
        if p.get("robots", "") != entry.get("robots", ""):
            reasons.append("robots meta")
        if p.get("h1", []) != entry["h1"]:
            reasons.append("H1")
        if p.get("jsonld_hash", "") != entry["jsonld_hash"]:
            reasons.append("structured data")
        if not reasons:
            continue

        d = []
        if "title" in reasons:
            d.append(f'  title: "{p.get("title","")}"  ->  "{entry["title"]}"')
        if "meta description" in reasons:
            d.append(f'  meta description: "{p.get("meta_description","")}"  ->  "{entry["meta_description"]}"')
        if "canonical" in reasons:
            d.append(f'  canonical: "{p.get("canonical","")}"  ->  "{entry["canonical"]}"')
        if "robots meta" in reasons:
            d.append(f'  robots: "{p.get("robots","")}"  ->  "{entry.get("robots","")}"')
        if "H1" in reasons:
            d.append(f'  H1: {p.get("h1",[])}  ->  {entry["h1"]}')
        if "visible text" in reasons:
            dl = entry["text_len"] - p.get("text_len", 0)
            d.append(f'  visible text: {p.get("text_len",0)} -> {entry["text_len"]} chars ({dl:+d})')
            if old_text:
                diff = list(difflib.unified_diff(
                    old_text.splitlines(), vt.splitlines(), lineterm="", n=0))
                snippet = [x for x in diff[2:]
                           if x and x[0] in "+-" and not x.startswith(("+++", "---"))]
                for x in snippet[:20]:
                    d.append("    " + x)
                if len(snippet) > 20:
                    d.append(f"    ... {len(snippet) - 20} more changed lines")
        changed.append((u, reasons, "\n".join(d)))

    added = [u for u in urls if u not in prev_pages] if prev is not None else []
    removed = [u for u in prev_pages if u not in seen] if prev is not None else []

    snap = {"captured_at": now_iso, "sitemap_url": SITEMAP_URL,
            "sitemap_count": total, "pages": pages}
    with open(SNAP_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1, sort_keys=True)
    with open(os.path.join(HIST_DIR, f"{today.isoformat()}.json"), "w",
              encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1, sort_keys=True)
    for old in sorted(os.listdir(HIST_DIR))[:-40]:
        try:
            os.remove(os.path.join(HIST_DIR, old))
        except OSError:
            pass

    baseline = prev is None
    c = len(changed)
    if baseline:
        subject = f"Baseline established - {total} URLs captured on heypolo.com"
    else:
        subject = f"{c}/{total} URLs changed {window_label(prev_date, today)}"
        extras = []
        if added:
            extras.append(f"+{len(added)} new")
        if removed:
            extras.append(f"-{len(removed)} removed")
        if extras:
            subject += " (" + ", ".join(extras) + ")"

    L = [f"heypolo.com sitemap change report - {now_iso}"]
    if baseline:
        L += ["",
              f"First run: captured {total} URLs as the baseline. No comparison "
              f"yet - real change reports start on the next run."]
    else:
        L += [f"Compared against the snapshot from {prev_date or 'unknown'}.",
              f"{c} of {total} sitemap URLs changed."]
        if added:
            L.append(f"{len(added)} new URL(s) appeared in the sitemap.")
        if removed:
            L.append(f"{len(removed)} URL(s) were removed from the sitemap.")
    if errors:
        L.append(f"{len(errors)} URL(s) could not be fetched - NOT counted as changes.")
    L.append("")

    if not baseline:
        L.append(f"## Changed ({c})")
        if not changed:
            L.append("_None._")
        for u, reasons, detail in changed:
            L.append(f"- {u}")
            L.append(f"    changed: {', '.join(reasons)}")
            if detail:
                L.append(detail)
        L += ["", f"## Added to sitemap ({len(added)})"]
        if added:
            for u in added:
                title = pages.get(u, {}).get("title", "")
                fetch_note = "" if u in pages else "  [could not be fetched yet - see Fetch errors]"
                L.append(f'- {u}: "{title}"{fetch_note}' if title else f"- {u}{fetch_note}")
        else:
            L.append("_None._")
        L += ["", f"## Removed from sitemap ({len(removed)})"]
        if removed:
            for u in removed:
                title = prev_pages.get(u, {}).get("title", "")
                L.append(f'- {u}: "{title}"' if title else f"- {u}")
        else:
            L.append("_None._")
        L.append("")

    L.append(f"## Fetch errors ({len(errors)})")
    L += [f"- {u} - {e}" for u, e in errors] or ["_None._"]
    L += ["", f"Sitemap: {SITEMAP_URL}  |  URLs in sitemap: {total}"]

    body_md = "\n".join(L)
    with open(os.path.join(REPORT_DIR, f"{today.isoformat()}.md"), "w",
              encoding="utf-8") as f:
        f.write(body_md + "\n")
    with open(LATEST_REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump({"subject": subject, "body_markdown": body_md,
                   "changed_count": c, "total": total,
                   "changed_urls": [u for u, _, _ in changed],
                   "added": added, "removed": removed,
                   "errors": [{"url": u, "error": e} for u, e in errors],
                   "baseline": baseline, "generated_at": now_iso},
                  f, ensure_ascii=False, indent=1)
    for old in sorted(x for x in os.listdir(REPORT_DIR)
                      if re.match(r"\d{4}-\d\d-\d\d\.md$", x))[:-90]:
        try:
            os.remove(os.path.join(REPORT_DIR, old))
        except OSError:
            pass

    print(subject)
    print(f"changed={c} total={total} errors={len(errors)} "
          f"added={len(added)} removed={len(removed)} baseline={baseline}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        os.makedirs(REPORT_DIR, exist_ok=True)
        msg = f"heypolo monitor FAILED: {e}\n\n{traceback.format_exc()}"
        with open(LATEST_REPORT_JSON, "w", encoding="utf-8") as f:
            json.dump({"subject": "heypolo sitemap monitor FAILED",
                       "body_markdown": msg, "changed_count": -1,
                       "total": 0, "baseline": False, "error": str(e)},
                      f, ensure_ascii=False, indent=1)
        print(msg, file=sys.stderr)
        sys.exit(1)
