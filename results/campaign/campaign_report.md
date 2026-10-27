Model: nvidia/nemotron-3.5-lightning-30b-a3b | LLM endpoint: https://integrate.api.nvidia.com/v1 (LLM_MODE=nvidia) | guard backend: nvidia | engine: plain | date: 2026-10-27

| Mode | Runs | Hijack rate | Damage rate | Mean damage | Task success |
|---|---|---|---|---|---|
| OFF | 41 | 0.6829 | 0.6098 | 0.8293 | 0.3902 |
| ON | 41 | 0.0976 | 0.0 | 0.0 | 1.0 |

### What stopped it (ON, 32 runs where the attack was active)

| Defence | Count |
|---|---|
| detection (total) | 28 |
| detection: nvidia_judge | 3 |
| detection: nvidia_safety | 20 |
| detection: regex | 5 |
| policy: approval_rejected | 1 |
| policy: customer_out_of_scope | 1 |
| containment (quarantine + compensation) | 2 |
| damage got through | 0: - |
