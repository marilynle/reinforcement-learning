from copy import deepcopy
import imageio
from itertools import chain
import math
from threading import Thread
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
from tqdm import tqdm
import numpy as np
import os
from queue import Queue
import random

from environments import EnvironmentFactory


class ExperienceDataset(Dataset):
    def __init__(self, experience, backup_n=5, gamma=0.99):
        super(ExperienceDataset, self).__init__()
        assert (backup_n > 0)

        self._exp = []
        for rollout in experience:
            if backup_n == float('inf'):
                ret = 0
                for x in reversed(rollout):
                    exp = dict()
                    ret *= gamma
                    ret += x['reward']
                    exp['state'] = x['state']
                    exp['action'] = x['action']
                    exp['action_dist'] = x['action_dist']
                    exp['return'] = ret
                    exp['terminal'] = x['terminal']
                    self._exp.append(exp)
            else:
                for i, x in enumerate(rollout):
                    exp = dict()
                    exp['state'] = x['state']
                    exp['action'] = x['action']
                    exp['action_dist'] = x['action_dist']
                    r, current_gamma = 0, 1
                    for j in range(i, min(len(rollout), i + backup_n)):
                        r += current_gamma * rollout[j]['reward']
                        current_gamma *= gamma
                    exp['return'] = r
                    nth_exp = rollout[min(i + backup_n, len(rollout) - 1)]
                    exp['s_prime'] = nth_exp['state']
                    exp['terminal'] = x['terminal']
                    exp['gamma'] = gamma**(min(len(rollout), i + backup_n) - i)
                    self._exp.append(exp)

        self._length = len(self._exp)
        self._backup_n = backup_n

    def __getitem__(self, index):
        chosen_exp = self._exp[index]

        if self._backup_n == float('inf'):
            return (chosen_exp['state'],
                    chosen_exp['action_dist'],
                    chosen_exp['action'],
                    chosen_exp['return'],
                    chosen_exp['terminal'])
        else:
            return (chosen_exp['state'],
                    chosen_exp['action_dist'],
                    chosen_exp['action'],
                    chosen_exp['return'],
                    chosen_exp['s_prime'],
                    chosen_exp['terminal'],
                    chosen_exp['gamma'])

    def __len__(self):
        return self._length


class RlAlgorithm(object):
    def __init__(self, env_factory, experiment_name='project', gif_epochs=0, csv_file='latest_run.csv'):
        assert (isinstance(env_factory, EnvironmentFactory))
        self._env_factory = env_factory

        self._experiment_name = experiment_name
        self._gif_epochs = gif_epochs
        self._gif_path, self._csv_file = self._prepare_experiment(experiment_name, gif_epochs, csv_file)

    def train(self, epochs, rollouts_per_epoch=100, max_episode_length=200,
              policy_epochs=5, batch_size=256, environment_threads=1, data_loader_threads=1,
              gif_name=''):
        raise NotImplementedError()

    @staticmethod
    def _prepare_experiment(experiment_name, gif_epochs, csv_file):
        experiment_path = os.path.join('experiments', experiment_name)
        if not os.path.isdir(experiment_path):
            os.makedirs(experiment_path)
        gif_path = None
        if gif_epochs:
            gif_path = os.path.join(experiment_path, 'gifs')
            if not os.path.isdir(gif_path):
                os.mkdir(gif_path)
        csv_file = os.path.join(experiment_path, csv_file)

        # Clear the csv file
        with open(csv_file, 'w') as f:
            f.write('avg_reward, value_loss, policy_loss')
        return gif_path, csv_file


def multinomial_likelihood(dist, idx):
    return dist[range(dist.shape[0]), idx.long()[:, 0]].unsqueeze(1)


def get_log_p(data, mu, sigma):
    """get negative log likelihood from normal distribution"""
    return -torch.log(torch.sqrt(2 * math.pi * sigma ** 2)) - (data - mu) ** 2 / (2 * sigma ** 2)


