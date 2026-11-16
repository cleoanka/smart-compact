# smart-compact

Decide what survives compaction in [Claude Code](https://code.claude.com/docs): pin what must never be lost, mark what is certainly garbage, and have the rest judged against the current goal. It runs entirely in hooks, so marking costs no extra model call.

[Türkçe](README.tr.md)

> Unofficial community tool. Not affiliated with or endorsed by Anthropic.

## The problem

When a long session compacts, Claude Code replaces the history with a summary written by the model. You don't control what the summary keeps. A constraint you stated two hours ago may come back paraphrased or not at all, while the contents of a file you deleted may still take up room.

Fixed rules such as "always keep created files" don't help, because importance depends on the situation: a throwaway script is noise, the script you are about to ship is not.

## How it works

The model marks a line at the moment it writes it, when it knows best whether the line matters. A hook reads the marks from the session transcript when a compaction starts.

| Mark | Meaning | What happens at compaction |
| :- | :- | :- |
| `◆ text` | Pin | Saved by the hook and put back **verbatim** after every compaction, native or smart |
| `◇ text` | Discard | The line and whatever it names are listed for the summarizer as "leave out completely, no pointer" |
| `◆✗ fragment` | Unpin | Retires an earlier pin that contains the fragment |
| *(no mark)* | Undecided | Judged at compaction time against the current goal |

A mark is a line of a reply, or of your own message, that starts with the symbol. Tool calls, tool results, the output of `!` shell commands, subagent text, thinking, quoted lines (`> …`) and fenced code blocks are not scanned.

```
reply text ─► session transcript (.jsonl on disk, never rewritten)
                     │
/compact ───────────►│ PreCompact hook reads the marks
                     ├─ ◆ pins     ─► saved ─► SessionStart hook re-injects them verbatim
                     ├─ ◇ discards ─► discard list added to the summary instructions
                     └─ smart?     ─► KEEP / POINTER / DROP policy added to the summary instructions
```

For unmarked material, a smart compaction asks the summarizer one question per item: *if this is dropped, will continuing the work cause a mistake or repeated work?* The answer puts it in one of three tiers:

- **KEEP**, in detail: decisions with reasons, verified facts, user corrections, current state, identifiers the goal still needs.
- **POINTER**, one line: plausibly useful things, reduced to `what -> where` (a path, or "transcript"). When the summarizer is unsure, it must choose this tier.
- **DROP**: raw tool output, file contents, superseded attempts.

Nothing is truly lost. Compaction appends to the transcript file and never rewrites it, so the full history stays on disk.

### Reading it back without paying for it

A transcript is a bad thing to read. Each line is one whole message, and a line that holds a tool result can be tens of thousands of characters. If the model greps the file for a detail, the entire line enters the context and is then re-read on every later turn.

So after each compaction the hook gives the model a `recall` command instead of the bare path:

```
smart-compact.py recall <transcript> retry_budget degraded
[line 212 2026-11-15T10:08 tool_result] …status=ok latency_ms=412 2026-11-15T10:08:37 worker=12 shard=k7 status=degraded retry_budget=17 owner=team-osprey 2026-11-15T10:08:38 worker=3…
```

- Every word must occur in the same message. Newest matches come first.
- Each hit is cut to a window of about 480 characters around the match. One call prints at most 6 hits and 4,000 characters, however large the matching lines are.
- `--line N [--at OFFSET]` pages through one message, 3,000 characters at a time, when a snippet is not enough.
- `--discards` lists what the last compaction was told to leave out, in case a discard mark turns out to have been wrong.

It searches reply text, tool inputs, tool results and shell output. It is a plain substring search over the file: no index, no model call.

## Install

```bash
git clone https://github.com/cleoanka/smart-compact
cd smart-compact
python3 install.py --claude-md
```

Then start a new Claude Code session.

The installer:

- copies `hooks/smart-compact.py` into `~/.claude/hooks/`
- adds five hook entries to `~/.claude/settings.json`, after backing the file up next to itself; your other hooks and settings are left as they are
- writes seven small command files into `~/.claude/commands/`
- with `--claude-md`, appends the marking rule from [`docs/claude-md-rule.md`](docs/claude-md-rule.md) to `~/.claude/CLAUDE.md`

The rule matters: without it the model does not know the marks exist, and only the commands and your own marks work.

| Option | Effect |
| :- | :- |
| `--lang en` or `--lang tr` | Language of command output. Default: taken from your locale |
| `--python PATH` | Interpreter the hooks run with. Default: the one running the installer, or its base interpreter when that is a virtualenv |
| `--config-dir PATH` | Use another Claude Code config directory. Default: `$CLAUDE_CONFIG_DIR` or `~/.claude` |
| `--uninstall` | Remove the hooks, commands and script. Saved state stays in `~/.claude/smart-compact/` |

Running the installer again updates an existing install without duplicating anything. If you already have your own command with one of these names, such as `/drop`, the installer leaves it alone and smart-compact does not intercept it.

**Requirements:** Python 3.9 or newer, and a Claude Code version with the `PreCompact`, `PostCompact`, `SessionStart`, `UserPromptExpansion` and `Stop` hooks. Developed and tested on Claude Code 2.1.325 on macOS.

## Use

### Marks

Once the rule is in `CLAUDE.md`, the model writes marks on its own:

```
report.py now runs and writes out/summary.csv.

◆ Constraint: standard library only, no pandas.
◆ Root cause: csv.DictReader returns amount as str; summing it with int raised TypeError.
◆ Deliverable: src/report.py, output in out/summary.csv
```

```
percent.py is deleted.

◆✗ Deliverable: src/percent.py
◆ Decision: percent.py removed at the user's request; the percentage idea is closed.
◇ the contents of percent.py, its design notes and its sample output
```

You can mark things yourself. Start a line of your message with `◆ `, `◇ `, `!pin ` or `!drop `, or use the commands below.

### Commands

All of these are answered by the hook. They never reach the model and use no tokens.

| Command | What it does |
| :- | :- |
| `/pin <text>` | Pin one line |
| `/unpin <number or fragment>` | Remove a pin |
| `/drop <text>` | Mark something as worthless for the next compaction |
| `/undrop <number or fragment>` | Remove a discard mark before it is used |
| `/pins` | List pins and pending discard marks |
| `/smartcompact [focus]` | Compact now with the smart policy |
| `/smartautocompact [on\|off\|300k\|40%\|status]` | Smart auto-compaction for this session |

### Which compaction does what

| | Pins restored | Discard list applied | KEEP / POINTER / DROP policy |
| :- | :-: | :-: | :-: |
| `/compact`, native auto-compaction | yes | yes | no |
| `/smartcompact`, or `/compact smart` | yes | yes | yes |
| Auto-compaction while smart auto is on | yes | yes | yes |

Plain `/compact` keeps Claude Code's own summary style, so you can adopt pins without changing anything else. `/compact smart: focus on the auth work` adds your own focus to the policy.

### Smart auto-compaction

Native auto-compaction waits until the context is nearly full, about 967K tokens on 1M-context models. `/smartautocompact 300k` compacts with the smart policy as soon as a turn ends above 300K.

- The setting belongs to the session you type it in. Other sessions are not affected, and new sessions start with it off.
- The limit cannot exceed the native auto-compact window. If it would, it is set equal to it.
- The floor is 100K, the same as Claude Code's own.
- If the context is still above the limit on the first turn after a compaction, compacting again cannot help, so smart auto stops for that session.

### Typing `/compact` for you

A hook cannot start a compaction, and neither can a custom command. `/smartcompact` and smart auto-compaction therefore type `/compact smart` into your terminal:

- inside **tmux**, with `tmux send-keys`
- in **iTerm2** on macOS, with AppleScript (macOS may ask once for permission to control iTerm2)

In any other terminal, `/smartcompact` shows you the command to type, and smart auto cannot trigger by itself. Pins, discard marks and `/compact smart` typed by hand work everywhere.

## Cost: cache, tokens, context

### What it adds

- **No model calls.** Marks are a few characters in a reply the model is already writing. Commands are handled by a hook.
- **No cache invalidation during normal work.** Nothing here changes the system prompt, the tool list or earlier messages, which are the things that [invalidate the prompt cache](https://code.claude.com/docs/en/prompt-caching.md#actions-that-invalidate-the-cache).
- **The rule in `CLAUDE.md`**: about 450 tokens of standing context, cached with the rest of your project context.
- **At compaction**: up to about 1,700 characters of policy and 3,400 characters of discard list in the summarization request, then at most 8,000 characters of pins re-injected afterwards, plus about 500 characters for the recall command.

### What it saves

- **Every later turn is cheaper when the summary is smaller.** Claude Code re-sends the whole conversation on each turn and pays the cached-token rate to re-read it, so whatever a summary carries is paid for again on every turn until the next compaction. Discarded material and one-line pointers shrink that.
- **Looking something up stays small.** `recall` returns a window around the match instead of the transcript line, so a lookup adds a few hundred tokens to the context, not the whole tool result it came from.
- **Fewer repeated mistakes.** A lost constraint costs far more than tokens: the model redoes work or breaks a rule you already set. Pins come back word for word, independent of what the summarizer decided.
- **Compacting earlier, on your terms.** A session that runs to 900K tokens re-reads up to 900K cached tokens per turn. A lower smart limit keeps each turn smaller.
- **Compacting while the cache is warm.** The summarization request [reads the history from the cache when it is warm and reprocesses it at full price when it is cold](https://code.claude.com/docs/en/prompt-caching.md#compacting-the-conversation). Smart auto triggers at the end of a turn, when the cache has just been used.

### What it does not do

- It cannot remove anything from the context *before* a compaction. A `◇` line stays in the context until the next compaction happens. Claude Code offers no way for a hook to edit the live context.
- Compaction itself is not free. It invalidates the conversation cache, so the next turn rebuilds it. A limit set too low makes you pay that often and can cost more than it saves. This is why smart auto has a floor and stops when compacting stops helping.
- The native auto-compact window is read from your user `settings.json` and the environment, and assumed to be about 967K otherwise. On a 200K-context model, or when the window is set in project or managed settings, set a smart limit below the real window yourself.
- Typing `/compact smart` for you is a keystroke injection. If you are already typing a new prompt at that moment, the text lands in your draft.
- Pins made with `/pin` belong to the session id. A fork or a resume that gets a new id keeps the pins written in the transcript, not the ones added by command.
- A discard mark that names a pin, or is named by one, is ignored: protecting wins over discarding. Beyond that the hook cannot tell whether a mark is wise.
- The discard list is an instruction to a language model. It was followed in every test below, and the hook logs how many marked lines were quoted back into a summary (`drop_leaks` in `log.jsonl`), but it is not a hard guarantee the way pins are.

## Security note

A pin is text that gets put back in front of the model after every compaction, so a pin is trusted text. The hook only reads the model's own reply text and what you type. Tool results, shell output, subagents, quoted lines and code blocks are skipped, so a web page or a file cannot create a mark by merely being read. One path remains: if the model itself repeats outside text as an unquoted line that starts with `◆` or `◇`, that line counts. Check `/pins` when you work with untrusted content, and remove anything you did not expect with `/unpin` or `/undrop`.

## What was tested

Real sessions, Claude Code 2.1.325, macOS:

| Check | Result |
| :- | :- |
| Model writes marks unprompted (Mythos 5.2, three-turn task: fix a script under three constraints, write a second script, then abandon it) | 5 pins for constraints, root cause and deliverable; then `◆✗` for the abandoned file, a pin for the decision, and one `◇` for its contents |
| Pins after `/smartcompact` | All 6 came back verbatim |
| Discard mark after `/smartcompact` | The abandoned file's contents and output were absent from the summary; a plain `/compact` on a fork of the same session, with hooks off, kept them |
| Summary size in that session | 4,544 characters smart, 6,257 native |
| `/drop` followed by plain `/compact` | The dropped item disappeared from the summary; pins restored; an older, already-used `◇` was not listed again |
| Commands | No model call |
| Recall after compaction (Opus 5.5): an 800-line log was read, the session compacted, the file deleted, then an unremarkable line was asked for | The model ran `recall` once on its own and answered correctly. The tool result was 1,001 characters; the transcript line holding the file is about 40,000 |

Read these numbers for what they are: one small session of about 62K tokens. At that size the token difference is negligible, and the native summary happened to keep the pinned facts too. The size comparison says nothing certain about long sessions, which I have not measured side by side.

The discard list depends on the model too. With Mythos 5.2 the marked material and the mark were both absent from the summary. Haiku 5.5, in three runs on a nearly empty session, left the material out but added a note that the item "was removed from the summary", quoting the mark. That costs one line and is counted in `drop_leaks`.

Marking depends on the model. Mythos 5.2 followed the rule in each run. Haiku 5.5 was inconsistent: two pins in one run, none in another. The wording of the rule matters as well: an earlier, more cautious wording ("when unsure, don't pin") produced no pins at all on either model.

Not tested: Linux and Windows in real sessions (the unit tests run on Linux in CI), terminals other than tmux and iTerm2, and native auto-compaction at its own limit with smart auto on.

## Files

| Path | Contents |
| :- | :- |
| `~/.claude/hooks/smart-compact.py` | The hook script. No dependencies outside the standard library |
| `~/.claude/smart-compact/log.jsonl` | One line per event: pins, discard marks, context size, summary size |
| `~/.claude/smart-compact/summaries/` | Every compaction summary, for checking what the policy did |
| `~/.claude/smart-compact/pins/`, `manual/`, `cfg/`, `run/` | Per-session state |

Per-session state older than 30 days is deleted automatically. Every failure in the hook exits 0, and the installed hook commands end with `|| true`, so a broken install cannot block a compaction. If more pins exist than fit in the 8,000 characters available for re-injection, the oldest ones are handed to the summarizer to be copied into the summary word for word.

## Development

```bash
python3 -m unittest discover -s tests
```

The tests run the script the way Claude Code does, with hook JSON on stdin, against a temporary config directory.

## License

[MIT](LICENSE)
