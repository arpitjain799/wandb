import base64
import logging
import os
import queue
import socket
import time
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from importlib.machinery import SourceFileLoader
from pprint import pformat
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

import click
import optuna
from optuna.pruners import HyperbandPruner, SuccessiveHalvingPruner

import wandb
from wandb.apis.public import Artifact, QueuedRun, Run
from wandb.sdk.launch.sweeps import SchedulerError
from wandb.sdk.launch.sweeps.scheduler import RunState, Scheduler, SweepRun, _Worker
from wandb.wandb_agent import _create_sweep_command_args

logger = logging.getLogger(__name__)
optuna.logging.set_verbosity(optuna.logging.WARNING)


LOG_PREFIX = f"{click.style('optuna sched:', fg='bright_blue')} "


class OptunaComponents(Enum):
    main_file = "optuna_wandb.py"
    storage = "optuna.db"
    study = "optuna-study"
    pruner = "optuna-pruner"
    sampler = "optuna-sampler"


@dataclass
class _OptunaRun:
    num_metrics: int
    trial: optuna.Trial
    sweep_run: SweepRun


def _encode(run_id: str) -> str:
    """
    Helper to hash the run id for backend format
    """
    return base64.b64decode(bytes(run_id.encode("utf-8"))).decode("utf-8").split(":")[2]


def _get_module(
    module_name: str, filepath: str
) -> Tuple[Optional[ModuleType], Optional[str]]:
    """
    Helper function that loads a python module from provided filepath
    """
    try:
        loader = SourceFileLoader(module_name, filepath)
        mod = ModuleType(loader.name)
        loader.exec_module(mod)
    except Exception as e:
        return None, str(e)

    return mod, None


