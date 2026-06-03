# AGENTS.md — Multi Agent Kernel

---

## Code Style

All code in this project is **Python**, formatted to the following conventions:

**Naming**
- Variables, functions, module names: `snake_case`
- Classes: `PascalCase`
- Constants: `UPPER_SNAKE_CASE`
- Private members: `_prefixed_with_underscore`

**Structure**
- Each module lives in its own file. Do not put unrelated logic in the same file.
- Functions should do one thing. If a function exceeds ~40 lines, ask whether it should be split.
- No global mutable state. Pass state explicitly through function arguments or well-defined dataclass instances.
- Use `dataclasses` or `pydantic` models for structured data — no raw dicts as function arguments.
- Type annotations are mandatory on all function signatures.

**Imports**
- Standard library first, then third-party, then internal (`mak.*`). Separate groups with a blank line.
- Never use wildcard imports (`from x import *`).

**Error Handling**
- Use explicit exceptions with descriptive messages.
- Do not silently swallow exceptions. If you catch an exception, log it and re-raise or handle deliberately.
- Define domain-specific exceptions in `mak/core/exceptions.py`.

**Comments & Docstrings**
- Public functions and classes require docstrings.
- Inline comments explain *why*, not *what*. The code should explain what.
- Do not leave TODO comments in committed code — open a tracked issue instead.

---