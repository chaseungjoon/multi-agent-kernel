# Simulated agent scaling 1 — Results

Analysis covers 32 completed arm runs.

The MAK kernel, node store, scheduler, transactions, reconstruction and git
operations are real. Agent latency/tokens/correctness and conflict-resolver
latency/line drops are modeled from the selected profile.

## Preregistered hypotheses

- **H1 crossover: supported.** Mean MAK margin: uniform=29.061s, Zipf=18.398s.

- **H2 merge cost is superlinear: refuted.** Conflict growth=4.00x versus agent growth=4.00x.

- **H3 worktree correctness decay: supported.** Observed registration drops: MAK=0, worktrees=1.

- **H4 node granularity matters: supported.** Mean makespan: node=63.482s, file=72.868s.

- **H5 kernel overhead stays small: supported.** Maximum measured commit p95=0.054020s; default shortest agent sample=7.5s.
