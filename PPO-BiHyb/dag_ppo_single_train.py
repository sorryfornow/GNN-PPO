import argparse
import torch
from torch import nn
import os
import time
import yaml
import subprocess
import sys
import random
import numpy as np
from torch.multiprocessing import Pool, cpu_count
from copy import deepcopy

from src.dag_ppo_single_model import ActorNet, CriticNet, GraphEncoder
from utils.utils import print_args
from utils.tfboard_helper import TensorboardUtil
from utils.dag_graph import DAGraph
from dag_data.dag_generator import load_tpch_tuples
from dag_ppo_single_eval import evaluate


class ItemsContainer:
    def __init__(self):
        self.__reward = []
        self.__inp_graph = []
        self.__makespan = []
        self.__node_candidates = []
        self.__done = []

    def append(self, reward, inp_graph, makespan, node_candidates, done):
        self.__reward.append(reward)
        self.__inp_graph.append(inp_graph)
        self.__makespan.append(makespan)
        self.__node_candidates.append(node_candidates)
        self.__done.append(done)

    @property
    def reward(self):
        return deepcopy(self.__reward)

    @property
    def inp_graph(self):
        return deepcopy(self.__inp_graph)

    @property
    def makespan(self):
        return deepcopy(self.__makespan)

    @property
    def node_candidates(self):
        return deepcopy(self.__node_candidates)

    @property
    def done(self):
        return deepcopy(self.__done)

    def update(self, idx, reward=None, inp_graph=None, makespan=None, node_candidates=None, done=None):
        if reward is not None:
            self.__reward[idx] = reward
        if inp_graph is not None:
            self.__inp_graph[idx] = inp_graph
        if makespan is not None:
            self.__makespan[idx] = makespan
        if node_candidates is not None:
            self.__node_candidates[idx] = node_candidates
        if done is not None:
            self.__done[idx] = done


class Memory:
    def __init__(self):
        self.actions = []
        self.states = []
        self.candidates = []
        self.logprobs = []
        self.rewards = []
        self.is_terminals = []

    def clear_memory(self):
        del self.actions[:]
        del self.states[:]
        del self.candidates[:]
        del self.logprobs[:]
        del self.rewards[:]
        del self.is_terminals[:]


class ActorCritic(nn.Module):
    def __init__(self, dag_graph, node_feature_dim, node_output_size, batch_norm, one_hot_degree, gnn_layers):
        super(ActorCritic, self).__init__()

        self.state_encoder = GraphEncoder(node_feature_dim, node_output_size, batch_norm, one_hot_degree, gnn_layers)
        self.actor_net = ActorNet(dag_graph, node_output_size * 4, batch_norm)
        self.value_net = CriticNet(dag_graph, node_output_size * 4, batch_norm)

    def forward(self):
        raise NotImplementedError

    def act(self, inp_graph, node_candidates, memory):
        state_feat = self.state_encoder(inp_graph)
        actions, action_logits, entropy = self.actor_net(state_feat, node_candidates)

        memory.states.append(inp_graph)
        memory.candidates.append(node_candidates)
        memory.actions.append(actions)
        memory.logprobs.append(action_logits)

        return actions

    def evaluate(self, inp_graph, node_candidates, action):
        state_feat = self.state_encoder(inp_graph)
        _, action_logits, entropy = self.actor_net(state_feat, node_candidates, action)
        state_value = self.value_net(state_feat)

        return action_logits, state_value, entropy


