"""
Bale Web chat exporter -> XML  (full-history version)
Usage:
  1) pip install playwright && playwright install chromium
  2) python bale_export.py
  3) In the opened browser: log in (OTP), open the target chat, press Enter in terminal.

Why this version exists:
  Bale Web uses a virtualized message list: only messages near the viewport stay
  in the DOM. So we must harvest messages at EVERY scroll step and accumulate
  them (keyed by data-sid), instead of extracting once at the end.
"""
import asyncio
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright

STATE_FILE = "bale_state.json"
OUTPUT_FILE = "bale_chat.xml"
SCROLLER_SEL = "#message_list_scroller_id"
MSG_SEL = '[aria-label="message-item"]'
DATE_DIVIDER_SEL = ".Wqgb2D"   # may change; fallback handled below

ME_NAME = "Ali"
OTHER_NAME = "Friend"

# JS runs in-page: extracts all currently-loaded messages.
EXTRACT_JS = r"""
() => {
  const root = document.querySelector('#message_list_scroller_id');
  const out = [];
  if (!root) return out;

  const msgs = root.querySelectorAll('[aria-label="message-item"]');
  msgs.forEach(el => {
    const sid = el.getAttribute('data-sid') || '';
    const ts  = el.getAttribute('data-date') || '';

    // Outgoing = has message-state-icon (sent/seen) OR the bubble sits on the "out" side
    const hasState = !!el.querySelector('[data-testid="message-state-icon"]');
    // Outgoing bubbles use flex-direction row-reverse ancestor; check computed style as fallback
    const block = el.querySelector('.message-block') || el;
    const cs = getComputedStyle(block);
    const isOut = hasState || (cs.flexDirection === 'row-reverse');

    // Text: collect paragraphs inside text containers, excluding reply-preview & forwarded header
    const textNodes = [];
    el.querySelectorAll('[data-sentry-component="NewTextContainerFC"]').forEach(t => {
      // skip text inside reply preview
      if (t.closest('[data-sentry-component="Preview"]')) return;
      const spans = t.querySelectorAll('span.p');
      if (spans.length) {
        spans.forEach(s => textNodes.push(s.innerText.trim()));
      } else {
        textNodes.push(t.innerText.trim());
      }
    });
    // Receipt-style bubbles
    el.querySelectorAll('.h3Qw2Y').forEach(t => textNodes.push(t.innerText.trim()));

    // Reply preview text
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

    // Forwarded
    let fwd = null;
    const f = el.querySelector('[data-sentry-component="ForwardedFC"]');
    if (f) {
      const b = f.querySelector('bdi');
      fwd = b ? b.innerText.trim() : f.innerText.trim();
    }

    // Time label shown in bubble
    const tEl = el.querySelector('p.x3ai0M');
    const timeLabel = tEl ? tEl.innerText.trim() : '';

    // Attachments (documents)
    const docs = [];
    el.querySelectorAll('a[data-prevent-context-menu-mobile]').forEach(a => {
      const name = a.querySelector('p[dir="auto"]');
      const size = a.querySelectorAll('p')[1];
      docs.push({
        name: name ? name.innerText.trim() : '',
        size: size ? size.innerText.trim() : ''
      });
    });

    // Other media kinds
    const hasPhoto   = !!el.querySelector('[data-sentry-component="PhotoFC"], [data-sentry-component="AlbumFC"]');
    const hasVideo   = !!el.querySelector('[data-sentry-component="Video"]');
    const hasSticker = !!el.querySelector('[data-testid="sticker-message"]');
    const hasVoice   = !!el.querySelector('[data-sentry-component="NewAudioProgressbar"]');
    const isCall     = !!el.querySelector('[aria-label="Callout-icon"]');

    // Reactions (emoji alts)
    const reactions = [...el.querySelectorAll('.TevIIa img.emoji')].map(i => i.alt);

    // Replace emoji <img> with alt text in text nodes (innerText drops imgs)
    // -> do it by re-reading with alt substitution
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

# JS: map each message sid -> nearest preceding date divider text
DATE_MAP_JS = r"""
() => {
  const root = document.querySelector('#message_list_scroller_id');
  const map = {};
  if (!root) return map;
  let current = '';
  // walk all descendants in document order
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


def fmt_ts(ms):
    """Format a ms timestamp for progress logging."""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


async def collect_all(page, pause=1.0, max_stable=10):
    """
    Scroll upward step by step and harvest messages at every step,
    because the list is virtualized (old messages leave the DOM).
    Stops when no new unique messages appear for `max_stable` rounds in a row.
    """
    collected = {}   # sid -> message dict
    date_map = {}    # sid -> date label
    stable = 0
    last_total = -1

    while stable < max_stable:
        # 1) harvest what's currently in the DOM
        msgs = await page.evaluate(EXTRACT_JS)
        dmap = await page.evaluate(DATE_MAP_JS)
        for m in msgs:
            if m["sid"] and m["sid"] not in collected:
                collected[m["sid"]] = m
        for sid, label in dmap.items():
            # keep the first non-empty label we see for each sid
            if label and not date_map.get(sid):
                date_map[sid] = label

        # 2) scroll up: jump to top to trigger loading of older messages
        await page.evaluate(f"""
            (() => {{
              const s = document.querySelector('{SCROLLER_SEL}');
              s.scrollTop = 0;
            }})()
        """)
        await asyncio.sleep(pause)

        # small nudge down/up: some apps only load on a scroll *event*
        await page.evaluate(f"""
            (() => {{
              const s = document.querySelector('{SCROLLER_SEL}');
              s.scrollTop = 40;
              s.dispatchEvent(new Event('scroll'));
              s.scrollTop = 0;
              s.dispatchEvent(new Event('scroll'));
            }})()
        """)
        await asyncio.sleep(pause)

        total = len(collected)
        if total:
            stamps = [int(m["ts"]) for m in collected.values() if str(m["ts"]).isdigit()]
            oldest = fmt_ts(min(stamps)) if stamps else "?"
            newest = fmt_ts(max(stamps)) if stamps else "?"
        else:
            oldest = newest = "?"
        print(f"  unique messages: {total} | oldest: {oldest} | newest: {newest}   ",
              end="\r")

        if total == last_total:
            stable += 1
        else:
            stable = 0
            last_total = total

    print()
    # one last harvest after the final scroll
    msgs = await page.evaluate(EXTRACT_JS)
    dmap = await page.evaluate(DATE_MAP_JS)
    for m in msgs:
        if m["sid"] and m["sid"] not in collected:
            collected[m["sid"]] = m
    for sid, label in dmap.items():
        if label and not date_map.get(sid):
            date_map[sid] = label

    return list(collected.values()), date_map


def sanitize(s: str) -> str:
    # remove characters illegal in XML 1.0
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s or "")


