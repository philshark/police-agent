#!/usr/bin/env python3
"""heypolo.com sitemap change monitor. Stdlib only, uses curl for fetching."""
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
TEXT_DIFF_LINE_CAP = 15  # max before/after lines shown per changed page

BLOCK_TAGS = (r"(?:p|div|section|article|header|footer|main|li|ul|ol|tr|table"
              r"|h[1-6]|br|hr|nav|figure|figcaption|blockquote|pre|dd|dt)")


def sha(s):
    return hashlib.sha256((s or "").encode("utf-8", "replace")).hexdigest()


def slug(url):
    return hashlib.sha1(url.encode()).hexdigest()


def esc(s):
    return html.escape(s if s is not None else "", quote=True)


def fetch(url):
    """Return (body:str|None, status:int, error:str|None)."""
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


def strip_links(h):
    """Remove <a>...</a> elements (tag + their text) entirely, so link text -
    internal or external, new or edited - never counts toward a content change.
    Only used for the page-body text that feeds change detection; H1/title
    extraction (via visible_text() elsewhere) is untouched."""
    return re.sub(r"<a\b[^>]*>.*?</a>", " ", h, flags=re.S | re.I)


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
        ln = re.sub(r"[ \t ]+", " ", ln).strip()
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


def build_change_record(u, p, entry, old_text, vt):
    """Diff old entry `p` vs fresh `entry`; None if unchanged, else a dict of
    url/reasons/fields(before-after tuples)/text(before-after line preview)."""
    reasons = []
    fields = []

    if p.get("text_hash") != entry["text_hash"]:
        reasons.append("visible text")
    if p.get("title", "") != entry["title"]:
        reasons.append("title")
        fields.append(("Title", p.get("title", ""), entry["title"]))
    if p.get("meta_description", "") != entry["meta_description"]:
        reasons.append("meta description")
        fields.append(("Meta description", p.get("meta_description", ""), entry["meta_description"]))
    if p.get("canonical", "") != entry["canonical"]:
        reasons.append("canonical")
        fields.append(("Canonical", p.get("canonical", ""), entry["canonical"]))
    if p.get("robots", "") != entry.get("robots", ""):
        reasons.append("robots meta")
        fields.append(("Robots meta", p.get("robots", ""), entry.get("robots", "")))
    if p.get("h1", []) != entry["h1"]:
        reasons.append("H1")
        fields.append(("H1", str(p.get("h1", [])), str(entry["h1"])))
    if p.get("jsonld_hash", "") != entry["jsonld_hash"]:
        reasons.append("structured data")

    if not reasons:
        return None

    text_change = None
    if "visible text" in reasons:
        old_len, new_len = p.get("text_len", 0), entry["text_len"]
        old_lines, new_lines, more_old, more_new = [], [], 0, 0
        if old_text:
            diff = list(difflib.unified_diff(
                old_text.splitlines(), vt.splitlines(), lineterm="", n=0))
            snippet = [x for x in diff[2:]
                       if x and x[0] in "+-" and not x.startswith(("+++", "---"))]
            raw_old = [x[1:] for x in snippet if x.startswith("-")]
            raw_new = [x[1:] for x in snippet if x.startswith("+")]
            old_lines, more_old = raw_old[:TEXT_DIFF_LINE_CAP], max(0, len(raw_old) - TEXT_DIFF_LINE_CAP)
            new_lines, more_new = raw_new[:TEXT_DIFF_LINE_CAP], max(0, len(raw_new) - TEXT_DIFF_LINE_CAP)
        text_change = {"old_len": old_len, "new_len": new_len,
                       "old_lines": old_lines, "new_lines": new_lines,
                       "more_old": more_old, "more_new": more_new}

    return {"url": u, "reasons": reasons, "fields": fields, "text": text_change}


