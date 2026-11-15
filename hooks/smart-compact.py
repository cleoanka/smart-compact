#!/usr/bin/env python3
"""smart-compact: pin-aware, discard-aware compaction for Claude Code.

Hook modes (stdin = hook input JSON):
  pre     PreCompact           collect pins and discard marks; print the summary instructions
  start   SessionStart         (source=compact) re-inject pins verbatim
  post    PostCompact          log, save the generated summary, prune old state
  expand  UserPromptExpansion  the slash commands below, handled without a model call
  stop    Stop                 smart auto-compact: trigger when context passes the smart window

Marks, read from the session transcript at compaction time:
  "◆ <text>"   pin      carried verbatim through EVERY compaction (native or smart)
  "◇ <text>"   discard  the line and what it names are left out of the next summary
  "◆✗ <frag>"  unpin    retire an earlier pin
A mark is a line of a reply or of a user message that starts with the symbol.
User messages also accept "!pin <text>", "📌 <text>" and "!drop <text>".

Commands (zero tokens): /pin /unpin /drop /undrop /pins /smartcompact /smartautocompact

The smart summary policy applies only to smart compactions:
  /smartcompact  (= "/compact smart[: extra]")   or   auto-compaction while smart auto is on.
A plain /compact and native auto-compaction keep Claude Code's own summary behaviour; pins and
discard marks still apply to them.

Never blocks or breaks compaction: every failure exits 0.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
STATE = os.path.join(CONFIG_DIR, "smart-compact")
PIN_DIR = os.path.join(STATE, "pins")
MAN_DIR = os.path.join(STATE, "manual")
SUM_DIR = os.path.join(STATE, "summaries")
RUN_DIR = os.path.join(STATE, "run")
CFG_DIR = os.path.join(STATE, "cfg")
LOG = os.path.join(STATE, "log.jsonl")
SETTINGS = os.path.join(CONFIG_DIR, "settings.json")

PIN, DISCARD, UNPIN = "◆", "◇", "◆✗"
PIN_CHAR_BUDGET = 8000          # SessionStart additionalContext is capped at 10,000 chars
DISCARD_CHAR_BUDGET = 3000      # PreCompact stdout is capped at 10,000 chars; the policy needs room
DISCARD_LINE_MAX = 300
OVERFLOW_CHAR_BUDGET = 4000      # pins that do not fit PIN_CHAR_BUDGET are handed to the summarizer
DEFAULT_NATIVE_WINDOW = 967_000  # documented default for 1M-context models
NO_1M_WINDOW = 200_000
DEFAULT_SMART_WINDOW = 300_000
MIN_WINDOW = 100_000            # same floor as Claude Code's own auto-compact window
RETRIGGER_SECONDS = 180
STATE_MAX_AGE_DAYS = 30
LOG_MAX_BYTES = 1_000_000

POLICY = """SMART-COMPACT POLICY (write the summary in the conversation's language).
Goal anchor: the user's most recent requests and the open tasks define what matters now.
Judge every item with one question: "If this is dropped, will continuing the work cause a mistake or repeated work?"

Three tiers:
1. KEEP (detailed): decisions with their reasons, verified facts and root causes, user constraints and corrections, the current state of the work and next steps, exact identifiers (paths, IDs, commands that worked) that the current goal still needs.
2. POINTER (one line each): anything plausibly useful but not clearly needed right now: files created or edited, scripts written, side investigations, rejected approaches with a few-word reason. Format: "- <what> -> <where: path or 'transcript'>". When unsure between KEEP and DROP, choose POINTER, never DROP.
3. DROP: raw tool output, file contents, search results, attempts superseded by later ones, chit-chat.

Pinned items (lines starting with "◆") are re-injected verbatim right after compaction; do not copy them into the summary.
The hook tracks pins itself, including changes made in the latest messages that you cannot see: do not judge which pins are still valid, and do not ask for a pin to be retired or rewritten.
The full pre-compaction transcript stays on disk; anything dropped can be recovered from it.
"""

OVERFLOW_HEAD = """PIN OVERFLOW (smart-compact). The pinned lines below do not fit the space the hook has for re-injecting pins. Copy each of them into the summary word for word, under a heading "Pinned (overflow)", keeping the leading "◆".
"""

COMMAND_SIGNATURE = "The smart-compact hook should have handled"  # first words of our command files
HARNESS_TAGS = ("<system-reminder>", "<command-", "<local-command", "<task-notification>",
                "<bash-input>", "<bash-stdout>", "<bash-stderr>")

DISCARD_HEAD = """DISCARD LIST (smart-compact). Each line below was marked with "◇" by the user or the assistant as certainly worthless from that point on. Leave these lines, and the attempts, output or files they name, out of the summary completely: no mention, no pointer, no "rejected approach" entry. This overrides every other instruction about what to keep, except that a user constraint or a pinned ("◆") item is never discarded. Write the summary as if these items had never been part of the conversation: it must not report that anything was discarded, and it must not mention or quote this list.
"""


# ---------- messages ----------

MESSAGES = {
    "en": {
        "starting": "smart-compact: starting smart compaction…",
        "cannot_type": "smart-compact: cannot type into this terminal (not tmux or iTerm2). Run it yourself: {cmd}",
        "auto_usage": "smart-compact: not understood. Usage: /smartautocompact [on|off|300k|40%|status]",
        "floor": " (floor is {n})",
        "clamped": " (native auto-compact fires at {n}; the smart limit cannot exceed it, so it was set equal)",
        "auto_line": "smart auto-compact (this session only): {state}, limit {n}{note}",
        "on": "ON", "off": "OFF",
        "native_line": "native auto-compact: {state}, limit {n}",
        "native_on": "on", "native_off": "off",
        "ctx_line": "current context: {n}",
        "ctx_unknown": "current context: not measured yet",
        "no_inject_note": "note: this terminal is not tmux or iTerm2, so the smart limit cannot trigger a compaction; "
                          "the smart policy is applied when native auto-compaction runs.",
        "thrash_note": "note: stopped for this session, because the context was still above the limit right after "
                       "a compaction. Set the limit again to re-arm it.",
        "pin_usage": "smart-compact: usage: /pin <one line to keep>",
        "pinned": "◆ pinned ({n} pins): {text}",
        "drop_usage": "smart-compact: usage: /drop <one line naming what is worthless>",
        "dropped": "◇ marked for discard at the next compaction ({n} marks): {text}",
        "none": "smart-compact: no pins or discard marks in this session. Add with /pin <text> or /drop <text>.",
        "pins_head": "smart-compact: {n} pins (kept through every compaction)",
        "drops_head": "smart-compact: {n} discard marks (removed at the next compaction)",
        "pins_tail": "remove with: /unpin <number or fragment>   /undrop <number or fragment>",
        "unpin_usage": "smart-compact: usage: /unpin <number or fragment>",
        "undrop_usage": "smart-compact: usage: /undrop <number or fragment>",
        "no_number": "smart-compact: there is no item {i} ({n} listed). See /pins.",
        "no_match": "smart-compact: nothing contains '{a}'. See /pins.",
        "ambiguous": "smart-compact: several items match; be more specific or give a number:",
        "unpinned": "smart-compact: pin removed: {text}",
        "undropped": "smart-compact: discard mark removed: {text}",
        "error": "smart-compact: error: {e}",
    },
    "tr": {
        "starting": "smart-compact: akıllı compact başlatılıyor…",
        "cannot_type": "smart-compact: bu terminale komut yazamıyorum (tmux ya da iTerm2 değil). Elle çalıştır: {cmd}",
        "auto_usage": "smart-compact: anlaşılmadı. Kullanım: /smartautocompact [on|off|300k|40%|durum]",
        "floor": " (alt sınır {n})",
        "clamped": " (normal auto-compact sınırı {n}; akıllı sınır onun üstüne çıkamaz, eşitlendi)",
        "auto_line": "smart auto-compact (yalnız bu oturum): {state}, sınır {n}{note}",
        "on": "AÇIK", "off": "KAPALI",
        "native_line": "normal auto-compact: {state}, sınır {n}",
        "native_on": "açık", "native_off": "kapalı",
        "ctx_line": "şu anki context: {n}",
        "ctx_unknown": "şu anki context: henüz ölçülemedi",
        "no_inject_note": "not: bu terminal tmux ya da iTerm2 değil; akıllı sınırda compact tetikleyemem, "
                          "normal auto-compact çalıştığında akıllı politika uygulanır.",
        "thrash_note": "not: bu oturumda durdu, çünkü compact'tan hemen sonra context hâlâ sınırın üstündeydi. "
                       "Yeniden kurmak için sınırı tekrar ayarla.",
        "pin_usage": "smart-compact: kullanım: /pin <kalıcı olacak tek satır>",
        "pinned": "◆ pinlendi ({n} pin): {text}",
        "drop_usage": "smart-compact: kullanım: /drop <işe yaramayan şeyi adlandıran tek satır>",
        "dropped": "◇ sıradaki compact'ta silinecek ({n} işaret): {text}",
        "none": "smart-compact: bu oturumda pin ya da silme işareti yok. /pin <metin> ya da /drop <metin> ile ekle.",
        "pins_head": "smart-compact: {n} pin (her compact'tan sağ çıkar)",
        "drops_head": "smart-compact: {n} silme işareti (sıradaki compact'ta atılır)",
        "pins_tail": "kaldırmak için: /unpin <numara ya da parça>   /undrop <numara ya da parça>",
        "unpin_usage": "smart-compact: kullanım: /unpin <numara ya da metinden bir parça>",
        "undrop_usage": "smart-compact: kullanım: /undrop <numara ya da metinden bir parça>",
        "no_number": "smart-compact: {i} numaralı öğe yok ({n} öğe var). /pins ile bak.",
        "no_match": "smart-compact: '{a}' içeren öğe yok. /pins ile bak.",
        "ambiguous": "smart-compact: birden çok öğe eşleşti, daha ayırt edici yaz ya da numara ver:",
        "unpinned": "smart-compact: pin kaldırıldı: {text}",
        "undropped": "smart-compact: silme işareti kaldırıldı: {text}",
        "error": "smart-compact: hata: {e}",
    },
}


def language() -> str:
    """SMART_COMPACT_LANG, then <state>/lang (written by the installer), then the locale."""
    cand = os.environ.get("SMART_COMPACT_LANG", "")
    if not cand:
        try:
            with open(os.path.join(STATE, "lang"), encoding="utf-8") as f:
                cand = f.read().strip()
        except OSError:
            cand = ""
    if not cand:
        cand = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    code = cand.lower()[:2]
    return code if code in MESSAGES else "en"


def msg(key: str, **kw) -> str:
    return MESSAGES[language()][key].format(**kw)


# ---------- small helpers ----------

def log(event: dict) -> None:
    try:
        os.makedirs(STATE, exist_ok=True)
        event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def session_key(data: dict) -> str:
    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(data.get("session_id") or "unknown"))
    aid = data.get("agent_id")
    return f"{sid}--{re.sub(r'[^A-Za-z0-9_.-]', '_', str(aid))}" if aid else sid


def fmt_tokens(n: int) -> str:
    return f"{n / 1000:.0f}k" if n < 1_000_000 else f"{n / 1_000_000:.2f}M"


def block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))


def prune_state() -> None:
    """Delete per-session state older than STATE_MAX_AGE_DAYS and keep the log bounded."""
    cutoff = time.time() - STATE_MAX_AGE_DAYS * 86400
    for d in (PIN_DIR, MAN_DIR, SUM_DIR, RUN_DIR, CFG_DIR):
        try:
            for name in os.listdir(d):
                p = os.path.join(d, name)
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
        except OSError:
            pass
    try:
        if os.path.getsize(LOG) > LOG_MAX_BYTES:
            with open(LOG, encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-2000:]
            with open(LOG + ".tmp", "w", encoding="utf-8") as f:
                f.writelines(tail)
            os.replace(LOG + ".tmp", LOG)
    except OSError:
        pass


# ---------- config and native window ----------

def config_path(data: dict) -> str:
    # per session: a limit set in one session must not affect the others
    return os.path.join(CFG_DIR, session_key({"session_id": data.get("session_id")}) + ".json")


def load_config(data: dict) -> dict:
    cfg = read_json(config_path(data), {})
    try:
        window = int(cfg.get("window", DEFAULT_SMART_WINDOW))
    except (TypeError, ValueError):
        window = DEFAULT_SMART_WINDOW
    return {"auto": bool(cfg.get("auto", False)), "window": window}


def parse_size(text: str):
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*([kKmM]?)", text.strip())
    if not m:
        return None
    val = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    if unit == "m":
        val *= 1_000_000
    elif unit == "k" or (unit == "" and val < 5000):  # "300" means 300k, like /autocompact
        val *= 1000
    return int(val)


def truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def native_auto(model: str):
    """(enabled, window) of Claude Code's own auto-compaction, as far as user settings and env show it."""
    s = read_json(SETTINGS, {})
    if not isinstance(s, dict):
        s = {}
    env = {**(s.get("env") or {}), **os.environ}
    enabled = s.get("autoCompactEnabled") is not False
    if truthy(env.get("DISABLE_AUTO_COMPACT")) or truthy(env.get("DISABLE_COMPACT")):
        enabled = False
    window = None
    for cand in (env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"),
                 ((s.get("modelSettings") or {}).get(model) or {}).get("autoCompactWindow"),
                 s.get("autoCompactWindow")):
        if cand in (None, "", "auto"):
            continue
        size = cand if isinstance(cand, int) else parse_size(str(cand))
        if size:
            window = size
            break
    window = window or DEFAULT_NATIVE_WINDOW
    if truthy(env.get("CLAUDE_CODE_DISABLE_1M_CONTEXT")):
        window = min(window, NO_1M_WINDOW)
    return enabled, window


# ---------- transcript reading ----------

def texts_of(entry: dict):
    """Yield (role, text) for human-readable text blocks only."""
    etype = entry.get("type")
    if etype not in ("user", "assistant"):
        return
    if entry.get("isMeta") or entry.get("isCompactSummary") or entry.get("isSidechain"):
        return
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        blocks = content
    else:
        return
    for b in blocks:
        if not isinstance(b, dict) or b.get("type") != "text":
            continue  # skips tool_use, tool_result, thinking, images
        t = b.get("text") or ""
        if etype == "user" and t.lstrip().startswith(HARNESS_TAGS):
            continue  # harness records, including the output of "!" shell commands
        yield etype, t


def marks_in(role: str, text: str):
    """Yield (kind, body) for every mark line in one text block. Fenced code is not scanned."""
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        s = line.strip()
        if s.startswith(">"):
            continue  # quoted text is someone else's words, never a mark
        s = s.lstrip("*-_ ").strip()
        if s.startswith(UNPIN):
            kind, body = "unpin", s[len(UNPIN):]
        elif s.startswith(PIN):
            kind, body = "pin", s[len(PIN):]
        elif s.startswith(DISCARD):
            kind, body = "drop", s[len(DISCARD):]
        elif role == "user" and s.startswith("!pin "):
            kind, body = "pin", s[5:]
        elif role == "user" and s.startswith("📌"):
            kind, body = "pin", s[1:]
        elif role == "user" and s.startswith("!drop "):
            kind, body = "drop", s[6:]
        else:
            continue
        body = body.strip().strip("*_").strip()
        if body:
            yield kind, body


def transcript_events(transcript_path: str):
    """(events, boundary): ordered (timestamp, kind, text) marks, and the last compaction's timestamp."""
    events, boundary = [], ""
    if not transcript_path or not os.path.isfile(transcript_path):
        return events, boundary
    needles = (PIN, DISCARD, "!pin", "!drop", "📌", "compact_boundary")
    with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            if not any(n in raw for n in needles):
                continue  # fast path
            try:
                entry = json.loads(raw)
            except Exception:
                continue
            if not isinstance(entry, dict):
                continue
            ts = entry.get("timestamp") or ""
            if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
                boundary = max(boundary, ts)
                continue
            for role, text in texts_of(entry):
                for kind, body in marks_in(role, text):
                    events.append((ts, kind, body))
    return events, boundary


def manual_path(data: dict) -> str:
    return os.path.join(MAN_DIR, session_key(data) + ".json")


def effective_marks(data: dict, transcript_path: str):
    """(pins, drops): live pins, and discard marks made since the last compaction, both chronological."""
    events, boundary = transcript_events(transcript_path)
    manual = read_json(manual_path(data), [])
    for ev in manual if isinstance(manual, list) else []:
        if isinstance(ev, dict):
            events.append((ev.get("ts", ""), ev.get("kind", "pin"), ev.get("text", "")))
    events.sort(key=lambda e: e[0])  # ISO-8601 UTC strings sort chronologically; the sort is stable
    pins, drops = [], []
    for ts, kind, text in events:
        if not text:
            continue
        if kind == "unpin":
            low = text.lower()
            pins = [p for p in pins if low not in p.lower()]
        elif kind == "unpin-exact":  # from /unpin, which names one pin
            pins = [p for p in pins if p != text]
        elif kind == "pin":
            if text not in pins:
                pins.append(text)
        elif kind == "drop":
            if ts > boundary and text not in drops:  # older marks were consumed by that compaction
                drops.append(text)
        elif kind == "undrop":
            drops = [d for d in drops if d != text]
    return pins, drops


def add_manual(data: dict, kind: str, text: str) -> None:
    path = manual_path(data)
    items = read_json(path, [])
    if not isinstance(items, list):
        items = []
    items.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()), "kind": kind, "text": text})
    write_json(path, items)