def build_xml(messages, date_map, me="me", other="other"):
    root = ET.Element("chat", {"source": "bale-web",
                               "exported_at": datetime.now(timezone.utc).isoformat()})
    seen = set()
    for m in messages:
        if m["sid"] in seen:
            continue
        seen.add(m["sid"])

        try:
            dt = datetime.fromtimestamp(int(m["ts"]) / 1000, tz=timezone.utc)
            iso = dt.isoformat()
        except Exception:
            iso = ""

        # message type
        if m["media"]["call"]:      mtype = "call"
        elif m["media"]["voice"]:   mtype = "voice"
        elif m["media"]["sticker"]: mtype = "sticker"
        elif m["media"]["video"]:   mtype = "video"
        elif m["media"]["photo"]:   mtype = "photo"
        elif m["docs"]:             mtype = "document"
        else:                       mtype = "text"

        e = ET.SubElement(root, "message", {
            "id": sanitize(m["sid"]),
            "sender": me if m["isOut"] else other,
            "timestamp": iso,
            "time_label": sanitize(m["timeLabel"]),
            "date_label": sanitize(date_map.get(m["sid"], "")),
            "type": mtype,
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
        if m["reactions"]:
            ET.SubElement(e, "reactions").text = sanitize(" ".join(m["reactions"]))

    ET.indent(root, space="  ")
    return ET.ElementTree(root)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx_kwargs = {}
        if Path(STATE_FILE).exists():
            ctx_kwargs["storage_state"] = STATE_FILE
        ctx = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()
        await page.goto("https://web.bale.ai/")

        input("Log in, OPEN the target chat, then press Enter here... ")
        await ctx.storage_state(path=STATE_FILE)

        await page.wait_for_selector(SCROLLER_SEL)
        print("Scrolling & collecting full history...")
        messages, date_map = await collect_all(page)
        print(f"Extracted {len(messages)} messages")

        # sort by timestamp (safety)
        messages.sort(key=lambda m: int(m["ts"] or 0))

        tree = build_xml(messages, date_map, me=ME_NAME, other=OTHER_NAME)
        tree.write(OUTPUT_FILE, encoding="utf-8", xml_declaration=True)
        print(f"Saved -> {OUTPUT_FILE}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