class OptunaScheduler(Scheduler):
    def __init__(
        self,
        *args: Optional[Any],
        **kwargs: Optional[Any],
    ):
        super().__init__(*args, **kwargs)
        self._job_queue: "queue.Queue[SweepRun]" = queue.Queue()
        self._polling_sleep = 2  # seconds

        # Optuna
        self.study: Optional[optuna.study.Study] = None
        self._storage_path: Optional[str] = None
        self._trial_func = self._make_trial
        self._optuna_runs: Dict[str, _OptunaRun] = {}

    @property
    def study_name(self):
        if self.study:
            return self.study.study_name

        return f"optuna-study-{self._sweep_id}"

    @property
    def trials_pretty(self) -> str:
        """
        Prints out trials from the current optuna study in a pleasing
        format, showing the total/best/last metrics

        returns a string with whitespace
        """
        trials = {}
        for trial in self.study.trials:
            i = trial.number + 1
            vals = list(trial.intermediate_values.values())
            if len(vals) > 0:
                best = max(vals) if self.study.direction == "maximize" else min(vals)
                trials[
                    f"trial-{i}"
                ] = f"total: {len(vals)}, best: {round(best, 5)}, last: {round(vals[-1], 5)}"
            else:
                trials[f"trial-{i}"] = "total: 0, best: None, last: None"
        return pformat(trials)

    def _validate_optuna_study(self, study: optuna.Study) -> Optional[str]:
        """
        Accepts an optuna study, runs validation
        Returns an error string if validation fails
        """
        if study.trials > 0:
            wandb.termlog(f"{LOG_PREFIX}User provided study has prior trials")

        if study.user_attrs:
            wandb.termwarn(
                f"{LOG_PREFIX}Provided user_attrs are ignored from provided study ({study.user_attrs})"
            )

        if study._storage is not None:
            wandb.termlog(
                f"{LOG_PREFIX}User provided study has storage:{study._storage}"
            )

        # TODO(gst): implement *requirements*
        return None

    def _load_optuna_from_user_provided_artifact(
        self,
        artifact_name: str,
    ) -> Tuple[
        Optional[optuna.Study],
        Optional[optuna.pruners.BasePruner],
        Optional[optuna.samplers.BaseSampler],
    ]:
        """
        Loads custom optuna classes from user-supplied artifact

        Returns:
            study: a custom optuna study object created by the user
            pruner: a custom optuna pruner supplied by user
            sampler: a custom optuna sampler supplied by user
        """
        wandb.termlog(f"{LOG_PREFIX}User set optuna.artifact, attempting download.")

        # load user-set optuna class definition file
        artifact = self._wandb_run.use_artifact(artifact_name, type="optuna")
        if not artifact:
            raise SchedulerError(
                f"{LOG_PREFIX}Failed to load artifact: {artifact_name}"
            )

        path = artifact.download()
        mod, err = _get_module("optuna", f"{path}/{OptunaComponents.main_file.value}")
        if not mod:
            raise SchedulerError(
                f"{LOG_PREFIX}Failed to load optuna from artifact: "
                f"{artifact_name} with error: {err}"
            )

        # Set custom optuna trial creation method
        if mod.objective:
            self._trial_func = self._make_trial_from_objective
            self._objective_func = mod.objective

        if mod.study:
            wandb.termlog(
                f"{LOG_PREFIX}User provided study, ignoring pruner and sampler"
            )
            val_error: Optional[str] = self._validate_optuna_study(mod.study())
            if val_error:
                raise SchedulerError(err)
            return mod.study(), None, None

        pruner = mod.pruner() if mod.pruner else None
        sampler = mod.sampler() if mod.sampler else None
        return None, pruner, sampler

    def _get_and_download_artifact(self, component: OptunaComponents) -> Optional[str]:
        """
        Finds and downloads an artifact, returns name of downloaded artifact
        """
        try:
            artifact_name = f"{self._entity}/{self._project}/{component.name}:latest"
            component_artifact: Artifact = self._wandb_run.use_artifact(artifact_name)
            path = component_artifact.download()

            storage_files = os.listdir(path)
            if component.value in storage_files:
                if path.startswith("./"):  # TODO(gst): robust way of handling this
                    path = path[2:]
                return f"{path}/{component.value}"
        except wandb.errors.CommError as e:
            raise SchedulerError(str(e))
        except Exception as e:
            raise SchedulerError(str(e))

        return None

    def _load_optuna(self) -> None:
        """
        If our run was resumed, attempt to resture optuna artifacts from run state

        Create an optuna study with a sqlite backened for loose state management
        """
        optuna_artifact_name = self._sweep_config.get("optuna", {}).get("artifact")
        if optuna_artifact_name:
            study, pruner, sampler = self._load_optuna_from_user_provided_artifact(
                optuna_artifact_name
            )
        else:
            study, pruner, sampler = None, None, None

        existing_storage = None
        if self._wandb_run.resumed or self._kwargs.get("resumed"):
            existing_storage = self._get_and_download_artifact(OptunaComponents.storage)

        if study:  # study supercedes pruner and sampler
            if existing_storage:
                wandb.termwarn("Resuming state w/ user-provided study is unsupported")

            self.study = study
            return

        # making a new study
        pruner_args = self._sweep_config.get("optuna", {}).get("pruner", {})
        if pruner and pruner_args and optuna_artifact_name:
            wandb.termwarn(
                f"{LOG_PREFIX}Loaded pruner from given artifact, `pruner_args` are ignored"
            )
        elif pruner_args:
            pruner = self._make_optuna_pruner(pruner_args)
        else:
            wandb.termlog(f"{LOG_PREFIX}No pruner args, using Optuna defaults")

        sampler_args = self._sweep_config.get("optuna", {}).get("sampler", {})
        if sampler and sampler_args and optuna_artifact_name:
            wandb.termwarn(
                f"{LOG_PREFIX}Loaded sampler from given artifact, `sampler_args` are ignored"
            )
        elif sampler_args:
            sampler = self._make_optuna_sampler(sampler_args)
        else:
            wandb.termlog(f"{LOG_PREFIX}No sampler args, using Optuna defaults")

        direction = self._sweep_config.get("metric", {}).get("goal")
        _create_msg = f"{LOG_PREFIX} {'Loading' if existing_storage else 'Creating'}"
        self._storage_path = existing_storage or OptunaComponents.storage.value
        _create_msg += (
            f" optuna study: {self.study_name} [storage:'{self._storage_path}'"
        )
        if direction:
            _create_msg += f", direction:'{direction}'"
        if pruner:
            _create_msg += f", pruner:'{pruner.__class__}'"
        if sampler:
            _create_msg += f", sampler:'{sampler.__class__}'"

        wandb.termlog(f"{_create_msg}]")
        self.study = optuna.create_study(
            study_name=self.study_name,
            storage=f"sqlite:///{self._storage_path}",
            pruner=pruner,
            sampler=sampler,
            load_if_exists=True,
            direction=direction,
        )

        if existing_storage:
            wandb.termlog(
                f"{LOG_PREFIX}Loaded ({len(self.study.trials)}) prior runs from storage: "
                f"{existing_storage}:\n {self.trials_pretty}"
            )

    def _load_state(self) -> None:
        """
        Called when Scheduler class invokes start()
        Load optuna study sqlite data from an artifact in controller run
        """
        self._load_optuna()

    def _save_state(self) -> None:
        """
        Called when Scheduler class invokes exit()

        Save optuna study sqlite data to an artifact in the controller run
        """
        artifact = wandb.Artifact(OptunaComponents.storage.name, type="optuna")
        artifact.add_file(self._storage_path)
        self._wandb_run.log_artifact(artifact)

        wandb.termlog(f"{LOG_PREFIX}Saved study with trials:\n{self.trials_pretty}")

        return True

    def _start(self) -> None:
        """
        Load optuna state, then register workers as agents
        """
        for worker_id in range(self._num_workers):
            wandb.termlog(f"{LOG_PREFIX}Starting AgentHeartbeat worker {worker_id}")
            agent_config = self._api.register_agent(
                f"{socket.gethostname()}-{worker_id}",  # host
                sweep_id=self._sweep_id,
                project_name=self._project,
                entity=self._entity,
            )
            self._workers[worker_id] = _Worker(
                agent_config=agent_config,
                agent_id=agent_config["id"],
            )

    def _heartbeat(self, worker_id: int) -> None:
        """
        Query job queue for available jobs if we have space in our worker cap
        """
        if not self.is_alive():
            return

        if self._job_queue.empty() and len(self._runs) < self._num_workers:
            config, trial = self._trial_func()
            run: dict = self._api.upsert_run(
                project=self._project,
                entity=self._entity,
                sweep_name=self._sweep_id,
                config=config,
            )[0]
            srun = SweepRun(
                id=_encode(run["id"]),
                args=config,
                worker_id=worker_id,
            )
            # internal scheduler handling needs this
            self._runs[srun.id] = srun
            self._job_queue.put(srun)
            # track the trial and metrics for optuna
            self._optuna_runs[srun.id] = _OptunaRun(
                num_metrics=0,
                trial=trial,
                sweep_run=srun,
            )

    def _run(self) -> None:
        """
        Poll currently known runs for new metrics
        report new metrics to optuna
        send kill signals to existing runs if pruned
        hearbeat workers with backend
        create new runs if necessary from optuna suggestions
        launch new runs
        """
        # go through every run we know is alive and get metrics
        to_kill = self._poll_running_runs()
        for run_id in to_kill:
            del self._optuna_runs[run_id]
            self._stop_run(run_id)

        for worker_id in self._workers:
            self._heartbeat(worker_id)

        try:
            srun: SweepRun = self._job_queue.get(timeout=self._queue_timeout)
        except queue.Empty:
            if len(self._runs) == 0:
                wandb.termlog(f"{LOG_PREFIX}No jobs in Sweeps RunQueue, waiting...")
                time.sleep(self._queue_sleep)
            else:
                # wait on actively running runs
                time.sleep(self._polling_sleep)
            return

        # If run is already stopped just ignore the request
        if srun.state in [
            RunState.DEAD,
            RunState.UNKNOWN,
        ]:
            return

        # send to launch
        command = _create_sweep_command_args({"args": srun.args})["args_dict"]
        self._add_to_launch_queue(
            run_id=srun.id,
            config={"overrides": {"run_config": command}},
        )

    def _get_run_history(self, run_id: str) -> Tuple[List[int], bool]:
        """
        Gets logged metric history for a given run_id
        """
        if run_id in self._runs:
            queued_run: Optional[QueuedRun] = self._runs[run_id].queued_run
            if not queued_run or queued_run.state == "pending":
                return []

            # TODO(gst): just noop here?
            queued_run.wait_until_running()

        try:
            api_run: Run = self._public_api.run(self._runs[run_id].full_name)
        except Exception as e:
            logger.debug(f"Failed to poll run from public api with error: {str(e)}")
            return []

        metric_name = self._sweep_config["metric"]["name"]

        # TODO(gst): make this more robust to typos --> warn if None? Scan for likely other metrics?
        # TODO(gst): how do we get metrics for already finished runs?
        history = api_run.scan_history(keys=["_step", metric_name])
        metrics = [x[metric_name] for x in history]

        return metrics

    def _poll_run(self, orun: _OptunaRun) -> bool:
        """
        Polls metrics for a run, returns true if finished
        """
        metrics = self._get_run_history(orun.sweep_run.id)
        for i, metric in enumerate(metrics[orun.num_metrics :]):
            logger.debug(f"{orun.sweep_run.id} (step:{i+orun.num_metrics}) {metrics}")
            # check if already logged
            if (
                orun.num_metrics + i
                not in orun.trial._cached_frozen_trial.intermediate_values
            ):
                orun.trial.report(metric, orun.num_metrics + i)

            if orun.trial.should_prune():
                wandb.termlog(f"{LOG_PREFIX}Optuna pruning run: {orun.sweep_run.id}")
                self.study.tell(orun.trial, state=optuna.trial.TrialState.PRUNED)
                return True

        if len(metrics) != 0:
            orun.num_metrics = len(metrics)
            return False

        # run hasn't started or is complete
        if orun.num_metrics == 0:  # hasn't started yet
            logger.debug(f"Run ({orun.sweep_run.id}) completed but logged no metrics!")
            return False
        else:  # run is complete
            last_value = orun.trial._cached_frozen_trial.intermediate_values[
                orun.num_metrics - 1
            ]
            self.study.tell(
                trial=orun.trial,
                state=optuna.trial.TrialState.COMPLETE,
                values=last_value,
            )
            wandb.termlog(
                f"{LOG_PREFIX}Completing trail with num-metrics: {orun.num_metrics}"
            )

        return True

    def _poll_running_runs(self) -> List[str]:
        """
        Iterates through runs, getting metrics, reporting to optuna

        Returns list of runs optuna marked as PRUNED, to be deleted
        """
        # TODO(gst): make threadsafe?
        wandb.termlog(f"{LOG_PREFIX}Polling runs for metrics.")
        to_kill = []
        for run_id, orun in self._optuna_runs.items():
            run_finished = self._poll_run(orun)
            if run_finished:
                wandb.termlog(f"{LOG_PREFIX}Run: {run_id} finished.")
                logger.debug(f"Finished run, study state: {self.study.trials}")
                to_kill += [run_id]

        return to_kill

    def _make_trial(self) -> Tuple[Dict[str, Any], optuna.Trial]:
        """
        Use a wandb.config to create an optuna trial object with correct
            optuna distributions
        """
        trial = self.study.ask()
        config: Dict[str, Dict[str, Any]] = defaultdict(dict)
        for param, extras in self._sweep_config["parameters"].items():
            if values := extras.get("values"):  # categorical
                config[param]["value"] = trial.suggest_categorical(param, values)
            elif value := extras.get("value"):
                config[param]["value"] = trial.suggest_categorical(param, [value])
            elif type(extras.get("min")) == float:
                log = "log" in param
                config[param]["value"] = trial.suggest_float(
                    param, extras.get("min"), extras.get("max"), log=log
                )
            elif type(extras.get("min")) == int:
                log = "log" in param
                config[param]["value"] = trial.suggest_int(
                    param, extras.get("min"), extras.get("max"), log=log
                )
            else:
                logger.debug(f"Unknown parameter type! {param=}, {extras=}")
        return config, trial

    def _make_trial_from_objective(self) -> Tuple[Dict[str, Any], optuna.Trial]:
        """
        This is the core logic that turns a user-provided MOCK objective func
            into wandb params, allowing for pythonic search spaces.
            MOCK: does not actually train, only configures params

        First creates a copy of our real study, quarantined from fake metrics

        Then calls optuna optimize on the copy study, passing in the
        loaded-from-user objective function with an aggresive timeout:
            ensures the model does not actually train.

        Retrieves created mock-trial from study copy and formats params for wandb

        Finally, ask our real study for a trial with fixed params = retrieved

        Returns wandb formatted config and optuna trial from real study
        """
        wandb.termlog(f"{LOG_PREFIX}Making trial params from objective func")
        study_copy = optuna.create_study()
        study_copy.add_trials(self.study.trials)
        try:
            # TODO(gst): this the right timeout val?
            study_copy.optimize(self._objective_func, n_trials=1, timeout=2)
        except TimeoutError:
            raise SchedulerError(
                "Passed optuna objective functions cannot actually train."
                " Must execute in 2 seconds. See docs."
            )

        temp_trial = study_copy.trials[-1]
        # convert from optuna-type param config to wandb-type param config
        config: Dict[str, Dict[str, Any]] = defaultdict(dict)
        for param, value in temp_trial.params.items():
            config[param]["value"] = value

        new_trial = self.study.ask(fixed_distributions=temp_trial.distributions)

        return config, new_trial

    def _make_optuna_pruner(
        self, pruner_args: Dict[str, Any]
    ) -> optuna.pruners.BasePruner:
        """
        Uses sweep config values in the optuna dict to configure pruner.
        Example sweep_config.yaml:

        ```
        method: optuna
        optuna:
           pruner:
              type: SuccessiveHalvingPruner
              min_resource: 10
              reduction_factor: 3
        ```
        """
        optuna.pruners.__all__

        type_ = pruner_args.get("type")
        if type_ == "HyperbandPruner":
            wandb.termlog(f"{LOG_PREFIX}Using the optuna HyperbandPruner")
            return HyperbandPruner(
                min_resource=pruner_args.get("min_resource", 1),
                max_resource=pruner_args.get("epochs"),
                reduction_factor=pruner_args.get("reduction_factor", 3),
            )
        elif type_ == "SuccessiveHalvingPruner":
            wandb.termlog(f"{LOG_PREFIX}Using the optuna SuccessiveHalvingPruner")
            return SuccessiveHalvingPruner(
                min_resource=pruner_args.get("min_resource", 1),
                reduction_factor=pruner_args.get("reduction_factor", 3),
            )

        raise SchedulerError(f"Pruner: {type_} not yet supported.")

    def _make_optuna_sampler(
        self, sampler_args: Dict[str, Any]
    ) -> optuna.samplers.BaseSampler:
        type_ = sampler_args.get("type")

        if type_ == "RandomSampler":
            return optuna.samplers.RandomSampler(seed=sampler_args.get("seed"))

        raise SchedulerError(f"Sampler: {type_} not yet supported.")

    def _exit(self) -> None:
        pass


