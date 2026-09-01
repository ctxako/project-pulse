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

Arrows move through the moves and wrap at either end. Enter copies the selected
move's full agent prompt to the clipboard. Ctrl-R rescans immediately, GitHub
included. Ctrl-C quits.

The pane lays out to the real terminal width from 60 columns up (132 at most) so
nothing wraps, and needs about 20 rows. As the window gets shorter it folds
rather than scrolling, and returns the moment you resize.

Numbers are never reused within a session, and a pane owns the numbering that
`prompt` and `dismiss` resolve against while it is open. `pulse prompt 1` turns a
displayed recommendation into an agent-ready prompt; it numbers against the list
you were last shown, which Pulse remembers along with the vibe and limit you
used.

Pick the kind of work that sounds good today, or emit JSON for another workflow:

```sh
./pulse --vibe hygiene     # also: github, terminal, workflow, organize, build
./pulse --local
./pulse --json > /tmp/project-pulse.json
```

## // WORTH CONSIDERING

Everything above this line in the pane is derived from local state.
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
this machine, one equal-size card each, redrawn once a second. It is a pane of
its own: no repository or GitHub scan, no numbering, Ctrl-C to quit. `pulse
agents --once` prints a single frame.

Claude sessions come from `claude agents --json` and their session trails; Codex
sessions come from its thread-writer locks and rollouts. Each card shows the
provider, the workspace folder, one of five states (`still`, `thinking`,
`editing`, `finishing`, `waiting`), how long it has held that state, and a
one-word activity. Prompts, tool arguments, tool output, and full paths never
reach the screen.

## Agent pipelines

The dashboard summarizes orchestrators in the workspace's `AgentWorkflows/`
directory, plus any pipeline declared in `pulse.toml`. It reads their launchers,
logs, queues, and review records without touching configuration or code.
Configured workflows with no history show as `READY`; after activity they report
`IDLE` or `QUEUED` with a run count. `pulse pipelines` adds last-run time and log
detail, and the same data appears under `pipelines` in `--json`.

## Development

```sh
python3 -m unittest discover -s tests -v
```

That same command runs in GitHub Actions on every push and pull request, against
Python 3.11 and 3.13.