def render_markdown(subject, baseline, total, c, prev_date, window,
                     changed, added_items, removed_items, errors, sitemap_url):
    L = [subject, ""]
    if baseline:
        L += [f"First run: captured {total} URLs as the baseline. No comparison "
              f"yet - real change reports start on the next run."]
    else:
        L += [f"SUMMARY: {c} of {total} sitemap URLs changed {window} "
              f"(compared against the snapshot from {prev_date or 'unknown'})."]
        if added_items:
            L.append(f"  {len(added_items)} new URL(s) appeared in the sitemap.")
        if removed_items:
            L.append(f"  {len(removed_items)} URL(s) were removed from the sitemap.")
        if errors:
            L.append(f"  {len(errors)} URL(s) could not be fetched - NOT counted as changes.")
    L.append("")

    if not baseline:
        L.append(f"## Changed ({c})")
        if not changed:
            L.append("_None._")
        for rec in changed:
            L.append(f"### {rec['url']}")
            L.append(f"changed: {', '.join(rec['reasons'])}")
            for label, old, new in rec["fields"]:
                L.append(f"  {label}")
                L.append(f'    was: "{old}"')
                L.append(f'    now: "{new}"')
            t = rec["text"]
            if t:
                dl = t["new_len"] - t["old_len"]
                L.append(f'  Visible text ({t["old_len"]} -> {t["new_len"]} chars, {dl:+d})')
                if t["old_lines"] or t["new_lines"]:
                    L.append("    was:")
                    for x in t["old_lines"]:
                        L.append("      " + x)
                    if t["more_old"]:
                        L.append(f"      ... {t['more_old']} more removed lines")
                    L.append("    now:")
                    for x in t["new_lines"]:
                        L.append("      " + x)
                    if t["more_new"]:
                        L.append(f"      ... {t['more_new']} more added lines")
            L.append("")

        L.append(f"## Added to sitemap ({len(added_items)})")
        if added_items:
            for u, title, fetched in added_items:
                note = "" if fetched else "  [could not be fetched yet - see Fetch errors]"
                L.append(f'- {u}: "{title}"{note}' if title else f"- {u}{note}")
        else:
            L.append("_None._")
        L.append("")

        L.append(f"## Removed from sitemap ({len(removed_items)})")
        if removed_items:
            for u, title in removed_items:
                L.append(f'- {u}: "{title}"' if title else f"- {u}")
        else:
            L.append("_None._")
        L.append("")

    L.append(f"## Fetch errors ({len(errors)})")
    L += [f"- {u} - {e}" for u, e in errors] or ["_None._"]
    L += ["", f"Sitemap: {sitemap_url}  |  URLs in sitemap: {total}"]
    return "\n".join(L)


