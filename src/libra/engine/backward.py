"""
Backward Analysis Engine
========================

:Author: Caterina Urban
"""

import itertools
import operator
import time
from copy import deepcopy
from functools import reduce
from multiprocessing import Value, Manager, Queue, Process, cpu_count, Lock
from typing import Tuple, Set, Dict, List, FrozenSet

from apronpy.coeff import PyMPQScalarCoeff
from apronpy.interval import Interval
from apronpy.lincons0 import ConsTyp
from apronpy.manager import PyBoxMPQManager, PyPolkaMPQstrictManager, PyManager
from apronpy.polka import PyPolka
from apronpy.tcons1 import PyTcons1, PyTcons1Array
from apronpy.texpr0 import TexprOp, TexprRtype, TexprRdir
from apronpy.texpr1 import PyTexpr1
from apronpy.var import PyVar
from pip._vendor.colorama import Fore, Style, Back

from libra.abstract_domains.bias.bias_domain import BiasState
from libra.abstract_domains.state import State
from libra.core.cfg import Node, Function, Activation
from libra.core.expressions import BinaryComparisonOperation, Literal, VariableIdentifier, BinaryBooleanOperation
from libra.engine.interpreter import Interpreter
from libra.semantics.backward import DefaultBackwardSemantics

rtype = TexprRtype.AP_RTYPE_REAL
rdir = TexprRdir.AP_RDIR_RND
OneHot1 = Tuple[VariableIdentifier, BinaryBooleanOperation]      # one-hot value for 1 feature
OneHotN = Tuple[OneHot1, ...]                                    # one-hot values for n features
lock = Lock()


def one_hots(variables: List[VariableIdentifier]) -> Set[OneHot1]:
    """Compute all possible one-hots for a given list of variables

    :param variables: list of variables one-hot encoding a categorical input feature
    :return: set of Libra expressions corresponding to each possible value of the one-hot encoding
    (paired with the variable being one in the encoded value for convenience ---
    the variable is the first element of the tuple)
    """
    values: Set[OneHot1] = set()
    arity = len(variables)
    for i in range(arity):
        # the current variable has value one
        one = Literal('1')
        lower = BinaryComparisonOperation(one, BinaryComparisonOperation.Operator.LtE, variables[i])
        upper = BinaryComparisonOperation(variables[i], BinaryComparisonOperation.Operator.LtE, one)
        value = BinaryBooleanOperation(lower, BinaryBooleanOperation.Operator.And, upper)
        # everything else has value zero
        zero = Literal('0')
        for j in range(0, i):
            lower = BinaryComparisonOperation(zero, BinaryComparisonOperation.Operator.LtE, variables[j])
            upper = BinaryComparisonOperation(variables[j], BinaryComparisonOperation.Operator.LtE, zero)
            conj = BinaryBooleanOperation(lower, BinaryBooleanOperation.Operator.And, upper)
            value = BinaryBooleanOperation(conj, BinaryBooleanOperation.Operator.And, value)
        for j in range(i + 1, arity):
            lower = BinaryComparisonOperation(zero, BinaryComparisonOperation.Operator.LtE, variables[j])
            upper = BinaryComparisonOperation(variables[j], BinaryComparisonOperation.Operator.LtE, zero)
            conj = BinaryBooleanOperation(lower, BinaryBooleanOperation.Operator.And, upper)
            value = BinaryBooleanOperation(value, BinaryBooleanOperation.Operator.And, conj)
        values.add((variables[i], value))
    return values


