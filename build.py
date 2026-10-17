#!/usr/bin/env python3
"""Build Magnus Daily: fetch RSS feeds, embed resized images, write index.html.
Usage: python3 build.py OUTPUT_HTML
"""
import sys, json, re, html, base64, io, subprocess, hashlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as ET
from PIL import Image

OUT = sys.argv[1] if len(sys.argv) > 1 else "index.html"
STANDALONE = "--standalone" in sys.argv

HEAD = """<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Daybook">
<meta name="robots" content="noindex">
<link rel="manifest" href="manifest.json">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}
body{margin:0;font:14px system-ui,sans-serif}img{max-width:100%}[hidden]{display:none!important}</style>
</head><body>"""
MEDIA = "{http://search.yahoo.com/mrss/}"
ATOM = "{http://www.w3.org/2005/Atom}"

SECTIONS = [
    ("uk", "UK", [("BBC", "https://feeds.bbci.co.uk/news/uk/rss.xml"),
                  ("Guardian", "https://www.theguardian.com/uk-news/rss")], 12),
    ("world", "World", [("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
                        ("Guardian", "https://www.theguardian.com/world/rss")], 12),
    ("environment", "Environment", [("BBC", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
                                    ("Guardian", "https://www.theguardian.com/uk/environment/rss")], 10),
    ("ai", "AI", [("Guardian", "https://www.theguardian.com/technology/artificialintelligenceai/rss"),
                  ("The Verge", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
                  ("Ars Technica", "https://arstechnica.com/ai/feed/"),
                  ("Ethan Mollick", "https://www.oneusefulthing.org/feed"),
                  ("Simon Willison", "https://simonwillison.net/atom/everything/")], 12),
    ("technology", "Technology", [("BBC", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
                                  ("Guardian", "https://www.theguardian.com/uk/technology/rss")], 10),
    ("work", "Work", [("Guardian", "https://www.theguardian.com/money/work-and-careers/rss"),
                      ("Personnel Today", "https://www.personneltoday.com/feed/"),
                      ("The Conversation", "https://theconversation.com/uk/business/articles.atom")], 12),
    ("writers", "Writers", [("Kevin Kelly", "https://kk.org/thetechnium/feed/"),
                            ("Austin Kleon", "https://austinkleon.substack.com/feed"),
                            ("Maria Popova", "https://www.themarginalian.org/feed/"),
                            ("Seth Godin", "https://seths.blog/feed/"),
                            ("Robin Sloan", "https://robinsloan.com/feed.xml")], 12),
    ("diary", "Country Diary", [("Guardian", "https://www.theguardian.com/environment/series/country-diary/rss")], 7),
    ("genx", "Gen X & Ageism", [
        ("Google News", "https://news.google.com/rss/search?q=%28%22Generation+X%22+OR+%22Gen+X%22%29+%28work+OR+workers+OR+jobs+OR+employment+OR+career+OR+redundancy+OR+retirement+OR+pension+OR+leadership%29+when%3A14d&hl=en-GB&gl=GB&ceid=GB:en"),
        ("Google News", "https://news.google.com/rss/search?q=%28ageism+OR+%22age+discrimination%22+OR+%22older+workers%22%29+when%3A14d&hl=en-GB&gl=GB&ceid=GB:en")], 14),
]


# Publishers with hard paywalls or tight meters, dropped from the Google News searches
PAYWALLED = [
    "the times", "sunday times", "financial times", "ft.com", "telegraph", "economist",
    "wall street journal", "wsj", "bloomberg", "new york times", "nytimes", "washington post",
    "the atlantic", "new yorker", "harvard business review", "hbr", "business insider",
    "insider", "spectator", "new statesman", "wired", "the information", "barron",
    "the athletic", "thetimes", "times of london", "evening standard insider", "politico pro",
    "personnel today premium", "hr magazine premium", "mit technology review", "fortune",
    "the australian", "globe and mail", "le monde", "haaretz", "boston globe", "la times",
    "los angeles times", "chicago tribune", "the herald", "the scotsman", "irish times",
]


def gated(src):
    s = src.lower()
    return any(p in s for p in PAYWALLED)


def curl(url, binary=False):
    r = subprocess.run(["curl", "-sL", "--max-time", "15", "-A", "Mozilla/5.0 MagnusDaily", url],
                       capture_output=True)
    if r.returncode != 0:
        return None
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def clean(text, limit=220):
    t = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"(Continue reading\.*|Read the full story at .*)$", "", t).strip()
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0] + "…"
    return t


def pick_image(item, source):
    if source == "BBC":
        th = item.find(MEDIA + "thumbnail")
        if th is not None and th.get("url"):
            return re.sub(r"/standard/\d+/", "/standard/800/", th.get("url"))
    enc = item.find("enclosure")
    if enc is not None and (enc.get("type") or "").startswith("image") and enc.get("url"):
        return enc.get("url")
    best, bw = None, 0
    for mc in item.findall(MEDIA + "content"):
        w = int(mc.get("width") or 0)
        if mc.get("url") and w > bw:
            best, bw = mc.get("url"), w
    return best


def parse(source, url):
    xml = curl(url)
    out = []
    if not xml:
        return out
    try:
        root = ET.fromstring(xml.encode("utf-8"))
    except ET.ParseError:
        return out
    if root.tag == ATOM + "feed":
        return parse_atom(root, source)
    for it in root.iter("item"):
        title = clean(it.findtext("title"), 300)
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        if re.search(r"[–-]\s*(video|in pictures|live)$", title, re.I) or "/live/" in link:
            continue
        src = source
        if source == "Google News":
            s = it.find("source")
            if s is not None and s.text:
                src = s.text.strip()
                title = re.sub(r"\s+-\s+" + re.escape(src) + r"$", "", title)
            if gated(src):
                continue
            summary = ""
        else:
            summary = clean(it.findtext("description"))
            link = re.sub(r"\?at_medium=RSS.*$", "", link)
        try:
            dt = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc)
        except Exception:
            dt = datetime.now(timezone.utc)
        # BBC feeds include video/sport/iplayer links that age badly; keep articles
        if source == "BBC" and ("/videos/" in link or "/sounds/" in link or "/iplayer/" in link):
            continue
        out.append({"t": title, "u": link, "s": summary, "src": src,
                    "d": dt.isoformat(), "img": pick_image(it, source) if source != "Google News" else None})
    return out


def parse_atom(root, source):
    out = []
    for e in root.iter(ATOM + "entry"):
        title = clean(e.findtext(ATOM + "title"), 300)
        link = ""
        for l in e.findall(ATOM + "link"):
            if l.get("rel", "alternate") == "alternate":
                link = l.get("href", "")
        if not title or not link:
            continue
        body = e.findtext(ATOM + "content") or e.findtext(ATOM + "summary") or ""
        m = re.search(r"<p>(.*?)</p>", body, re.S)
        img = re.search(r'<img[^>]+src="([^"]+)"', body)
        try:
            dt = datetime.fromisoformat((e.findtext(ATOM + "published") or e.findtext(ATOM + "updated")).replace("Z", "+00:00"))
        except Exception:
            dt = datetime.now(timezone.utc)
        out.append({"t": title, "u": link, "s": clean(m.group(1) if m else body), "src": source,
                    "d": dt.astimezone(timezone.utc).isoformat(), "img": html.unescape(img.group(1)) if img else None})
    return out


def norm(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())[:60]


PER_SOURCE = {"ai": 3, "writers": 3}


def build_section(sec):
    key, label, feeds, n = sec
    items = []
    for source, url in feeds:
        got = sorted(parse(source, url), key=lambda x: x["d"], reverse=True)
        items.extend(got[:PER_SOURCE[key]] if key in PER_SOURCE else got)
    if key == "diary":
        for x in items:
            m = re.match(r"(?i)country diary(?: \d{4})?:\s*(.*?)(?:\s*\|\s*(.+))?$", x["t"])
            if m:
                x["t"] = m.group(1)
                x["src"] = "Country Diary" + (" · " + m.group(2) if m.group(2) else "")
    else:
        items = [x for x in items if "country-diary" not in x["u"]]
    seen, uniq = set(), []
    for x in sorted(items, key=lambda x: x["d"], reverse=True):
        k = norm(x["t"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(x)
    # interleave sources so one publisher does not swamp the top
    if len(feeds) > 1 and feeds[0][0] != "Google News":
        by = {}
        for x in uniq:
            by.setdefault(x["src"], []).append(x)
        lists = list(by.values())
        mixed = []
        while any(lists):
            for l in lists:
                if l:
                    mixed.append(l.pop(0))
        uniq = mixed
    # lead story should have a picture
    for i, x in enumerate(uniq[:6]):
        if x["img"]:
            uniq.insert(0, uniq.pop(i))
            break
    return key, label, uniq[:n]


def embed(url, width):
    data = curl(url, binary=True)
    if not data:
        return None
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=58, optimize=True, progressive=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def main():
    with ThreadPoolExecutor(8) as ex:
        sections = list(ex.map(build_section, SECTIONS))
    seen = set()
    for i, (key, label, items) in enumerate(sections):
        keep = []
        for x in items:
            k = norm(x["t"])
            if k not in seen:
                seen.add(k)
                keep.append(x)
        sections[i] = (key, label, keep)
    jobs = []
    for key, label, items in sections:
        for i, x in enumerate(items):
            if x["img"]:
                jobs.append((x, 760 if i == 0 else 480))
    with ThreadPoolExecutor(12) as ex:
        res = list(ex.map(lambda j: embed(j[0]["img"], j[1]), jobs))
    for (x, _), r in zip(jobs, res):
        x["img"] = r
    data = {"built": datetime.now(timezone.utc).isoformat(),
            "sections": [{"key": k, "label": l, "items": it} for k, l, it in sections]}
    tpl = open(__file__.replace("build.py", "template.html"), encoding="utf-8").read()
    page = tpl.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    if STANDALONE:
        page = HEAD + page + "</body></html>"
    open(OUT, "w", encoding="utf-8").write(page)
    total = sum(len(s["items"]) for s in data["sections"])
    print(f"wrote {OUT}: {total} stories, {len(page)//1024} KB")


if __name__ == "__main__":
    main()
