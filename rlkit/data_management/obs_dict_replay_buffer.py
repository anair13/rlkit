import numpy as np
from gym.spaces import Dict, Discrete

from rlkit.data_management.replay_buffer import ReplayBuffer

from multiworld.core.multitask_env import MultitaskEnv
import rlkit.data_management.images as image_np

class ObsDictReplayBuffer(ReplayBuffer):
    """
    Save goals from the same trajectory into the replay buffer.
    Only add_path is implemented.

    Implementation details:
     - Every sample from [0, self._size] will be valid.
     - Observation and next observation are saved separately. It's a memory
       inefficient to save the observations twice, but it makes the code
       *much* easier since you no longer have to worry about termination
       conditions.
    """

    def __init__(
            self,
            max_size,
            env,
            ob_keys_to_save=None,
            internal_keys=None,
            observation_key='observation',
            save_data_in_snapshot=False,
    ):
        """

        :param max_size:
        :param env:
        :param ob_keys_to_save: List of keys to save
        """
        if ob_keys_to_save is None:
            ob_keys_to_save = []
        else:  # in case it's a tuple
            ob_keys_to_save = list(ob_keys_to_save)
        if internal_keys is None:
            internal_keys = []
        self.internal_keys = internal_keys
        assert isinstance(env.observation_space, Dict)
        self.max_size = max_size
        self.env = env
        self.ob_keys_to_save = ob_keys_to_save
        self.observation_key = observation_key
        self.save_data_in_snapshot = save_data_in_snapshot

        self._action_dim = env.action_space.low.size
        self._actions = np.zeros((max_size, self._action_dim), dtype=np.float32)
        # Make everything a 2D np array to make it easier for other code to
        # reason about the shape of the data
        # self._terminals[i] = a terminal was received at time i
        self._terminals = np.zeros((max_size, 1), dtype='uint8')
        self._rewards = np.zeros((max_size, 1))
        # self._obs[key][i] is the value of observation[key] at time i
        self._obs = {}
        self._next_obs = {}
        self.ob_spaces = self.env.observation_space.spaces
        for key in [observation_key]:
            if key not in ob_keys_to_save:
                ob_keys_to_save.append(key)
        for key in ob_keys_to_save + internal_keys:
            assert key in self.ob_spaces, \
                "Key not found in the observation space: %s" % key
            arr_initializer = np
            if key.startswith('image'):
                arr_initializer = image_np
            self._obs[key] = arr_initializer.zeros(
                (max_size, self.ob_spaces[key].low.size), dtype=np.float32)
            self._next_obs[key] = arr_initializer.zeros(
                (max_size, self.ob_spaces[key].low.size), dtype=np.float32)

        self._top = 0
        self._size = 0

        # Let j be any index in self._idx_to_future_obs_idx[i]
        # Then self._next_obs[j] is a valid next observation for observation i
        self._idx_to_future_obs_idx = [None] * max_size

        if isinstance(self.env.action_space, Discrete):
            raise NotImplementedError("TODO")

    def add_sample(self, observation, action, reward, terminal,
                   next_observation, **kwargs):
        raise NotImplementedError("Only use add_path")

    def terminate_episode(self):
        pass

    def num_steps_can_sample(self):
        return self._size

    def add_path(self, path):
        obs = path["observations"]
        actions = path["actions"]
        rewards = path["rewards"]
        next_obs = path["next_observations"]
        terminals = path["terminals"]
        path_len = len(rewards)

        actions = flatten_n(actions)
        # why do we need: obs[:, 0] on this line and the next line sometimes?
        obs = flatten_dict(obs, self.ob_keys_to_save + self.internal_keys)
        next_obs = flatten_dict(next_obs, self.ob_keys_to_save + self.internal_keys)

        if self._top + path_len >= self.max_size:
            num_pre_wrap_steps = self.max_size - self._top
            # numpy slice
            pre_wrap_buffer_slice = np.s_[
                                    self._top:self._top + num_pre_wrap_steps, :
                                    ]
            pre_wrap_path_slice = np.s_[0:num_pre_wrap_steps, :]

            num_post_wrap_steps = path_len - num_pre_wrap_steps
            post_wrap_buffer_slice = slice(0, num_post_wrap_steps)
            post_wrap_path_slice = slice(num_pre_wrap_steps, path_len)
            for buffer_slice, path_slice in [
                (pre_wrap_buffer_slice, pre_wrap_path_slice),
                (post_wrap_buffer_slice, post_wrap_path_slice),
            ]:
                self._actions[buffer_slice] = actions[path_slice]
                self._terminals[buffer_slice] = terminals[path_slice]
                self._rewards[buffer_slice] = terminals[path_slice]
                for key in self.ob_keys_to_save + self.internal_keys:
                    self._obs[key][buffer_slice] = obs[key][path_slice]
                    self._next_obs[key][buffer_slice] = next_obs[key][path_slice]
            # Pointers from before the wrap
            for i in range(self._top, self.max_size):
                self._idx_to_future_obs_idx[i] = np.hstack((
                    # Pre-wrap indices
                    np.arange(i, self.max_size),
                    # Post-wrap indices
                    np.arange(0, num_post_wrap_steps)
                ))
            # Pointers after the wrap
            for i in range(0, num_post_wrap_steps):
                self._idx_to_future_obs_idx[i] = np.arange(
                    i,
                    num_post_wrap_steps,
                )
        else:
            slc = np.s_[self._top:self._top + path_len, :]
            self._actions[slc] = actions
            self._terminals[slc] = terminals
            self._rewards[slc] = rewards

            for key in self.ob_keys_to_save + self.internal_keys:
                self._obs[key][slc] = obs[key]
                self._next_obs[key][slc] = next_obs[key]
            for i in range(self._top, self._top + path_len):
                self._idx_to_future_obs_idx[i] = np.arange(
                    i, self._top + path_len
                )
        self._top = (self._top + path_len) % self.max_size
        self._size = min(self._size + path_len, self.max_size)

    def _sample_indices(self, batch_size):
        return np.random.randint(0, self._size, batch_size)

    def random_batch(self, batch_size):
        indices = np.random.randint(0, self._size, batch_size)
        obs = self._obs[self.observation_key][indices]
        actions = self._actions[indices]
        rewards = self._rewards[indices]
        next_obs = self._next_obs[self.observation_key][indices]
        terminals = self._terminals[indices]
        batch = {
            'observations': obs,
            'actions': actions,
            'rewards': rewards,
            'terminals': terminals,
            'next_observations': next_obs,
            'indices': np.array(indices).reshape(-1, 1),
        }
        return batch

    def _sample_goals_from_env(self, batch_size):
        return self.env.sample_goals(batch_size)

    def _batch_obs_dict(self, indices):
        return {
            key: self._obs[key][indices]
            for key in self.ob_keys_to_save
        }

    def _batch_next_obs_dict(self, indices):
        return {
            key: self._next_obs[key][indices]
            for key in self.ob_keys_to_save
        }

    def get_snapshot(self):
        snapshot = super().get_snapshot()
        if self.save_data_in_snapshot:
            snapshot.update({
                'observations': self.get_slice(self._obs, slice(0, self._top)),
                'next_observations': self.get_slice(
                    self._next_obs, slice(0, self._top)
                ),
                'actions': self._actions[:self._top],
                'terminals': self._terminals[:self._top],
                'rewards': self._rewards[:self._top],
                'idx_to_future_obs_idx': (
                    self._idx_to_future_obs_idx[:self._top]
                ),
            })
        return snapshot

    def get_slice(self, obs_dict, slc):
        new_dict = {}
        for key in self.ob_keys_to_save + self.internal_keys:
            new_dict[key] = obs_dict[key][slc]
        return new_dict


