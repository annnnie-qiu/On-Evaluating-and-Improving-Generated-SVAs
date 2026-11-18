#!/usr/bin/env bash
# Usage: bash run_step_a_ablation.sh DATASET AQ_ABLATION_RUNNER
set -euo pipefail
dataset=${1:?Pass the dataset directory}
runner=${2:?Pass the aq_ablation_runner directory}
dataset=$(cd "$dataset" && pwd)
runner=$(cd "$runner" && pwd)
if [[ ! -d "$dataset/buggy_assertions" ]]; then
    echo "Controlled BA requires: $dataset/buggy_assertions" >&2
    exit 1
fi
echo 'Full Step A baseline: Opus / Sol, three independent checks per model'
python3 -u "$runner/four_model_spec_checker.py" \
    --project-root "$dataset" -buggy --model opus --model gpt61 --runs 3
for gate in syntax fpv vacuity; do
    python3 -u "$runner/run_ablation.py" \
        --project-root "$dataset" --checker "$runner/four_model_spec_checker.py" \
        -buggy -remove "$gate" --model opus --model gpt61 --runs 3
done
echo 'Without LLM: one deterministic shared arm'
python3 -u "$runner/run_ablation.py" \
    --project-root "$dataset" -buggy -remove llm --runs 1
# The ablation runner ensures the normal reference manifests exist for BA.
python3 -u "$runner/four_model_spec_checker.py" \
    --project-root "$dataset" --ba-only --model opus --model gpt61 --runs 3