def multinomial_selection(dist):
    batch_size = dist.shape[0]
    actions = np.empty((batch_size, 1), dtype=np.uint8)
    probs_np = dist.cpu().detach().numpy()
    for i in range(batch_size):
        action_one_hot = np.random.multinomial(1, probs_np[i])
        action_idx = np.argmax(action_one_hot)
        actions[i, 0] = action_idx
    return actions


def get_epsilon_greedy_selection(epsilon):
    def epsilon_greedy_selection(values):
        batch_size, num_actions = values.shape
        actions = np.empty((batch_size, 1), dtype=np.uint8)
        values_np = values.cpu().detach().numpy()
        for i in range(batch_size):
            if random.random() < epsilon:
                actions[i] = np.argmax(values_np[i])
            else:
                actions[i, 0] = random.randint(0, num_actions - 1)
        return actions

    return epsilon_greedy_selection


# class DQN(RlAlgorithm):
#     def __init__(self, env_factory, action_value_network, device=torch.device('cpu'),
#                  gamma=0.99, lr=1e-3, betas=(0.9, 0.999),
#                  weight_decay=0.01, experiment_name='project',
#                  gif_epochs=0, csv_file='latest_run.csv',
#                  epsilon_start=0.95, epsilon_end=0.1, epsilon_decay_epochs=100):
#         super(DQN, self).__init__(env_factory, experiment_name, gif_epochs, csv_file)
#
#         self._target_network = deepcopy(action_value_network).to(device)
#         self._learning_network = action_value_network.to(device)
#
#         self._params = chain(self._learning_network.parameters(), self._target_network.parameters())
#         self._optimizer = optim.Adam(self._params, lr=lr, betas=betas, weight_decay=weight_decay)
#         self._value_criteria = nn.MSELoss()
#
#         self._gamma = gamma
#         self._epsilon_start = epsilon_start
#         self._epsilon_end = epsilon_end
#         self._epsilon_decay_epochs = epsilon_decay_epochs
#         self._epsilon = self._epsilon_start
#         self._device = device
#
#     def train(self, epochs, rollouts_per_epoch=100, max_episode_length=200,
#               policy_epochs=5, batch_size=256, environment_threads=1, data_loader_threads=1,
#               gif_name=''):
#         loop = tqdm(total=epochs, position=0, leave=False)
#
#         # Prepare the environments
#         environments = [self._env_factory.new() for _ in range(environment_threads)]
#         rollouts_per_thread = rollouts_per_epoch // environment_threads
#         remainder = rollouts_per_epoch % environment_threads
#         rollout_nums = ([rollouts_per_thread + 1] * remainder) + (
#                     [rollouts_per_thread] * (environment_threads - remainder))
#
#         for e in range(epochs):
#             # Run the environments
#             experience_queue = Queue()
#             reward_queue = Queue()
#             threads = [Thread(target=_run_envs, args=(environments[i],
#                                                       None,
#                                                       self._learning_network,
#                                                       get_epsilon_greedy_selection(self._epsilon),
#                                                       experience_queue,
#                                                       reward_queue,
#                                                       rollout_nums[i],
#                                                       max_episode_length,
#                                                       self._gamma,
#                                                       self._device)) for i in range(environment_threads)]
#             for x in threads:
#                 x.start()
#             for x in threads:
#                 x.join()
#
#             # Collect the experience
#             rollouts = list(experience_queue.queue)
#             avg_r = sum(reward_queue.queue) / reward_queue.qsize()
#             loop.set_description('avg reward: % 6.2f' % avg_r)
#
#             # Make gifs
#             if self._gif_epochs and e % self._gif_epochs == 0:
#                 _make_gif(rollouts[0], os.path.join(gif_path, gif_name + '%d.gif' % e))
#
#             experience_dataset = ExperienceDataset(rollouts)
#             data_loader = DataLoader(experience_dataset, num_workers=data_loader_threads, batch_size=batch_size,
#                                      shuffle=True,
#                                      pin_memory=True)
#             avg_policy_loss = 0
#             avg_val_loss = 0
#             for _ in range(policy_epochs):
#                 avg_policy_loss = 0
#                 avg_val_loss = 0
#                 for state, old_action_dist, old_action, reward, ret in data_loader:
#                     state = _prepare_tensor_batch(state, self._device)
#                     old_action_dist = _prepare_tensor_batch(old_action_dist, self._device)
#                     old_action = _prepare_tensor_batch(old_action, self._device)
#                     ret = _prepare_tensor_batch(ret, self._device).unsqueeze(1)
#
#                     self._optimizer.zero_grad()
#
#                     # If there is an embedding net, carry out the embedding
#                     if self._embedding_network:
#                         state = self._embedding_network(state)
#
#                     # Calculate the ratio term
#                     current_action_dist = self._policy_network(state, False)
#                     current_likelihood = self._likelihood_fn(current_action_dist, old_action)
#                     old_likelihood = self._likelihood_fn(old_action_dist, old_action)
#                     ratio = (current_likelihood / old_likelihood)
#
#                     # Calculate the value loss
#                     expected_returns = self._value_network(state)
#                     val_loss = self._value_criteria(expected_returns, ret)
#
#                     # Calculate the policy loss
#                     advantage = ret - expected_returns.detach()
#                     lhs = ratio * advantage
#                     rhs = torch.clamp(ratio, self._ppo_lower_bound, self._ppo_upper_bound) * advantage
#                     policy_loss = -torch.mean(torch.min(lhs, rhs))
#
#                     # For logging
#                     avg_val_loss += val_loss.item()
#                     avg_policy_loss += policy_loss.item()
#
#                     # Backpropagate
#                     loss = policy_loss + val_loss
#                     loss.backward()
#                     self._optimizer.step()
#
#                 # Log info
#                 avg_val_loss /= len(data_loader)
#                 avg_policy_loss /= len(data_loader)
#                 loop.set_description(
#                     'avg reward: % 6.2f, value loss: % 6.2f, policy loss: % 6.2f' % (
#                     avg_r, avg_val_loss, avg_policy_loss))
#             with open(self._csv_file, 'a+') as f:
#                 f.write('%6.2f, %6.2f, %6.2f\n' % (avg_r, avg_val_loss, avg_policy_loss))
#             print()
#             loop.update(1)


