# Copyright 2020 Adap GmbH. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Federating: Fast and Slow."""


import math
import statistics
from logging import DEBUG
from typing import Callable, Dict, List, Optional, Tuple, cast

import numpy as np

from flower.client_manager import ClientManager
from flower.client_proxy import ClientProxy
from flower.logger import log
from flower.typing import EvaluateRes, FitIns, FitRes, Weights

from .aggregate import aggregate, weighted_loss_avg
from .fedavg import FedAvg
from .parameter import parameters_to_weights, weights_to_parameters

E = 0.001
E_TIMEOUT = 0.0001


class FastAndSlow(FedAvg):
    """Strategy implementation which alternates between fast and slow rounds."""

    # pylint: disable-msg=too-many-arguments,too-many-instance-attributes,too-many-locals
    def __init__(
        self,
        fraction_fit: float = 0.1,
        fraction_eval: float = 0.1,
        min_fit_clients: int = 1,
        min_eval_clients: int = 1,
        min_available_clients: int = 1,
        eval_fn: Optional[Callable[[Weights], Optional[Tuple[float, float]]]] = None,
        min_completion_rate_fit: float = 0.5,
        min_completion_rate_evaluate: float = 0.5,
        on_fit_config_fn: Optional[Callable[[int], Dict[str, str]]] = None,
        on_evaluate_config_fn: Optional[Callable[[int], Dict[str, str]]] = None,
        importance_sampling: bool = True,
        dynamic_timeout: bool = True,
        dynamic_timeout_percentile: float = 0.8,
        alternating_timeout: bool = False,
        r_fast: int = 1,
        r_slow: int = 1,
        t_fast: int = 10,
        t_slow: int = 10,
    ) -> None:
        super().__init__(
            fraction_fit=fraction_fit,
            fraction_eval=fraction_eval,
            min_fit_clients=min_fit_clients,
            min_eval_clients=min_eval_clients,
            min_available_clients=min_available_clients,
            eval_fn=eval_fn,
            on_fit_config_fn=on_fit_config_fn,
            on_evaluate_config_fn=on_evaluate_config_fn,
        )
        self.min_completion_rate_fit = min_completion_rate_fit
        self.min_completion_rate_evaluate = min_completion_rate_evaluate
        self.importance_sampling = importance_sampling
        self.dynamic_timeout = dynamic_timeout
        self.dynamic_timeout_percentile = dynamic_timeout_percentile
        self.alternating_timeout = alternating_timeout
        self.r_fast = r_fast
        self.r_slow = r_slow
        self.t_fast = t_fast
        self.t_slow = t_slow
        self.contributions: Dict[str, List[Tuple[int, int, int]]] = {}
        self.durations: List[Tuple[str, float, int, int]] = []

    # pylint: disable-msg=too-many-locals
    def on_configure_fit(
        self, rnd: int, weights: Weights, client_manager: ClientManager
    ) -> List[Tuple[ClientProxy, FitIns]]:
        """Configure the next round of training."""

        # Block until `min_num_clients` are available
        sample_size, min_num_clients = self.num_fit_clients(
            client_manager.num_available()
        )
        success = client_manager.wait_for(num_clients=min_num_clients, timeout=60)
        if not success:
            # Do not continue if not enough clients are available
            return []

        # Sample clients
        if self.alternating_timeout:
            log(
                DEBUG,
                "FedFS round %s, sample %s clients (based on all previous contributions)",
                str(rnd),
                str(sample_size),
            )
            clients = self._contribution_based_sampling(
                sample_size=sample_size, client_manager=client_manager
            )
        elif self.importance_sampling:
            if rnd == 1:
                # Sample with 1/k in the first round
                log(
                    DEBUG,
                    "FedFS round %s, sample %s clients with 1/k",
                    str(rnd),
                    str(sample_size),
                )
                clients = self._one_over_k_sampling(
                    sample_size=sample_size, client_manager=client_manager
                )
            else:
                fast_round = is_fast_round(
                    rnd - 1, r_fast=self.r_fast, r_slow=self.r_slow
                )
                log(
                    DEBUG,
                    "FedFS round %s, sample %s clients, fast_round %s",
                    str(rnd),
                    str(sample_size),
                    str(fast_round),
                )
                clients = self._fs_based_sampling(
                    sample_size=sample_size,
                    client_manager=client_manager,
                    fast_round=fast_round,
                )
        else:
            clients = self._one_over_k_sampling(
                sample_size=sample_size, client_manager=client_manager
            )

        # Prepare parameters and config
        parameters = weights_to_parameters(weights)
        config = {}
        if self.on_fit_config_fn is not None:
            # Use custom fit config function if provided
            config = self.on_fit_config_fn(rnd)

        # Set timeout for this round
        if self.dynamic_timeout:
            if self.durations:
                candidates = timeout_candidates(
                    durations=self.durations, max_timeout=self.t_slow,
                )
                timeout = next_timeout(
                    candidates=candidates, percentile=self.dynamic_timeout_percentile,
                )
                config["timeout"] = str(timeout)
            else:
                # Initial round has not past durations, use max_timeout
                config["timeout"] = str(self.t_slow)
        elif self.alternating_timeout:
            use_fast_timeout = is_fast_round(rnd - 1, self.r_fast, self.r_slow)
            config["timeout"] = str(self.t_fast if use_fast_timeout else self.t_slow)
        else:
            config["timeout"] = str(self.t_slow)

        # Fit instructions
        fit_ins = (parameters, config)

        # Return client/config pairs
        return [(client, fit_ins) for client in clients]

    def _one_over_k_sampling(
        self, sample_size: int, client_manager: ClientManager
    ) -> List[ClientProxy]:
        """Sample clients with probability 1/k."""
        sample_size, min_num_clients = self.num_fit_clients(
            client_manager.num_available()
        )
        clients = client_manager.sample(
            num_clients=sample_size, min_num_clients=min_num_clients
        )
        return clients

    def _contribution_based_sampling(
        self, sample_size: int, client_manager: ClientManager
    ) -> List[ClientProxy]:
        """Sample clients depending on their past contributions."""
        # Get all clients and gather their contributions
        all_clients: Dict[str, ClientProxy] = client_manager.all()
        cid_idx: Dict[int, str] = {}
        raw: List[float] = []
        for idx, (cid, _) in enumerate(all_clients.items()):
            cid_idx[idx] = cid
            penalty = 0.0
            if cid in self.contributions.keys():
                contribs: List[Tuple[int, int, int]] = self.contributions[cid]
                penalty = statistics.mean([c / m for _, c, m in contribs])
            # `p` should be:
            #   - High for clients which have never been picked before
            #   - Medium for clients which have contributed, but not used their entire budget
            #   - Low (but not 0) for clients which have been picked and used their budget
            raw.append(1.1 - penalty)

        # Sample clients
        return normalize_and_sample(
            all_clients=all_clients,
            cid_idx=cid_idx,
            raw=np.array(raw),
            sample_size=sample_size,
            use_softmax=False,
        )

    def _fs_based_sampling(
        self, sample_size: int, client_manager: ClientManager, fast_round: bool
    ) -> List[ClientProxy]:
        """Sample clients with 1/k * c/m in fast rounds and 1 - c/m in slow rounds."""
        all_clients: Dict[str, ClientProxy] = client_manager.all()
        k = len(all_clients)
        cid_idx: Dict[int, str] = {}
        raw: List[float] = []
        for idx, (cid, _) in enumerate(all_clients.items()):
            cid_idx[idx] = cid

            if cid in self.contributions.keys():
                # Previously selected clients
                contribs: List[Tuple[int, int, int]] = self.contributions[cid]

                # pylint: disable-msg=invalid-name
                _, c, m = contribs[-1]
                c_over_m = c / m
                # pylint: enable-msg=invalid-name

                if fast_round:
                    importance = (1 / k) * c_over_m + E
                else:
                    importance = 1 - c_over_m + E
            else:
                # Previously unselected clients
                if fast_round:
                    importance = 1 / k
                else:
                    importance = 1
            raw.append(importance)

        log(
            DEBUG,
            "FedFS _fs_based_sampling, sample %s clients, raw %s",
            str(sample_size),
            str(raw),
        )

        return normalize_and_sample(
            all_clients=all_clients,
            cid_idx=cid_idx,
            raw=np.array(raw),
            sample_size=sample_size,
            use_softmax=False,
        )

    def on_aggregate_fit(
        self,
        rnd: int,
        results: List[Tuple[ClientProxy, FitRes]],
        failures: List[BaseException],
    ) -> Optional[Weights]:
        """Aggregate fit results using weighted average."""
        if not results:
            return None

        # Check if enough results are available
        completion_rate = len(results) / (len(results) + len(failures))
        if completion_rate < self.min_completion_rate_fit:
            # Not enough results for aggregation
            return None

        # Convert results
        weights_results = [
            (parameters_to_weights(parameters), num_examples)
            for client, (parameters, num_examples, _, _) in results
        ]
        weights_prime = aggregate(weights_results)

        if self.importance_sampling:
            # Track contributions to the global model
            for client, fit_res in results:
                cid = client.cid
                contribution: Tuple[int, int, int] = (rnd, fit_res[1], fit_res[2])
                if cid not in self.contributions.keys():
                    self.contributions[cid] = []
                self.contributions[cid].append(contribution)

        if self.dynamic_timeout:
            self.durations = []
            for client, (_, num_examples, num_examples_ceil, fit_duration) in results:
                cid_duration = (
                    client.cid,
                    fit_duration,
                    num_examples,
                    num_examples_ceil,
                )
                self.durations.append(cid_duration)

        return weights_prime

    def on_aggregate_evaluate(
        self,
        rnd: int,
        results: List[Tuple[ClientProxy, EvaluateRes]],
        failures: List[BaseException],
    ) -> Optional[float]:
        """Aggregate evaluation losses using weighted average."""
        if not results:
            return None

        # Check if enough results are available
        completion_rate = len(results) / (len(results) + len(failures))
        if completion_rate < self.min_completion_rate_evaluate:
            # Not enough results for aggregation
            return None

        return weighted_loss_avg([evaluate_res for _, evaluate_res in results])