def context_usage(transcript_path: str):
    """(tokens, model) of the last main-thread assistant message."""
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            f.seek(max(0, size - 4_000_000))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
        for raw in reversed(lines):
            if '"usage"' not in raw:
                continue
            try:
                e = json.loads(raw)
            except Exception:
                continue
            if not isinstance(e, dict) or e.get("type") != "assistant" or e.get("isSidechain"):
                continue
            m = e.get("message") or {}
            u = m.get("usage") or {}
            total = sum(int(u.get(k) or 0) for k in
                        ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))
            if total:
                return total, m.get("model") or ""
    except Exception:
        pass
    return 0, ""


# ---------- typing a command into this terminal ----------

def find_tmux():
    found = shutil.which("tmux")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"):
        if os.path.exists(cand):
            return cand
    return None


def can_inject() -> bool:
    if os.environ.get("TMUX_PANE") and find_tmux():
        return True
    return ":" in os.environ.get("ITERM_SESSION_ID", "") and os.path.exists("/usr/bin/osascript")


def inject(text: str, delay: float) -> bool:
    """Type `text` + Enter into the terminal this Claude Code runs in, after `delay` seconds."""
    pane = os.environ.get("TMUX_PANE")
    iterm = os.environ.get("ITERM_SESSION_ID", "")
    tmux = find_tmux() if pane else None
    if pane and tmux:
        script = 'sleep "$1"; "$2" send-keys -t "$3" -l -- "$4"; sleep 0.3; "$2" send-keys -t "$3" Enter'
        args = ["/bin/sh", "-c", script, "sh", str(delay), tmux, pane, text]
    elif ":" in iterm and os.path.exists("/usr/bin/osascript"):
        osa = ('on run argv\n'
               ' set sid to item 1 of argv\n'
               ' tell application "iTerm2"\n'
               '  repeat with w in windows\n'
               '   repeat with t in tabs of w\n'
               '    repeat with s in sessions of t\n'
               '     if (id of s as string) is sid then\n'
               '      tell s to write text (item 2 of argv)\n'
               '      return\n'
               '     end if\n'
               '    end repeat\n'
               '   end repeat\n'
               '  end repeat\n'
               ' end tell\n'
               'end run')
        script = 'sleep "$1"; /usr/bin/osascript -e "$2" "$3" "$4"'
        args = ["/bin/sh", "-c", script, "sh", str(delay), osa, iterm.split(":", 1)[1], text]
    else:
        return False
    try:
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except Exception as e:
        log({"ev": "inject-error", "err": repr(e)})
        return False


