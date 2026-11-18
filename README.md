# Assertion Qualification Code

Source code for assertion qualification, fault evaluation and assertion improvement.

## Files

- `bsg_cache/`: BSG Cache inputs and tools.
- `bsg_dataflow/`: BSG Dataflow inputs and tools.
- `i2c_AssertMiner_complete/`: I2C inputs and tools.
- `sha3_AssertLLM_complete/`: SHA3 inputs and tools.
- `assertion_loop/`: assertion improvement and budget experiments.
- `stepb/stepb/`: standalone fault evaluation tools.
- `opus_sol_ablation_upgrade/`: optional model update tools.

Each dataset keeps RTL, input assertions, controlled assertions, bind files,
specifications and configuration. Test code includes synthetic fixtures.
Run outputs, logs, caches, backups and API keys are not included.

## Setup

Use Python 3.10 or newer, `curl`, and a licensed JasperGold installation.
JasperGold must be available as `jg`, or configured in the AQ config.

```bash
python3 -m pip install -r requirements.txt
export OPENROUTER_API_KEY="YOUR_KEY"
export QWEN_API_TOKEN="YOUR_TOKEN"
```

Set only the keys used by your selected checker. Review model IDs and API URLs
in `assertion_loop/config.json` and each checker before use.

## Start

From this folder, inspect one dataset:

```bash
cd i2c_AssertMiner_complete
python3 aq_qualification_pipeline_v1/run_aq_qualification.py inventory
```

See each tool's README and `--help` for run commands.

## Source notes

The supplied BSG Dataflow AQ config contains BSG Cache mappings. Review it
before running. Some BSG RTL files need headers and helper modules that are
not in the supplied archive.

The assertion loop stops on a named property call in Dataflow's
`bsg_shift_reg_assertions.v`. That input is kept as supplied.

`bsg_dataflow/rtl/bsg_scatter_gather.py` is a legacy generator. It uses Python 2
print syntax and ends with an unfinished string. It is kept as supplied.
The supplied `bsg_scatter_gather.sv` is the RTL input.

The Dataflow and SHA3 specifications were reconstructed for this project.
Their source notes are kept in `spec/`. Original copyright and license notices
are kept in the source files.
