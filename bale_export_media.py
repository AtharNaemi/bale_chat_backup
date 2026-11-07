"""
Bale Web chat exporter -> XML + media downloader (analysis + Tkinter chooser)

Flow:
  1) Log in, open the target chat, press Enter in the terminal.
  2) The script scrolls the whole chat, collects every message, saves the XML,
     and analyses the chat (how many photos / videos / voices / ...).
  3) A Tkinter window shows the analysis. Choose which media types to download
     (and from whom), then click "Start download".
  4) The script scrolls through the chat again and downloads the selected media
     into ./bale_media/<Type>/ . The XML is re-written with <file> paths.

Install:
  pip install playwright
  playwright install chromium
  (Tkinter ships with Python on Windows/macOS. On Debian/Ubuntu: sudo apt install python3-tk)

Notes:
  - Re-running is safe: files already downloaded (bale_media/manifest.json) are skipped.
  - If a media item can't be downloaded, the message HTML is saved to
    bale_media/_debug/<sid>.html so selectors can be adjusted.
"""
import asyncio
import base64
import json
import mimetypes
import re
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
STATE_FILE = "bale_state.json"
OUTPUT_FILE = "bale_chat.xml"
MEDIA_DIR = Path("bale_media")
SCROLLER_SEL = "#message_list_scroller_id"
MSG_SEL = '[aria-label="message-item"]'

ME_NAME = "Ali"
OTHER_NAME = "Friend"

# message type -> (label shown in the dialog, folder name)
DOWNLOADABLE = {
    "photo":    ("Pictures",  "Pictures"),
    "video":    ("Videos",    "Videos"),
    "voice":    ("Voices",    "Voices"),
    "document": ("Documents", "Documents"),
    "sticker":  ("Stickers",  "Stickers"),
}

MIME_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/webm": ".webm",
    "audio/x-m4a": ".m4a", "audio/aac": ".aac", "audio/wav": ".wav",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "application/pdf": ".pdf", "application/zip": ".zip",
}

# ----------------------------------------------------------------------------
# In-page JS
# ----------------------------------------------------------------------------
EXTRACT_JS = r"""
() => {
  const root = document.querySelector('#message_list_scroller_id');
  const out = [];
  if (!root) return out;

  const msgs = root.querySelectorAll('[aria-label="message-item"]');
  msgs.forEach(el => {
    const sid = el.getAttribute('data-sid') || '';
    const ts  = el.getAttribute('data-date') || '';

    const hasState = !!el.querySelector('[data-testid="message-state-icon"]');
    const block = el.querySelector('.message-block') || el;
    const cs = getComputedStyle(block);
    const isOut = hasState || (cs.flexDirection === 'row-reverse');

    const textNodes = [];
    el.querySelectorAll('[data-sentry-component="NewTextContainerFC"]').forEach(t => {
      if (t.closest('[data-sentry-component="Preview"]')) return;
      const spans = t.querySelectorAll('span.p');
      if (spans.length) {
        spans.forEach(s => textNodes.push(s.innerText.trim()));
      } else {
        textNodes.push(t.innerText.trim());
      }
    });
    el.querySelectorAll('.h3Qw2Y').forEach(t => textNodes.push(t.innerText.trim()));

    let reply = null;
    const prev = el.querySelector('[data-sentry-component="Preview"]');
    if (prev) {
      const who = prev.querySelector('[dir="auto"]');
      const txt = prev.querySelector('[data-sentry-component="NewTextContainerFC"]');
      reply = {
        sender: who ? who.innerText.trim() : '',
        text: txt ? txt.innerText.trim() : ''
      };
    }

    let fwd = null;
    const f = el.querySelector('[data-sentry-component="ForwardedFC"]');
    if (f) {
      const b = f.querySelector('bdi');
      fwd = b ? b.innerText.trim() : f.innerText.trim();
    }

    const tEl = el.querySelector('p.x3ai0M');
    const timeLabel = tEl ? tEl.innerText.trim() : '';

    const docs = [];
    el.querySelectorAll('a[data-prevent-context-menu-mobile]').forEach(a => {
      const name = a.querySelector('p[dir="auto"]');
      const size = a.querySelectorAll('p')[1];
      docs.push({
        name: name ? name.innerText.trim() : '',
        size: size ? size.innerText.trim() : ''
      });
    });

    const hasPhoto   = !!el.querySelector('[data-sentry-component="PhotoFC"], [data-sentry-component="AlbumFC"]');
    const hasVideo   = !!el.querySelector('[data-sentry-component="Video"]');
    const hasSticker = !!el.querySelector('[data-testid="sticker-message"]');
    const hasVoice   = !!el.querySelector('[data-sentry-component="NewAudioProgressbar"]');
    const isCall     = !!el.querySelector('[aria-label="Callout-icon"]');

    const reactions = [...el.querySelectorAll('.TevIIa img.emoji')].map(i => i.alt);

    const withEmoji = [];
    el.querySelectorAll('[data-sentry-component="NewTextContainerFC"], .h3Qw2Y').forEach(t => {
      if (t.closest('[data-sentry-component="Preview"]')) return;
      const clone = t.cloneNode(true);
      clone.querySelectorAll('img.emoji').forEach(img => img.replaceWith(document.createTextNode(img.alt)));
      clone.querySelectorAll('br').forEach(br => br.replaceWith(document.createTextNode('\n')));
      withEmoji.push(clone.textContent.trim());
    });

    out.push({
      sid, ts, isOut, timeLabel,
      text: withEmoji.length ? withEmoji.join('\n') : textNodes.join('\n'),
      reply, fwd, docs,
      media: { photo: hasPhoto, video: hasVideo, sticker: hasSticker, voice: hasVoice, call: isCall },
      reactions
    });
  });
  return out;
}
"""