# ---------- hook modes ----------

def is_smart(data: dict) -> bool:
    if data.get("trigger") == "manual":
        return bool(re.match(r"smart(?:\s*:|\s|$)", (data.get("custom_instructions") or "").strip().lower()))
    return load_config(data)["auto"]


def discard_block(drops) -> str:
    lines, used = [], 0
    for d in reversed(drops):  # newest first under the budget
        d = " ".join(d.split())
        if len(d) > DISCARD_LINE_MAX:
            d = d[:DISCARD_LINE_MAX - 1] + "…"
        if used + len(d) + 3 > DISCARD_CHAR_BUDGET:
            break
        lines.append("- " + d)
        used += len(d) + 3
    lines.reverse()
    return DISCARD_HEAD + "\n".join(lines) + "\n" if lines else ""


def split_pins(pins):
    """(fit, overflow): the newest pins that fit the re-injection budget, and the older rest."""
    fit, used = [], 0
    for i in range(len(pins) - 1, -1, -1):
        cost = len(pins[i]) + 3
        if used + cost > PIN_CHAR_BUDGET:
            return pins[i + 1:], pins[:i + 1]
        used += cost
    return pins, []


def overflow_block(overflow) -> str:
    lines, used = [], 0
    for p in reversed(overflow):
        if used + len(p) + 3 > OVERFLOW_CHAR_BUDGET:
            break
        lines.append(PIN + " " + p)
        used += len(p) + 3
    lines.reverse()
    return OVERFLOW_HEAD + "\n".join(lines) + "\n" if lines else ""


