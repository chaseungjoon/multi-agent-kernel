<div align="center">

# Multi Agent Kernel (MAK)

<img src="https://img.shields.io/badge/3.11-grey?logo=python"/>
<img src="https://img.shields.io/badge/Version-0.5.5 Beta-blue"/> 
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
- [Run](#run)
  - [CLI App](#cli-app)
  - [CLI Command](#cli-command)
- [Configuration & API Keys](#configuration--api-keys)
- [Benchmark](#benchmark)
- [Contribute](#contribute)
- [License](#license)


## The Idea

Most multi-agent coding systems give each agent a Git branch and merge at the end. A **message-passing** model where conflicts surface late, after the dependency
information needed to resolve them is gone.

The Multi Agent Kernel takes the **shared-memory** approach. 

- The codebase is decomposed into
independently lockable `AST nodes` (functions, methods, classes, headers). 

- Files on
disk are derived artifacts reconstructed from a `versioned node store`.

- The kernel owns a `symbol-level lock table` and resolves conflicts at *scheduling* time, where the
dependency graph is still explicit. 

- Each agent receives only the nodes it holds write
locks on, edits them in isolation, and returns the modified fragments. The kernel
reassembles the file.

- Around those write targets the kernel assembles the agent's `read context`
automatically: same-file siblings, cross-file callers, and — for a task that depends
on another — whatever that dependency just built, so an agent writing a brand-new
module is never guessing at the API of the module beside it. A task that would arrive
with *no* context at all is a kernel bug, and MAK fails it rather than shipping the
guess.

- Before dispatch, the kernel cross-checks the planner's proposed plan against that
same AST-derived dependency graph — grounding hallucinated node ids, adding missing
`depends_on` edges, and flagging spurious ones — so a bad LLM guess is corrected
before it reaches the scheduler, not after a collision.

> Check out the [knowledge graph](https://mak-kg.vercel.app) for this project. (created with [graphify](https://github.com/safishamsi/graphify))

## Install

> Prerequisites: **Python ≥ 3.11**, **[uv](https://docs.astral.sh/uv/) (or [pipx](https://github.com/pypa/pipx))**, **git**

```bash
# Recommended 
uv tool install git+https://github.com/chaseungjoon/multi-agent-kernel

# Or with pipx
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

./bin/mak
```

</details>

## Update

```bash
mak update
```

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


## Configuration & API Keys

**API keys.** MAK drives hosted models from **three providers — Anthropic, OpenAI,
and Google Gemini**. Keys are read from the environment
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`) or from
`~/.config/mak/.env` — the TUI's `/apikey` command (and its first-run setup)
writes them there for you. Exported environment variables always win.
In a source checkout, a legacy `mak/.env` is also read.

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