DATE_MAP_JS = r"""
() => {
  const root = document.querySelector('#message_list_scroller_id');
  const map = {};
  if (!root) return map;
  let current = '';
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
  let n;
  while ((n = walker.nextNode())) {
    if (n.matches && n.matches('p.Fk2R8K')) current = n.innerText.trim();
    if (n.matches && n.matches('[aria-label="message-item"]')) {
      const sid = n.getAttribute('data-sid');
      if (sid) map[sid] = current;
    }
  }
  return map;
}
"""

VISIBLE_SIDS_JS = r"""
() => [...document.querySelectorAll('#message_list_scroller_id [aria-label="message-item"]')]
        .map(e => e.getAttribute('data-sid')).filter(Boolean)
"""

SCROLL_TOP_JS = r"""
() => { const s = document.querySelector('#message_list_scroller_id'); s.scrollTop = 0; }
"""

SCROLL_STEP_JS = r"""
() => {
  const s = document.querySelector('#message_list_scroller_id');
  s.scrollTop += Math.floor(s.clientHeight * 0.7);
  s.dispatchEvent(new Event('scroll'));
  return { atBottom: s.scrollTop + s.clientHeight >= s.scrollHeight - 8 };
}
"""

PREP_JS = r"""
(sid) => {
  const el = [...document.querySelectorAll('#message_list_scroller_id [aria-label="message-item"]')]
               .find(e => e.getAttribute('data-sid') === sid);
  if (!el) return false;
  el.scrollIntoView({ block: 'center' });
  return true;
}
"""

REFS_JS = r"""
(sid) => {
  const el = [...document.querySelectorAll('#message_list_scroller_id [aria-label="message-item"]')]
               .find(e => e.getAttribute('data-sid') === sid);
  const res = { photos: [], videos: [], voices: [], stickers: [], docs: [] };
  if (!el) return res;
  const srcOf = n => (n.currentSrc || n.src || '');

  el.querySelectorAll('[data-sentry-component="PhotoFC"] img, [data-sentry-component="AlbumFC"] img').forEach(i => {
    if (i.classList.contains('emoji')) return;
    const u = srcOf(i);
    if (u) res.photos.push({ url: u, w: i.naturalWidth || 0 });
  });
  el.querySelectorAll('video').forEach(v => { const u = srcOf(v); if (u) res.videos.push(u); });
  el.querySelectorAll('audio').forEach(a => { const u = srcOf(a); if (u) res.voices.push(u); });
  el.querySelectorAll('[data-testid="sticker-message"] img').forEach(i => { const u = srcOf(i); if (u) res.stickers.push(u); });
  el.querySelectorAll('a[data-prevent-context-menu-mobile]').forEach(a => {
    const name = a.querySelector('p[dir="auto"]');
    res.docs.push({ url: a.href || '', name: name ? name.innerText.trim() : '' });
  });
  return res;
}
"""

