from meld.remd import slave_runner
from meld.remd.reseed import NullReseeder
import logging


logger = logging.getLogger(__name__)


class MasterReplicaExchangeRunner(object):
    """
    Class to coordinate running of replica exchange

    This class doesn't really know much about the calculation that is happening,
    but it's the glue that holds everything together.
    """

    #
    # read only properties
    #

    @property
    def n_replicas(self):
        return self._n_replicas

    @property
    def alphas(self):
        return self._alphas

    @property
    def step(self):
        return self._step

    @property
    def max_steps(self):
        return self._max_steps

    @property
    def ramp_steps(self):
        return self._ramp_steps

    #
    # public methods
    #

    def __init__(self, n_replicas, max_steps, ladder, adaptor, ramp_steps=None):
        """
        Initialize a MasterReplicaExchangeRunner

        Parameters
            n_replicas -- number of replicas
            max_steps -- maximum number of steps to run
            ladder -- Ladder object to handle exchanges
            adaptor -- Adaptor object to handle alphas adaptation
            ramp_steps -- integer number of steps to ramp up force constants at start of simulation

        """
        self._n_replicas = n_replicas
        self._max_steps = max_steps
        self._step = 1
        self.ladder = ladder
        self.adaptor = adaptor
        self._ramp_steps = ramp_steps

        self._alphas = None
        self._setup_alphas()

        self.reseeder = NullReseeder()

    def to_slave(self):
        """
        Return a SlaveReplicaExchangeRunner based on self.

        """
        return slave_runner.SlaveReplicaExchangeRunner.from_master(self)

    def run(self, communicator, system_runner, store):
        """
        Run replica exchange until finished

        Parameters
            communicator -- A communicator object to talk with slaves
            system_runner -- a ReplicaRunner object to run the simulations
            store -- a Store object to handle storing data to disk

        """
        logger.info('Beginning replica exchange')
        # check to make sure n_replicas matches
        assert self._n_replicas == communicator.n_replicas
        assert self._n_replicas == store.n_replicas

        # load previous state from the store
        states = store.load_states(stage=self.step - 1)

        while self._step <= self._max_steps:
            logger.info('Running replica exchange step %d of %d.',
                        self._step, self._max_steps)
            # update alphas
            ramp_weight = self._compute_ramp_weight()
            system_runner.set_alpha(0., ramp_weight)
            self._alphas = self.adaptor.adapt(self._alphas, self._step)
            communicator.broadcast_alphas_to_slaves(self._alphas)
            communicator.barrier()

            # do one step
            my_state = communicator.broadcast_states_to_slaves(states)
            communicator.barrier()
            if self._step == 1:
                logger.info('First step, minimizing and then running.')
                my_state = system_runner.minimize_then_run(my_state)
            else:
                logger.info('Running molecular dynamics.')
                my_state = system_runner.run(my_state)

            # gather all of the states
            states = communicator.gather_states_from_slaves(my_state)
            communicator.barrier()

            # send them to the slaves
            communicator.broadcast_states_for_energy_calc_to_slaves(states)
            communicator.barrier()

            # compute our energy for each state
            my_energies = self._compute_energies(states, system_runner)
            energies = communicator.gather_energies_from_slaves(my_energies)
            communicator.barrier()

            # ask the ladder how to permute things
            permutation_vector = self.ladder.compute_exchanges(energies, self.adaptor)
            states = self._permute_states(permutation_vector, states)

            # perform reseeding if it is time
            self.reseeder.reseed(self.step, states, store)

            # store everything
            store.save_states(states, self.step)
            store.append_traj(states[0], self.step)
            store.save_alphas(self._alphas, self.step)
            store.save_permutation_vector(permutation_vector, self.step)
            store.save_energy_matrix(energies, self.step)
            store.save_acceptance_probabilities(self.adaptor.get_acceptance_probabilities(), self.step)
            store.save_data_store()

            # on to the next step!
            self._step += 1
            store.save_remd_runner(self)
            store.backup(self.step - 1)
        logger.info('Finished %d steps of replica exchange successfully.', self._max_steps)

    #
    # private helper methods
    #

    @staticmethod
    def _compute_energies(states, system_runner):
        my_energies = []
        for state in states:
            my_energies.append(system_runner.get_energy(state))
        return my_energies

    @staticmethod
    def _permute_states(permutation_matrix, states):
        old_coords = [s.positions for s in states]
        old_energy = [s.energy for s in states]
        for i, index in enumerate(permutation_matrix):
            states[i].positions = old_coords[index]
            states[i].energy = old_energy[index]
        return states

    def _setup_alphas(self):
        delta = 1.0 / (self._n_replicas - 1.0)
        self._alphas = [i * delta for i in range(self._n_replicas)]

    def _compute_ramp_weight(self):
        if self._ramp_steps is None:
            return 1.0
        else:
            if self._step > self._ramp_steps:
                return 1.0
            else:
                return float(self.step + 1) / float(self._ramp_steps)