def render_html(subject, baseline, total, c, prev_date, window,
                 changed, added_items, removed_items, errors, sitemap_url):
    P = ['<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;'
         'color:#1a1a1a;font-size:14px;line-height:1.55;max-width:720px;">']

    if baseline:
        P.append(f'<p style="font-size:15px;">&#128203; <strong>Baseline established.</strong> '
                  f'Captured {total} URLs on heypolo.com. No comparison yet &mdash; '
                  f'real change reports start next run.</p>')
    else:
        bits = [f'<strong>{c} of {total}</strong> sitemap URLs changed {esc(window)} '
                f'(vs. the snapshot from {esc(prev_date or "unknown")}).']
        if added_items:
            bits.append(f'{len(added_items)} new URL(s) appeared.')
        if removed_items:
            bits.append(f'{len(removed_items)} URL(s) removed.')
        if errors:
            bits.append(f'{len(errors)} URL(s) could not be fetched (not counted as changes).')
        P.append('<p style="font-size:15px;background:#f3f4f6;border-radius:8px;'
                  'padding:12px 16px;">&#128203; ' + " ".join(bits) + '</p>')

    if not baseline:
        P.append(f'<h2 style="font-size:16px;margin:24px 0 4px;border-bottom:1px solid #e5e7eb;'
                  f'padding-bottom:6px;">Changed ({c})</h2>')
        if not changed:
            P.append('<p style="color:#9ca3af;">None.</p>')
        for rec in changed:
            P.append(f'<h3 style="font-size:14px;margin:20px 0 4px;word-break:break-all;">'
                      f'<a href="{esc(rec["url"])}" style="color:#1d4ed8;text-decoration:none;">'
                      f'{esc(rec["url"])}</a></h3>')
            P.append(f'<div style="color:#6b7280;font-size:12px;margin-bottom:6px;">'
                      f'changed: {esc(", ".join(rec["reasons"]))}</div>')
            if rec["fields"]:
                P.append('<table style="width:100%;border-collapse:collapse;font-size:13px;'
                          'margin-bottom:6px;">')
                for label, old, new in rec["fields"]:
                    old_disp = esc(old) if old else '<em style="color:#9ca3af;">(empty)</em>'
                    new_disp = esc(new) if new else '<em style="color:#9ca3af;">(empty)</em>'
                    P.append('<tr>'
                             f'<td style="width:130px;color:#6b7280;vertical-align:top;'
                             f'padding:4px 10px 4px 0;">{esc(label)}</td>'
                             '<td style="padding:4px 0;">'
                             f'<div style="color:#b91c1c;">was: {old_disp}</div>'
                             f'<div style="color:#15803d;">now: {new_disp}</div>'
                             '</td></tr>')
                P.append('</table>')
            t = rec["text"]
            if t:
                dl = t["new_len"] - t["old_len"]
                P.append(f'<div style="color:#6b7280;font-size:12px;margin:6px 0 2px;">'
                          f'Visible text: {t["old_len"]} &rarr; {t["new_len"]} chars '
                          f'({dl:+d})</div>')
                if t["old_lines"] or t["new_lines"]:
                    old_block = esc("\n".join(t["old_lines"]))
                    if t["more_old"]:
                        old_block += f"\n... {t['more_old']} more removed lines"
                    new_block = esc("\n".join(t["new_lines"]))
                    if t["more_new"]:
                        new_block += f"\n... {t['more_new']} more added lines"
                    P.append('<div style="background:#fef2f2;border-left:3px solid #dc2626;'
                              'padding:8px 12px;margin:4px 0;white-space:pre-wrap;'
                              'font-family:Menlo,Consolas,monospace;font-size:12px;">'
                              f'<strong>was:</strong>\n{old_block}</div>')
                    P.append('<div style="background:#f0fdf4;border-left:3px solid #16a34a;'
                              'padding:8px 12px;margin:4px 0 14px;white-space:pre-wrap;'
                              'font-family:Menlo,Consolas,monospace;font-size:12px;">'
                              f'<strong>now:</strong>\n{new_block}</div>')

        P.append(f'<h2 style="font-size:16px;margin:24px 0 4px;border-bottom:1px solid #e5e7eb;'
                  f'padding-bottom:6px;">Added to sitemap ({len(added_items)})</h2>')
        if added_items:
            P.append('<ul style="padding-left:18px;margin:6px 0;">')
            for u, title, fetched in added_items:
                note = ('' if fetched else
                        ' <span style="color:#9ca3af;">[could not be fetched yet]</span>')
                title_bit = f': "{esc(title)}"' if title else ""
                P.append(f'<li style="margin-bottom:4px;">'
                          f'<a href="{esc(u)}" style="color:#1d4ed8;">{esc(u)}</a>'
                          f'{title_bit}{note}</li>')
            P.append('</ul>')
        else:
            P.append('<p style="color:#9ca3af;">None.</p>')

        P.append(f'<h2 style="font-size:16px;margin:24px 0 4px;border-bottom:1px solid #e5e7eb;'
                  f'padding-bottom:6px;">Removed from sitemap ({len(removed_items)})</h2>')
        if removed_items:
            P.append('<ul style="padding-left:18px;margin:6px 0;">')
            for u, title in removed_items:
                title_bit = f': "{esc(title)}"' if title else ""
                P.append(f'<li style="margin-bottom:4px;color:#4b5563;">{esc(u)}{title_bit}</li>')
            P.append('</ul>')
        else:
            P.append('<p style="color:#9ca3af;">None.</p>')

    P.append(f'<h2 style="font-size:16px;margin:24px 0 4px;border-bottom:1px solid #e5e7eb;'
              f'padding-bottom:6px;">Fetch errors ({len(errors)})</h2>')
    if errors:
        P.append('<ul style="padding-left:18px;margin:6px 0;">')
        for u, e in errors:
            P.append(f'<li>{esc(u)} &mdash; {esc(e)}</li>')
        P.append('</ul>')
    else:
        P.append('<p style="color:#9ca3af;">None.</p>')

    P.append(f'<p style="color:#9ca3af;font-size:12px;margin-top:24px;">'
              f'Sitemap: <a href="{esc(sitemap_url)}" style="color:#9ca3af;">{esc(sitemap_url)}</a>'
              f' &nbsp;|&nbsp; URLs in sitemap: {total}</p>')
    P.append('</div>')
    return "\n".join(P)


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

        vt = visible_text(strip_links(body))
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

        rec = build_change_record(u, p, entry, old_text, vt)
        if rec:
            changed.append(rec)

    added = [u for u in urls if u not in prev_pages] if prev is not None else []
    removed = [u for u in prev_pages if u not in seen] if prev is not None else []
    added_items = [(u, pages.get(u, {}).get("title", ""), u in pages) for u in added]
    removed_items = [(u, prev_pages.get(u, {}).get("title", "")) for u in removed]

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
    window = "" if baseline else window_label(prev_date, today)
    if baseline:
        subject = f"Baseline established - {total} URLs captured on heypolo.com"
    else:
        subject = f"{c}/{total} URLs changed {window}"
        extras = []
        if added:
            extras.append(f"+{len(added)} new")
        if removed:
            extras.append(f"-{len(removed)} removed")
        if extras:
            subject += " (" + ", ".join(extras) + ")"

    body_md = render_markdown(subject, baseline, total, c, prev_date, window,
                               changed, added_items, removed_items, errors, SITEMAP_URL)
    body_html = render_html(subject, baseline, total, c, prev_date, window,
                             changed, added_items, removed_items, errors, SITEMAP_URL)

    with open(os.path.join(REPORT_DIR, f"{today.isoformat()}.md"), "w",
              encoding="utf-8") as f:
        f.write(body_md + "\n")
    with open(os.path.join(REPORT_DIR, f"{today.isoformat()}.html"), "w",
              encoding="utf-8") as f:
        f.write(body_html + "\n")
    with open(LATEST_REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump({"subject": subject, "body_markdown": body_md, "body_html": body_html,
                   "changed_count": c, "total": total,
                   "changed_urls": [rec["url"] for rec in changed],
                   "added": added, "removed": removed,
                   "errors": [{"url": u, "error": e} for u, e in errors],
                   "baseline": baseline, "generated_at": now_iso},
                  f, ensure_ascii=False, indent=1)
    for old in sorted(x for x in os.listdir(REPORT_DIR)
                      if re.match(r"\d{4}-\d\d-\d\d\.(md|html)$", x))[:-180]:
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
                       "body_markdown": msg,
                       "body_html": f"<pre>{esc(msg)}</pre>",
                       "changed_count": -1,
                       "total": 0, "baseline": False, "error": str(e)},
                      f, ensure_ascii=False, indent=1)
        print(msg, file=sys.stderr)
        sys.exit(1)
