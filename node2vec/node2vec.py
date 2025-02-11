"""Implementation of the Node2vec algorithm."""
import os
from collections import defaultdict
from typing import Optional

import numpy as np
import networkx as nx
import gensim
from joblib import Parallel, delayed
from tqdm import tqdm

from .parallel import parallel_generate_walks


class Node2Vec:
    """Implements Node2vec algorithm."""

    FIRST_TRAVEL_KEY = 'first_travel_key'
    PROBABILITIES_KEY = 'probabilities'
    NEIGHBORS_KEY = 'neighbors'
    WEIGHT_KEY = 'weight'
    NUM_WALKS_KEY = 'num_walks'
    WALK_LENGTH_KEY = 'walk_length'
    P_KEY = 'p'
    Q_KEY = 'q'

    def __init__(
        self,
        graph: nx.Graph,
        dimensions: int = 128,
        walk_length: int = 80,
        num_walks: int = 10,
        p: float = 1,
        q: float = 1,
        weight_key: str = 'weight',
        workers: int = 1,
        sampling_strategy: Optional[dict] = None,
        quiet: bool = False,
        temp_folder: Optional[str] = None
    ):
        """Initiates Node2Vec.

        Pre-computes walking probabilities and generates the walks.

        :param graph: Input graph
        :param dimensions: Embedding dimensions (default: 128)
        :param walk_length: Number of nodes in each walk (default: 80)
        :param num_walks: Number of walks per node (default: 10)
        :param p: Return hyper parameter (default: 1)
        :param q: Inout parameter (default: 1)
        :param weight_key: On weighted graphs, this is the key for the weight
                           attribute (default: 'weight')
        :param workers: Number of workers for parallel execution (default: 1)
        :param sampling_strategy: Node specific sampling strategies, supports
                                  setting node specific `q`, `p`, `num_walks`
                                  and `walk_length`. Use these keys exactly.
                                  If not set, will use the global ones which
                                  were passed on the object initialization
        :param temp_folder: Path to folder with enough space to hold the memory
                            map of self.d_graph (for big graphs); to be passed
                            `joblib.Parallel.temp_folder`
        """
        self.graph = graph
        self.dimensions = dimensions
        self.walk_length = walk_length
        self.num_walks = num_walks
        self.p = p
        self.q = q
        self.weight_key = weight_key
        self.workers = workers
        self.quiet = quiet
        self.d_graph = defaultdict(dict)

        if sampling_strategy is None:
            self.sampling_strategy = {}
        else:
            self.sampling_strategy = sampling_strategy

        self.temp_folder, self.require = None, None
        if temp_folder:
            if not os.path.isdir(temp_folder):
                raise NotADirectoryError(
                    "temp_folder does not exist or "
                    "is not a directory. ({})".format(temp_folder)
                )

            self.temp_folder = temp_folder
            self.require = "sharedmem"

        self._precompute_probabilities()
        self.walks = self._generate_walks()

    def _precompute_probabilities(self):
        """Pre-computes transition probabilities for each node."""
        d_graph = self.d_graph
        first_travel_done = set()

        nodes_generator = self.graph.nodes() if self.quiet \
            else tqdm(self.graph.nodes(),
                      desc='Computing transition probabilities')

        for source in nodes_generator:

            # Init probabilities dict for first travel
            if self.PROBABILITIES_KEY not in d_graph[source]:
                d_graph[source][self.PROBABILITIES_KEY] = dict()

            for current_node in self.graph.neighbors(source):

                # Init probabilities dict
                if self.PROBABILITIES_KEY not in d_graph[current_node]:
                    d_graph[current_node][self.PROBABILITIES_KEY] = dict()

                unnormalized_weights = list()
                first_travel_weights = list()
                d_neighbors = list()

                # Calculate unnormalized weights
                for destination in self.graph.neighbors(current_node):

                    p = (
                        self.sampling_strategy[current_node].get(self.P_KEY,
                                                                 self.p)
                        if current_node in self.sampling_strategy else self.p
                    )
                    q = (
                        self.sampling_strategy[current_node].get(self.Q_KEY,
                                                                 self.q)
                        if current_node in self.sampling_strategy else self.q
                    )

                    if destination == source:
                        # Backwards probability
                        ss_weight = self.graph[current_node][destination].get(
                            self.weight_key, 1
                        ) * 1 / p
                    elif destination in self.graph[source]:
                        # If the neighbor is connected to the source
                        ss_weight = self.graph[current_node][destination].get(
                            self.weight_key, 1
                        )
                    else:
                        ss_weight = self.graph[current_node][destination].get(
                            self.weight_key, 1
                        ) * 1 / q

                    # Assign the unnormalized sampling strategy weight,
                    # normalize during random walk
                    unnormalized_weights.append(ss_weight)
                    if current_node not in first_travel_done:
                        first_travel_weights.append(
                            self.graph[current_node][destination].get(
                                self.weight_key, 1
                            )
                        )
                    d_neighbors.append(destination)

                # Normalize
                unnormalized_weights = np.array(unnormalized_weights)
                d_graph[current_node][self.PROBABILITIES_KEY][
                    source] = unnormalized_weights / unnormalized_weights.sum()

                if current_node not in first_travel_done:
                    unnormalized_weights = np.array(first_travel_weights)
                    d_graph[current_node][self.FIRST_TRAVEL_KEY] = (
                        unnormalized_weights / unnormalized_weights.sum()
                    )
                    first_travel_done.add(current_node)

                # Save neighbors
                d_graph[current_node][self.NEIGHBORS_KEY] = d_neighbors

    def _generate_walks(self) -> list:
        """Generates the random walks which will be used as the skip-gram input.

        :return: List of walks. Each walk is a list of nodes.
        """
        flatten = lambda l: [item for sublist in l for item in sublist]  # noqa: E501,E731

        # Split num_walks for each worker
        num_walks_lists = np.array_split(range(self.num_walks), self.workers)

        walk_results = Parallel(n_jobs=self.workers,
                                temp_folder=self.temp_folder,
                                require=self.require)(
            delayed(parallel_generate_walks)(self.d_graph,
                                             self.walk_length,
                                             len(num_walks),
                                             idx,
                                             self.sampling_strategy,
                                             self.NUM_WALKS_KEY,
                                             self.WALK_LENGTH_KEY,
                                             self.NEIGHBORS_KEY,
                                             self.PROBABILITIES_KEY,
                                             self.FIRST_TRAVEL_KEY,
                                             self.quiet) for
            idx, num_walks
            in enumerate(num_walks_lists, 1))

        walks = flatten(walk_results)

        return walks

    def fit(self, **skip_gram_params) -> gensim.models.Word2Vec:
        """Creates the embeddings using gensim's Word2Vec.

        :param skip_gram_params: Parameters for `gensim.models.Word2Vec` - do
                                 NOT supply 'size' it is taken from the
                                 Node2Vec `dimensions` parameter
        :type skip_gram_params: dict
        :return: A gensim word2vec model
        """
        if 'workers' not in skip_gram_params:
            skip_gram_params['workers'] = self.workers

        if 'size' not in skip_gram_params:
            skip_gram_params['size'] = self.dimensions

        return gensim.models.Word2Vec(self.walks, **skip_gram_params)