def mode_pre(data: dict) -> None:
    tp = data.get("transcript_path") or ""
    smart, drops, overflow = False, [], []
    try:
        smart = is_smart(data)
        pins, drops = effective_marks(data, tp)
        pins, overflow = split_pins(pins)
        write_json(os.path.join(PIN_DIR, session_key(data) + ".json"),
                   {"transcript_path": tp, "pins": pins, "overflow": len(overflow), "drops": drops})
        tokens, _ = context_usage(tp)
        log({"ev": "pre", "key": session_key(data), "trigger": data.get("trigger"), "smart": smart,
             "pins": len(pins), "overflow": len(overflow), "drops": len(drops), "context_tokens": tokens})
    except Exception as e:  # never break compaction
        log({"ev": "pre-error", "err": repr(e)})
    out = "\n".join(b for b in (POLICY if smart else "", discard_block(drops), overflow_block(overflow)) if b)
    if out:
        sys.stdout.write(out)


def mode_start(data: dict) -> None:
    if data.get("source") != "compact":
        return
    try:
        saved = read_json(os.path.join(PIN_DIR, session_key(data) + ".json"), None)
        if not isinstance(saved, dict):
            return
        pins = saved.get("pins") or []
        tp = saved.get("transcript_path") or data.get("transcript_path") or ""
        if not pins:
            return  # nothing to carry; stay silent, cost nothing
        if not isinstance(pins, list):
            return
        omitted = int(saved.get("overflow") or 0)
        lines = [PIN + " " + p for p in pins]
        head = ["SMART-COMPACT: pinned items carried over verbatim (chronological; on conflict the later one wins)."]
        if omitted:
            head.append(f"({omitted} older pins did not fit here; the summary carries them under 'Pinned (overflow)'.)")
        tail = [f"Full pre-compaction transcript (JSONL, grep it when a summary pointer needs detail): {tp}"]
        ctx = "\n".join(head + lines + tail)
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}},
                         ensure_ascii=False))
        log({"ev": "start", "key": session_key(data), "pins": len(lines), "omitted": omitted, "chars": len(ctx)})
    except Exception as e:
        log({"ev": "start-error", "err": repr(e)})


