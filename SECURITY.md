# Security Policy

MAK runs code-writing models against a real working directory, holds provider
API keys, and can commit and push to Git. Reports that help keep those
boundaries tight are welcome.

## Supported versions

MAK is in beta (see [`mak/_version.py`](mak/_version.py)). Only the latest
release on `main` receives security fixes. There are no backports to earlier
pre-releases.

| Version | Supported |
|---|---|
| Latest `0.x` beta on `main` | ✅ |
| Any earlier pre-release | ❌ |

## Reporting a vulnerability

**Do not open a public issue, pull request or discussion for a vulnerability.**

Report it privately through GitHub:

1. Go to the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Fill in the advisory form.

Please include:

- the MAK version (`mak --version`) and how it was installed;
- the OS and Python version;
- the agent and planner backends involved (`anthropic_api`, `ollama_api`,
  `claude_code`, an OpenAI-compatible endpoint, …);
- the steps, config (with secrets removed) or task that reproduces it;
- what an attacker gains: a path written outside the work dir, a leaked
  credential, code execution you did not opt into, and so on.

### What to expect

- An acknowledgement within **7 days**.
- An assessment, including whether it is in scope and how severe it is, within
  **14 days**.
- A fix in a new release, then a published GitHub Security Advisory. You are
  credited in it unless you ask not to be.

Please give us a reasonable time to fix the issue before you disclose it
publicly. We will agree a date with you, by default no later than 90 days after
the report.

## Scope

MAK makes several security guarantees. A way around any of them is in scope.

| Guarantee | Where it is enforced |
|---|---|
| **Writes stay inside the work dir.** A node id comes from a model and becomes a filesystem path. Absolute paths, `..` components, symlink escapes and anything under `.mak/` are refused. | [§1 `paths.py`](docs/subsystems/01-core.md), checked again at the planner, at `install_plan` and at the node store |
| **An agent writes only what it was granted.** Output outside the lock grant is dropped, never written. | [§3.4](docs/subsystems/03-ast-pipeline.md#34-collection-agent-output--node-store), [§7.6](docs/subsystems/07-agent-runner.md#76-the-wire-protocol-protocolpy) |
| **Agent output is not executed by the kernel.** Fragments are checked with `compile()`, never `exec`'d or imported, on the commit path. | [§3](docs/subsystems/03-ast-pipeline.md) |
| **Credentials are never stored in config or sent to the wrong host.** Config names environment variables, never values. A real `OPENAI_API_KEY` is never forwarded to a custom or local `base_url`. Keys files are written `0600`. | [§7.8](docs/subsystems/07-agent-runner.md#78-local-transports), [§8](docs/subsystems/08-endpoints.md), [§12](docs/subsystems/12-configuration.md) |
| **Secrets stay out of logs and prompts.** Provider error bodies are redacted before logging. Planner prompts elide default values, which can hold secrets or internal URLs. `/endpoint export` emits secret-free YAML. | [§8](docs/subsystems/08-endpoints.md), [§9](docs/subsystems/09-planner.md) |
| **The user's Git index is untouched.** Audit commits are built in a private index and include only the task's files. | [§10](docs/subsystems/10-git-integration.md) |
| **The sandbox isolates CLI agents.** Under `--sandbox`, CLI agents run in Docker with only the work dir mounted and `--network none` by default. | [§7.7](docs/subsystems/07-agent-runner.md#77-the-runner-and-sandbox) |

Reports against the benchmark harness, `research/` or the demo project are
welcome, but they are handled as ordinary bugs.

## Out of scope: code you choose to run

Some features run code by design. That behaviour is not a vulnerability, but a
way to trigger it without opting in is in scope.

- **Teardown runs `session.test_command`** in the work dir, against code that
  agents wrote. Run MAK on projects and with models you trust, or run it in a
  container.
- **Optional gates run project code.** `semantic.impact_tests` runs tests and
  `semantic.import_smoke` imports modules, each in a subprocess. Both are off
  by default.
- **CLI agents (`claude_code`, `codex`, `copilot`) run unsandboxed** unless you
  pass `--sandbox`. They have whatever access the CLI itself grants.
- **Model quality.** A model that writes insecure or wrong code inside its
  grant, or that follows instructions planted in the code it was shown, is
  outside what MAK can prevent. Review plans (do not use `--no-review` on
  untrusted input) and review the audit commits.
- **Pushing.** `git.auto_push` is off by default. When it is on, MAK pushes to
  the configured remote with your Git credentials.
- **Vulnerabilities in a provider's API, in Ollama, in a CLI agent or in a
  dependency.** Report those to their maintainers. Tell us if MAK's use of the
  dependency makes the issue worse.

## Hardening tips

- Keep keys in `~/.config/mak/.env` (created `0600`) or the environment, never
  in `config.yaml`. The legacy in-package `mak/.env` is deprecated.
- Use `--sandbox` for CLI agents, and run MAK itself in a container when the
  task or repository is untrusted.
- Leave `git.auto_push` off, or keep `session.test_policy: require_pass`.
- Set `session.max_total_tokens` to cap spend.
- Use `session.on_external_edit: conflict` if MAK must never build on edits it
  did not make.
