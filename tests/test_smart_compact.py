"""Black-box tests: run the hook script the way Claude Code does (JSON on stdin, mode as argv)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "hooks", "smart-compact.py")
INSTALL = os.path.join(ROOT, "install.py")
SID = "11111111-2222-3333-4444-555555555555"


def entry(role, text, ts, **extra):
    content = text if role == "user" else [{"type": "text", "text": text}]
    return {"type": role, "timestamp": ts, "message": {"role": role, "content": content}, **extra}


def usage_entry(tokens, ts, model="claude-test"):
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "model": model, "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 10, "cache_read_input_tokens": tokens - 10,
                                  "cache_creation_input_tokens": 0, "output_tokens": 0}}}


BOUNDARY = {"type": "system", "subtype": "compact_boundary"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = os.path.join(self.tmp.name, "claude")
        os.makedirs(os.path.join(self.cfg, "commands"))
        for name in ("smartcompact", "smartautocompact", "pin", "pins", "unpin", "drop", "undrop"):
            with open(os.path.join(self.cfg, "commands", name + ".md"), "w") as f:
                f.write(f"---\n---\nThe smart-compact hook should have handled /{name} without reaching you.\n")
        self.transcript = os.path.join(self.tmp.name, "t.jsonl")
        self.env = {"PATH": os.environ.get("PATH", ""), "CLAUDE_CONFIG_DIR": self.cfg,
                    "SMART_COMPACT_LANG": "en", "HOME": self.tmp.name}

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, entries):
        with open(self.transcript, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def run_hook(self, mode, env=None, **data):
        payload = {"session_id": SID, "transcript_path": self.transcript, **data}
        p = subprocess.run([sys.executable, "-I", SCRIPT, mode], input=json.dumps(payload),
                           capture_output=True, text=True, env={**self.env, **(env or {})})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stderr, "")
        return p.stdout

    def command(self, name, args="", env=None):
        out = self.run_hook("expand", env=env, command_name=name, command_args=args)
        return json.loads(out)["reason"] if out else ""

    def saved(self):
        with open(os.path.join(self.cfg, "smart-compact", "pins", SID + ".json"), encoding="utf-8") as f:
            return json.load(f)

    def restored(self):
        out = self.run_hook("start", source="compact")
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


class Marks(Base):
    def test_pins_from_replies_and_user_lines(self):
        self.write([
            entry("user", "do the thing\n!pin budget is 40 hours", "2026-01-01T00:00:01Z"),
            entry("assistant", "Working.\n◆ Constraint: stdlib only\n- ◆ Root cause: str + int\n**◆ Bold pin**",
                  "2026-01-01T00:00:02Z"),
            entry("user", "📌 ship by Friday", "2026-01-01T00:00:03Z"),
        ])
        self.run_hook("pre", trigger="manual", custom_instructions="")
        self.assertEqual(self.saved()["pins"], ["budget is 40 hours", "Constraint: stdlib only",
                                                "Root cause: str + int", "Bold pin", "ship by Friday"])

    def test_unpin_mark_and_reply_only_prefixes(self):
        self.write([
            entry("assistant", "◆ limit is 10 requests per second\n!pin not a pin from the assistant",
                  "2026-01-01T00:00:01Z"),
            entry("assistant", "◆✗ 10 requests\n◆ limit is 25 requests per second", "2026-01-01T00:00:02Z"),
        ])
        self.run_hook("pre", trigger="auto")
        self.assertEqual(self.saved()["pins"], ["limit is 25 requests per second"])

    def test_non_text_content_is_ignored(self):
        self.write([
            {"type": "assistant", "timestamp": "2026-01-01T00:00:01Z", "message": {"content": [
                {"type": "tool_use", "name": "Write", "input": {"content": "◆ inside a tool call"}},
                {"type": "thinking", "thinking": "◆ inside thinking"}]}},
            {"type": "user", "timestamp": "2026-01-01T00:00:02Z", "message": {"content": [
                {"type": "tool_result", "content": "◆ inside a tool result"},
                {"type": "text", "text": "<system-reminder>◆ reminder</system-reminder>"}]}},
            entry("user", "◆ meta", "2026-01-01T00:00:03Z", isMeta=True),
            entry("user", "◆ old summary", "2026-01-01T00:00:04Z", isCompactSummary=True),
            entry("assistant", "◆ from a subagent", "2026-01-01T00:00:04Z", isSidechain=True),
            entry("user", "<bash-stdout>◆ printed by a shell command\n!pin also printed</bash-stdout>",
                  "2026-01-01T00:00:04Z"),
            entry("assistant", "The page says:\n> ◆ quoted pin\n> ◇ quoted discard", "2026-01-01T00:00:04Z"),
            entry("assistant", "Example:\n```\n◆ inside a code fence\n◇ also fenced\n```\n◆ real pin",
                  "2026-01-01T00:00:05Z"),
        ])
        self.run_hook("pre", trigger="auto")
        self.assertEqual(self.saved(), {"transcript_path": self.transcript, "pins": ["real pin"],
                                        "overflow": 0, "drops": []})

    def test_discard_marks_reach_the_summary_instructions(self):
        self.write([
            entry("assistant", "◇ the three regex attempts above and their output", "2026-01-01T00:00:01Z"),
            entry("user", "!drop the screenshot discussion", "2026-01-01T00:00:02Z"),
        ])
        plain = self.run_hook("pre", trigger="manual", custom_instructions="keep the API notes")
        self.assertNotIn("SMART-COMPACT POLICY", plain)
        self.assertIn("DISCARD LIST", plain)
        self.assertIn("- the three regex attempts above and their output", plain)
        self.assertIn("- the screenshot discussion", plain)
        smart = self.run_hook("pre", trigger="manual", custom_instructions="smart: focus on auth")
        self.assertTrue(smart.startswith("SMART-COMPACT POLICY"))
        self.assertIn("DISCARD LIST", smart)
        for text, expected in (("smart", True), ("Smart focus on auth", True), ("smartphone notes", False)):
            out = self.run_hook("pre", trigger="manual", custom_instructions=text)
            self.assertEqual("SMART-COMPACT POLICY" in out, expected, text)

    def test_discard_marks_are_consumed_by_a_compaction(self):
        self.write([
            entry("assistant", "◇ old dead end\n◆ kept forever", "2026-01-01T00:00:01Z"),
            {**BOUNDARY, "timestamp": "2026-01-01T00:00:02Z"},
            entry("assistant", "◇ new dead end", "2026-01-01T00:00:03Z"),
        ])
        out = self.run_hook("pre", trigger="auto")
        self.assertEqual(self.saved()["drops"], ["new dead end"])
        self.assertEqual(self.saved()["pins"], ["kept forever"])
        self.assertNotIn("old dead end", out)

    def test_no_output_when_nothing_to_say(self):
        self.write([entry("assistant", "nothing marked", "2026-01-01T00:00:01Z")])
        self.assertEqual(self.run_hook("pre", trigger="manual", custom_instructions=""), "")
        self.assertEqual(self.restored(), "")

    def test_discard_list_is_bounded(self):
        self.write([entry("assistant", "\n".join(f"◇ item {i} " + "x" * 400 for i in range(40)),
                          "2026-01-01T00:00:01Z")])
        out = self.run_hook("pre", trigger="auto")
        self.assertLess(len(out), 4000)
        self.assertIn("item 39", out)
        self.assertNotIn("item 0 ", out)


class Restore(Base):
    def test_pins_come_back_verbatim_with_the_transcript_path(self):
        self.write([entry("assistant", "◆ Deliverable: out/report.csv", "2026-01-01T00:00:01Z")])
        self.run_hook("pre", trigger="auto")
        ctx = self.restored()
        self.assertIn("◆ Deliverable: out/report.csv", ctx)
        self.assertIn(self.transcript, ctx)

    def test_only_after_a_compaction(self):
        self.write([entry("assistant", "◆ a pin", "2026-01-01T00:00:01Z")])
        self.run_hook("pre", trigger="auto")
        self.assertEqual(self.run_hook("start", source="startup"), "")

    def test_budget_keeps_the_newest_pins(self):
        self.write([entry("assistant", "\n".join(f"◆ pin {i:03d} " + "y" * 200 for i in range(60)),
                          "2026-01-01T00:00:01Z")])
        out = self.run_hook("pre", trigger="auto")
        ctx = self.restored()
        self.assertLess(len(ctx), 10000)
        self.assertIn("pin 059", ctx)
        self.assertNotIn("pin 000", ctx)
        self.assertIn("older pins did not fit", ctx)
        # the pins that do not fit are handed to the summarizer instead of being lost
        self.assertIn("PIN OVERFLOW", out)
        self.assertIn("◆ pin 022", out)
        self.assertNotIn("pin 059", out)
        self.assertLess(len(out), 10000)


class Commands(Base):
    def setUp(self):
        super().setUp()
        self.write([entry("assistant", "◆ from the transcript", "2026-01-01T00:00:01Z")])

    def test_pin_unpin_drop_undrop_pins(self):
        self.assertIn("pinned (2 pins)", self.command("pin", "  manual   pin  "))
        self.assertIn("1 marks", self.command("drop", "the benchmark detour"))
        listing = self.command("pins")
        self.assertIn("1. ◆ from the transcript", listing)
        self.assertIn("2. ◆ manual pin", listing)
        self.assertIn("1. ◇ the benchmark detour", listing)
        self.assertIn("pin removed: from the transcript", self.command("unpin", "1"))
        self.assertIn("discard mark removed", self.command("undrop", "benchmark"))
        self.assertNotIn("◇", self.command("pins"))
        self.run_hook("pre", trigger="auto")
        self.assertEqual(self.saved()["pins"], ["manual pin"])
        self.assertEqual(self.saved()["drops"], [])

    def test_unpin_by_number_removes_only_that_pin(self):
        self.command("pin", "Use foo")
        self.command("pin", "Use foo bar")
        self.assertIn("pin removed: Use foo", self.command("unpin", "2"))
        listing = self.command("pins")
        self.assertIn("Use foo bar", listing)
        self.assertEqual(listing.count("Use foo"), 1)

    def test_other_peoples_commands_are_not_intercepted(self):
        self.assertEqual(self.run_hook("expand", command_name="dbtools:drop", command_args="table x"), "")
        with open(os.path.join(self.cfg, "commands", "drop.md"), "w") as f:
            f.write("my own /drop command\n")
        self.assertEqual(self.run_hook("expand", command_name="drop", command_args="table x"), "")
        self.assertIn("pinned", self.command("pin", "still ours"))

    def test_bad_arguments(self):
        self.assertIn("usage", self.command("pin"))
        self.assertIn("no item 9", self.command("unpin", "9"))
        self.assertIn("nothing contains", self.command("unpin", "zebra"))
        self.command("pin", "alpha one")
        self.command("pin", "alpha two")
        self.assertIn("several items match", self.command("unpin", "alpha"))

    def test_turkish_messages(self):
        self.assertIn("pinlendi", self.command("pin", "satır", env={"SMART_COMPACT_LANG": "tr"}))

    def test_unknown_command_is_left_alone(self):
        self.assertEqual(self.run_hook("expand", command_name="help", command_args=""), "")

    def test_smartcompact_without_a_supported_terminal(self):
        self.assertIn("/compact smart: auth only", self.command("smartcompact", "auth\nonly"))


class AutoCompact(Base):
    def setUp(self):
        super().setUp()
        self.write([usage_entry(250_000, "2026-01-01T00:00:01Z")])

    def test_limit_is_per_session_and_floored(self):
        out = self.command("smartautocompact", "50k")
        self.assertIn("ON, limit 100k (floor is 100k)", out)
        self.assertIn("current context: 250k", out)
        other = json.loads(self.run_hook("expand", session_id="another-session",
                                         command_name="smartautocompact", command_args="status"))["reason"]
        self.assertIn("OFF, limit 300k", other)

    def test_limit_cannot_exceed_native_window(self):
        with open(os.path.join(self.cfg, "settings.json"), "w") as f:
            json.dump({"autoCompactWindow": 200000}, f)
        out = self.command("smartautocompact", "500k")
        self.assertIn("limit 200k", out)
        self.assertIn("cannot exceed", out)
        self.assertIn("native auto-compact: on, limit 200k", out)

    def test_percent_and_off(self):
        self.assertIn("ON, limit 484k", self.command("smartautocompact", "50%"))
        self.assertIn("OFF", self.command("smartautocompact", "off"))
        self.assertIn("not understood", self.command("smartautocompact", "banana"))

    def test_stop_hook_does_nothing_without_a_supported_terminal(self):
        self.command("smartautocompact", "100k")
        self.assertEqual(self.run_hook("stop"), "")
        self.assertFalse(os.path.exists(os.path.join(self.cfg, "smart-compact", "run", SID + ".trigger")))

    def test_stop_hook_triggers_in_tmux_and_guards_against_thrash(self):
        fake = os.path.join(self.tmp.name, "bin")
        os.makedirs(fake)
        with open(os.path.join(fake, "tmux"), "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(fake, "tmux"), 0o755)
        env = {"TMUX_PANE": "%99", "PATH": fake + os.pathsep + self.env["PATH"]}
        run = os.path.join(self.cfg, "smart-compact", "run", SID)
        self.command("smartautocompact", "100k", env=env)
        self.run_hook("stop", env=env)
        self.assertTrue(os.path.exists(run + ".trigger"))
        self.run_hook("post", trigger="manual", compact_summary="summary", env=env)
        self.run_hook("stop", env=env)  # still 250k right after compacting: stop for this session
        self.assertTrue(os.path.exists(run + ".thrash"))
        self.assertIn("stopped for this session", self.command("smartautocompact", "status", env=env))
        self.assertNotIn("stopped", self.command("smartautocompact", "200k", env=env))  # re-armed
        self.assertFalse(os.path.exists(run + ".thrash"))

    def test_a_compaction_before_smart_auto_is_on_does_not_disarm_it(self):
        fake = os.path.join(self.tmp.name, "bin")
        os.makedirs(fake)
        with open(os.path.join(fake, "tmux"), "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(fake, "tmux"), 0o755)
        env = {"TMUX_PANE": "%99", "PATH": fake + os.pathsep + self.env["PATH"]}
        run = os.path.join(self.cfg, "smart-compact", "run", SID)
        self.run_hook("post", trigger="manual", compact_summary="summary", env=env)  # plain /compact, auto off
        self.command("smartautocompact", "100k", env=env)
        self.run_hook("stop", env=env)
        self.assertFalse(os.path.exists(run + ".thrash"))
        self.assertTrue(os.path.exists(run + ".trigger"))


class Robustness(Base):
    def test_garbage_input_never_fails(self):
        for mode in ("pre", "start", "post", "expand", "stop", "nonsense"):
            p = subprocess.run([sys.executable, "-I", SCRIPT, mode], input="not json",
                               capture_output=True, text=True, env=self.env)
            self.assertEqual((p.returncode, p.stderr), (0, ""))

    def test_missing_transcript_and_broken_lines(self):
        self.assertEqual(self.run_hook("pre", trigger="auto", transcript_path="/nonexistent"), "")
        with open(self.transcript, "w") as f:
            f.write('{"broken ◆\n[1, "◆ list"]\n"◆ string"\n')
        self.assertEqual(self.run_hook("pre", trigger="auto"), "")

    def test_post_saves_summary_and_counts_leaks(self):
        self.write([entry("assistant", "◇ the flaky retry experiment", "2026-01-01T00:00:01Z")])
        self.run_hook("pre", trigger="auto")
        self.run_hook("post", trigger="auto", compact_summary="We also tried The Flaky Retry Experiment.")
        with open(os.path.join(self.cfg, "smart-compact", "log.jsonl"), encoding="utf-8") as f:
            last = json.loads(f.readlines()[-1])
        self.assertEqual((last["ev"], last["drops"], last["drop_leaks"]), ("post", 1, 1))
        self.assertEqual(len(os.listdir(os.path.join(self.cfg, "smart-compact", "summaries"))), 1)


class Installer(Base):
    def setUp(self):
        super().setUp()
        for name in os.listdir(os.path.join(self.cfg, "commands")):
            os.remove(os.path.join(self.cfg, "commands", name))
        os.rmdir(os.path.join(self.cfg, "commands"))

    def install(self, *args):
        p = subprocess.run([sys.executable, INSTALL, "--config-dir", self.cfg, *args],
                           capture_output=True, text=True, env=self.env)
        return p

    def settings(self):
        with open(os.path.join(self.cfg, "settings.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_install_is_idempotent_and_keeps_other_settings(self):
        other = {"type": "command", "command": "/usr/bin/true"}
        with open(os.path.join(self.cfg, "settings.json"), "w") as f:
            json.dump({"model": "x", "hooks": {"Stop": [{"hooks": [other]}]}}, f)
        self.assertEqual(self.install().returncode, 0)
        first = self.settings()
        self.assertEqual(self.install("--lang", "tr", "--claude-md").returncode, 0)
        second = self.settings()
        self.assertEqual(first, second)
        self.assertEqual(second["model"], "x")
        self.assertEqual(second["hooks"]["Stop"][0], {"hooks": [other]})
        self.assertTrue(second["hooks"]["Stop"][1]["hooks"][0]["command"].endswith(" stop || true"))
        self.assertEqual(len(second["hooks"]["Stop"]), 2)
        for event in ("PreCompact", "PostCompact", "SessionStart", "UserPromptExpansion"):
            self.assertEqual(len(second["hooks"][event]), 1)
        self.assertTrue(os.path.exists(os.path.join(self.cfg, "hooks", "smart-compact.py")))
        with open(os.path.join(self.cfg, "commands", "drop.md"), encoding="utf-8") as f:
            self.assertIn("işaretle", f.read())
        with open(os.path.join(self.cfg, "CLAUDE.md"), encoding="utf-8") as f:
            self.assertEqual(f.read().count("smart-compact:start"), 1)
        self.install("--claude-md")
        with open(os.path.join(self.cfg, "CLAUDE.md"), encoding="utf-8") as f:
            self.assertEqual(f.read().count("◇ the three failed"), 1)

    def test_installed_hook_command_runs(self):
        self.install()
        cmd = self.settings()["hooks"]["UserPromptExpansion"][0]["hooks"][0]["command"]
        payload = json.dumps({"session_id": SID, "transcript_path": "", "command_name": "pin",
                              "command_args": "from the installed hook"})
        p = subprocess.run(cmd, shell=True, input=payload, capture_output=True, text=True, env=self.env)
        self.assertIn("from the installed hook", json.loads(p.stdout)["reason"])

    def test_uninstall_removes_only_ours(self):
        with open(os.path.join(self.cfg, "settings.json"), "w") as f:
            json.dump({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/true"}]}]}}, f)
        os.makedirs(os.path.join(self.cfg, "commands"))
        with open(os.path.join(self.cfg, "commands", "pin.md"), "w") as f:
            f.write("someone else's /pin command\n")
        out = self.install()
        self.assertIn("SKIPPED", out.stdout)
        self.assertEqual(self.install("--uninstall").returncode, 0)
        self.assertEqual(self.settings(), {"hooks": {"Stop": [{"hooks": [{"type": "command",
                                                                         "command": "/usr/bin/true"}]}]}})
        self.assertFalse(os.path.exists(os.path.join(self.cfg, "hooks", "smart-compact.py")))
        self.assertEqual(os.listdir(os.path.join(self.cfg, "commands")), ["pin.md"])

    def test_a_shared_hook_group_keeps_the_other_hook(self):
        self.install()
        s = self.settings()
        mine = {"type": "command", "command": "/usr/bin/true"}
        s["hooks"]["PreCompact"][0]["hooks"].append(mine)
        s["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "/opt/not-smart-compact.py stop"}]})
        with open(os.path.join(self.cfg, "settings.json"), "w") as f:
            json.dump(s, f)
        self.install("--uninstall")
        self.assertEqual(self.settings()["hooks"], {
            "PreCompact": [{"hooks": [mine]}],
            "Stop": [{"hooks": [{"type": "command", "command": "/opt/not-smart-compact.py stop"}]}]})

    def test_symlinked_settings_stay_a_symlink_and_backups_do_not_collide(self):
        real = os.path.join(self.tmp.name, "dotfiles-settings.json")
        with open(real, "w") as f:
            json.dump({"model": "x"}, f)
        os.chmod(real, 0o600)
        link = os.path.join(self.cfg, "settings.json")
        os.symlink(real, link)
        self.install()
        self.install("--uninstall")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.stat(real).st_mode & 0o777, 0o600)
        with open(real) as f:
            self.assertEqual(json.load(f), {"model": "x"})
        backups = [n for n in os.listdir(self.tmp.name) if "smart-compact-backup" in n]
        self.assertEqual(len(backups), 2)

    def test_language_choice_survives_a_later_install(self):
        self.install("--lang", "tr")
        self.install()
        with open(os.path.join(self.cfg, "commands", "pin.md"), encoding="utf-8") as f:
            self.assertIn("pinle", f.read())

    def test_broken_settings_are_not_touched(self):
        path = os.path.join(self.cfg, "settings.json")
        with open(path, "w") as f:
            f.write("{ not json")
        self.assertEqual(self.install().returncode, 1)
        with open(path) as f:
            self.assertEqual(f.read(), "{ not json")
        self.assertFalse(os.path.exists(os.path.join(self.cfg, "hooks", "smart-compact.py")))


if __name__ == "__main__":
    unittest.main()