class PPO(RlAlgorithm):
    _action_type = {
        'multinomial': (multinomial_selection, multinomial_likelihood),
    }

    def __init__(self, env_factory, policy_network, value_network, action_selection='multinomial',
                 embedding_network=None, device=torch.device('cpu'), epsilon=0.2, gamma=0.99, lr=1e-3,
                 betas=(0.9, 0.999), weight_decay=0.01, experiment_name='project', gif_epochs=0,
                 csv_file='latest_run.csv', entropy_coefficient=1e-3, backup_n=5):
        super(PPO, self).__init__(env_factory, experiment_name, gif_epochs, csv_file)

        self._policy_network = policy_network.to(device)
        self._value_network = value_network.to(device)
        if backup_n == float('inf'):
            self._value_target_network = None
        else:
            self._value_target_network = deepcopy(self._value_network)

        self._params = chain(self._policy_network.parameters(), self._value_network.parameters())
        if embedding_network:
            embedding_network = embedding_network.to(device)
            self._params = chain(self._params, embedding_network.parameters())
        self._embedding_network = embedding_network

        self._optimizer = optim.Adam(self._params, lr=lr, betas=betas, weight_decay=weight_decay)
        # self._optimizer = optim.RMSprop(self._params, lr=lr, weight_decay=weight_decay)
        self._value_criteria = nn.MSELoss()

        self._action_selection_fn, self._likelihood_fn = PPO._action_type[action_selection]
        self._ppo_lower_bound = 1 - epsilon
        self._ppo_upper_bound = 1 + epsilon
        self._gamma = gamma
        self._entropy_coefficient = entropy_coefficient
        self._backup_n = backup_n
        self._device = device

    def train(self, epochs, rollouts_per_epoch=100, max_episode_length=200,
              policy_epochs=5, batch_size=256, environment_threads=1, data_loader_threads=1,
              gif_name=''):
        loop = tqdm(total=epochs, position=0, leave=False)

        # Prepare the environments
        environments = [self._env_factory.new() for _ in range(environment_threads)]
        rollouts_per_thread = rollouts_per_epoch // environment_threads
        remainder = rollouts_per_epoch % environment_threads
        rollout_nums = ([rollouts_per_thread + 1] * remainder) + (
                [rollouts_per_thread] * (environment_threads - remainder))

        for e in range(epochs):
            if self._value_target_network is not None:
                self._value_target_network = deepcopy(self._value_network)

            # Run the environments
            experience_queue = Queue()
            reward_queue = Queue()
            threads = [Thread(target=_run_envs, args=(environments[i],
                                                      self._embedding_network,
                                                      self._policy_network,
                                                      self._action_selection_fn,
                                                      experience_queue,
                                                      reward_queue,
                                                      rollout_nums[i],
                                                      max_episode_length,
                                                      self._device)) for i in range(environment_threads)]
            for x in threads:
                x.start()
            for x in threads:
                x.join()

            # Collect the experience
            rollouts = list(experience_queue.queue)
            avg_r = sum(reward_queue.queue) / reward_queue.qsize()
            loop.set_description('avg reward: % 6.2f' % avg_r)

            # Make gifs
            if self._gif_epochs and e % self._gif_epochs == 0:
                _make_gif(rollouts[0], os.path.join(self._gif_path, gif_name + '%d.gif' % e))

            # Update the policy
            experience_dataset = ExperienceDataset(rollouts, self._backup_n, self._gamma)
            data_loader = DataLoader(experience_dataset, num_workers=data_loader_threads, batch_size=batch_size,
                                     shuffle=True,
                                     pin_memory=True,
                                     drop_last=True)
            avg_policy_loss = 0
            avg_val_loss = 0
            avg_entropy_loss = 0
            for _ in range(policy_epochs):
                avg_policy_loss = 0
                avg_val_loss = 0
                avg_entropy_loss = 0
                for exp in data_loader:
                    if self._backup_n == float('inf'):
                        state, old_action_dist, old_action, ret, terminal = exp
                        s_prime = None
                    else:
                        state, old_action_dist, old_action, ret, s_prime, terminal, discount = exp
                        s_prime = _prepare_tensor_batch(s_prime, self._device)
                        discount = _prepare_tensor_batch(discount, self._device).unsqueeze(1)

                    state = _prepare_tensor_batch(state, self._device)
                    old_action_dist = _prepare_tensor_batch(old_action_dist, self._device)
                    old_action = _prepare_tensor_batch(old_action, self._device)
                    ret = _prepare_tensor_batch(ret, self._device).unsqueeze(1)
                    terminal = _prepare_tensor_batch(terminal, self._device).unsqueeze(1)

                    self._optimizer.zero_grad()

                    # If there is an embedding net, carry out the embedding
                    if self._embedding_network:
                        state = self._embedding_network(state)
                        if s_prime is not None:
                            s_prime = self._embedding_network(s_prime).detach()

                    # Calculate the ratio term
                    current_action_dist = self._policy_network(state)
                    current_likelihood = self._likelihood_fn(current_action_dist, old_action)
                    old_likelihood = self._likelihood_fn(old_action_dist, old_action)
                    ratio = (current_likelihood / old_likelihood)

                    # Calculate the value loss
                    expected_returns = self._value_network(state)
                    if s_prime is None:
                        val_loss = self._value_criteria(expected_returns, ret)
                        target = ret
                    else:
                        target = ret + discount * self._value_target_network(s_prime).detach()
                        target = (1 - terminal) * target
                        val_loss = self._value_criteria(expected_returns, target)

                    # Calculate the policy loss
                    advantage = target - expected_returns.detach()
                    lhs = ratio * advantage
                    rhs = torch.clamp(ratio, self._ppo_lower_bound, self._ppo_upper_bound) * advantage
                    policy_loss = -torch.mean(torch.min(lhs, rhs))

                    # Calculate the entropy loss
                    entropy_loss = -self._entropy_coefficient * torch.mean(_discrete_entropy(current_action_dist))

                    # For logging
                    avg_val_loss += val_loss.item()
                    avg_policy_loss += policy_loss.item()
                    avg_entropy_loss += entropy_loss.item()

                    # Backpropagate
                    loss = policy_loss + val_loss + entropy_loss
                    loss.backward()
                    self._optimizer.step()

                # Log info
                avg_val_loss /= len(data_loader)
                avg_policy_loss /= len(data_loader)
                loop.set_description(
                    'avg reward: % 6.2f, value loss: % 6.2f, policy loss: % 6.2f, entropy loss: % 6.2f' % (
                        avg_r, avg_val_loss, avg_policy_loss, avg_entropy_loss))
            with open(self._csv_file, 'a+') as f:
                f.write('%6.2f, %6.2f, %6.2f\n' % (avg_r, avg_val_loss, avg_policy_loss))
            print()
            loop.update(1)


