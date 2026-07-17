<div align="center">

# Multi Agent Kernel (MAK)

<img src="https://img.shields.io/badge/3.11-grey?logo=python"/>
<img src="https://img.shields.io/badge/Version-0.3.1 Beta-blue"/> 
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

> Check out the [knowledge graph](https://mak-kg.vercel.app) for this project. (created with [graphify](https://github.com/safishamsi/graphify))

## Install

**Python ≥ 3.11**

```bash
# with uv (recommended)
uv tool install git+https://github.com/chaseungjoon/multi-agent-kernel

# or with pipx
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

## Run

> ⚠️ ***Currently, MAK only supports Python codebases***, there are plans to add other language support in the near future.

### CLI App

Launch the interactive app from any directory:

```bash
mak
```

![](screenshots/mak-cli.png)

**Features**

* Type `/` to browse all commands with one-line descriptions; Tab completes. `/help` lists commands and shortcuts.
* Live session status (models, planner, agents, workdir, approval, tokens) in a toolbar under the prompt — `/status` prints the full detail.
* Set api keys of providers with `/apikey` command.
* Set working directory with `/work-dir <path>`
* Set models with `/models <provider-1>:<model> <provider-2>:<model> ... `
* Set planner model with `/planner <provider>:<model>` 
* Set number of agents with `/max-agents <int>`
* Point to a custom config with `/config /path/to/config.yaml` — bare `/config` returns to auto-discovery (see [Configuration & API Keys](#configuration--api-keys))
* Omit user review of planner with `/no-review true` (default false, not recommended to turn on)
* `/clear` clears the screen, `/exit` (or `/quit`, Ctrl+C) quits, Ctrl+J inserts a newline for multi-line tasks.

---

### CLI Command

For scripted / non-interactive runs, use `mak run` (equivalently `python3 -m mak`
in a source checkout). Set your API keys first — see
[Configuration & API Keys](#configuration--api-keys). You only need keys for the
agents you actually run.

> ***⚠️ Just to be safe, create a separate branch for MAK to work on***

```bash
# Example with claude opus 4.8, gpt-5.6 sol and gemini 3.5 flash
mak run --task "your task" --work-dir /path/to/project \
  --models anthropic:claude-opus-4-8 openai:gpt-5.6-sol gemini:gemini-3.5-flash

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

# Default model
--models anthropic
--models openai
--models gemini

# Set model
--models anthropic:claude-opus-4-8
--models openai:gpt-5.6-terra
--models gemini:gemini-3.1-pro-preview

# Use multiple providers
--models anthropic openai gemini
--models anthropic:claude-opus-4-8 openai:gpt-5.6-sol gemini:gemini-3.5-flash

# Use single provider with multiple agents
--models anthropic --max-agents 5 
--models anthropic:claude-opus-4-8 --max-agents 3

# Choose a custom config file (default: auto-discovered, see below)
--config /path/to/config.yaml

```

**[Default models list for each provider](mak/config.yaml)**


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
  --models anthropic --max-agents 3
```

## Contribute

[**CONTRIBUTING.md**](CONTRIBUTING.md) is the full guide — architecture, every
subsystem in depth, setup, the quality gates, coding standards, and where to help.

## License

[MIT](LICENSE) © 2026 Seungjoon Cha
