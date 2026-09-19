# Wave 21 simulator

This benchmark keeps the coordination mechanisms real. Each MAK arm runs the
production `mak.session.Session`, scheduler, lock table, node store, commit
transaction, reconstruction, and conflict detector. Each worktree arm runs real
`git worktree`, commit, merge or cherry-pick, and conflict detection commands.

Model calls are replaced by `SimBackend`. It samples latency from a fitted
log-normal distribution, or bootstraps the empirical samples when fit quality is
poor. Input and output tokens are linear functions of prompt bytes. A configured
failure returns wrong but parseable Python on both sides. Worktree resolution
unions registrations and independently drops each contended line according to a
Beta posterior. The random seed derives from `(seed, call kind, operation,
attempt)`, so matching work receives identical samples in every arm.

The bundled `profiles/default.json` is a documented placeholder so the benchmark
runs without API keys or prior data. Set `MAK_BENCH_CALLS_PATH` during real
benchmark runs to collect JSONL telemetry, then fit a replacement:

```bash
python benchmark/sim/fit.py benchmark/.calls/my-run.jsonl \
  --output benchmark/sim/profiles/my-model.json
```

## Threats to validity

- Simulated agents do not slow down with growing context except through the
  fitted prompt-byte token model and call profile.
- Simulated implementations are correct unless `failure_rate` is nonzero. This
  isolates coordination loss but understates the varied mistakes of real models.
- Human-sized and agent-sized tasks can have different latency distributions.
- All real kernel and git work runs on one machine, so CPU and disk set the
  throughput ceiling and may not represent a distributed deployment.
- Resolver line drops are independent in the model. Real merge mistakes can be
  correlated within a file or response.
- Calibration points at 3, 6, and 10 agents should be reported before treating a
  fitted profile as predictive outside the observed range.

`calibrate.py` pairs those real and simulated JSONL points and reports makespan,
conflict, and token prediction error. The checked-in `CALIBRATION.md` records the
current zero-spend run as inconclusive rather than inventing calibration data.
