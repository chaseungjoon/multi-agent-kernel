# Getting started

> [Contributing guide](../CONTRIBUTING.md) · Next: [Testing](testing.md) →

This page gets a development checkout running and shows where everything lives.
After that, [Testing](testing.md) covers the gates a change has to pass and
[Workflow](workflow.md) covers how to land it.

## Prerequisites

- **Python ≥ 3.11** (CI runs 3.11 and 3.13).
- **git** on `PATH`.
- Provider SDKs (`anthropic`, `openai`, `google-genai`) install as dependencies
  and are imported lazily. The test suite never needs a key.

## Setup

```bash
git clone https://github.com/chaseungjoon/multi-agent-kernel
cd multi-agent-kernel

python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -e ".[dev]"             # mypy, pytest, types-PyYAML; ruff is a base dependency

pre-commit install                  # optional (its paths differ from CI's; see Wave R)
```

Check the install by running the three [quality gates](testing.md#quality-gates).
They need no keys and make no network calls.

For real model calls, put keys in `~/.config/mak/.env` (or run `mak` and use
`/apikey`). [`mak/.env.example`](../mak/.env.example) lists the variable names.
How keys are read is covered in [§12](subsystems/12-configuration.md).

## Project layout

```
cli/                          # interactive app (prompt_toolkit + rich) and `mak` script
├── __main__.py               # `mak` dispatcher: TUI, run, gc, examples, update
├── app.py                    # MakCli: REPL loop, task execution
├── commands.py               # slash-command handlers
├── completer.py              # COMMANDS list + MakCompleter
├── local.py                  # /local wizard and sub-commands
├── config_sync.py            # mirror the config's planner and agents into CliState
├── project_config.py         # offer to create <work dir>/.mak/config.yaml
├── runner.py                 # CliState → RunRequest bridge, token counter, git diff
├── setup.py                  # first-run mode and key wizard
├── ui.py                     # rich rendering
├── core/
│   ├── api_keys.py           # ~/.config/mak/.env parse/merge/write
│   ├── local_hosts.py        # remembered local hosts
│   ├── local_seams.py        # LocalSeams: discover / client / host probe
│   ├── models.py             # thin adapter over mak/models/
│   └── state.py              # CliState
└── endpoints/                # /endpoint: commands, wizard, prompts, render

mak/
├── __main__.py               # `mak run`
├── _version.py               # the single version source
├── application/              # request, config, route, keys, session, env — both front ends
├── bootstrap.py              # composition root
├── config.py / config.yaml   # config schema, loading, discovery; packaged default
├── examples/                 # packaged example configs (`mak examples`)
├── session/                  # the Session state machine and its collaborators (§11)
│   ├── core.py               #   Session: lifecycle, run loop, spend ceiling
│   ├── wave.py, wiring.py    #   WaveState (per-wave state); wire() (collaborators)
│   ├── types.py, events.py   #   result types and protocols; EventLog, timed_phase
│   ├── store_view.py, workspace.py
│   ├── reconcile.py, planning.py, recovery.py, finalize.py, watchdog.py
│   ├── dispatch.py, cross_file.py, grants.py, concurrency.py, batch.py
│   ├── parking.py, outcomes.py, failures.py, repair.py, adjudication.py
│   ├── post_wave.py, fixups.py, results.py, usage.py
│   └── commit/               #   verdict, pipeline, checks, registrar, reads,
│                             #     interface, prospective, apply
├── cascade.py                # post-wave fix-up loop, CascadeOutcome
├── execution_result.py       # whole-run outcome
├── teardown.py               # suite outcome, push policy
├── test_runner.py            # teardown TestRunner
├── core/                     # types, exceptions, logging, atomic, paths, budget, task_codec
├── node_store/               # store, ingestion, makignore, reconstruction, transaction,
│                             #   journal, registrar, api_digest
├── lock_manager/             # rwlock, lock_table, conflicts, deadlock_detector,
│                             #   project_lease, resources
├── scheduler/                # dag, scheduler, lock_policy
├── conflict_detector/        # detector, node_ids, module_index, and one module per check
├── semantic/                 # read_set, stale, interface, contracts, locking, symbols,
│                             #   cascade_graph, registry_merge, sources, project_files,
│                             #   gates, gate_types, type_gate, impact_tests, import_smoke,
│                             #   overlay, adjudicator
├── planner/                  # planner, plan_schema, outline, inventory, retrieval, expansion,
│                             #   callers, telemetry, llm, response, review, depgraph,
│                             #   validation, findings, contracts
├── agent_runner/
│   ├── runner.py, registry.py, protocol.py, sandbox.py, stop_signals.py
│   ├── adapters/             # base, budget, result_schema, repair, anthropic/openai/
│   │                         #   gemini/ollama API adapters, cli_adapter + three CLI adapters
│   └── wrappers/             # CLI bridge: bridge, claude_code, codex, copilot
├── endpoints/                # types, profiles, parse, resolution, builtin, agents, store,
│                             #   capabilities, error_classification, health
├── models/                   # catalog, curation, manifest, providers, refresh, registry, seed.json
├── local/                    # ollama_client, runtime, discovery, recommended
└── git_integration/git.py

tests/                        # mirrors mak/ and cli/; tests/support/ holds a fake OpenAI server;
│                             #   tests/golden/ records and replays golden event logs
benchmark/                    # see docs/benchmarks-and-research.md
research/
└── contention_study/         # see docs/benchmarks-and-research.md (own venv, tests, and gates)
demo/                         # a small demo project and config
diagram/                      # Mermaid architecture, sequence and commit-pipeline diagrams
docs/                         # this documentation; CONTRIBUTING.md is its index
```

Each package maps to one subsystem reference; the
[overview](overview.md#where-each-piece-lives) has the table.

---

[Contributing guide](../CONTRIBUTING.md) · Next: [Testing](testing.md) →