def mode_post(data: dict) -> None:
    try:
        summary = data.get("compact_summary") or ""
        os.makedirs(SUM_DIR, exist_ok=True)
        name = f"{session_key(data)}-{time.strftime('%Y%m%d-%H%M%S')}.md"
        with open(os.path.join(SUM_DIR, name), "w", encoding="utf-8") as f:
            f.write(summary)
        if load_config(data)["auto"]:  # lets the Stop hook see whether this compaction got under the limit
            os.makedirs(RUN_DIR, exist_ok=True)
            with open(os.path.join(RUN_DIR, session_key(data) + ".compacted"), "w") as f:
                f.write("1")
        saved = read_json(os.path.join(PIN_DIR, session_key(data) + ".json"), {})
        drops = saved.get("drops") or [] if isinstance(saved, dict) else []
        low = summary.lower()
        leaks = sum(1 for d in drops if d[:80].lower() in low)  # a marked line quoted back into the summary
        log({"ev": "post", "key": session_key(data), "trigger": data.get("trigger"),
             "summary_chars": len(summary), "drops": len(drops), "drop_leaks": leaks, "file": name})
        prune_state()
    except Exception as e:
        log({"ev": "post-error", "err": repr(e)})


# ---------- slash commands ----------

def cmd_smartcompact(data: dict, args: str) -> None:
    focus = one_line(args)
    text = "/compact smart" + (f": {focus}" if focus else "")
    if inject(text, 0.6):
        block(msg("starting"))
    else:
        block(msg("cannot_type", cmd=text))


