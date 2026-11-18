"""Shared qualified baseline; model, pool, environment, repeat and budgets paired."""
from pathlib import Path
from .common import read,save,digest,EnvironmentSetupError
from .engine import run_unit,StepAAttemptLimit
from .llm import RepeatedRequestError

def run_pair(dataset,unit,model,repeat,config,client,pool,lock,experiment,budgets,conditions=("targeted","none")):
    experiment=Path(experiment)
    base=experiment/"_baselines"/model/f"run_{repeat:02d}"/"units"/unit["name"]
    base_config={**config,"feedback_policy":"targeted","feedback_condition":"shared_baseline"}
    run_unit(dataset,unit,model,repeat,base_config,client,pool,lock,base,[1],evaluate_test=False)
    baseline=read(base/"rounds"/"round_01"/"round.json")
    baseline.update(baseline_environment=lock.identity,baseline_unit=unit["name"],baseline_pool_identity=pool["identity"],baseline_source=str(base),
                    baseline_request_sha256=[r["request_sha256"] for r in client.records() if r.get("generator_model")==model and r.get("repeat")==repeat and r.get("unit")==unit["name"] and r.get("feedback_condition")=="shared_baseline"])
    save(base/"paired_baseline.json",baseline)
    failures=[]
    for condition in conditions:
        destination=experiment/condition/model/f"run_{repeat:02d}"/"units"/unit["name"]
        cfg={**config,"feedback_policy":condition,"feedback_condition":condition}
        try:
            run_unit(dataset,unit,model,repeat,cfg,client,pool,lock,destination,budgets,evaluate_test=False,baseline_state=baseline)
        except EnvironmentSetupError:
            raise
        except Exception as error:
            status="STEP_A_LIMIT_REACHED" if isinstance(error,StepAAttemptLimit) else "NO_PROGRESS" if isinstance(error,RepeatedRequestError) else "FAILED"
            failure={"dataset":str(dataset),"model":model,"repeat":repeat,"unit":unit["name"],"feedback_condition":condition,"status":status,"error":str(error)}
            save(destination/"error.json",failure);failures.append(failure)
            print(f"[CONTROL_ARM_STOP] {condition}/{model}/{unit['name']}: {error}; checkpoint retained; next arm",flush=True)
    return failures