def is_fast_round(rnd: int, r_fast: int, r_slow: int) -> bool:
    """Determine if the round is fast or slow."""
    remainder = rnd % (r_fast + r_slow)
    return remainder - r_fast < 0


def softmax(logits: np.ndarray) -> np.ndarray:
    """Compute softmax."""
    e_x = np.exp(logits - np.max(logits))
    return cast(np.ndarray, e_x / e_x.sum(axis=0))


def normalize_and_sample(
    all_clients: Dict[str, ClientProxy],
    cid_idx: Dict[int, str],
    raw: np.ndarray,
    sample_size: int,
    use_softmax: bool = False,
) -> List[ClientProxy]:
    """Normalize the relative importance and sample clients accordingly."""
    indices = np.arange(len(all_clients.keys()))
    if use_softmax:
        probs = softmax(np.array(raw))
    else:
        probs = raw / sum(raw)

    log(
        DEBUG,
        "FedFS normalize_and_sample, sample %s clients from %s, probs: %s",
        str(sample_size),
        str(len(indices)),
        str(probs),
    )
    sampled_indices = np.random.choice(
        indices, size=sample_size, replace=False, p=probs
    )
    clients = [all_clients[cid_idx[idx]] for idx in sampled_indices]
    return clients


def timeout_candidates(
    durations: List[Tuple[str, float, int, int]], max_timeout: int
) -> List[float]:
    """Calculate timeout candidates based on previous round training durations."""
    scaled_timeout_candidates = [
        fit_duration * float(num_ex_ceil) / (float(num_ex) + E_TIMEOUT)
        for _, fit_duration, num_ex, num_ex_ceil in durations
    ]
    return [min(st, max_timeout) for st in scaled_timeout_candidates]


def next_timeout(candidates: List[float], percentile: float) -> int:
    """Cacluate timeout for the next round."""
    candidates.sort()
    num_included = math.ceil(len(candidates) * percentile)
    timeout_raw = candidates[num_included - 1]
    timeout_ceil = math.ceil(timeout_raw)
    return timeout_ceil