class ObsDictRelabelingBuffer(ObsDictReplayBuffer):
    """
    Save goals from the same trajectory into the replay buffer.
    Only add_path is implemented.

    Implementation details:
     - Every sample from [0, self._size] will be valid.
     - Observation and next observation are saved separately. It's a memory
       inefficient to save the observations twice, but it makes the code
       *much* easier since you no longer have to worry about termination
       conditions.
    """

    def __init__(
            self,
            max_size,
            env,
            fraction_goals_rollout_goals=1.0,
            fraction_goals_env_goals=0.0,
            goal_keys=None,
            desired_goal_key='desired_goal',
            achieved_goal_key='achieved_goal',
            vectorized=False,
            ob_keys_to_save=None,
            use_multitask_rewards=True,
            **kwargs
    ):
        """

        :param max_size:
        :param env:
        :param fraction_goals_rollout_goals: Default, no her
        :param fraction_resampled_goals_env_goals:  of the resampled
        goals, what fraction are sampled from the env. Reset are sampled from future.
        :param ob_keys_to_save: List of keys to save
        """
        if ob_keys_to_save is None:
            ob_keys_to_save = []
        else:  # in case it's a tuple
            ob_keys_to_save = list(ob_keys_to_save)
        if desired_goal_key not in ob_keys_to_save:
            ob_keys_to_save.append(desired_goal_key)
        if achieved_goal_key not in ob_keys_to_save:
            ob_keys_to_save.append(achieved_goal_key)
        super().__init__(
            max_size,
            env,
            ob_keys_to_save=ob_keys_to_save,
            **kwargs
        )
        if goal_keys is None:
            goal_keys = [desired_goal_key]
        self.goal_keys = goal_keys
        if desired_goal_key not in self.goal_keys:
            self.goal_keys.append(desired_goal_key)
        assert isinstance(env.observation_space, Dict)
        assert 0 <= fraction_goals_rollout_goals
        assert 0 <= fraction_goals_env_goals
        assert 0 <= fraction_goals_rollout_goals + fraction_goals_env_goals
        assert fraction_goals_rollout_goals + fraction_goals_env_goals <= 1
        self.fraction_goals_rollout_goals = fraction_goals_rollout_goals
        self.fraction_goals_env_goals = fraction_goals_env_goals
        self.desired_goal_key = desired_goal_key
        self.achieved_goal_key = achieved_goal_key
        self.vectorized = vectorized
        self.use_multitask_rewards = use_multitask_rewards

    def random_batch(self, batch_size):
        indices = self._sample_indices(batch_size)
        resampled_goals = self._next_obs[self.desired_goal_key][indices]

        num_env_goals = int(batch_size * self.fraction_goals_env_goals)
        num_rollout_goals = int(batch_size * self.fraction_goals_rollout_goals)
        num_future_goals = batch_size - (num_env_goals + num_rollout_goals)
        new_obs_dict = self._batch_obs_dict(indices)
        new_next_obs_dict = self._batch_next_obs_dict(indices)

        if num_env_goals > 0:
            env_goals = self._sample_goals_from_env(num_env_goals)
            last_env_goal_idx = num_rollout_goals + num_env_goals

            resampled_goals[num_rollout_goals:last_env_goal_idx] = (
                env_goals[self.desired_goal_key]
            )
            for goal_key in self.goal_keys:
                new_obs_dict[goal_key][num_rollout_goals:last_env_goal_idx] = \
                    env_goals[goal_key]
                new_next_obs_dict[goal_key][num_rollout_goals:last_env_goal_idx] = \
                    env_goals[goal_key]
        if num_future_goals > 0:
            future_obs_idxs = []
            for i in indices[-num_future_goals:]:
                possible_future_obs_idxs = self._idx_to_future_obs_idx[i]
                # This is generally faster than random.choice. Makes you wonder what
                # random.choice is doing
                num_options = len(possible_future_obs_idxs)
                next_obs_i = int(np.random.randint(0, num_options))
                future_obs_idxs.append(possible_future_obs_idxs[next_obs_i])
            future_obs_idxs = np.array(future_obs_idxs)
            resampled_goals[-num_future_goals:] = self._next_obs[
                self.achieved_goal_key
            ][future_obs_idxs]
            for goal_key in self.goal_keys:
                new_obs_dict[goal_key][-num_future_goals:] = \
                    self._next_obs[goal_key][future_obs_idxs]
                new_next_obs_dict[goal_key][-num_future_goals:] = \
                    self._next_obs[goal_key][future_obs_idxs]

        new_obs_dict[self.desired_goal_key] = resampled_goals
        new_next_obs_dict[self.desired_goal_key] = resampled_goals
        resampled_goals = new_next_obs_dict[self.desired_goal_key]

        new_actions = self._actions[indices]

        if self.use_multitask_rewards: # isinstance(self.env, MultitaskEnv):
            new_rewards = self.env.compute_rewards(
                new_actions,
                new_next_obs_dict,
            )
        else:  # Assuming it's a (possibly wrapped) gym GoalEnv
            new_rewards = np.ones((batch_size, 1))
            for i in range(batch_size):
                new_rewards[i] = self.env.compute_reward(
                    new_next_obs_dict[self.achieved_goal_key][i],
                    new_next_obs_dict[self.desired_goal_key][i],
                    None
                )
        if not self.vectorized:
            new_rewards = new_rewards.reshape(-1, 1)

        new_obs = new_obs_dict[self.observation_key]
        new_next_obs = new_next_obs_dict[self.observation_key]
        batch = {
            'observations': new_obs,
            'actions': new_actions,
            'rewards': new_rewards,
            'terminals': self._terminals[indices],
            'next_observations': new_next_obs,
            'resampled_goals': resampled_goals,
            'indices': np.array(indices).reshape(-1, 1),
        }
        return batch

def flatten_n(xs):
    xs = np.asarray(xs)
    return xs.reshape((xs.shape[0], -1))


def flatten_dict(dicts, keys):
    """
    Turns list of dicts into dict of np arrays
    """
    return {
        key: flatten_n([d[key] for d in dicts])
        for key in keys
    }
