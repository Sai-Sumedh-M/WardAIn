Backend: nvidia (official NVIDIA result)

Attacks: 45 (15 seed + 30 generated); benign notes: 10; guard_unavailable: 0
Models: safety `nvidia/nemotron-3.5-content-safety`, judge `nvidia/nemotron-3.5-lightning-30b-a3b`, attacker `nvidia/nemotron-3.5-lightning-30b-a3b`

| Layers | Detection (all) | Detection (seed) | Detection (generated) | False positives |
|---|---|---|---|---|
| regex | 11.1% | 33.3% | 0.0% | 0.0% |
| regex+safety | 60.0% | 86.7% | 46.7% | 0.0% |
| regex+judge | 48.9% | 86.7% | 30.0% | 0.0% |
| regex+safety+judge | 68.9% | 100.0% | 53.3% | 0.0% |
