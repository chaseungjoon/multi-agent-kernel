# Real-model calibration

Calibration is currently **inconclusive**: this zero-spend Wave 21 run contains
no new real-model calls. The runner and error calculator are ready for N = 3, 6,
and 10 points from Haiku, Flash, or local Qwen. Compare those JSONL rows with the
matching simulated sweep using:

```bash
python benchmark/sim/calibrate.py \
  --simulated benchmark/results/calibration_sim.jsonl \
  --real benchmark/results/calibration_real.jsonl \
  --output benchmark/results/calibration_error.json
```

The output reports absolute percentage error for makespan, conflicts, and tokens
at every paired agent-count/contention point and their mean errors. No claim that
the default profile matches a real model should be made until this report has
matched rows.
