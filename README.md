<div align="center">

# Multi Agent Kernel (MAK)

<img src="https://img.shields.io/badge/3.11-grey?logo=python"/>
<img src="https://img.shields.io/badge/Version-0.6.1 Beta-blue"/> 
<img src="https://img.shields.io/badge/CI-Passing-green?logo=github"/> 
<img src="https://img.shields.io/badge/License-MIT-red"/> 

---

</br>

A kernel for **concurrent** multi-agent software development.

Multiple agents edit one shared working directory at the same time.

No worktrees, no merge step, no late-stage reconciliation.

The Multi Agent Kernel arbitrates concurrent access the way an OS
arbitrates shared memory between threads.

</div>

</br>

## Table of Contents

- [The Idea](#the-idea)
- [Install](#install)
- [Update](#update)
- [Housekeeping](#housekeeping)
- [Run](#run)
  - [CLI App](#cli-app)
  - [CLI Command](#cli-command)
- [Local Models](#local-models)
- [Configuration & API Keys](#configuration--api-keys)
- [Benchmark](#benchmark)
- [Contribute](#contribute)
- [License](#license)

## The Idea

Most multi-agent coding systems give each agent a Git branch and merge at the end —
a **message-passing** model where conflicts surface late, after the dependency
information needed to resolve them is gone.

MAK takes the **shared-memory** approach instead: the codebase is decomposed into
independently lockable `AST nodes` (functions, methods, classes, headers), and files
on disk are derived artifacts reconstructed from a `versioned node store`. A
`symbol-level lock table` resolves conflicts at *scheduling* time, while the
dependency graph is still explicit, so each agent edits only the nodes it holds
write locks on and the kernel reassembles the file. Around those write targets the
kernel automatically builds the agent's read context — same-file siblings,
cross-file callers, and a dependency's just-built output — budget-bounded so it
stays relevant rather than growing with the repo; a task that would arrive with no
context at all is a kernel bug, not a shrug. Before dispatch, the planner's proposed
plan is cross-checked against that same dependency graph — grounding hallucinated
node ids and correcting bad edges before they reach the scheduler — and after a
wave, MAK re-checks what it left behind and offers any fix-ups as another
reviewable plan.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full architecture, or the
[knowledge graph](https://mak-kg.vercel.app) (built with
[graphify](https://github.com/safishamsi/graphify)).

## Install

> Prerequisites: **Python ≥ 3.11**, **[uv](https://docs.astral.sh/uv/) (or [pipx](https://github.com/pypa/pipx))**, **git**

**With uv (Recommended)**
```bash
uv tool install git+https://github.com/chaseungjoon/multi-agent-kernel
```

**With pipx**
```bash
pipx install git+https://github.com/chaseungjoon/multi-agent-kernel
```

```bash
mak --version
```

<details>
<summary><b>From source</b> (for contributors)</summary>

```bash
git clone https://github.com/chaseungjoon/multi-agent-kernel
cd multi-agent-kernel
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Run binary
./bin/mak
```

</details>

## Update

```bash
mak update
```

`mak update` moves to the newest **published release tag**, prints the version it
is moving to before installing, and reports honestly when you are already current.
It only ever updates a `uv tool` install; a source checkout is left alone (use
`git pull`). Until this repo publishes its first tag, `mak update` falls back to
the tip of `main` and says so.

## Housekeeping

```bash
mak gc              # prune this project's node store
mak gc /path/to/project
```

Every edit MAK commits writes a new version of the node into `.mak/node_store/`.
Recent versions are kept so a bad edit can be rolled back — five by default,
tunable with `node_store.version_retention` (minimum 2, or `-1` to keep every
version forever) — and anything older is pruned as the commit lands. `mak gc`
applies that policy to a store written by an older MAK, which kept everything,
and removes fragment directories that no node addresses any more. It takes the same
one-owner-per-project lease a run does, so it will not prune versions out from under
a session that is still working.

## Run

> ⚠️ ***Currently, MAK only supports Python codebases***, there are plans to add other language support in the near future.

### CLI App

Launch the interactive app from any directory:

```bash
mak
```

![](screenshots/mak-cli.png)

**Features**

> Type `/` to browse all commands with one-line descriptions. (Tab autocomplete)
>
> `/help` lists commands and shortcuts.

* `/status` - Live session status (models, planner, agents, workdir, approval, tokens)
* `/apikey` - Set api keys of providers
* `/work-dir <path>` - Set working directory
* `/models <provider-1>:<model> <provider-2>:<model> ...` - Set agent models
* `/planner <provider>:<model>` - Set planner model
* `/refresh-models` - Re-fetch the model list from each provider right now
* `/local` - Detect a local runtime, pull a model, run fully offline (see [Local Models](#local-models))
* `/mode [cloud|local|hybrid]` - Show or switch how this session gets its models
* `/max-agents <int>` - Set number of agents
* `/config` - Returns to auto-discovery (see [Configuration & API Keys](#configuration--api-keys))
* `/config /path/to/config.yaml` - Point to a custom config
* `/no-review true` - Omit user review of planner (default false, not recommended to turn on)
* `/clear` - clears the screen, `/exit` (or `/quit`, Ctrl+C) quits, Ctrl+J inserts a newline for multi-line tasks.

---

### CLI Command

For scripted / non-interactive runs, use `mak run` (equivalently `python3 -m mak`
in a source checkout). Set your API keys first — see
[Configuration & API Keys](#configuration--api-keys). You only need keys for the
agents you actually run.

> ***⚠️ Just to be safe, create a separate branch for MAK to work on***

```bash
# Example with claude opus 5, gpt-5.6 sol and gemini 3.5 flash
mak run --task "your task" --work-dir /path/to/project \
  --models anthropic:claude-opus-5 openai:gpt-5.6-sol gemini:gemini-3.5-flash

# Example with claude sonnet 5 X 5 (provider default model)
mak run --task "your task" --work-dir /path/to/project \
  --models anthropic --max-agents 5
```

**Command line arguments**
```bash
# Describe task
--task "Describe your task here"

# Set working directory
--work-dir /path/to/project

# Omit human review (Not recommended)
--no-review

# Resume a crashed run from .mak/task_graph.json (no --task needed)
--recover

# Default model
--models anthropic
--models openai
--models gemini

# Set model
--models anthropic:claude-opus-5
--models openai:gpt-5.6-terra
--models gemini:gemini-3.1-pro-preview

# Use multiple providers (tasks are distributed round-robin across them)
--models anthropic openai gemini
--models anthropic:claude-opus-5 openai:gpt-5.6-sol gemini:gemini-3.5-flash

# Use single provider with multiple agents
--models anthropic --max-agents 5 
--models anthropic:claude-opus-5 --max-agents 3

# Local models — no API key needed (see Local Models below)
--models ollama:qwen2.5-coder:14b
--models local:my-model@http://localhost:8000/v1

# Choose a custom config file (default: auto-discovered, see below)
--config /path/to/config.yaml

```

**[Default models list for each provider](mak/config.yaml)** — kept current
automatically: MAK re-fetches each provider's model list in the background twice a
month (1st and 15th), so new models show up in `/models` and `/planner` without an
update. Run `/refresh-models` to fetch immediately instead of waiting.

> **Note on `claude-fable-5`:** MAK supports Anthropic's most capable model, but it
> comes with caveats — it requires an org with **30-day data retention** (zero-data-retention
> orgs get a 400 on every request), it can decline requests with a `refusal` stop reason
> (which MAK treats as a failed task), and it is priced above Opus tier ($10/$50 per MTok).
> MAK prints this warning whenever you select it as a planner or agent model.

## Local Models

MAK runs against a model on your own machine — no API key, no data leaving it.

```bash
mak                 # → choose "Local" → /local detects a runtime, pulls a
                     #   model if none is installed, and you're running
```

Or non-interactively:

```bash
mak examples local-ollama > mak.yaml   # a ready-to-run config for Ollama
mak run --task "your task" --work-dir /path/to/project

# or point directly at a runtime, no config file needed
mak run --task "your task" --work-dir /path/to/project \
  --models ollama:qwen2.5-coder:14b
```

`ollama:<model>` talks to [Ollama](https://ollama.com)'s native API and defaults
to `http://localhost:11434`; `local:<model>@<url>` talks to any
OpenAI-compatible server (vLLM, LM Studio, llama.cpp) at an explicit endpoint.
Neither needs a key, and **a real cloud API key already in your environment is
never sent to a local endpoint** — MAK forwards only what you explicitly
configure, or a harmless placeholder.

MAK also sizes the model's context window for you and refuses a bundle that
would not fit, rather than letting it be silently truncated into a wrong
answer. If your local model plans worse than it edits, pair it with a hosted
planner — `mak.yaml` naming a cloud `planner.model` beside local `agents:` — the
`/local` wizard recommends this automatically for smaller models. See
[`mak/examples/`](mak/examples/) for ready-made configs (`local-ollama`,
`local-openai-compatible`, `hybrid-cloud-planner-local-agents`,
`fully-local-offline`), and [CONTRIBUTING.md §7.7/§14](CONTRIBUTING.md) for
the full detail.

## Configuration & API Keys

**API keys.** MAK drives hosted models from **three providers — Anthropic, OpenAI,
and Google Gemini** — plus any local runtime, which needs none. Keys are read from the environment
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`) or from
`~/.config/mak/.env` — the TUI's `/apikey` command (and its first-run setup)
writes them there for you, creating the file readable only by you (`0600`).
Exported environment variables always win.

> **Deprecated:** a source checkout's `mak/.env` is still read, but it lives
> inside the package directory and nothing enforces its permissions — a working
> copy is routinely left world-readable with live keys in it. MAK now warns when
> it reads one; move your keys to `~/.config/mak/.env` (or just run `/apikey`).
> The next release stops reading the legacy location.

**Config file.** When `--config` (or `/config`) is not given, MAK auto-discovers
its configuration, first match wins:

1. `./mak.yaml` — a per-project config in the current directory
2. `~/.config/mak/config.yaml` (respects `$XDG_CONFIG_HOME`) — your user default
3. The built-in default shipped with the package ([view it](mak/config.yaml))

To customize, copy the built-in default to either location and edit it.

**What MAK reads.** `node_store.include_patterns` / `exclude_patterns` decide which
files are ingested. Setting `exclude_patterns` **replaces** the defaults, so start
from the shipped list rather than writing a shorter one — it excludes generated and
vendored directories (`.git`, `build`, `dist`, `.tox`, `.mypy_cache`,
`.pytest_cache`, `site-packages`, `node_modules`, `.venv`, `__pycache__`) as well as
MAK's own `.mak/` store. MAK skips its own `.mak/` directory regardless of what you
configure, and prunes any node left behind by an older version that did ingest it —
if you have a `.mak/` from before v0.5.3, the next run cleans it up (deleting the
directory yourself is the blunt alternative).

**Capping what a run costs.** Nothing bounds a run's spend by default: retries,
iterations, and cascade waves multiply out. Set `session.max_total_tokens` to cap
it — input plus output, every agent call plus the planner's, counted from what
each provider reported on its own response. On a breach MAK stops dispatching,
lets what is already in flight finish and commit, and reports the run as failed
naming the budget. It never interrupts a commit, so the working tree is never
left half-written.

```yaml
session:
  max_total_tokens: 2000000   # unset (the default) is unbounded
```

**When someone edits a file MAK manages.** MAK's node store — not the filesystem —
is its source of truth, so it has to reconcile with the working tree each time it
starts. Edit, rename, or delete anything between runs and the next session adopts
it: your edit becomes the node's next version, a deleted function is retired (its
history kept, but no longer reconstructed), and a deleted file's nodes go with it.
This is the default because the alternative is MAK overwriting your work. If you
would rather it stop and let you look, set `session.on_external_edit: "conflict"` —
it then refuses to start on a file that changed underneath it, *before* planning, so
no agent is ever handed content your tree no longer holds.

**One MAK per project.** A session takes an exclusive lease on the project's `.mak/`
before it touches anything, so a second `mak` on the same checkout fails
immediately, naming the process that holds it — rather than the two of them
interleaving writes to the same nodes. Different projects run concurrently as usual.
If a run is killed, the lease is released by the operating system, so the next one
starts normally with nothing to clean up.

**Pushing.** `git.auto_push` only fires when the whole run succeeded — every wave,
with no cascade wave declined or cut short — **and** your test suite actually passed.
"No `test_command` configured" is reported as *skipped*, not as a pass, so a project
with no suite never auto-pushes; set `session.test_policy: "allow_skip"` if you want
it to anyway. MAK's own commits are always scoped to the files a task changed and
are built in a private Git index, so whatever you have staged is neither committed
nor disturbed.

```yaml
session:
  on_external_edit: "adopt"     # or "conflict" — stop when a file changed
  test_policy: "require_pass"   # or "allow_skip" — for a project with no suite
git:
  require_clean_tree: false     # true = refuse to start on a dirty tree
```

**Where `.mak/` lives.** MAK's node store, task graph, and session log always live
under `--work-dir` (default `session.mak_dir` is `.mak`, relative to the project) —
never relative to the directory you happened to launch `mak` from. This matters when
you drive more than one project from the same shell: each project keeps its own
state, so a run against `../other-project` can never read or write another
project's `.mak/`. If a stale `.mak/` from before this was fixed sits next to your
shell, MAK reports it on stderr and leaves it alone rather than adopting it — delete
it by hand once you've confirmed you don't need it.

## Benchmark

[`benchmark/`](benchmark/) pits MAK against a traditional git-worktree multi-agent workflow on
the same workload with the same agents (3× `claude-sonnet-4-6`). Every operation **must
edit one shared registry function**. The numbers below are the **mean of 10 independent runs**

- [`benchmark/project_template_2/`](benchmark/project_template_2/) — 90 operations, 9 modules

  | | MAK | Git worktrees |
  |---|---|---|
  | Avg. Tokens | **18,339** | 23,760 |
  | Avg. Time | 226.5s | **99.5s** |
  | Avg. Accuracy | **94%** (253.1/270) | 93% (251.6/270) |
  | Avg. Merge conflicts | **0** | 2 |

> MAK spends **23% fewer tokens** and hits **zero merge conflicts** by construction. It also has a slight edge in accuracy.
>
> [More statistics](/benchmark/STATS.md)

Both sides got a few of the harder algorithms wrong, but the worktree side
additionally resulted in **2 merge conflicts.**

MAK is **slower** than traditional worktree based operations because every task contends on that one symbol, so MAK
serializes those writes while the worktrees edit in parallel and reconcile afterward: the
trade is **correctness by construction** and **token efficiency** for execution time on a deliberately
maximally-contended workload. 

Run it yourself (all targets) with

```bash
python3 benchmark/run_benchmark.py --mode real \
  --models anthropic:claude-sonnet-5 anthropic:claude-sonnet-5 anthropic:claude-sonnet-5
```

## Contribute

[**CONTRIBUTING.md**](CONTRIBUTING.md) is the full guide — architecture, every
subsystem in depth, setup, the quality gates, coding standards, and where to help.

Everyone participating in this project is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) © 2026 Seungjoon Cha
