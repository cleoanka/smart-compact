#!/usr/bin/env python3
"""Install or remove smart-compact for Claude Code.

  python3 install.py                 install or update (idempotent)
  python3 install.py --claude-md     also append the marking rule to CLAUDE.md
  python3 install.py --lang tr       force the language of command output (en, tr)
  python3 install.py --python PATH   interpreter the hooks should run with
  python3 install.py --uninstall     remove hooks, commands and the script (state is kept)

The Claude Code config directory is $CLAUDE_CONFIG_DIR or ~/.claude; override with --config-dir.
settings.json is backed up next to itself before it is changed.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT_NAME = "smart-compact.py"
MARK_START = "<!-- smart-compact:start -->"
MARK_END = "<!-- smart-compact:end -->"

# event -> (matcher or None, mode, timeout seconds)
HOOKS = [
    ("PreCompact", None, "pre", 20),
    ("PostCompact", None, "post", 10),
    ("SessionStart", "compact", "start", 10),
    ("UserPromptExpansion", "smartcompact|smartautocompact|pin|pins|unpin|drop|undrop", "expand", 10),
    ("Stop", None, "stop", 10),
]

# name -> (argument hint, {lang: description})
COMMANDS = {
    "smartcompact": ("[extra focus]", {
        "en": "Smart compaction: pins survive, discard marks are removed, the rest is judged against the current goal",
        "tr": "Akıllı compact: pinler kalır, silme işaretliler atılır, gerisi güncel hedefe göre değerlendirilir"}),
    "smartautocompact": ("[on|off|300k|40%|status]", {
        "en": "Set the smart auto-compact limit for this session, or turn it on or off",
        "tr": "Bu oturum için akıllı otomatik compact sınırını ayarla ya da aç/kapat"}),
    "pin": ("<text>", {
        "en": "Pin one line so it survives every compaction (no tokens used)",
        "tr": "Bu satırı her compact'tan sağ çıkacak şekilde pinle (token harcamaz)"}),
    "unpin": ("<number or fragment>", {
        "en": "Remove a pin",
        "tr": "Bir pini kaldır"}),
    "drop": ("<text>", {
        "en": "Mark something as worthless so the next compaction leaves it out (no tokens used)",
        "tr": "Bir şeyi işe yaramaz diye işaretle; sıradaki compact onu atar (token harcamaz)"}),
    "undrop": ("<number or fragment>", {
        "en": "Remove a discard mark",
        "tr": "Bir silme işaretini kaldır"}),
    "pins": ("", {
        "en": "List this session's pins and pending discard marks",
        "tr": "Bu oturumdaki pinleri ve bekleyen silme işaretlerini listele"}),
}

COMMAND_BODY = ("The smart-compact hook should have handled /{name} without reaching you. It did not run. "
                "Tell the user in one line that the smart-compact UserPromptExpansion hook is not active "
                "and do nothing else.\n")
COMMAND_SIGNATURE = "The smart-compact hook should have handled"


def stable_python() -> str:
    """The interpreter to write into the hooks: not one inside a virtualenv that may be deleted later."""
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        for cand in (getattr(sys, "_base_executable", None),
                     os.path.join(sys.base_prefix, "bin", "python3")):
            if cand and os.path.exists(cand) and not cand.startswith(sys.prefix + os.sep):
                return cand
        print("  WARNING  running inside a virtualenv; the hooks will stop working if it is removed. "
              "Use --python to choose another interpreter.")
    # Prefer a stable name for this same interpreter (/usr/bin/python3, /opt/homebrew/bin/python3)
    # over a versioned path that disappears with the next upgrade.
    real = os.path.realpath(sys.executable)
    names = [shutil.which("python3"), shutil.which("python")]
    names += [os.path.join(d, "python3") for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")]
    for cand in names:
        if cand and os.path.isabs(cand) and os.path.exists(cand) and os.path.realpath(cand) == real:
            return cand
    return sys.executable


def detect_lang() -> str:
    loc = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    return "tr" if loc.lower().startswith("tr") else "en"


OURS = re.compile(r"""(?:^|[/\\"'\s])smart-compact\.py["']?\s+(?:pre|post|start|expand|stop)\b""")


def is_ours(hook) -> bool:
    return isinstance(hook, dict) and bool(OURS.search(str(hook.get("command", ""))))


def strip_hooks(settings: dict) -> None:
    """Remove our hook entries only; other hooks in the same group, and their groups, stay."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = []
        for g in groups:
            inner = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(inner, list) or not any(is_ours(h) for h in inner):
                kept.append(g)
                continue
            rest = [h for h in inner if not is_ours(h)]
            if rest:
                kept.append({**g, "hooks": rest})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]


def add_hooks(settings: dict, python: str, script: str) -> None:
    hooks = settings.setdefault("hooks", {})
    for event, matcher, mode, timeout in HOOKS:
        # "|| true": a missing interpreter must never fail a compaction or a Stop
        command = f"{shlex.quote(python)} -I {shlex.quote(script)} {mode} || true"
        group = {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
        if matcher:
            group = {"matcher": matcher, **group}
        hooks.setdefault(event, []).append(group)


def load_settings(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if not text.strip():
        return {}
    data = json.loads(text)  # a broken settings.json must stop the install, not be overwritten
    if not isinstance(data, dict):
        raise ValueError("settings.json is not a JSON object")
    return data


def backup(path: str) -> str:
    base = f"{path}.smart-compact-backup-{time.strftime('%Y%m%d-%H%M%S')}"
    name, n = base, 1
    while os.path.exists(name):  # never overwrite an earlier backup
        n += 1
        name = f"{base}-{n}"
    shutil.copy2(path, name)
    return name


def save_settings(path: str, settings: dict) -> None:
    target = os.path.realpath(path)  # a symlinked settings.json keeps being a symlink
    if os.path.exists(target):
        print(f"  backup   {backup(target)}")
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
        f.write("\n")
    if os.path.exists(target):
        shutil.copymode(target, tmp)
    os.replace(tmp, target)
    print(f"  settings {path}")


def write_commands(cmd_dir: str, lang: str) -> None:
    os.makedirs(cmd_dir, exist_ok=True)
    for name, (hint, desc) in COMMANDS.items():
        path = os.path.join(cmd_dir, name + ".md")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                if COMMAND_SIGNATURE not in f.read():
                    print(f"  SKIPPED  {path} is your own command and was left alone; "
                          f"smart-compact's /{name} is unavailable while it exists")
                    continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"---\ndescription: \"{desc[lang]}\"\nargument-hint: \"{hint}\"\n"
                    f"disable-model-invocation: true\n---\n" + COMMAND_BODY.format(name=name))
    print(f"  commands {cmd_dir} ({', '.join('/' + n for n in COMMANDS)})")


def remove_commands(cmd_dir: str) -> None:
    for name in COMMANDS:
        path = os.path.join(cmd_dir, name + ".md")
        try:
            with open(path, encoding="utf-8") as f:
                ours = COMMAND_SIGNATURE in f.read()
        except OSError:
            continue
        if ours:
            os.remove(path)


def rule_text() -> str:
    with open(os.path.join(HERE, "docs", "claude-md-rule.md"), encoding="utf-8") as f:
        return f.read().strip()


def add_rule(claude_md: str) -> None:
    existing = ""
    if os.path.exists(claude_md):
        with open(claude_md, encoding="utf-8") as f:
            existing = f.read()
    block = f"{MARK_START}\n{rule_text()}\n{MARK_END}\n"
    if MARK_START in existing and MARK_END in existing:
        head, rest = existing.split(MARK_START, 1)
        new = head + block + rest.split(MARK_END, 1)[1].lstrip("\n")
    elif "smart-compact" in existing:
        print(f"  SKIPPED  {claude_md} already mentions smart-compact outside the markers; edit it by hand")
        return
    else:
        new = existing + ("" if existing.endswith("\n") or not existing else "\n") + ("\n" if existing else "") + block
    if new != existing:
        if existing:
            backup(claude_md)
        with open(claude_md, "w", encoding="utf-8") as f:
            f.write(new)
    print(f"  rule     {claude_md}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Install or remove smart-compact for Claude Code.")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--claude-md", action="store_true", help="append the marking rule to CLAUDE.md")
    ap.add_argument("--lang", choices=("en", "tr"), help="language of command output (default: from the locale)")
    ap.add_argument("--python", help="interpreter the hooks should run with (default: this one)")
    ap.add_argument("--config-dir", default=os.environ.get("CLAUDE_CONFIG_DIR")
                    or os.path.join(os.path.expanduser("~"), ".claude"))
    args = ap.parse_args()

    if sys.version_info < (3, 9):
        print("smart-compact needs Python 3.9 or newer.", file=sys.stderr)
        return 1

    cfg = os.path.abspath(args.config_dir)
    settings_path = os.path.join(cfg, "settings.json")
    hook_path = os.path.join(cfg, "hooks", SCRIPT_NAME)
    cmd_dir = os.path.join(cfg, "commands")
    state = os.path.join(cfg, "smart-compact")

    try:
        settings = load_settings(settings_path)
    except (ValueError, OSError) as e:
        print(f"Cannot read {settings_path}: {e}\nNothing was changed.", file=sys.stderr)
        return 1

    before = json.dumps(settings, sort_keys=True)
    strip_hooks(settings)

    if args.uninstall:
        print("Removing smart-compact:")
        if json.dumps(settings, sort_keys=True) != before:
            save_settings(settings_path, settings)
        remove_commands(cmd_dir)
        if os.path.exists(hook_path):
            os.remove(hook_path)
        print(f"Done. State and saved summaries are kept in {state}; delete that directory if you want them gone.\n"
              f"If you added the marking rule to CLAUDE.md, remove it by hand.")
        return 0

    print("Installing smart-compact:")
    os.makedirs(os.path.dirname(hook_path), exist_ok=True)
    shutil.copy2(os.path.join(HERE, "hooks", SCRIPT_NAME), hook_path)
    os.chmod(hook_path, 0o755)
    print(f"  script   {hook_path}")
    add_hooks(settings, args.python or stable_python(), hook_path)
    if json.dumps(settings, sort_keys=True) != before:
        save_settings(settings_path, settings)
    else:
        print(f"  settings {settings_path} (already up to date)")
    os.makedirs(state, exist_ok=True)
    lang_file = os.path.join(state, "lang")
    if args.lang:
        with open(lang_file, "w", encoding="utf-8") as f:
            f.write(args.lang + "\n")
    lang = detect_lang()
    try:
        with open(lang_file, encoding="utf-8") as f:
            saved = f.read().strip()
        lang = saved if saved in ("en", "tr") else lang  # an earlier --lang stays in force
    except OSError:
        pass
    write_commands(cmd_dir, lang)
    if args.claude_md:
        add_rule(os.path.join(cfg, "CLAUDE.md"))
    print("Done. Start a new Claude Code session (or run /hooks once) so the hooks are loaded.")
    if not args.claude_md:
        print("The model only writes marks if it is told to: add docs/claude-md-rule.md to your CLAUDE.md,\n"
              "or run this installer again with --claude-md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
