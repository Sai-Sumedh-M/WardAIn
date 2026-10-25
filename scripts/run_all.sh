#!/usr/bin/env bash
docker compose exec agents python -m scenarios.runner --scenario all --mode both