def validate_optuna_pruner(args: Dict[str, Any]) -> bool:
    _type = args.get("type")
    try:
        _ = load_optuna_pruner(_type, args)
    except Exception as e:
        wandb.termerror(str(e))
        return False
    return True


def validate_optuna_sampler(args: Dict[str, Any]) -> bool:
    _type = args.get("type")
    try:
        _ = load_optuna_sampler(_type, args)
    except Exception as e:
        wandb.termerror(str(e))
        return False
    return True


def load_optuna_pruner(_type: str, args: Dict[str, Any]) -> optuna.pruners.BasePruner:
    if _type == "BasePruner":
        return optuna.pruners.BasePruner(**args)
    elif _type == "NopPruner":
        return optuna.pruners.NopPruner(**args)
    elif _type == "MedianPruner":
        return optuna.pruners.MedianPruner(**args)
    elif _type == "HyperbandPruner":
        return optuna.pruners.HyperbandPruner(**args)
    elif _type == "PatientPruner":
        return optuna.pruners.PatientPruner(**args)
    elif _type == "PercentilePruner":
        return optuna.pruners.PercentilePruner(**args)
    elif _type == "SuccessiveHalvingPruner":
        return optuna.pruners.SuccessiveHalvingPruner(**args)
    elif _type == "ThresholdPruner":
        return optuna.pruners.ThresholdPruner(**args)

    raise Exception(f"Optuna pruner type: {_type} not supported")