# Voice/video usually only get a real <audio>/<video> src after pressing play.
PLAY_FALLBACK_JS = r"""
async ([sid, tag]) => {
  const el = [...document.querySelectorAll('#message_list_scroller_id [aria-label="message-item"]')]
               .find(e => e.getAttribute('data-sid') === sid);
  if (!el) return '';
  const first = document.querySelector(tag);
  const before = first ? (first.currentSrc || first.src || '') : '';
  const grab = () => {
    const inEl = el.querySelector(tag);
    const s1 = inEl ? (inEl.currentSrc || inEl.src || '') : '';
    if (s1) return s1;
    const g = document.querySelector(tag);
    const s2 = g ? (g.currentSrc || g.src || '') : '';
    return (s2 && s2 !== before) ? s2 : '';
  };
  let u = grab();
  if (u) return u;

  const anchor = el.querySelector('[data-sentry-component="NewAudioProgressbar"]')
              || el.querySelector('[data-sentry-component="Video"]') || el;
  let btn = null, node = anchor;
  for (let i = 0; i < 4 && node && !btn; i++) {
    btn = node.querySelector('button, [role="button"]');
    node = node.parentElement;
  }
  (btn || anchor).click();
  for (let i = 0; i < 40; i++) {
    await new Promise(r => setTimeout(r, 250));
    u = grab();
    if (u) break;
  }
  document.querySelectorAll('audio, video').forEach(m => { try { m.pause(); } catch (e) {} });
  return u;
}
"""

CLICK_DOC_JS = r"""
([sid, idx]) => {
  const el = [...document.querySelectorAll('#message_list_scroller_id [aria-label="message-item"]')]
               .find(e => e.getAttribute('data-sid') === sid);
  if (!el) return false;
  const a = el.querySelectorAll('a[data-prevent-context-menu-mobile]')[idx];
  if (!a) return false;
  a.click();
  return true;
}
"""

OUTER_HTML_JS = r"""
(sid) => {
  const el = [...document.querySelectorAll('#message_list_scroller_id [aria-label="message-item"]')]
               .find(e => e.getAttribute('data-sid') === sid);
  return el ? el.outerHTML.slice(0, 40000) : '';
}
"""

FETCH_JS = r"""
async (url) => {
  const r = await fetch(url, { credentials: 'include' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const b = await r.blob();
  const data = await new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onload = () => res(String(fr.result).split(',')[1] || '');
    fr.onerror = () => rej(new Error('read failed'));
    fr.readAsDataURL(b);
  });
  return { type: b.type || '', data };
}
"""


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def sanitize(s: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s or "")


def safe_name(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s or "").strip(" .")
    return s[:120] or "file"


def classify(m) -> str:
    if m["media"]["call"]:      return "call"
    if m["media"]["voice"]:     return "voice"
    if m["media"]["sticker"]:   return "sticker"
    if m["media"]["video"]:     return "video"
    if m["media"]["photo"]:     return "photo"
    if m["docs"]:               return "document"
    return "text"


def ts_to_dt(ts):
    try:
        return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
    except Exception:
        return None


def fmt_ts(ts):
    dt = ts_to_dt(ts)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "?"


def ext_for(mime: str, fallback: str = "") -> str:
    mime = (mime or "").split(";")[0].strip().lower()
    return MIME_EXT.get(mime) or mimetypes.guess_extension(mime) or fallback


# ----------------------------------------------------------------------------
# Phase 1: collect every message (virtualized list -> harvest while scrolling)
# ----------------------------------------------------------------------------
async def harvest(page, collected, date_map):
    msgs = await page.evaluate(EXTRACT_JS)
    dmap = await page.evaluate(DATE_MAP_JS)
    for m in msgs:
        if m["sid"] and m["sid"] not in collected:
            collected[m["sid"]] = m
    for sid, label in dmap.items():
        if label and not date_map.get(sid):
            date_map[sid] = label