class PPO:
    def __init__(self, dag_graph, args, device):
        self.lr = args.learning_rate
        self.betas = args.betas
        self.gamma = args.gamma
        self.eps_clip = args.eps_clip
        self.K_epochs = args.k_epochs

        self.device = device

        ac_params = dag_graph, args.node_feature_dim, args.node_output_size, args.batch_norm, \
                    args.one_hot_degree, args.gnn_layers

        self.policy = ActorCritic(*ac_params).to(self.device)
        self.optimizer = torch.optim.Adam(
            [{'params': self.policy.actor_net.parameters()},
             {'params': self.policy.value_net.parameters()},
             {'params': self.policy.state_encoder.parameters(), 'lr': self.lr / 10}],
            lr=self.lr, betas=self.betas)
        if len(args.lr_steps) > 0:
            # rescale lr_step value to match the action steps
            lr_steps = [step // args.update_timestep for step in args.lr_steps]
            self.lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(self.optimizer, lr_steps, gamma=0.1)
        else:
            self.lr_scheduler = None
        self.policy_old = ActorCritic(*ac_params).to(self.device)
        self.policy_old.load_state_dict(self.policy.state_dict())

        self.MseLoss = nn.MSELoss()

    def update(self, memory):
        # Time Difference estimate of state rewards:
        rewards = []

        with torch.no_grad():
            logprobs, state_values, dist_entropy = \
                self.policy.evaluate(memory.states[-1], memory.candidates[-1], memory.actions[-1].to(self.device))
        discounted_reward = state_values

        for reward, is_terminal in zip(reversed(memory.rewards), reversed(memory.is_terminals)):
            reward = torch.tensor(reward, dtype=torch.float32).to(self.device)
            discounted_reward = discounted_reward * (1 - torch.tensor(is_terminal, dtype=torch.float32).to(self.device))
            discounted_reward = reward + (self.gamma * discounted_reward).clone()
            rewards.insert(0, discounted_reward)

        # Normalizing the rewards:
        rewards = torch.cat(rewards)
        rewards = (rewards - rewards.mean()) / (rewards.std() + 1e-5)

        # convert list to tensor
        old_states = []
        for state in memory.states:
            old_states += state
        old_actions = torch.cat(memory.actions, dim=0)
        old_logprobs = torch.cat(memory.logprobs, dim=0)
        old_candidates = []
        for candi in memory.candidates:
            old_candidates += candi

        critic_loss_sum = 0

        # Optimize policy for K epochs:
        for _ in range(self.K_epochs):
            # Evaluating old actions and values :
            logprobs, state_values, dist_entropy = self.policy.evaluate(old_states, old_candidates, old_actions)

            # Finding the ratio (pi_theta / pi_theta__old):
            ratios = torch.exp(logprobs - old_logprobs)

            # Normalizing advantages
            advantages = rewards - state_values.detach()
            #advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-5)

            # Finding Surrogate Loss:
            surr1 = ratios * advantages
            surr2 = torch.clamp(ratios, 1 - self.eps_clip, 1 + self.eps_clip) * advantages
            actor_loss = -torch.min(surr1, surr2)
            critic_loss = self.MseLoss(state_values, rewards)
            entropy_reg = -0.01 * dist_entropy
            critic_loss_sum += critic_loss.detach().mean()

            # take gradient step
            self.optimizer.zero_grad()
            (actor_loss + critic_loss + entropy_reg).mean().backward()
            self.optimizer.step()
        if self.lr_scheduler:
            self.lr_scheduler.step()

        # Copy new weights into old policy:
        self.policy_old.load_state_dict(self.policy.state_dict())

        return critic_loss_sum / self.K_epochs  # mean critic loss


def main(args):
    # initialize manual seed
    if args.random_seed is not None:
        random.seed(args.random_seed)
        np.random.seed(args.random_seed)
        torch.manual_seed(args.random_seed)

    # create DAG graph environment
    resource_dim = 1
    raw_node_feature_dim = 1 + resource_dim  # (duration, resources)
    args.node_feature_dim = raw_node_feature_dim
    dag_graph = DAGraph(resource_dim=resource_dim,
                        feature_dim=args.node_feature_dim,
                        scheduler_type=args.scheduler_type)

    # load training/testing data
    vargs = (
        dag_graph,
        args.num_init_dags,
        raw_node_feature_dim,
        resource_dim,
        args.resource_limit,
        args.add_graph_features,
        args.scheduler_type
    )
    tuples_train, tuples_test = \
        load_tpch_tuples(args.train_sample, 0, *vargs), load_tpch_tuples(args.test_sample, 1, *vargs)

    # create tensorboard summary writer
    try:
        import tensorflow as tf
        # local mode: logs stored in ./runs/TIME_STAMP-MACHINE_ID
        tfboard_path = 'runs'
        import socket
        from datetime import datetime
        current_time = datetime.now().strftime('%b%d_%H-%M-%S')
        tfboard_path = os.path.join(tfboard_path, current_time + '_' + socket.gethostname())
        summary_writer = TensorboardUtil(tf.summary.FileWriter(tfboard_path))
    except (ModuleNotFoundError, ImportError):
        print('Warning: Tensorboard not loading, please install tensorflow to enable...')
        summary_writer = None

    # get current device (cuda or cpu)
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    # init models
    memory = Memory()
    ppo = PPO(dag_graph, args, device)
    num_workers = cpu_count()
    mp_pool = Pool(num_workers)

    # logging variables
    best_test_ratio = 0
    running_reward = 0
    critic_loss = []
    avg_length = 0
    timestep = 0
    prev_time = time.time()

    # training loop
    for i_episode in range(1, args.max_episodes + 1):
        items_batch = ItemsContainer()
        for b in range(args.batch_size):
            graph_index = ((i_episode - 1) * args.batch_size + b) % len(tuples_train)
            inp_graph, _, _, baselines = tuples_train[graph_index]  # we treat inp_graph as the state
            makespan = 0
            node_candidates = dag_graph.get_node_candidates(inp_graph)
            items_batch.append(0, inp_graph, makespan, node_candidates, False)

        for t in range(args.max_timesteps):
            timestep += 1

            # Running policy_old:
            with torch.no_grad():
                action_batch = ppo.policy_old.act(items_batch.inp_graph, items_batch.node_candidates, memory)

            def step_func_feeder(batch_size):
                batch_inp_graph = items_batch.inp_graph
                action_batch_cpu = action_batch.cpu()
                batch_makespan = items_batch.makespan
                for b in range(batch_size):
                    yield batch_inp_graph[b], action_batch_cpu[b].item(), batch_makespan[b]

            if args.batch_size > 1:
                pool_map = mp_pool.starmap_async(dag_graph.step_e2e, step_func_feeder(args.batch_size))
                step_list = pool_map.get()
            else:
                step_list = [dag_graph.step_e2e(*x) for x in step_func_feeder(args.batch_size)]
            for b, item in enumerate(step_list):
                reward, inp_graph, makespan, node_candidates, done = item
                items_batch.update(b, reward=reward, inp_graph=inp_graph, makespan=makespan,
                                   node_candidates=node_candidates, done=done)

            # Saving reward and is_terminal:
            memory.rewards.append(items_batch.reward)
            memory.is_terminals.append(items_batch.done)

            # update if its time
            if timestep % args.update_timestep == 0:
                closs = ppo.update(memory)
                critic_loss.append(closs)
                if summary_writer:
                    summary_writer.add_scalar('critic mse/train', closs, timestep)
                memory.clear_memory()

            running_reward += sum(items_batch.reward) / args.batch_size
            if any(items_batch.done):
                break

        avg_length += t+1

        # logging
        if i_episode % args.log_interval == 0:
            avg_length = avg_length / args.log_interval
            running_reward = running_reward / args.log_interval
            if len(critic_loss) > 0:
                critic_loss = torch.mean(torch.stack(critic_loss))
            else:
                critic_loss = -1
            now_time = time.time()
            avg_time = (now_time - prev_time) / args.log_interval
            prev_time = now_time

            if summary_writer:
                summary_writer.add_scalar('reward/train', running_reward, timestep)
                summary_writer.add_scalar('time/train', avg_time, timestep)
                for lr_id, x in enumerate(ppo.optimizer.param_groups):
                    summary_writer.add_scalar(f'lr/{lr_id}', x['lr'], timestep)

            print(
                f'Episode {i_episode} \t '
                f'avg length: {avg_length:.2f} \t '
                f'critic mse: {critic_loss:.4f} \t '
                f'reward: {running_reward:.4f} \t '
                f'time per episode: {avg_time:.2f}'
            )

            running_reward = 0
            avg_length = 0
            critic_loss = []

        # testing
        if i_episode % args.test_interval == 0:
            with torch.no_grad():
                # record time spent on test
                prev_test_time = time.time()
                #print("########## Evaluate on Train ##########")
                #train_dict = evaluate(ppo.policy, dag_graph, tuples_train, args.max_timesteps, args.search_size, mp_pool)
                #for key, val in train_dict.items():
                #    if isinstance(val, dict):
                #        if summary_writer:
                #            summary_writer.add_scalars(f'{key}/train-eval', val, timestep)
                #    else:
                #        if summary_writer:
                #            summary_writer.add_scalar(f'{key}/train-eval', val, timestep)
                print("########## Evaluate on Test ##########")
                # run testing
                test_dict = evaluate(ppo.policy, dag_graph, tuples_test, args.max_timesteps, args.search_size) #, mp_pool)
                # write to summary writter
                for key, val in test_dict.items():
                    if isinstance(val, dict):
                        if summary_writer:
                            summary_writer.add_scalars(f'{key}/test', val, timestep)
                    else:
                        if summary_writer:
                            summary_writer.add_scalar(f'{key}/test', val, timestep)
                print("########## Evaluate complete ##########")
                # fix running time value
                prev_time += time.time() - prev_test_time

            if test_dict["ratio"]["mean"] > best_test_ratio:
                best_test_ratio = test_dict["ratio"]["mean"]
                file_name = f'./PPO_E2E_{args.scheduler_type}_dag_num{args.num_init_dags}' \
                            f'_beam{args.search_size}_ratio{best_test_ratio:.4f}.pt'
                torch.save(ppo.policy.state_dict(), file_name)


def parse_arguments():
    parser = argparse.ArgumentParser(description='DAG scheduler. You have two ways of setting the parameters: \n'
                                                 '1) set parameters by command line arguments \n'
                                                 '2) specify --config path/to/config.yaml')
    # environment configs
    parser.add_argument('--scheduler_type', default='sft')
    parser.add_argument('--resource_limit', default=600, type=float)
    parser.add_argument('--add_graph_features', action='store_true')
    parser.add_argument('--num_init_dags', default=5, type=int)
    # parser.add_argument('--max_selected_edges', default=3, type=int)
    parser.add_argument('--gamma', default=0.99, type=float, help='discount factor for accumulated reward')
    parser.add_argument('--train_sample', default=50, type=int, help='number of training samples')
    parser.add_argument('--test_sample', default=50, type=int, help='number of testing samples')

    # decode(testing) configs
    # parser.add_argument('--beam_search', action='store_true')
    parser.add_argument('--search_size', default=5, type=int)

    # learning configs
    parser.add_argument('--learning_rate', default=0.002, type=float)
    parser.add_argument('--lr_steps', default=[], type=list)
    parser.add_argument('--batch_size', default=1, type=int, help='Batch size when sampling')
    parser.add_argument('--betas', default=(0.9, 0.999), help='Adam optimizer\'s beta')
    parser.add_argument('--max_episodes', default=50000, type=int, help='max training episodes')
    parser.add_argument('--max_timesteps', default=300, type=int, help='max timesteps in one episode')
    parser.add_argument('--update_timestep', default=2000, type=int, help='update policy every n timesteps')
    parser.add_argument('--k_epochs', default=4, type=int, help='update policy for K epochs')
    parser.add_argument('--eps_clip', default=0.2, type=float, help='clip parameter for PPO')

    # model parameters
    parser.add_argument('--one_hot_degree', default=0, type=int)
    parser.add_argument('--batch_norm', action='store_true')
    parser.add_argument('--node_output_size', default=16, type=int)
    parser.add_argument('--gnn_layers', default=10, type=int, help='number of GNN layers')

    # misc configs
    parser.add_argument('--config', default=None, type=str, help='path to config file,'
                        ' and command line arguments will be overwritten by the config file')
    parser.add_argument('--random_seed', default=None, type=int)
    parser.add_argument('--test_interval', default=500, type=int, help='run testing in the interval (episodes)')
    parser.add_argument('--log_interval', default=100, type=int, help='print avg reward in the interval (episodes)')
    parser.add_argument('--test_model_weight', default='', type=str, help='the path of model weight to be loaded')

    args = parser.parse_args()

    if args.config:
        with open('config/' + args.config) as f:
            cfg_dict = yaml.load(f)
            for key, val in cfg_dict.items():
                assert hasattr(args, key), f'Unknown config key: {key}'
                setattr(args, key, val)
            f.seek(0)
            print(f'Config file: {args.config}', )
            for line in f.readlines():
                print(line.rstrip())

    print_args(args)

    return args


if __name__ == '__main__':
    main(parse_arguments())
