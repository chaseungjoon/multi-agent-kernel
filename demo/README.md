# MAK demo — `dataforge`

A small mock project for driving MAK end-to-end. `dataforge` is a tiny library of
pure data-wrangling helpers shipped as **unimplemented stubs**; its test suite is the
full specification. The exercise: point MAK at the project and let it implement the
stubs concurrently, then run the tests to see how it did.

This is a harness for *using* MAK on a realistic target — not a benchmark, and not a
comparison to any other tool.

## Layout

```text
demo/
├── README.md           # this file
├── config.yaml         # a MAK config scoped to the demo (work_dir = demo/project)
└── project/            # the project MAK develops
    ├── conftest.py     # makes `dataforge` importable under pytest
    ├── dataforge/
    │   ├── __init__.py
    │   ├── strings.py    # normalize_whitespace, slugify, truncate, word_count
    │   ├── numbers.py    # clamp, mean, median, running_total
    │   └── sequences.py  # chunk, flatten, dedupe, partition
    └── tests/            # the spec — green once every stub is implemented
        ├── test_strings.py
        ├── test_numbers.py
        └── test_sequences.py
```

Twelve independent functions across three modules — enough surface for MAK to plan a
DAG of subtasks and edit many nodes concurrently. Every function has a precise
docstring (with examples); that is what each agent implements against.

## Run MAK against it

MAK calls a real model, so set an API key first:

```bash
export ANTHROPIC_API_KEY=sk-...
```

Then, from the repository root:

```bash
python -m mak \
  --task "Implement every function in the dataforge package according to its docstring, replacing each NotImplementedError stub with a correct implementation." \
  --config demo/config.yaml
```

MAK ingests `demo/project`, plans one subtask per function, shows you the plan for
review (press enter to approve — or add `--no-review` to skip), dispatches agents
concurrently, validates and commits each edit, and reconstructs the files in place.

Smaller run? Narrow the task, e.g. *"Implement the functions in
`dataforge/strings.py`."*

## Verify

The tests are the spec. Before MAK runs they all fail (every stub raises
`NotImplementedError`); after a successful run they should pass:

```bash
python -m pytest demo/project
```

## Reset

MAK edits the files in place. Commit `demo/` before a run so you can restore the
stub baseline afterwards:

```bash
git -C demo checkout -- project
```

## Notes

- `config.yaml` excludes `tests/` and `conftest.py` from ingestion, so MAK only
  develops the library and never touches the spec. It also disables git auto-commit,
  so a run leaves no commits in your repository.
- Runtime state (node store, lock table, task graph, session log) lands in
  `demo/.mak/`.
