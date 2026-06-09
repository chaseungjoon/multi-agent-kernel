# Multi Agent Kernel (MAK)

A kernel for **concurrent** multi-agent software development. 

Many agents edit one shared working directory at the same time — no worktrees, no merge step, no
late-stage reconciliation. The kernel arbitrates concurrent access the way an OS
arbitrates shared memory between threads.

## The idea

Most multi-agent coding systems give each agent a Git branch and merge at the end —
a **message-passing** model where conflicts surface late, after the dependency
information needed to resolve them is gone.

MAK takes the **shared-memory** approach. The codebase is decomposed into
independently lockable AST nodes (functions, methods, classes, headers); files on
disk are derived artifacts reconstructed from a versioned node store. 

The kernel owns a symbol-level lock table and resolves conflicts at *scheduling* time, where the
dependency graph is still explicit. Each agent receives only the nodes it holds write
locks on, edits them in isolation, and returns the modified fragments; the kernel
reassembles the file. Git is used only as an audit log.

## Run

### 1. Clone from source (Python ≥ 3.11):

```bash
git clone https://github.com/chaseungjoon/multi-agent-kernel
cd multi-agent-kernel
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

### 2. Set api keys for the default agents in `mak/.env`, then point MAK at a project:

```bash
touch mak/.env
echo 'ANTHROPIC_API_KEY=...' >> mak/.env
echo 'OPENAI_API_KEY=...' >> mak/.env
echo 'GEMINI_API_KEY=...' >> mak/.env

python3 -m mak --task "your task here" --work-dir path/to/project
```

MAK shows the plan for approval before editing (`--no-review` skips it); agents and
models are configured in `mak/config.yaml`. For a ready-made target, try the bundled
[demo](demo/):

```bash
python3 -m mak --task "Implement every function in the dataforge package per its docstring." \
  --config demo/config.yaml
python3 -m pytest demo/project   # verify the result
```

## Knowledge Graph

MAK utilizes [graphify](https://github.com/safishamsi/graphify)'s knowledge graph system to improve LLM understanding and visualization of the project.

check out [`graph.html`](graphify-out/graph.html) for a knowledge graph overview.


## Benchmark

[`benchmark/`](benchmark/) pits MAK against a real git-worktree multi-agent workflow on
the same workload with the same agents (3× `claude-sonnet-4-6`). The workload is 9
operations that **all must edit one shared registry function** — the contention point.

| | MAK | Git worktrees |
|---|---|---|
| Tokens | **2,052** | 3,192 |
| Time | 20.4s | **11.6s** |
| Accuracy | 100% | 100% |
| Merge conflicts | **0** | 2 |

MAK spent **36% fewer tokens** and hit **zero merge conflicts** by construction — the
worktree run made 2 extra model calls reconciling the shared file. 

It was *slower* in wall-clock here because every task contends on that one symbol, so MAK serializes those
writes while the worktrees edit in parallel and reconcile afterward: the trade is
correctness-by-construction and token efficiency for wall-clock on a deliberately
maximally-contended workload. Run it yourself with

```bash
python3 benchmark/run_benchmark.py --mode real --models anthropic:claude-sonnet-4-6 openai:gpt-4o gemini:gemini-3-pro
```

## Contributing

[**CONTRIBUTING.md**](CONTRIBUTING.md) is the full guide — architecture, every
subsystem in depth, setup, the quality gates, coding standards, and where to help.

## License

[MIT](LICENSE) © 2026 Seungjoon Cha