class BackwardInterpreter(Interpreter):
    """Backward control flow graph interpreter."""

    def __init__(self, cfg, manager, semantics, specification, widening=2, difference=0.25, precursory=None):
        super().__init__(cfg, semantics, widening=widening, precursory=precursory)
        self.manager: PyManager = manager                               # manager to be used for the analysis
        self._initial: BiasState = None                                 # initial analysis state

        self.specification = specification                              # input specification file
        self.sensitive: List[VariableIdentifier] = None                 # sensitive feature
        self.values: List[OneHot1] = None                               # all one-hot sensitive values
        self.uncontroversial1: List[List[VariableIdentifier]] = None    # uncontroversial features / one-hot encoded
        self.uncontroversial2: List[VariableIdentifier] = None          # uncontroversial features / custom encoded
        self.bounds: BinaryBooleanOperation = None      # bound between 0 and 1 of sensitive and one-hot encoded

        self.outputs: Set[VariableIdentifier] = None                    # output classes

        self.activations = None                                         # activation nodes
        self.active = None                                              # always active activations
        self.inactive = None                                            # always inactive activations
        self.packs = Manager().dict()                                   # packing of 1-hot splits
        self.count = 0                                                  # 1-hot split count
        self.patterns = Manager().dict()                                # packing of abstract activation patterns
        self.partitions = Value('i', 0)

        self.difference = difference                                    # minimum range (default: 0.25)

        self.biased = Value('d', 0.0)                                   # percentage that is biased
        self.feasible = Value('d', 0.0)                                 # percentage that could be analyzed
        self.explored = Value('d', 0.0)                                 # percentage that was explored
        self.analyzed = Value('i', 0)                                   # analyzed patterns

    @property
    def initial(self):
        """Initial analysis state

        :return: a deep copy of the initial analysis state
        """
        return deepcopy(self._initial)

    def feasibility(self, state, manager, key=None, do=False):
        """Determine feasibility (and activation patterns) for a partition of the input space

        :param state: state representing the partition of the input space
        :param manager: manager to be used for the (forward) analysis
        :param key: pre-determined activations
        :param do: whether to compute the activation patterns even if the analysis is not feasible
        :return: feasibility, patterns, last computed number of disjunctions
        """
        feasible = True
        patterns: List[Tuple[OneHot1, FrozenSet[Node], FrozenSet[Node]]] = list()
        disjunctions = len(self.activations)
        for idx, value in enumerate(self.values):
            result = deepcopy(state).assume({value[1]}, manager=manager)
            f_active = key[idx][0] if key else None
            f_inactive = key[idx][1] if key else None
            active, inactive = self.precursory.analyze(result, forced_active=f_active, forced_inactive=f_inactive)
            disjunctions = len(self.activations) - len(active) - len(inactive)
            if disjunctions > self.widening:
                feasible = False
                if not do:
                    break
            patterns.append((value, frozenset(active), frozenset(inactive)))
        return feasible, patterns, disjunctions

    def producer(self, queue3):
        """Produce all possible combinations of one-hots for the one-hot encoded uncontroversial features

        :param queue3: queue in which to put the combinations
        """
        one_hotn = itertools.product(*(one_hots(encoding) for encoding in self.uncontroversial1))
        for one_hot in one_hotn:
            queue3.put(one_hot)
        queue3.put(None)

    def consumer(self, queue3, entry, manager):
        """Consume a combination of one-hots and put it in its abstract activation pattern pack

        :param queue3: queue from which to get the combination
        :param entry: state from which to start the (forward) analysis
        :param manager: manager to be used for the analysis
        """
        while True:
            one_hot = queue3.get(block=True)
            if one_hot is None:
                queue3.put(None)
                break
            result1 = deepcopy(entry)
            for item in one_hot:
                result1 = result1.assume({item[1]}, manager=manager)
            key = list()
            for value in self.values:
                result2 = deepcopy(result1).assume({value[1]}, manager=manager)
                active, inactive = self.precursory.analyze(result2, earlystop=False)
                key.append((frozenset(active), frozenset(inactive)))
            _key = tuple(key)
            lock.acquire()
            curr = self.packs.get(_key, set())
            curr.add(one_hot)
            self.packs[_key] = curr
            lock.release()

    def packing(self, entry):
        """Pack all combinations of one-hots into abstract activation pattern packs

        :param entry: state from which to start the (forward) analysis
        """
        queue3 = Queue()
        start3 = time.time()
        processes = list()
        process = Process(target=self.producer, args=(queue3,))
        processes.append(process)
        for _ in range(cpu_count() - 1):
            process = Process(target=self.consumer, args=(queue3, entry, PyBoxMPQManager()))
            processes.append(process)
        for process in processes:
            process.start()
        for process in processes:
            process.join()
        end3 = time.time()
        _count = sum(len(pack) for pack in self.packs.values())
        assert self.count == _count
        print(Fore.YELLOW + '\nFound {} Packs for {} 1-Hot Combinations:'.format(len(self.packs), _count))
        score = lambda k: sum(len(s[0]) + len(s[1]) for s in k)
        for key, pack in sorted(self.packs.items(), key=lambda v: score(v[0]) + len(v[1]), reverse=True):
            sset = lambda s: '{{{}}}'.format(', '.join('{}'.format(e) for e in s))
            skey = ' | '.join('{}, {}'.format(sset(pair[0]), sset(pair[1])) for pair in key)
            sscore = '(score: {})'.format(score(key) + len(pack))
            spack = ' | '.join('{}'.format(','.join('{}'.format(item[0]) for item in one_hot)) for one_hot in pack)
            print(Fore.YELLOW, skey, '->', spack, sscore, Style.RESET_ALL)
        print(Fore.YELLOW + '1-Hot Splitting Time: {}s\n'.format(end3 - start3), Style.RESET_ALL)

    def worker1(self, id, color, queue1, manager):
        """Partition the analysis into feasible chunks and pack them into abstract activation pattern packs

        :param id: id of the process
        :param color: color associated with the process (for logging)
        :param queue1: queue from which to get the current chunk
        :param manager: manager to be used for the (forward) analysis
        """
        while True:
            assumptions, pivot1, unpacked, ranges, pivot2, splittable, percent, key = queue1.get(block=True)
            if assumptions is None:     # no more chunks
                queue1.put((None, None, None, None, None, None, None, None))
                break
            r_assumptions = '1-Hot: {}'.format(
                ', '.join('{}'.format('|'.join('{}'.format(var) for var in case)) for (case, _) in assumptions)
            ) if assumptions else ''
            r_ranges = 'Ranges: {}'.format(
                ', '.join('{} ∈ [{}, {}]'.format(feature, lower, upper) for feature, (lower, upper) in ranges)
            )
            r_partition = '{} | {}'.format(r_assumptions, r_ranges) if r_assumptions else '{}'.format(r_ranges)
            print(color + r_partition, Style.RESET_ALL)
            # bound the custom encoded uncontroversial features between their current lower and upper bounds
            bounds = self.bounds
            for feature, (lower, upper) in ranges:
                left = BinaryComparisonOperation(Literal(str(lower)), BinaryComparisonOperation.Operator.LtE, feature)
                right = BinaryComparisonOperation(feature, BinaryComparisonOperation.Operator.LtE, Literal(str(upper)))
                conj = BinaryBooleanOperation(left, BinaryBooleanOperation.Operator.And, right)
                bounds = BinaryBooleanOperation(bounds, BinaryBooleanOperation.Operator.And, conj)
            entry = self.initial.precursory.assume({bounds}, manager=manager)
            # take into account the accumulated assumptions on the one-hot encoded uncontroversial features
            for (_, assumption) in assumptions:
                entry = entry.assume({assumption}, manager=manager)
            # determine chunk feasibility for each possible value of the sensitive feature
            feasibility = self.feasibility(entry, manager, key=key)
            feasible: bool = feasibility[0]
            # pack the chunk, if feasible, or partition the space of values of all the uncontroversial features
            if feasible:    # the analysis is feasible
                with self.partitions.get_lock():
                    self.partitions.value += 1
                with self.feasible.get_lock():
                    self.feasible.value += percent
                with self.explored.get_lock():
                    self.explored.value += percent
                    if self.explored.value >= 100:
                        queue1.put((None, None, None, None, None, None, None, None))
                patterns: List[Tuple[OneHot1, Set[Node], Set[Node]]] = feasibility[1]
                key = list()
                for _, active, inactive in patterns:
                    key.append((frozenset(active), frozenset(inactive)))
                _key = tuple(key)
                value = (frozenset(assumptions), frozenset(unpacked), frozenset(ranges), percent)
                lock.acquire()
                curr = self.patterns.get(_key, set())
                curr.add(value)
                self.patterns[_key] = curr
                lock.release()
                progress = 'Progress for #{}: {}% of {}%'.format(id, self.feasible.value, self.explored.value)
                print(Fore.YELLOW + progress, Style.RESET_ALL)
            else:  # too many disjunctions, we need to split further
                print('Too many disjunctions ({})!'.format(feasibility[2]))
                if pivot1 < len(self.uncontroversial1):  # we still have to split the one-hot encoded
                    print('1-hot splitting for: {}'.format(
                        ' | '.join(
                            ', '.join('{}'.format(var) for var in encoding) for encoding in self.uncontroversial1)
                    ))
                    self.packing(entry)     # pack the one-hot combinations
                    # run the analysis on the ranked packs
                    score = lambda k: sum(len(s[0]) + len(s[1]) for s in k)
                    for key, pack in sorted(self.packs.items(), key=lambda v: score(v[0]) + len(v[1]), reverse=True):
                        _assumptions = list(assumptions)
                        items: List[OneHotN] = list(pack)  # multiple one-hot values for n features
                        for i in range(len(items[0])):  # for each feature...
                            variables: Set[VariableIdentifier] = set()
                            var, case = items[0][i]
                            variables.add(var)
                            for item in items[1:]:
                                var, nxt = item[i]
                                variables.add(var)
                                case = BinaryBooleanOperation(case, BinaryBooleanOperation.Operator.Or, nxt)
                            _assumptions.append((frozenset(variables), case))
                        _unpacked = frozenset(frozenset(item) for item in pack)
                        _pivot1 = len(self.uncontroversial1)
                        _percent = percent * len(pack) / self.count
                        queue1.put((_assumptions, _pivot1, _unpacked, ranges, pivot2, splittable, _percent, key))
                else:  # we can split the rest
                    if self.uncontroversial2 and splittable:
                        rangesdict = dict(ranges)
                        (lower, upper) = rangesdict[self.uncontroversial2[pivot2]]
                        if upper - lower <= self.difference:
                            print('Cannot range split for {} anymore!'.format(self.uncontroversial2[pivot2]))
                            _splittable = list(splittable)
                            _splittable.remove(self.uncontroversial2[pivot2])
                            _pivot2 = (pivot2 + 1) % len(self.uncontroversial2)
                            _splittable = list(_splittable)
                            queue1.put((assumptions, pivot1, unpacked, ranges, _pivot2, _splittable, percent, None))
                        else:
                            middle = lower + (upper - lower) / 2
                            print('Range split for {} at: {}'.format(self.uncontroversial2[pivot2], middle))
                            left = deepcopy(rangesdict)
                            left[self.uncontroversial2[pivot2]] = (lower, middle)
                            right = deepcopy(rangesdict)
                            right[self.uncontroversial2[pivot2]] = (middle, upper)
                            _pivot2 = (pivot2 + 1) % len(self.uncontroversial2)
                            _percent = percent / 2
                            _left, _right = list(left.items()), list(right.items())
                            queue1.put((assumptions, pivot1, unpacked, _left, _pivot2, splittable, _percent, None))
                            queue1.put((assumptions, pivot1, unpacked, _right, _pivot2, splittable, _percent, None))
                    else:
                        with self.explored.get_lock():
                            self.explored.value += percent
                            if self.explored.value >= 100:
                                queue1.put((None, None, None, None, None, None, None, None))
                        print(Fore.LIGHTRED_EX + 'Stopping here!', Style.RESET_ALL)
                        progress = 'Progress for #{}: {}% of {}%'.format(id, self.feasible.value, self.explored.value)
                        print(Fore.YELLOW + progress, Style.RESET_ALL)
                        # self.pick(assumptions, pivot1, ranges, pivot2, splittable, percent, do=True)

    def bias_check(self, chunk, result, ranges, percent):
        """Check for algorithmic bias

        :param chunk: chunk to be checked (string representation)
        :param result: result of the (backward) analysis for the current abstract activation pattern
        :param ranges: ranges for the custom encoded uncontroversial features in the chunk
        :param percent: percent of the input space covered by the chunk
        """
        nobias = True
        biases = set()
        b_ranges = dict()
        items = list(result.items())
        for i in range(len(items)):
            (outcome1, sensitive1), value1 = items[i]
            for j in range(i+1, len(items)):
                (outcome2, sensitive2), value2 = items[j]
                if outcome1 != outcome2 and sensitive1 != sensitive2:
                    for val1 in value1:
                        for val2 in value2:
                            intersection = deepcopy(val1).meet(val2)
                            for encoding in self.uncontroversial1:
                                intersection = intersection.forget(encoding)
                            for feature, (lower, upper) in ranges:
                                lte = BinaryComparisonOperation.Operator.LtE
                                left = BinaryComparisonOperation(Literal(str(lower)), lte, feature)
                                right = BinaryComparisonOperation(feature, lte, Literal(str(upper)))
                                conj = BinaryBooleanOperation(left, BinaryBooleanOperation.Operator.And, right)
                                intersection = intersection.assume({conj}, manager=self.manager)
                            # for assumption in assumptions0:
                            #     intersection = intersection.assume(assumption)
                            representation = repr(intersection.polka)
                            if not representation.startswith('-1.0 >= 0') and not representation == '⊥':
                                nobias = False
                                if representation not in biases:
                                    for uncontroversial in self.uncontroversial2:
                                        itv: Interval = intersection.polka.bound_variable(PyVar(uncontroversial.name))
                                        lower = eval(str(itv.interval.contents.inf.contents))
                                        upper = eval(str(itv.interval.contents.sup.contents))
                                        if uncontroversial in b_ranges:
                                            inf, sup = b_ranges[uncontroversial]
                                            b_ranges[uncontroversial] = (min(lower, inf), max(upper, sup))
                                        else:
                                            b_ranges[uncontroversial] = (lower, upper)
                                    biases.add(representation)
                                    found = '✘ Bias Found! in {}:\n{}'.format(chunk, representation)
                                    print(Fore.RED + found, Style.RESET_ALL)
        if nobias:
            print(Fore.GREEN + '✔︎ No Bias in {}'.format(chunk), Style.RESET_ALL)
        else:
            total_size = 1
            for _, (lower, upper) in ranges:
                total_size *= upper - lower
            biased_size = 1
            for (lower, upper) in b_ranges.values():
                biased_size *= upper - lower
            _percent = percent * biased_size / total_size
            with self.biased.get_lock():
                self.biased.value += _percent

    def from_node(self, node, initial, join):
        """Run the backward analysis

        :param node: node from which to start the (backward) analysis
        :param initial: state from which to start the (backward) analysis
        :param join: whether joins should be performed
        :return: the result of the (backward) analysis (at the beginning of the CFG)
        """
        state = initial
        if isinstance(node, Function):
            state = self.semantics.list_semantics(node.stmts, state)
            if state.is_bottom():
                yield None
            else:
                if self.cfg.predecessors(node):
                    yield from self.from_node(self.cfg.nodes[self.cfg.predecessors(node).pop()], state, join)
                else:
                    yield state
        elif isinstance(node, Activation):
            if node in self.active:  # only the active path is viable
                state = self.semantics.ReLU_call_semantics(node.stmts, state, self.manager, True)
                if state.is_bottom():
                    yield None
                else:
                    predecessor = self.cfg.nodes[self.cfg.predecessors(node).pop()]
                    yield from self.from_node(predecessor, state, join)
            elif node in self.inactive:  # only the inactive path is viable
                state = self.semantics.ReLU_call_semantics(node.stmts, state, self.manager, False)
                if state.is_bottom():
                    yield None
                else:
                    predecessor = self.cfg.nodes[self.cfg.predecessors(node).pop()]
                    yield from self.from_node(predecessor, state, join)
            else:  # both paths are viable
                active, inactive = deepcopy(state), deepcopy(state)
                state1 = self.semantics.ReLU_call_semantics(node.stmts, active, self.manager, True)
                state2 = self.semantics.ReLU_call_semantics(node.stmts, inactive, self.manager, False)
                if join:
                    state = state1.join(state2)
                    predecessor = self.cfg.nodes[self.cfg.predecessors(node).pop()]
                    yield from self.from_node(predecessor, state, join)
                else:
                    if state1.is_bottom():
                        if state2.is_bottom():
                            yield None
                        else:
                            predecessor = self.cfg.nodes[self.cfg.predecessors(node).pop()]
                            yield from self.from_node(predecessor, state2, join)
                    else:
                        predecessor = self.cfg.nodes[self.cfg.predecessors(node).pop()]
                        yield from self.from_node(predecessor, state1, join)
                        if state2.is_bottom():
                            yield None
                        else:
                            yield from self.from_node(predecessor, state2, join)
        else:
            if self.cfg.predecessors(node):
                yield from self.from_node(self.cfg.nodes[self.cfg.predecessors(node).pop()], state, join)
            else:
                yield state

    def worker2(self, id, color, queue2, manager, total):
        """Run the analysis for an abstract activation pattern and check the corresponding chunks for algorithmic bias

        :param id: id of the process
        :param color: color associated with the process (for logging)
        :param queue2: queue from which to get the current abstract activation pattern and corresponding chunks
        :param manager: manager to be used for the (backward) analysis
        :param total: total number of abstract activation patterns
        """
        while True:
            idx, (key, pack) = queue2.get(block=True)
            if idx is None:     # no more abstract activation patterns
                queue2.put((None, (None, None)))
                break
            print(color + 'Pattern #{} of {} [{}]'.format(idx, total, len(pack)), Style.RESET_ALL)
            check: Dict[Tuple[VariableIdentifier, VariableIdentifier], Set[BiasState]] = dict()
            for idx, (case, value) in enumerate(self.values):
                self.active, self.inactive = key[idx]
                for chosen in self.outputs:
                    remaining = self.outputs - {chosen}
                    discarded = remaining.pop()
                    outcome = BinaryComparisonOperation(discarded, BinaryComparisonOperation.Operator.Lt, chosen)
                    for discarded in remaining:
                        cond = BinaryComparisonOperation(discarded, BinaryComparisonOperation.Operator.Lt, chosen)
                        outcome = BinaryBooleanOperation(outcome, BinaryBooleanOperation.Operator.And, cond)
                    result = self.initial.assume({outcome}, manager=manager, bwd=True)
                    check[(chosen, case)] = set()
                    for state in self.from_node(self.cfg.out_node, deepcopy(result), False):
                        if state:
                            state = state.assume({value}, manager=manager)
                            check[(chosen, case)].add(state)
            # check for bias
            for assumptions, unpacked, ranges, percent in pack:
                r_assumptions = '1-Hot: {}'.format(
                    ', '.join('{}'.format('|'.join('{}'.format(var) for var in case)) for (case, _) in assumptions)
                ) if assumptions else ''
                r_ranges = 'Ranges: {}'.format(
                    ', '.join('{} ∈ [{}, {}]'.format(feature, lower, upper) for feature, (lower, upper) in ranges)
                )
                r_partition = '{} | {}'.format(r_assumptions, r_ranges) if r_assumptions else '{}'.format(r_ranges)
                if unpacked:
                    _percent = percent / len(unpacked)
                    for item in unpacked:
                        partition = deepcopy(check)
                        for states in partition.values():
                            for state in states:
                                for (_, assumption) in item:
                                    state.assume({assumption}, manager=manager)
                                # forget the sensitive variables
                                state.forget(self.sensitive)
                        self.bias_check(r_partition, partition, ranges, _percent)
                else:
                    partition = deepcopy(check)
                    for states in partition.values():
                        for state in states:
                            # forget the sensitive variables
                            state.forget(self.sensitive)
                    self.bias_check(r_partition, partition, ranges, percent)
            with self.analyzed.get_lock():
                self.analyzed.value += len(pack)
            analyzed = self.analyzed.value
            partitions = self.partitions.value
            biased = self.biased.value
            progress = 'Progress for #{}: {} of {} partitions ({}% biased)'.format(id, analyzed, partitions, biased)
            print(Fore.YELLOW + progress, Style.RESET_ALL)

    def analyze(self, initial, inputs=None, outputs=None, activations=None):
        """Backward analysis checking for algorithmic bias

        :param initial: (BiasState) state from which to start the analysis
        :param inputs: (Set[VariableIdentifier]) input variables
        :param outputs: (Set[VariableIdentifier]) output variables
        :param activations: (Set[Node]) CFG nodes corresponding to activation functions
        """
        print(Fore.BLUE + '\n||=================||')
        print('|| symbolic1: {}'.format(self.precursory.symbolic1))
        print('|| symbolic2: {}'.format(self.precursory.symbolic2))
        print('|| difference: {}'.format(self.difference))
        print('|| widening: {}'.format(self.widening))
        print('||=================||', Style.RESET_ALL)
        self._initial = initial
        with open(self.specification, 'r') as specification:
            """
            pick sensitive feature and fix its bounds / we assume one-hot encoding
            """
            arity = int(specification.readline().strip())
            self.sensitive = list()
            for i in range(arity):
                self.sensitive.append(VariableIdentifier(specification.readline().strip()))
            self.values: List[OneHot1] = list(one_hots(self.sensitive))
            # bound the sensitive feature between 0 and 1
            zero = Literal('0')
            one = Literal('1')
            left = BinaryComparisonOperation(zero, BinaryComparisonOperation.Operator.LtE, self.sensitive[0])
            right = BinaryComparisonOperation(self.sensitive[0], BinaryComparisonOperation.Operator.LtE, one)
            self.bounds = BinaryBooleanOperation(left, BinaryBooleanOperation.Operator.And, right)
            for sensitive in self.sensitive[1:]:
                left = BinaryComparisonOperation(zero, BinaryComparisonOperation.Operator.LtE, sensitive)
                right = BinaryComparisonOperation(sensitive, BinaryComparisonOperation.Operator.LtE, one)
                conj = BinaryBooleanOperation(left, BinaryBooleanOperation.Operator.And, right)
                self.bounds = BinaryBooleanOperation(self.bounds, BinaryBooleanOperation.Operator.And, conj)
            """
            determine the one-hot encoded uncontroversial features and fix their bounds
            """
            self.uncontroversial1 = list()
            while True:
                try:
                    arity = specification.readline().strip()
                    uncontroversial = list()
                    for i in range(int(arity)):
                        uncontroversial.append(VariableIdentifier(specification.readline().strip()))
                    self.uncontroversial1.append(uncontroversial)
                except ValueError:
                    break
            self.count = reduce(operator.mul, (len(encoding) for encoding in self.uncontroversial1), 1)
            # bound the one-hot encoded uncontroversial features between 0 and 1
            for encoding in self.uncontroversial1:
                for uncontroversial in encoding:
                    left = BinaryComparisonOperation(zero, BinaryComparisonOperation.Operator.LtE, uncontroversial)
                    right = BinaryComparisonOperation(uncontroversial, BinaryComparisonOperation.Operator.LtE, one)
                    conj = BinaryBooleanOperation(left, BinaryBooleanOperation.Operator.And, right)
                    self.bounds = BinaryBooleanOperation(self.bounds, BinaryBooleanOperation.Operator.And, conj)
            """
            determine the custom encoded uncontroversial features and fix their ranges
            """
            self.uncontroversial2 = list(inputs - set(self.sensitive) - set(itertools.chain(*self.uncontroversial1)))
            ranges: Dict[VariableIdentifier, Tuple[int, int]] = dict()
            for uncontroversial in self.uncontroversial2:
                ranges[uncontroversial] = (0, 1)
            # for uncontroversial in self.uncontroversial2:
            #     left = BinaryComparisonOperation(zero, BinaryComparisonOperation.Operator.LtE, uncontroversial)
            #     right = BinaryComparisonOperation(uncontroversial, BinaryComparisonOperation.Operator.LtE, one)
            #     conj = BinaryBooleanOperation(left, BinaryBooleanOperation.Operator.And, right)
            #     self.bounds = BinaryBooleanOperation(self.bounds, BinaryBooleanOperation.Operator.And, conj)
        self.outputs = outputs
        self.activations = activations
        cpu = cpu_count()
        print('\nAvailable CPUs: {}'.format(cpu))
        colors = [
            Fore.LIGHTMAGENTA_EX,
            Back.BLACK + Fore.WHITE,
            Back.LIGHTRED_EX + Fore.BLACK,
            Back.MAGENTA + Fore.BLACK,
            Back.BLUE + Fore.BLACK,
            Back.CYAN + Fore.BLACK,
            Back.LIGHTGREEN_EX + Fore.BLACK,
            Back.YELLOW + Fore.BLACK,
        ]
        """
        do the pre-analysis
        """
        print(Fore.BLUE + '\n||==============||')
        print('|| Pre-Analysis ||')
        print('||==============||\n', Style.RESET_ALL)
        # prepare the queue
        queue1 = Manager().Queue()
        queue1.put((list(), 0, list(), list(ranges.items()), 0, list(self.uncontroversial2), 100, None))
        # run the pre-analysis
        start1 = time.time()
        processes = list()
        for i in range(cpu):
            color = colors[i % len(colors)]
            process = Process(target=self.worker1, args=(i, color, queue1, PyBoxMPQManager()))
            processes.append(process)
            process.start()
        for process in processes:
            process.join()
        end1 = time.time()
        #
        print(Fore.BLUE + '\nFound: {} patterns for {} partitions'.format(len(self.patterns), self.partitions.value))
        prioritized = sorted(self.patterns.items(), key=lambda v: len(v[1]), reverse=True)
        for key, pack in prioritized:
            sset = lambda s: '{{{}}}'.format(', '.join('{}'.format(e) for e in s))
            skey = ' | '.join('{}, {}'.format(sset(pair[0]), sset(pair[1])) for pair in key)
            print(skey, '->', len(pack))
        #
        compressed = dict()
        for key1, pack1 in sorted(self.patterns.items(), key=lambda v: len(v[1]), reverse=False):
            unmerged = True
            for key2 in compressed:
                mergeable1, mergeable2 = True, True
                for (s11, s12), (s21, s22) in zip(key1, key2):
                    if (not s21.issubset(s11)) or (not s22.issubset(s12)):
                        mergeable1 = False
                    if (not s11.issubset(s21)) or (not s12.issubset(s22)):
                        mergeable2 = False
                if mergeable1:
                    unmerged = False
                    compressed[key2] = compressed[key2].union(pack1)
                    break
                if mergeable2:
                    unmerged = False
                    compressed[key1] = compressed[key2].union(pack1)
                    del compressed[key2]
                    break
            if unmerged:
                compressed[key1] = pack1
        prioritized = sorted(compressed.items(), key=lambda v: len(v[1]), reverse=True)
        if len(compressed) < len(self.patterns):
            print('Compressed to: {} patterns'.format(len(compressed)))
            for key, pack in prioritized:
                sset = lambda s: '{{{}}}'.format(', '.join('{}'.format(e) for e in s))
                skey = ' | '.join('{}, {}'.format(sset(pair[0]), sset(pair[1])) for pair in key)
                print(skey, '->', len(pack))
        #
        print('Pre-Analysis Time: {}s'.format(end1 - start1), Style.RESET_ALL)

        """
        do the analysis
        """
        print(Fore.BLUE + '\n||==========||')
        print('|| Analysis ||')
        print('||==========||\n', Style.RESET_ALL)
        # prepare the queue
        queue2 = Queue()
        for idx, (key, pack) in enumerate(prioritized):
            queue2.put((idx+1, (key, pack)))
        queue2.put((None, (None, None)))
        # run the analysis
        start2 = time.time()
        processes = list()
        for i in range(cpu):
            color = colors[i % len(colors)]
            man = PyPolkaMPQstrictManager()
            process = Process(target=self.worker2, args=(i, color, queue2, man, len(compressed)))
            processes.append(process)
            process.start()
        for process in processes:
            process.join()
        end2 = time.time()
        #
        result = '\nResult: {}% of {}% ({}% biased)'.format(self.feasible.value, self.explored.value, self.biased.value)
        print(Fore.BLUE + result)
        print('Pre-Analysis Time: {}s'.format(end1 - start1))
        print('Analysis Time: {}s'.format(end2 - start2), Style.RESET_ALL)

        log = '{} ({}% biased) {}s {}s'.format(self.feasible.value, self.biased.value, end1 - start1, end2 - start2)
        print('\nDone!')
        return log