def load_optuna_sampler(
    _type: str, args: Dict[str, Any]
) -> optuna.samplers.BaseSampler:
    if _type == "BaseSampler":
        return optuna.samplers.BaseSampler(**args)
    elif _type == "BruteForceSampler":
        return optuna.samplers.BruteForceSampler(**args)
    elif _type == "CmaEsSampler":
        return optuna.samplers.CmaEsSampler(**args)
    elif _type == "GridSampler":
        # TODO(gst): pretty sure this doens't work
        return optuna.samplers.GridSampler(**args)
    elif _type == "IntersectionSearchSpace":
        return optuna.samplers.IntersectionSearchSpace(**args)
    elif _type == "MOTPESampler":
        return optuna.samplers.MOTPESampler(**args)
    elif _type == "NSGAIISampler":
        return optuna.samplers.NSGAIISampler(**args)
    elif _type == "PartialFixedSampler":
        return optuna.samplers.PartialFixedSampler(**args)
    elif _type == "RandomSampler":
        return optuna.samplers.RandomSampler(**args)
    elif _type == "TPESampler":
        return optuna.samplers.TPESampler(**args)
    elif _type == "QMCSampler":
        return optuna.samplers.QMCSampler(**args)

    raise Exception(f"Optuna sampler type: {_type} not supported")
