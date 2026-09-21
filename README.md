<div align="center">

# Multi Agent Kernel (MAK)

<img src="https://img.shields.io/badge/3.11-grey?logo=python"/>
<img src="https://img.shields.io/badge/Version-0.8.1 Beta-blue"/> 
<img src="https://img.shields.io/badge/CI-Passing-green?logo=github"/> 
<img src="https://img.shields.io/badge/License-MIT-red"/> 

---

</br>

A kernel for **concurrent** multi-agent python software development.

Multiple agents edit one shared working directory at the same time.

No worktrees, no merge step, no late-stage reconciliation.

The Multi Agent Kernel arbitrates concurrent access the way an OS
arbitrates shared memory between threads.

![](graphics/02-shared-memory.png)

</div>

</br>

## Table of Contents

- [The Idea](#the-idea)
- [Install](#install)
- [Update](#update)
- [Run](#run)
  - [CLI App](#cli-app)
  - [CLI Command](#cli-command)
- [Local Models](#local-models)
- [Custom & OpenAI-Compatible Endpoints](#custom--openai-compatible-endpoints)
- [Configuration & API Keys](#configuration--api-keys)
- [Benchmark](#benchmark)
- [Contribute](#contribute)
- [License](#license)

## The Idea

### Traditional

Traditional **multi-agent coding systems** give each agent a Git branch and merge at the end —
a **message-passing** model where conflicts surface late, after the dependency
information needed to resolve them is gone.

![](graphics/04-worktrees-vs-mak.png)

### Multi-Agent Kernel

**MAK** takes the **shared-memory** approach instead: the codebase is decomposed into
independently lockable `AST nodes` (functions, methods, classes, headers), making it possible for multiple agents to edit the same file at the same time.

Files
on disk are derived artifacts reconstructed from a `versioned node store`. A `symbol-level lock table` resolves conflicts at ***scheduling*** time, while the
dependency graph is still explicit, so each agent edits only the nodes it holds
write locks on and the kernel reassembles the file. 

![](graphics/01-shared-file.png)

### Waves

> The planner is prompted to organizes jobs into **Waves**, maximizing parallelism by grouping jobs that can run concurrently without competing for write locks on the same AST nodes.

Before dispatching the agents, the planner's proposed
plan is cross-checked against the dependency graph. After a
wave, MAK re-checks what it left behind and offers any fix-ups as another
reviewable plan.

![](graphics/03-inside-the-kernel.png)


See [CONTRIBUTING.md](CONTRIBUTING.md) for the full architecture, or the
[diagrams](diagram/).

## Install

> Prerequisites: **Python ≥ 3.11**, **[uv](https://docs.astral.sh/uv/) (or [pipx](https://github.com/pypa/pipx))**, **git**

**uv (Recommended)**
```bash
uv tool install git+https://github.com/chaseungjoon/multi-agent-kernel
```

**pipx**
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

> ⚠️ The update feature is only supported for ***uv*** installs.

```bash
mak update
```

`mak update` moves to the newest **published release tag**.  Until this repo publishes its first tag, `mak update` falls back to
the tip of `main`.

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
* `/local` - Overview of this machine's runtimes and connected remote hosts; `/local url <host:port>` connects (and remembers) one (see [Local Models](#local-models))
* `/mode [cloud|local|hybrid]` - Show or switch how this session gets its models
* `/max-agents <int>` - Set number of concurrently running agents
* `/config` - Returns to auto-discovery (see [Configuration & API Keys](#configuration--api-keys))
* `/config /path/to/config.yaml` - Point to a custom config
* `/no-review true` - Omit user review of planner
* `/clear` - clears the screen, `/exit` (or `/quit`, Ctrl+C) quits, Ctrl+J inserts a newline for multi-line tasks.

---

### CLI Command

For scripted / non-interactive runs, use `mak run` (equivalently `python3 -m mak`
in a source checkout). Set your API keys first — see
[Configuration & API Keys](#configuration--api-keys). You only need keys for the
agents you actually run.

> ***⚠️ MAK is still in beta. So just to be safe, create a separate branch for MAK to work on***

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

> **Note on `claude-fable-5` and `claude-fable-5-1`:** MAK supports Anthropic's most capable model, but it
> comes with caveats — it requires an org with **30-day data retention** (zero-data-retention
> orgs get a 400 on every request), and it can decline requests with a `refusal` stop reason
> (which MAK treats as a failed task).

## Local Models

MAK supports local LLMs via an OpenAI-compatible server.

```bash
mak       #  choose "local" at first-run setup, or /local url http://host:port
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


MAK also sizes the model's context window for you and refuses a bundle that
would not fit, rather than letting it be silently truncated into a wrong
answer. 

If your local model plans worse than it edits, pair it with a hosted
planner — `mak.yaml` naming a cloud `planner.model` beside local `agents:` — the
first-run local setup recommends this automatically for smaller models. 

See
[mak/examples/](mak/examples/) for ready-made configs and [CONTRIBUTING.md §7.7/§14](CONTRIBUTING.md) for
the full detail.

## Custom & OpenAI-Compatible Endpoints

Beyond the three built-in providers, MAK can talk to **any service that speaks
the OpenAI Chat Completions API** — NVIDIA Build, OpenRouter, DeepSeek, Z.ai, a
self-hosted vLLM gateway, or anything else. Add one from the interactive CLI:

```bash
mak       #  then: /endpoint add
```

`/endpoint add` walks you through a preset (NVIDIA, OpenRouter, DeepSeek, Z.ai)
or a fully custom service, asks for the **name of the environment variable**
holding your key (never the key itself), and saves it to
`~/.config/mak/endpoints.json` so it's there on your next run. Other useful
sub-commands: `/endpoint list`, `/endpoint test <id>`, `/endpoint models <id>`,
`/endpoint export <id>` (prints a pasteable, secret-free `mak.yaml` block).

Once configured, an endpoint id works anywhere a provider name does:

```bash
export NVIDIA_API_KEY=...
mak run --task "your task" --work-dir /path/to/project \
  --models nvidia:meta/llama-3.3-70b-instruct

# Several models on the same endpoint, or several endpoints, in one run:
mak run --task "your task" --work-dir /path/to/project \
  --models nvidia:meta/llama-3.3-70b-instruct nvidia:qwen/qwen2.5-coder-32b-instruct \
           openrouter:some/model
```

Or non-interactively:

```bash
mak examples hosted-openai-compatible > mak.yaml   # NVIDIA Build, ready to edit
mak examples custom-endpoint > mak.yaml            # a provider-neutral template
```

**Models that don't support structured outputs work anyway.** MAK asks for a
strict JSON schema when it can, because that is what stops an agent replying
with prose instead of a result. Plenty of models — most free OpenRouter routes,
and anything behind an upstream provider that hasn't implemented it — don't
accept that request. You don't have to know which, or configure anything: MAK
reads what the endpoint publishes about each model, asks for the strongest
reply format that model actually supports, and falls back to a prompt-only JSON
contract for the ones that support none. A model whose limits aren't published
is discovered once per session, not once per task.

This is per **exact** model id, suffix included: `some/model` and
`some/model:free` are different products and are often routed to different
providers with different capabilities.

MAK never accepts a raw API key in a config file, on the command line, or in a
log — only the *name* of the environment variable that holds it. See
[mak/endpoints/profiles.py](mak/endpoints/profiles.py) for the preset table and
[CONTRIBUTING.md](CONTRIBUTING.md) (search "Universal OpenAI-compatible
endpoints" and "Capability-aware OpenRouter") for the full design.

## Configuration & API Keys

> MAK drives hosted models from **three built-in providers — Anthropic, OpenAI,
and Google Gemini** — plus any number of custom endpoints (see
[above](#custom--openai-compatible-endpoints)).

Keys are read from the environment
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, or an endpoint's own
configured variable) or from `~/.config/mak/.env`.

The TUI's `/apikey` command (and its first-run setup)
writes them there for you, creating the file readable only by you.
Exported environment variables always win.

> When `--config` (or `/config`) is not given, MAK auto-discovers
its configuration, first match wins:

1. `./mak.yaml` — a per-project config in the current directory
2. `~/.config/mak/config.yaml` (respects `$XDG_CONFIG_HOME`) — your user default
3. The built-in default shipped with the package ([view it](mak/config.yaml))

To customize, copy the built-in default to either location and edit it.



**Capping what a run costs.** Nothing bounds a run's spend by default: retries,
iterations, and cascade waves multiply out. Set `session.max_total_tokens` to cap
it

```yaml
session:
  max_total_tokens: 2000000   # unset (the default) is unbounded
```

**Keeping files out of MAK.** On its first run in a project, MAK creates a
`.makignore` next to your code with `.mak/` and `.git/` already listed. Edit it like a
`.gitignore` to keep generated, vendored, or scratch code out of the node store:

```gitignore
.mak/
.git/
scratch/
/legacy_script.py
migrations/*.py
!migrations/keep_me.py
```

A path you add is dropped from the node store on the next run; the file itself is not
touched. MAK always skips its own `.mak/` folder, even if you remove that line. See
[CONTRIBUTING.md §3.1.1](CONTRIBUTING.md#311-makignore--the-projects-own-ignore-list--makignorepy)
for the full syntax.

**One MAK per project.** A session takes an exclusive lease on the project's `.mak/`
before it touches anything, so a second `mak` on the same checkout fails
immediately.

**Semantic conflicts.** Node-level locks stop two agents from writing the same
symbol at once, but not two edits on *different* symbols that are each correct
alone and wrong together (a stale read, a signature changed under a new call, a
duplicated registry key). MAK also tracks and validates this: every task's
context is version-stamped and re-checked at commit, interface changes are
locked apart from body changes, and anything still slipping through is caught
and offered as a fix-up wave, same as any other cascade. It is on by default and
tunable under `semantic:` in `mak/config.yaml`; see
[CONTRIBUTING.md §5.2](CONTRIBUTING.md#52-semantic-conflicts-wave-20) for the
full mechanism and a worktree comparison.

## Benchmark

MAK has two complementary benchmark suites. The **real-model suite** measures
end-to-end coding quality and cost on fixed projects. The **simulated suite** holds
agent behavior constant to measure coordination as contention changes.

### Real-model benchmark

> Both workflows receive the same workload, models, and task assignments.

#### Partially contended workload

[`project_template_3`](benchmark/project_template_3/) — 58 tasks

| | MAK | Traditional |
|---|---|---|
| Avg. Tokens | **13,911** | 16,291 |
| Avg. Time | **57.07s** | 74.12s |
| Avg. Accuracy | **75%** (111.4/148) | 63% (93.7/148) |
| Avg. Merge conflicts | **0** | 4 |

#### Single-hot-symbol stress test

[`project_template_2`](benchmark/project_template_2/) — 90 operations across 9 modules

| | MAK | Traditional |
|---|---|---|
| Avg. Tokens | **18,339** | 23,760 |
| Avg. Time | 226.5s | **99.5s** |
| Avg. Accuracy | **94%** (253.1/270) | 93% (251.6/270) |
| Avg. Merge conflicts | **0** | 2 |

MAK used **15–23% fewer tokens** and avoided merge conflicts in both workloads. It
was faster when contention was spread across the project, but slower when every
task targeted one symbol, which is a limitation by design.

### Simulated scaling benchmark

![Four-agent MAK and worktree makespan under uniform and Zipf contention.](graphics/06-simulated-scaling-results.png)

The keyless smoke sweep uses real MAK coordination and real Git worktrees and
merges; only agent latency, token use, correctness, and conflict resolution are
modeled.

With four agents, node-level MAK finished in about **21 seconds under**
both contention shapes. File-level locking rose to 29 seconds for uniform and
53 seconds for [Zipf contention](https://en.wikipedia.org/wiki/Zipf%27s_law), while merge-at-end lost one Zipf registration.

### Reproduce results

```bash
# Real-model benchmark
python3 benchmark/run_benchmark.py --mode real \
  --models anthropic:claude-opus-5 --agents 3

# Keyless simulated smoke sweep
python3 benchmark/sweep.py --config benchmark/sweeps/smoke.yaml --fresh
```

[Benchmark details](benchmark/README.md) · [Real-model statistics](benchmark/STATS.md) ·
[Scaling verdicts](benchmark/sim/RESULTS.md)

## Contribute

[**CONTRIBUTING.md**](CONTRIBUTING.md) is the full guide — architecture, every
subsystem in depth, setup, the quality gates, coding standards, and where to help.

Everyone participating in this project is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) © 2026 Seungjoon Cha