class BiasBackwardSemantics(DefaultBackwardSemantics):

    def list_semantics(self, stmt, state) -> State:
        state.polka = state.polka.substitute(stmt[0], stmt[1])
        return state

    def ReLU_call_semantics(self, stmt, state, manager: PyManager = None, active: bool = True) -> State:
        assert manager is not None
        if active:  # assume h >= 0
            expr = PyTexpr1.var(state.environment, stmt)
            cond = PyTcons1.make(expr, ConsTyp.AP_CONS_SUPEQ)
            abstract1 = PyPolka(manager, state.environment, array=PyTcons1Array([cond]))
            state.polka = state.polka.meet(abstract1)
            return state
        else:  # assign h = 0, assume h < 0
            expr = PyTexpr1.var(state.environment, stmt)
            zero = PyTexpr1.cst(state.environment, PyMPQScalarCoeff(0.0))
            neg = PyTexpr1.binop(TexprOp.AP_TEXPR_SUB, zero, expr, rtype, rdir)
            cond = PyTcons1.make(neg, ConsTyp.AP_CONS_SUP)
            abstract1 = PyPolka(manager, state.environment, array=PyTcons1Array([cond]))
            zero = PyTexpr1.cst(state.environment, PyMPQScalarCoeff(0.0))
            state.polka = state.polka.substitute(stmt, zero).meet(abstract1)
            return state