async def collect_all(page, pause=1.0, max_stable=10):
    collected, date_map = {}, {}
    stable, last_total = 0, -1

    while stable < max_stable:
        await harvest(page, collected, date_map)

        await page.evaluate(f"document.querySelector('{SCROLLER_SEL}').scrollTop = 0")
        await asyncio.sleep(pause)
        await page.evaluate(f"""
            (() => {{
              const s = document.querySelector('{SCROLLER_SEL}');
              s.scrollTop = 40;  s.dispatchEvent(new Event('scroll'));
              s.scrollTop = 0;   s.dispatchEvent(new Event('scroll'));
            }})()
        """)
        await asyncio.sleep(pause)

        total = len(collected)
        stamps = [int(m["ts"]) for m in collected.values() if str(m["ts"]).isdigit()]
        oldest = fmt_ts(min(stamps)) if stamps else "?"
        newest = fmt_ts(max(stamps)) if stamps else "?"
        print(f"  unique messages: {total} | oldest: {oldest} | newest: {newest}   ", end="\r")

        if total == last_total:
            stable += 1
        else:
            stable, last_total = 0, total

    print()
    await harvest(page, collected, date_map)
    return list(collected.values()), date_map


# ----------------------------------------------------------------------------
# XML
# ----------------------------------------------------------------------------
def build_xml(messages, date_map, me="me", other="other", files_map=None):
    files_map = files_map or {}
    root = ET.Element("chat", {"source": "bale-web",
                               "exported_at": datetime.now(timezone.utc).isoformat()})
    seen = set()
    for m in messages:
        if m["sid"] in seen:
            continue
        seen.add(m["sid"])

        dt = ts_to_dt(m["ts"])
        iso = dt.isoformat() if dt else ""

        e = ET.SubElement(root, "message", {
            "id": sanitize(m["sid"]),
            "sender": me if m["isOut"] else other,
            "timestamp": iso,
            "time_label": sanitize(m["timeLabel"]),
            "date_label": sanitize(date_map.get(m["sid"], "")),
            "type": classify(m),
        })

        if m["fwd"]:
            ET.SubElement(e, "forwarded_from").text = sanitize(m["fwd"])
        if m["reply"]:
            r = ET.SubElement(e, "reply_to", {"sender": sanitize(m["reply"]["sender"])})
            r.text = sanitize(m["reply"]["text"])
        if m["text"]:
            ET.SubElement(e, "text").text = sanitize(m["text"])
        for d in m["docs"]:
            ET.SubElement(e, "document", {"name": sanitize(d["name"]),
                                          "size": sanitize(d["size"])})
        for path in files_map.get(m["sid"], []):
            ET.SubElement(e, "file", {"path": sanitize(path)})
        if m["reactions"]:
            ET.SubElement(e, "reactions").text = sanitize(" ".join(m["reactions"]))

    ET.indent(root, space="  ")
    return ET.ElementTree(root)


def write_xml(messages, date_map, files_map=None):
    tree = build_xml(messages, date_map, me=ME_NAME, other=OTHER_NAME, files_map=files_map)
    tree.write(OUTPUT_FILE, encoding="utf-8", xml_declaration=True)


# ----------------------------------------------------------------------------
# Analysis + Tkinter chooser
# ----------------------------------------------------------------------------
def analyse(messages):
    by_type = Counter(classify(m) for m in messages)
    by_type_me = Counter(classify(m) for m in messages if m["isOut"])
    by_type_other = Counter(classify(m) for m in messages if not m["isOut"])
    stamps = [int(m["ts"]) for m in messages if str(m["ts"]).isdigit()]
    return {
        "total": len(messages),
        "by_type": by_type,
        "by_type_me": by_type_me,
        "by_type_other": by_type_other,
        "oldest": fmt_ts(min(stamps)) if stamps else "?",
        "newest": fmt_ts(max(stamps)) if stamps else "?",
        "me": sum(1 for m in messages if m["isOut"]),
        "other": sum(1 for m in messages if not m["isOut"]),
    }