# def _calculate_returns(trajectory, gamma):
#     current_return = 0
#     for i in reversed(range(len(trajectory))):
#         current_exp = trajectory[i]
#         current_return = current_exp['reward'] + gamma * current_return
#         current_exp['return'] = current_return


def _run_envs(env, embedding_net, policy, action_selection_fn, experience_queue, reward_queue, num_rollouts,
              max_episode_length, device):
    for _ in range(num_rollouts):
        current_rollout = []
        s = env.reset()
        episode_reward = 0
        for _ in range(max_episode_length):
            input_state = _prepare_numpy(s, device)
            if embedding_net:
                input_state = embedding_net(input_state)

            action_dist = policy(input_state)
            action = action_selection_fn(action_dist)[0]  # Remove the batch dimension
            s_prime, r, t = env.step(action)

            if t:
                r = 0

            current_exp = {
                'state': s,
                'action_dist': action_dist.cpu().detach().numpy()[0],
                'action': action,
                'reward': r,
                'terminal': t
            }
            current_rollout.append(current_exp)
            episode_reward += r
            if t:
                break
            s = s_prime

        experience_queue.put(current_rollout)
        reward_queue.put(episode_reward)


# def _prepare_dqn_dataset(rollouts):
#     for rollout in rollouts:
#         for i in range(len(rollout) - 1):
#             current_exp = rollout[i]
#             next_exp = rollout[i + 1]
#             exp = {
#                 's' = current_exp['state']
#             'a' = current_exp['action']
#             'r' = current_exp['reward']
#             's_prime' = next_exp['state']
#             't' = next_exp['terminal']
#             }

def _prepare_numpy(ndarray, device):
    return torch.from_numpy(ndarray).float().unsqueeze(0).to(device)


def _prepare_tensor_batch(tensor, device):
    return tensor.detach().float().to(device)


def _make_gif(rollout, filename):
    with imageio.get_writer(filename, mode='I', duration=1 / 30) as writer:
        for x in rollout:
            writer.append_data(((x['state'][:, :, 0] + 0.5) * 255).astype(np.uint8))


def _discrete_entropy(array):
    log_prob = torch.log(array)
    return -torch.sum(log_prob * array, dim=1, keepdim=True)