def cmd_smartauto(data: dict, args: str) -> None:
    cfg = load_config(data)
    tokens, model = context_usage(data.get("transcript_path") or "")
    n_on, n_win = native_auto(model)
    a = args.strip().lower()
    note = ""
    if a in ("on", "aç", "ac"):
        cfg["auto"] = True
    elif a in ("off", "kapat"):
        cfg["auto"] = False
    elif a not in ("", "status", "durum"):
        size = None
        if a.endswith("%"):
            try:
                size = int(n_win * float(a[:-1].replace(",", ".")) / 100)
            except ValueError:
                size = None
        else:
            size = parse_size(a)
        if not size:
            block(msg("auto_usage"))
            return
        if size < MIN_WINDOW:
            size, note = MIN_WINDOW, msg("floor", n=fmt_tokens(MIN_WINDOW))
        cfg["window"] = size
        cfg["auto"] = True
    if n_on and cfg["window"] > n_win:
        cfg["window"] = n_win
        note = msg("clamped", n=fmt_tokens(n_win))
    write_json(config_path(data), cfg)
    if a not in ("", "status", "durum"):  # a new decision clears an earlier "compacting does not help" stop
        for suffix in (".thrash", ".compacted", ".trigger"):
            try:
                os.remove(os.path.join(RUN_DIR, session_key(data) + suffix))
            except OSError:
                pass
    stopped = os.path.exists(os.path.join(RUN_DIR, session_key(data) + ".thrash"))
    lines = [
        msg("auto_line", state=msg("on" if cfg["auto"] else "off"), n=fmt_tokens(cfg["window"]), note=note),
        msg("native_line", state=msg("native_on" if n_on else "native_off"), n=fmt_tokens(n_win)),
        msg("ctx_line", n=fmt_tokens(tokens)) if tokens else msg("ctx_unknown"),
    ]
    if cfg["auto"] and stopped:
        lines.append(msg("thrash_note"))
    if cfg["auto"] and not can_inject():
        lines.append(msg("no_inject_note"))
    block("\n".join(lines))


