# ADR 0003: Unsupervised anomaly detection on feature windows

- **Status:** Accepted
- **Date:** 2026-11-23

## Context

Faults are rare and varied, and labelled failure data rarely exists for a new line. The detector
must learn healthy behaviour, alert on departures from it, stay quiet on normal load changes, and
say why it alerted.

## Decision

- **Windows of 10 readings** (10 s at 1 Hz) per press, reduced to eight features:
  - vibration mean and standard deviation, and crest factor (peak over RMS);
  - temperature mean and slope (°C per minute);
  - current mean, speed mean, and current per thousand rpm.
- **Training uses healthy data only.** The simulated presses follow a load cycle that couples
  current, speed, vibration, and temperature (with thermal lag), so the model must learn
  relationships between signals, not fixed ranges.
- **Threshold** is the decision-score quantile on a separate healthy validation set for the target
  false-alarm rate. The model is evaluated on a held-out healthy set and on each fault, both at
  full severity and as a five-minute ramp.
- **Debounce:** an alert is raised after 3 anomalous windows in a row and cleared after 6 normal
  ones. Alerts carry the three features furthest from training (z-scores) as the explanation.
- **v1 vs v2**:

  | Model | Algorithm | False alerts per press-day | Recall (bearing / overheat / jam) | Detection on a 5-minute ramp |
  |---|---|---|---|---|
  | v1 | Isolation Forest, 50 trees | 0 | 1.00 / 0.04 / 0.92 | 60 s / never / 110 s |
  | v2 | One-Class SVM (RBF) on standardized features | 0 | 1.00 / 1.00 / 1.00 | 50 s / 90 s / 100 s |

  Isolation Forest only draws split points inside the range of its training data. A reading far
  beyond that range on one signal (a frame 18 °C too hot while everything else is normal) therefore
  takes the same path as the most extreme healthy reading, and scores as barely unusual. The
  One-Class SVM's score keeps falling with distance from the healthy envelope. v1 stays in the
  project as the baseline whose blind spot the rollout fixes.

## Consequences

- The thresholds are set from data, not by hand, and the model card records the evaluation.
  Retuning on site is a module-twin change (`edgectl tune`).
- Simulation start-up mattered. The first version started each press at the temperature for its
  instantaneous load. Thermal lag holds a running frame much nearer the cycle average, so a press
  started at peak load read four standard deviations hot, and the first two minutes of every run
  raised a false alert. The simulator now starts warm.
