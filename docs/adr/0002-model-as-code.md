# ADR 0002: Model as code, trained in the image build

- **Status:** Accepted
- **Date:** 2026-11-23

## Context

The model has to reach the device with a verifiable identity, be easy to roll forward and back,
and run on a small VM without the training stack.

## Decision

- **Training is part of the image build.** The detector's Dockerfile has two stages:
  - The first stage installs scikit-learn and runs `ml/train.py` with a model config
    (`ml/models/v1.json`, `v2.json`), selected by the `MODEL` build argument.
  - The second stage contains only ONNX Runtime, the module code, and the exported model.
- **Training is deterministic.** Seeded simulation and fitting produce byte-identical ONNX files
  for the same config and library versions (the card records them), so the model's sha256 is a
  stable identity. A different scikit-learn or skl2onnx release yields a different hash, which is
  why the device reports the hash it is actually running.
- **Every model ships with a card** (`model.json`) containing:
  - version, algorithm, and parameters;
  - sha256 and decision threshold;
  - feature names, with training means and standard deviations (used to explain alerts);
  - evaluation metrics and library versions.

  The module refuses to start if the model's bytes don't match the card, or if the card's feature
  list differs from the code's.
- **The export is checked.** Training fails if ONNX Runtime and scikit-learn disagree by more than
  1e-4 on any validation, test, or fault window.
- **Rollout is a deployment.** `infra/rollout.ps1 -Model v2`:
  - builds a new detector image, tagged `<source hash>-model-v2`;
  - applies a manifest pinned to its digest.

  IoT Edge restarts only that module. The module twin reports the running model's version, sha256,
  and algorithm. Rolling back is a rollout of the earlier version.

## Consequences

- Code and model move together: an image digest identifies both, and there is no separate model
  store to secure or keep in sync.
- Changing a model means rebuilding an image (a few minutes on the VM). That is acceptable at
  this cadence; models that change hourly would warrant downloading them through the module twin.