def one_line(args: str) -> str:
    return " ".join(args.split())


def cmd_pin(data: dict, args: str) -> None:
    text = one_line(args)
    if not text:
        block(msg("pin_usage"))
        return
    add_manual(data, "pin", text)
    pins, _ = effective_marks(data, data.get("transcript_path") or "")
    block(msg("pinned", n=len(pins), text=text))


def cmd_drop(data: dict, args: str) -> None:
    text = one_line(args)
    if not text:
        block(msg("drop_usage"))
        return
    add_manual(data, "drop", text)
    _, drops = effective_marks(data, data.get("transcript_path") or "")
    block(msg("dropped", n=len(drops), text=text))


def cmd_pins(data: dict, args: str) -> None:
    pins, drops = effective_marks(data, data.get("transcript_path") or "")
    if not pins and not drops:
        block(msg("none"))
        return
    out = []
    if pins:
        out += [msg("pins_head", n=len(pins))] + [f"{i}. {PIN} {p}" for i, p in enumerate(pins, 1)]
    if drops:
        out += [msg("drops_head", n=len(drops))] + [f"{i}. {DISCARD} {d}" for i, d in enumerate(drops, 1)]
    block("\n".join(out + [msg("pins_tail")]))


def pick(items, a: str):
    """Resolve a number or a fragment to one item; block with a message and return None otherwise."""
    if a.isdigit():
        i = int(a)
        if not 1 <= i <= len(items):
            block(msg("no_number", i=i, n=len(items)))
            return None
        return items[i - 1]
    targets = [p for p in items if a.lower() in p.lower()]
    if not targets:
        block(msg("no_match", a=a))
        return None
    if len(targets) > 1:
        block(msg("ambiguous") + "\n" + "\n".join(f"- {t}" for t in targets))
        return None
    return targets[0]


