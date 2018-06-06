# -*- coding: utf-8 -*-

# Copyright 2018 IBM.
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
# =============================================================================
"""
The Quantum Phase Estimation Algorithm.
"""

import logging

from functools import reduce
import numpy as np
from qiskit import QuantumRegister, ClassicalRegister, QuantumCircuit
from qiskit.tools.qi.pauli import Pauli
from qiskit.tools.qi.qi import qft
from qiskit_acqua import Operator, QuantumAlgorithm, AlgorithmError
from qiskit_acqua import get_initial_state_instance, get_iqft_instance

logger = logging.getLogger(__name__)


class QPE(QuantumAlgorithm):
    """The Quantum Phase Estimation algorithm."""

    PROP_NUM_TIME_SLICES = 'num_time_slices'
    PROP_PAULIS_GROUPING = 'paulis_grouping'
    PROP_EXPANSION_MODE = 'expansion_mode'
    PROP_EXPANSION_ORDER = 'expansion_order'
    PROP_NUM_ANCILLAE = 'num_ancillae'
    PROP_USE_BASIS_GATES = 'use_basis_gates'

    DEFAULT_PROP_NUM_TIME_SLICES = 1
    DEFAULT_PROP_PAULIS_GROUPING = 'default'        # grouped_paulis
    ALTERNATIVE_PROP_PAULIS_GROUPING = 'random'     # paulis
    DEFAULT_PROP_EXPANSION_MODE = 'trotter'
    ALTERNATIVE_PROP_EXPANSION_MODE = 'suzuki'
    DEFAULT_PROP_EXPANSION_ORDER = 2
    DEFAULT_PROP_NUM_ANCILLAE = 1

    QPE_CONFIGURATION = {
        'name': 'QPE',
        'description': 'Quantum Phase Estimation for Quantum Systems',
        'input_schema': {
            '$schema': 'http://json-schema.org/schema#',
            'id': 'qpe_schema',
            'type': 'object',
            'properties': {
                PROP_NUM_TIME_SLICES: {
                    'type': 'integer',
                    'default': DEFAULT_PROP_NUM_TIME_SLICES,
                    'minimum': 0
                },
                PROP_PAULIS_GROUPING: {
                    'type': 'string',
                    'default': DEFAULT_PROP_PAULIS_GROUPING,
                    'oneOf': [
                        {'enum': [
                            DEFAULT_PROP_PAULIS_GROUPING,
                            ALTERNATIVE_PROP_PAULIS_GROUPING
                        ]}
                    ]
                },
                PROP_EXPANSION_MODE: {
                    'type': 'string',
                    'default': DEFAULT_PROP_EXPANSION_MODE,
                    'oneOf': [
                        {'enum': [
                            DEFAULT_PROP_EXPANSION_MODE,
                            ALTERNATIVE_PROP_EXPANSION_MODE
                        ]}
                    ]
                },
                PROP_EXPANSION_ORDER: {
                    'type': 'integer',
                    'default': DEFAULT_PROP_EXPANSION_ORDER,
                    'minimum': 1
                },
                PROP_NUM_ANCILLAE: {
                    'type': 'integer',
                    'default': DEFAULT_PROP_NUM_ANCILLAE,
                    'minimum': 1
                },
                PROP_USE_BASIS_GATES: {
                    'type': 'boolean',
                    'default': True
                }
            },
            'additionalProperties': False
        },
        'problems': ['energy'],
        'depends': ['initial_state', 'iqft'],
        'defaults': {
            'initial_state': {
                'name': 'ZERO'
            },
            'iqft': {
                'name': 'STANDARD'
            }
        }
    }

    def __init__(self, configuration=None):
        super().__init__(configuration or self.QPE_CONFIGURATION.copy())
        self._operator = None
        self._state_in = None
        self._num_time_slices = 0
        self._paulis_grouping = None
        self._expansion_mode = None
        self._expansion_order = None
        self._num_ancillae = 0
        self._use_basis_gates = False
        self._ret = {}

    def init_params(self, params, algo_input):
        """
        Initialize via parameters dictionary and algorithm input instance
        Args:
            params: parameters dictionary
            algo_input: EnergyInput instance
        """
        if algo_input is None:
            raise AlgorithmError("EnergyInput instance is required.")

        operator = algo_input.qubit_op

        qpe_params = params.get(QuantumAlgorithm.SECTION_KEY_ALGORITHM)
        num_time_slices = qpe_params.get(QPE.PROP_NUM_TIME_SLICES)
        paulis_grouping = qpe_params.get(QPE.PROP_PAULIS_GROUPING)
        expansion_mode = qpe_params.get(QPE.PROP_EXPANSION_MODE)
        expansion_order = qpe_params.get(QPE.PROP_EXPANSION_ORDER)
        num_ancillae = qpe_params.get(QPE.PROP_NUM_ANCILLAE)
        use_basis_gates = qpe_params.get(QPE.PROP_USE_BASIS_GATES)

        # Set up initial state, we need to add computed num qubits to params
        init_state_params = params.get(QuantumAlgorithm.SECTION_KEY_INITIAL_STATE)
        init_state_params['num_qubits'] = operator.num_qubits
        init_state = get_initial_state_instance(init_state_params['name'])
        init_state.init_params(init_state_params)

        # Set up iqft, we need to add num qubits to params which is our num_ancillae bits here
        iqft_params = params.get(QuantumAlgorithm.SECTION_KEY_IQFT)
        iqft_params['num_qubits'] = num_ancillae
        iqft = get_iqft_instance(iqft_params['name'])
        iqft.init_params(iqft_params)

        self.init_args(
            operator, init_state, iqft, num_time_slices, num_ancillae,
            paulis_grouping=paulis_grouping, expansion_mode=expansion_mode,
            expansion_order=expansion_order, use_basis_gates=use_basis_gates)

    def init_args(self, operator, state_in, iqft, num_time_slices, num_ancillae,
                  paulis_grouping='default', expansion_mode='trotter', expansion_order=1, use_basis_gates=True):
        if self._backend.find('statevector') >= 0:
            raise ValueError('Selected backend does not support measurements.')
        self._operator = operator
        self._state_in = state_in
        self._iqft = iqft
        self._num_time_slices = num_time_slices
        self._num_ancillae = num_ancillae
        self._paulis_grouping = paulis_grouping
        self._expansion_mode = expansion_mode
        self._expansion_order = expansion_order
        self._use_basis_gates = use_basis_gates
        self._ret = {}

    def _construct_qpe_evolution(self):
        """Implement the Quantum Phase Estimation algorithm"""

        a = QuantumRegister(self._num_ancillae, name='a')
        c = ClassicalRegister(self._num_ancillae, name='c')
        q = QuantumRegister(self._operator.num_qubits, name='q')
        self._ret['circuit_components'] = {}
        self._ret['circuit_components']['registers'] = {'a': a, 'q': q, 'c': c}

        self._ret['circuit_components']['state_init'] = self._state_in.construct_circuit('circuit', q)

        # # Put all ancillae in uniform superposition
        qc = QuantumCircuit(a)
        qc.h(a)
        self._ret['circuit_components']['ancilla_superposition'] = qc

        # phase kickbacks via dynamics
        pauli_list = self._operator.reorder_paulis(grouping=self._paulis_grouping)
        if len(pauli_list) == 1:
            slice_pauli_list = pauli_list
        else:
            if self._expansion_mode == 'trotter':
                slice_pauli_list = pauli_list
            elif self._expansion_mode == 'suzuki':
                slice_pauli_list = Operator._suzuki_expansion_slice_pauli_list(
                    pauli_list,
                    1,
                    self._expansion_order
                )
            else:
                raise ValueError('Unrecognized expansion mode {}.'.format(self._expansion_mode))
        qc = QuantumCircuit(a, q)
        for i in range(self._num_ancillae):
            qc += self._operator.construct_evolution_circuit(
                slice_pauli_list, -2 * np.pi, self._num_time_slices, q, a, ctl_idx=i,
                use_basis_gates=self._use_basis_gates
            )
            qc.u1(np.pi * (2 ** i), a[i])

        self._ret['circuit_components']['phase_kickback'] = qc

        qc = QuantumCircuit(a)
        # inverse qft on ancillae
        # qc.swap(a[0], a[1])
        # QPE.qft(qc, a, self._num_ancillae)
        self._iqft.construct_circuit('circuit', a, qc)
        self._ret['circuit_components']['iqft'] = qc

        # measuring ancillae
        qc = QuantumCircuit(c, a)
        qc.measure(a, c)
        self._ret['circuit_components']['measure'] = qc

        self._ret['circuit'] = reduce(
            QuantumCircuit.__add__,
            [
                self._ret['circuit_components'][component]
                for component in ['state_init', 'ancilla_superposition', 'phase_kickback', 'iqft', 'measure']
            ]
        )

    def _setup_qpe(self):
        self._operator._check_representation('paulis')
        self._ret['translation'] = sum([abs(p[0]) for p in self._operator.paulis])
        self._ret['stretch'] = 0.5 / self._ret['translation']

        # translate the operator
        self._operator._simplify_paulis()
        translation_op = Operator([
            [
                self._ret['translation'],
                Pauli(
                    np.zeros(self._operator.num_qubits),
                    np.zeros(self._operator.num_qubits)
                )
            ]
        ])
        translation_op._simplify_paulis()
        self._operator += translation_op

        # stretch the operator
        for p in self._operator._paulis:
            p[0] = p[0] * self._ret['stretch']

        self._construct_qpe_evolution()
        logger.info('QPE circuit depth is roughly {}.'.format(
            len(self._ret['circuit'].qasm().split('\n'))
        ))

    def _compute_energy(self):
        if 'circuit' not in self._ret:
            self._setup_qpe()
        result = self.execute(self._ret['circuit'])

        rd = result.get_counts(self._ret['circuit'])
        rets = sorted([(rd[k], k) for k in rd])[::-1]
        ret = rets[0][-1][::-1]
        retval = sum([t[0] * t[1] for t in zip(
            [1 / 2 ** p for p in range(1, self._num_ancillae + 1)],
            [int(n) for n in ret]
        )])

        self._ret['measurements'] = rets
        self._ret['top_measurement_label'] = ret
        self._ret['top_measurement_decimal'] = retval
        self._ret['energy'] = retval / self._ret['stretch'] - self._ret['translation']

    def run(self):
        self._compute_energy()
        return self._ret