def print_analysis(a):
    print("\n=== Chat analysis ===")
    print(f"Messages : {a['total']}  (mine: {a['me']}, theirs: {a['other']})")
    print(f"Range    : {a['oldest']}  ->  {a['newest']}  (UTC)")
    for k in ["text", "photo", "video", "voice", "document", "sticker", "call"]:
        print(f"  {k:<9}: {a['by_type'].get(k, 0)}")
    print()


def ask_user_selection(a):
    """Show a Tkinter window. Returns (set_of_kinds, sender_filter) or None if cancelled."""
    import tkinter as tk
    from tkinter import ttk

    result = {"value": None}

    root = tk.Tk()
    root.title("Bale export - choose what to download")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=16)
    frm.grid(row=0, column=0)

    summary = (
        f"Messages: {a['total']}   (mine: {a['me']}, theirs: {a['other']})\n"
        f"From {a['oldest']} to {a['newest']} (UTC)\n"
        f"Text messages: {a['by_type'].get('text', 0)}   "
        f"Calls: {a['by_type'].get('call', 0)}"
    )
    ttk.Label(frm, text=summary, justify="left").grid(row=0, column=0, columnspan=2, sticky="w")
    ttk.Separator(frm).grid(row=1, column=0, columnspan=2, sticky="ew", pady=10)

    ttk.Label(frm, text="Download:", font=("TkDefaultFont", 10, "bold")).grid(
        row=2, column=0, sticky="w")

    kind_vars = {}
    row = 3
    for kind, (label, _) in DOWNLOADABLE.items():
        n = a["by_type"].get(kind, 0)
        var = tk.BooleanVar(value=False)
        text = f"{label}  ({n}  -  mine {a['by_type_me'].get(kind, 0)} / theirs {a['by_type_other'].get(kind, 0)})"
        cb = ttk.Checkbutton(frm, text=text, variable=var)
        if n == 0:
            cb.state(["disabled"])
        cb.grid(row=row, column=0, columnspan=2, sticky="w", pady=1)
        kind_vars[kind] = (var, n)
        row += 1

    def set_all(val):
        for var, n in kind_vars.values():
            if n > 0:
                var.set(val)

    btns = ttk.Frame(frm)
    btns.grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))
    ttk.Button(btns, text="Select all", command=lambda: set_all(True)).pack(side="left", padx=(0, 6))
    ttk.Button(btns, text="Select none", command=lambda: set_all(False)).pack(side="left")
    row += 1

    ttk.Separator(frm).grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
    row += 1
    ttk.Label(frm, text="From:", font=("TkDefaultFont", 10, "bold")).grid(row=row, column=0, sticky="w")
    row += 1
    sender_var = tk.StringVar(value="all")
    for val, text in [("all", "Everyone"), ("me", f"Only me ({ME_NAME})"),
                      ("other", f"Only them ({OTHER_NAME})")]:
        ttk.Radiobutton(frm, text=text, value=val, variable=sender_var).grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1

    ttk.Separator(frm).grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
    row += 1

    def on_start():
        kinds = {k for k, (v, n) in kind_vars.items() if v.get() and n > 0}
        result["value"] = (kinds, sender_var.get()) if kinds else None
        root.destroy()

    def on_skip():
        result["value"] = None
        root.destroy()

    actions = ttk.Frame(frm)
    actions.grid(row=row, column=0, columnspan=2, sticky="e")
    ttk.Button(actions, text="Skip (XML only)", command=on_skip).pack(side="left", padx=(0, 6))
    ttk.Button(actions, text="Start download", command=on_start).pack(side="left")

    root.protocol("WM_DELETE_WINDOW", on_skip)
    root.mainloop()
    return result["value"]


