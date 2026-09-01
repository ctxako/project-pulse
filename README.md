# Project Pulse

**A terminal instrument that turns a workspace of Git repositories into a short
list of good next moves.**

[![tests](https://github.com/ctxako/project-pulse/actions/workflows/tests.yml/badge.svg)](https://github.com/ctxako/project-pulse/actions/workflows/tests.yml)

Point it at a directory of repositories. It reads their Git state, their GitHub
state, and the agent pipelines running beside them, then ranks what is worth
doing right now: an open loop to close, finished work to publish, a project that
needs a status, a repeated chore worth automating. Every move carries the reason
it was raised and the command that starts it.

![The Project Pulse live pane](docs/pulse.png)

## Install

Requires Python 3.11+ and Git. The GitHub CLI and Claude Code are optional
integrations. No third-party Python packages, nothing to build.

```sh
git clone https://github.com/ctxako/project-pulse.git
cd project-pulse
./pulse --root ~/your-projects
```

Put it on your `PATH` and it works from anywhere:

```sh
ln -s "$PWD/pulse" ~/.local/bin/pulse
pulse
```

With no arguments, Pulse scans `~/Projects`. To scan somewhere else, pass
`--root`, set `$PROJECTS_ROOT`, or write a config file (see
[Configuration](#configuration)).

Pulse is a single Python file on purpose. A tool you clone and run on one line
has no install step to get wrong. It earns a package the day it needs a daemon,
a plugin boundary, or a second entry point.

## What it reads

Pulse never asks you to describe your workspace; it looks. For every repository
under the root it reads the working tree, the branch and its upstream, the commit
dates, and the project's shape (language, tests, CI). With the GitHub CLI
authenticated it adds pull requests that need a decision and pull requests
waiting on your review. If agents are working in the workspace, it reads their
own session files to show what they are doing.

**Everything it does is read-only.** Pulse suggests commands; it never runs a Git
write, changes a repository, or edits a GitHub issue or pull request. Dismissing
a move updates only Pulse's own state file at
`~/.local/state/project-pulse/state.json`.

The one command that reaches off the machine is `pulse ideas --refresh`, which
spends tokens and sends a model the shape of your workspace: repository names,
the tooling you have, your recent `LEDGER.md` entries, and the first lines of
each README, private ones included. `pulse ideas --pack` prints exactly what
would be sent without sending it. Nothing else in Pulse leaves the machine.

## Use it

```sh
./pulse
./pulse --once
./pulse next
./pulse repos
./pulse pipelines
./pulse doctor
./pulse prompt 1
./pulse dismiss 1
./pulse ideas
./pulse did 1
./pulse agents
```

Bare `pulse` on a terminal is a live pane: it paints the dashboard and stays,
repainting local Git state every minute and GitHub every five. Use `--once` for
a single glance. When output is piped or `--json` is used, Pulse is one-shot
automatically. `next`, `repos`, `doctor`, `prompt`, and `dismiss` are always
one-shot.

Live mode uses the terminal's alternate screen, so its repaints do not enter
scrollback, and Ctrl-C restores the screen that was visible before. `pulse
--once` prints to the normal screen and stays in scrollback.

It favors closing open loops, publishing finished work, keeping repositories
organized, improving terminal tooling, and putting repetitive work on autopilot.
It does not invent app bugs for you to fix.

## The live pane

Up and down arrows move through the moves and wrap at either end. Enter copies
the selected move's full agent prompt to the clipboard and flashes its number
badge. Ctrl-R rescans immediately, GitHub included, without resetting numbering.
Ctrl-C quits.

The pane lays out to the real terminal width from 60 columns up (132 at most) so
nothing wraps, and needs about 20 rows. As the window gets shorter it folds in a
fixed order rather than scrolling: fewer moves first, then the runway keeps only
its count. Below the minimum it shows the wordmark and the size it needs, and
returns the moment you resize.

Numbers are never reused within a session. When an item's condition clears, it
shows a dim check in place for about five minutes and then leaves; new arrivals
take the next unused number. Ctrl-C and rerun to start at 1 again.

While a pane is open it owns the numbering that `prompt` and `dismiss` resolve
against, so a `pulse --once` in another terminal renders a fresh snapshot without
overwriting the pane's numbers, and says so. A pane that dies without cleaning up
releases its claim after a few minutes.

`pulse prompt 1` turns a displayed recommendation into an agent-ready prompt.
`prompt` and `dismiss` number against the list you were last shown, which Pulse
remembers in its state file along with the vibe and limit you used. If the item
you pick no longer appears in a fresh scan, Pulse still prints the prompt and
notes on stderr that it may already be resolved.

Pick the kind of work that sounds good today, or emit JSON for another workflow:

```sh
./pulse --vibe hygiene     # also: github, terminal, workflow, organize, build
./pulse --local
./pulse --json > /tmp/project-pulse.json
```

## Worth considering

Everything above the `// WORTH CONSIDERING` line is derived from local state.
Nothing in Git can imply "write an agent for this", so the band below it is
written by a model and holds three to five deliberately ambitious moves: a new
automation, an agent or pipeline, a script, a technology worth adopting here.
Refactors, cleanups, and "add tests" are out of scope; the runway above covers
maintenance.

**The band only changes when you ask.** There is no timer and no scan trigger. An
idea you have not retired survives every refresh verbatim, and a refresh only
fills the slots you have freed. With the band full, `--refresh` refuses before it
spends anything.

```sh
./pulse ideas                      # show the band
./pulse ideas --refresh            # write a new set with the model (spends tokens)
./pulse ideas --type agent         # filter the band by kind
./pulse did 7                      # retire one you acted on
./pulse dismiss 1                  # hide one without marking it done
./pulse dismissed                  # review hidden ideas
./pulse restore 1                  # bring one back
```

An idea carries the build-it prompt its generator wrote; `pulse prompt 7` prints
that prompt followed only by its Pulse ID. A dismissed idea stays stored, so a
refresh will not re-propose it under the same name.

Refreshing is a two-sided contract, so an agent you are already talking to can
write the band instead of Pulse calling out itself:

```sh
./pulse ideas --pack               # dump the context pack Pulse would send
./pulse ideas --set -              # read ideas back in from stdin
```

Ideas come back as `{"ideas": [{"kind", "title", "detail", "prompt"}]}`. A
payload that is incomplete or names an unknown kind is refused whole, and a
failed refresh leaves the stored band untouched. Pulse enforces the spread across
kinds itself, on what a pass brings in rather than on what you kept, so a band can
end a pass one idea short but never says the same thing three ways. Override the
model with `PULSE_IDEAS_MODEL`.

## Configuration

Pulse runs with no configuration at all. Anything site-specific lives in a TOML
file: copy `pulse.toml.example` to `~/.config/project-pulse/pulse.toml`, or point
`$PULSE_CONFIG` at one.

```toml
root = "~/Projects"
workflows_dir = "AgentWorkflows"

[[pipeline]]
name = "nightly-capture"
detect = "demo-app/scripts/record-capture.sh"
start_command = "cd demo-app && make capture"
log = "~/logs/capture-runs.log"
```

The workspace root resolves in this order: `--root`, `$PROJECTS_ROOT`, the config
file's `root`, then `~/Projects`. A `[[pipeline]]` entry is skipped unless its
`detect` path exists, so one config can describe several machines. A missing or
malformed config is not an error; Pulse falls back to its defaults.

## GitHub enrichment

When the GitHub CLI is authenticated, Pulse adds authored pull requests that need
a decision and pull requests waiting for your review. Check the connection with
`gh auth status` and `pulse doctor`. Without GitHub, every local feature still
works and the dashboard explains how to restore enrichment.

## What it currently notices

- staged, modified, untracked, and conflicted paths
- branches without an upstream
- branches ahead of or behind their upstream
- repositories with no origin remote (unless declared local-only, below)
- old topic branches that need a decision
- testable projects without GitHub Actions
- projects without a README
- terminal recipes that could become real commands
- stale authored PRs, draft PRs, and review requests
- unattended agent pipelines in `AgentWorkflows/`

Recommendations are ranked, then diversified so the default screen offers
different kinds of work instead of five versions of the same cleanup.

## Repositories that stay off GitHub

A repository with no origin remote is asked, every scan, whether it belongs on
GitHub. Answer it once, in the repository itself:

```sh
git -C ~/Projects/Example config --bool pulse.localOnly true
```

Pulse then lists it as `local-only (declared)` in `pulse repos`. Unset it to
reopen the question. Only the publish decision is settled; every other card still
applies.

## Live agents

`pulse agents` shows the interactive Claude Code and Codex sessions running on
this machine, the counterpart to the unattended pipelines in the dashboard's
footer. It is a pane of its own, redrawn once a second: it runs no repository or
GitHub scan, claims no numbering, and answers only Ctrl-C. `pulse agents --once`
prints a single frame. The dashboard does not carry the band.

Every agent gets the same transparent, equal-size box, and the glyph inside it is
the status display. It never paints a background, so a translucent terminal stays
translucent. The grid fits two through six complete cards per row and never
compacts them; extra sessions fill identical rows when height permits, and only
sessions beyond the visible rows are reported as `+N QUEUED`.

- **Sources.** Claude sessions come from `claude agents --json`, refreshed every
  five seconds, with activity read from the matching
  `~/.claude/projects/<slug>/<session>.jsonl`. An unreadable inventory fails
  closed for that refresh rather than letting a stale record claim a card. Codex
  sessions come from `~/.codex/thread-writer-locks/<id>.lock` and the matching
  rollout under `~/.codex/sessions/`.
- **What is shown.** Provider, the workspace folder, the session's state, how
  long it has been in that state, and a one-word activity: a tool name or a file
  basename. Prompts, tool arguments, tool output, answers, and full paths never
  reach the screen.
- **States.** Five: `still`, `thinking`, `editing`, `finishing` (held two minutes
  after a turn ends), and `waiting`. Waiting is strict: an unanswered
  `AskUserQuestion` (Claude) or `request_user_input` or escalated approval
  (Codex), held until the answer lands or the session closes. Silence alone is
  never waiting.
- **Positions.** The first six positions are stable and sessions are admitted in
  start-time order; a first-row position never moves while it is live. A closed
  session fades for five seconds, then the oldest queued session takes its place.
  Assignments live in memory only, so restarting rebuilds the order from the
  providers' start times.

## Agent pipelines

The dashboard summarizes orchestrators in the workspace's `AgentWorkflows/`
directory, plus any pipeline declared in `pulse.toml`. It reads their launchers,
logs, queues, and review records without touching configuration or code.
Configured workflows with no history show as `READY`; after activity they report
`IDLE` or `QUEUED` with a run count. `pulse pipelines` adds last-run time and log
detail, and the same data appears under `pipelines` in `--json`.

## Decisions

A few behaviors look arbitrary until you know what they were weighed against.

**A refresh fills the slots you freed; it never rewrites the band.** The model is
the expensive, non-deterministic part, and an idea you kept is a judgment you
already made. Regenerating everything is less code, and it silently discards
choices and charges you for the privilege.

**The agent band shows a basename, never a path.** It reads live session files
that hold prompts, tool arguments, and tool output. The more useful band, showing
the path being edited, turns every screenshot into a disclosure. A test asserts
it by feeding in a record carrying both a path and a password and checking that
neither survives.

**Untrusted text is cleaned where it arrives, not where it prints.** Sanitizing
at each print site means finding every one of them again next time, and still
leaves raw bytes in the state file and on the clipboard, which is the one that
matters since Pulse trains you to press Enter and paste.

**A malformed config is not an error.** Failing loudly on bad TOML is correct for
a build tool and wrong for an instrument you leave open, where a dashboard that
refuses to start is worse than one that starts knowing less.

**A Codex session is proved live, not assumed live.** A thread-writer lock file
is empty; its meaning is the kernel advisory lock the running thread holds on it,
and Codex leaves the file behind when a terminal is closed on it. Pulse takes a
shared non-blocking `flock` and reads the answer from contention. Any error other
than contention counts as held, so an odd filesystem shows a session rather than
hiding one.

## Development

```sh
python3 -m unittest discover -s tests -v
```

That same command runs in GitHub Actions on every push and pull request, against
Python 3.11 and 3.13.
