Backend: nvidia (official NVIDIA result)

Attacks: 41 (15 seed + 26 generated, 4 ambiguous excluded); benign notes: 10; guard_unavailable: 0
Models: safety `nvidia/nemotron-3.5-content-safety`, judge `nvidia/nemotron-3.5-lightning-30b-a3b`, attacker `nvidia/nemotron-3.5-lightning-30b-a3b`

| Layers | Detection (all) | Detection (seed) | Detection (generated) | False positives |
|---|---|---|---|---|
| regex | 12.2% | 33.3% | 0.0% | 0.0% |
| regex+safety | 63.4% | 86.7% | 50.0% | 0.0% |
| regex+judge | 51.2% | 86.7% | 30.8% | 0.0% |
| regex+safety+judge | 73.2% | 100.0% | 57.7% | 0.0% |

Ambiguous (human-reviewed, excluded from the attack set): 4 items. Flagged by: regex 0/4, safety 1/4, judge 0/4.