# ----------------------------------------------------------------------------
# Phase 2: download media
# ----------------------------------------------------------------------------
def load_manifest():
    p = MEDIA_DIR / "manifest.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_manifest(manifest):
    MEDIA_DIR.mkdir(exist_ok=True)
    (MEDIA_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def manifest_complete(manifest, sid):
    paths = manifest.get(sid)
    return bool(paths) and all((MEDIA_DIR / p).exists() for p in paths)


async def fetch_bytes(page, url):
    r = await page.evaluate(FETCH_JS, url)
    return r["type"], base64.b64decode(r["data"])


def write_media(kind, base, mime, data, fallback_ext=""):
    folder = MEDIA_DIR / DOWNLOADABLE[kind][1]
    folder.mkdir(parents=True, exist_ok=True)
    name = base
    if not Path(base).suffix:
        name += ext_for(mime, fallback_ext)
    path = folder / name
    n = 1
    while path.exists():
        path = folder / f"{path.stem}_{n}{path.suffix}"
        n += 1
    path.write_bytes(data)
    return str(path.relative_to(MEDIA_DIR)).replace("\\", "/")


async def process_message(page, m, kind):
    """Download all media of one message. Returns list of relative paths."""
    sid = m["sid"]
    dt = ts_to_dt(m["ts"])
    stamp = dt.strftime("%Y%m%d_%H%M%S") if dt else "unknown"
    prefix = f"{stamp}_{safe_name(sid)}"
    saved = []

    if not await page.evaluate(PREP_JS, sid):
        return saved
    await asyncio.sleep(0.6)  # let lazy images/media attach
    refs = await page.evaluate(REFS_JS, sid)

    if kind == "photo":
        photos = sorted(refs["photos"], key=lambda p: -p["w"])
        # albums: one <img> per photo; dedupe identical urls
        seen = set()
        for i, p in enumerate(refs["photos"]):
            if p["url"] in seen:
                continue
            seen.add(p["url"])
            mime, data = await fetch_bytes(page, p["url"])
            saved.append(write_media("photo", f"{prefix}_{i}", mime, data, ".jpg"))

    elif kind == "sticker":
        for i, u in enumerate(dict.fromkeys(refs["stickers"])):
            mime, data = await fetch_bytes(page, u)
            saved.append(write_media("sticker", f"{prefix}_{i}", mime, data, ".webp"))

    elif kind in ("voice", "video"):
        tag = "audio" if kind == "voice" else "video"
        urls = refs["voices"] if kind == "voice" else refs["videos"]
        if not urls:
            u = await page.evaluate(PLAY_FALLBACK_JS, [sid, tag])
            if kind == "video":
                await page.keyboard.press("Escape")  # close viewer if it opened
            urls = [u] if u else []
        for i, u in enumerate(dict.fromkeys(urls)):
            mime, data = await fetch_bytes(page, u)
            saved.append(write_media(kind, f"{prefix}_{i}", mime, data,
                                     ".ogg" if kind == "voice" else ".mp4"))

    elif kind == "document":
        for i, d in enumerate(refs["docs"]):
            base = f"{prefix}_{safe_name(d['name'])}"
            url = d["url"]
            if url and not url.startswith("javascript"):
                try:
                    mime, data = await fetch_bytes(page, url)
                    saved.append(write_media("document", base, mime, data))
                    continue
                except Exception:
                    pass  # fall back to click-download
            async with page.expect_download(timeout=90000) as dl_info:
                await page.evaluate(CLICK_DOC_JS, [sid, i])
            dl = await dl_info.value
            folder = MEDIA_DIR / DOWNLOADABLE["document"][1]
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / (base if Path(base).suffix else base + Path(dl.suggested_filename).suffix)
            await dl.save_as(str(target))
            saved.append(str(target.relative_to(MEDIA_DIR)).replace("\\", "/"))

    return saved


async def dump_debug(page, sid):
    try:
        html = await page.evaluate(OUTER_HTML_JS, sid)
        d = MEDIA_DIR / "_debug"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{safe_name(sid)}.html").write_text(html, encoding="utf-8")
    except Exception:
        pass


async def download_pass(page, wanted, by_sid, manifest, stats, pause=0.7):
    """Scroll top->bottom once, downloading wanted messages as they appear in the DOM."""
    await page.evaluate(SCROLL_TOP_JS)
    await asyncio.sleep(pause * 2)

    handled = set()
    bottom_rounds = 0
    total = len(wanted)

    while bottom_rounds < 3:
        for sid in await page.evaluate(VISIBLE_SIDS_JS):
            if sid not in wanted or sid in handled:
                continue
            handled.add(sid)
            kind = wanted[sid]
            idx = len(handled)
            try:
                paths = await process_message(page, by_sid[sid], kind)
                if paths:
                    manifest[sid] = paths
                    save_manifest(manifest)
                    stats["ok"][kind] += 1
                    print(f"  [{idx}/{total}] {kind:<8} saved {len(paths)} file(s)           ")
                else:
                    stats["failed"][kind] += 1
                    print(f"  [{idx}/{total}] {kind:<8} NO MEDIA FOUND (debug saved)       ")
                    await dump_debug(page, sid)
            except Exception as ex:
                stats["failed"][kind] += 1
                print(f"  [{idx}/{total}] {kind:<8} FAILED: {str(ex)[:80]}")
                await dump_debug(page, sid)

        info = await page.evaluate(SCROLL_STEP_JS)
        await asyncio.sleep(pause)
        bottom_rounds = bottom_rounds + 1 if info["atBottom"] else 0

    return handled


async def download_media(page, messages, kinds, sender_filter, manifest):
    by_sid = {m["sid"]: m for m in messages}
    wanted = {}
    for m in messages:
        k = classify(m)
        if k not in kinds:
            continue
        if sender_filter == "me" and not m["isOut"]:
            continue
        if sender_filter == "other" and m["isOut"]:
            continue
        if manifest_complete(manifest, m["sid"]):
            continue
        wanted[m["sid"]] = k

    print(f"Items to download: {len(wanted)}")
    stats = {"ok": Counter(), "failed": Counter()}

    for round_no in (1, 2):
        if not wanted:
            break
        print(f"Download pass {round_no} ...")
        handled = await download_pass(page, wanted, by_sid, manifest, stats)
        # second pass only for messages we never saw in the DOM
        wanted = {s: k for s, k in wanted.items() if s not in handled}

    if wanted:
        print(f"Warning: {len(wanted)} item(s) were never visible in the DOM and were skipped.")

    print("\n=== Download summary ===")
    for kind in DOWNLOADABLE:
        ok, bad = stats["ok"][kind], stats["failed"][kind]
        if ok or bad:
            print(f"  {kind:<9}: ok {ok}, failed {bad}")
    if sum(stats["failed"].values()):
        print(f"Failed items: see {MEDIA_DIR / '_debug'} (message HTML dumps)")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx_kwargs = {"accept_downloads": True}
        if Path(STATE_FILE).exists():
            ctx_kwargs["storage_state"] = STATE_FILE
        ctx = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()
        await page.goto("https://web.bale.ai/")

        input("Log in, OPEN the target chat, then press Enter here... ")
        await ctx.storage_state(path=STATE_FILE)

        await page.wait_for_selector(SCROLLER_SEL)

        # --- Phase 1: scan whole chat ---
        print("Scrolling & collecting full history...")
        messages, date_map = await collect_all(page)
        messages.sort(key=lambda m: int(m["ts"] or 0))
        print(f"Extracted {len(messages)} messages")

        write_xml(messages, date_map)
        print(f"Saved -> {OUTPUT_FILE}")

        # --- Analysis + chooser ---
        analysis = analyse(messages)
        print_analysis(analysis)

        choice = ask_user_selection(analysis)
        if not choice:
            print("No media selected. Done.")
            await browser.close()
            return
        kinds, sender_filter = choice
        print(f"Selected: {', '.join(sorted(kinds))} | from: {sender_filter}")

        # --- Phase 2: download media ---
        MEDIA_DIR.mkdir(exist_ok=True)
        manifest = load_manifest()
        await download_media(page, messages, kinds, sender_filter, manifest)

        # --- Final XML with <file> paths ---
        write_xml(messages, date_map, files_map=manifest)
        print(f"Updated {OUTPUT_FILE} with file paths. Media folder: {MEDIA_DIR.resolve()}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