def cmd_unpin(data: dict, args: str) -> None:
    a = args.strip()
    if not a:
        block(msg("unpin_usage"))
        return
    pins, _ = effective_marks(data, data.get("transcript_path") or "")
    target = pick(pins, a)
    if target is not None:
        add_manual(data, "unpin-exact", target)
        block(msg("unpinned", text=target))


def cmd_undrop(data: dict, args: str) -> None:
    a = args.strip()
    if not a:
        block(msg("undrop_usage"))
        return
    _, drops = effective_marks(data, data.get("transcript_path") or "")
    target = pick(drops, a)
    if target is not None:
        add_manual(data, "undrop", target)
        block(msg("undropped", text=target))


COMMANDS = {"smartcompact": cmd_smartcompact, "smartautocompact": cmd_smartauto,
            "pin": cmd_pin, "pins": cmd_pins, "unpin": cmd_unpin,
            "drop": cmd_drop, "undrop": cmd_undrop}


def owns_command(name: str) -> bool:
    """True when the user-level command file is the one our installer wrote."""
    try:
        with open(os.path.join(CONFIG_DIR, "commands", name + ".md"), encoding="utf-8") as f:
            return COMMAND_SIGNATURE in f.read(2000)
    except OSError:
        return False


def mode_expand(data: dict) -> None:
    name = data.get("command_name") or ""
    fn = COMMANDS.get(name)  # exact names only: a plugin's "x:pin" is not ours
    if not fn or not owns_command(name):
        return
    try:
        fn(data, data.get("command_args") or "")
    except Exception as e:
        log({"ev": "expand-error", "cmd": name, "err": repr(e)})
        block(msg("error", e=repr(e)))


def mode_stop(data: dict) -> None:
    try:
        cfg = load_config(data)
        if not cfg["auto"] or data.get("agent_id") or not can_inject():
            return
        tokens, model = context_usage(data.get("transcript_path") or "")
        n_on, n_win = native_auto(model)
        window = min(cfg["window"], n_win) if n_on else cfg["window"]
        base = os.path.join(RUN_DIR, session_key(data))
        if os.path.exists(base + ".thrash"):
            return
        if os.path.exists(base + ".compacted"):
            # first turn after a compaction: if still over the window, compacting again cannot help
            os.remove(base + ".compacted")
            if tokens >= window:
                with open(base + ".thrash", "w") as f:
                    f.write(str(tokens))
                log({"ev": "auto-thrash-stop", "key": session_key(data), "context_tokens": tokens, "window": window})
                return
        if tokens < window:
            return
        mark = base + ".trigger"
        try:
            if time.time() - os.path.getmtime(mark) < RETRIGGER_SECONDS:
                return
        except OSError:
            pass
        os.makedirs(RUN_DIR, exist_ok=True)
        with open(mark, "w") as f:
            f.write(str(tokens))
        ok = inject("/compact smart", 1.5)
        log({"ev": "auto-trigger", "key": session_key(data), "context_tokens": tokens, "window": window, "sent": ok})
    except Exception as e:
        log({"ev": "stop-error", "err": repr(e)})


MODES = {"pre": mode_pre, "start": mode_start, "post": mode_post, "expand": mode_expand, "stop": mode_stop}


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # the marks are not ASCII
    except Exception:
        pass
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    MODES.get(mode, lambda d: None)(data)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
