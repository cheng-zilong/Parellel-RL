
#%%
'''
layer_init + 4 step 1 gradient + async buffer
'''
import torch
import torch.nn as nn
from utils.Network import *
from collections import deque
import time
from statistics import mean
from utils.ReplayBufferAsync import ReplayBufferAsync
from utils.LogAsync import logger
import torch.multiprocessing as mp
from utils.ActorAsync import NetworkActorAsync
from copy import deepcopy
import random
import numpy as np

class Nature_DQN:
    def __init__(self, make_env_fun, network_fun, optimizer_fun, *arg, **args):
        '''
        seed
        gamma
        clip_gradient
        eps_start
        eps_end
        eps_decay_steps
        start_training_steps
        update_target_freq
        eval_freq
        '''
        self.arg = arg 
        self.args = args 
        self._init_seed()

        self.env = make_env_fun(**args)
        self.network_lock = mp.Lock()
        self.train_actor = NetworkActorAsync(env = self.env, network_lock=self.network_lock, *arg, **args)
        self.train_actor.start()
        self.eval_actor = NetworkActorAsync(env = self.env, network_lock=mp.Lock(), *arg, **args)
        self.eval_actor.start()
        self.replay_buffer = ReplayBufferAsync(*arg, **args)
        self.replay_buffer.start()

        self.current_network = network_fun(self.env.observation_space.shape, self.env.action_space.n, **args).cuda().share_memory() 
        self.target_network  = network_fun(self.env.observation_space.shape, self.env.action_space.n, **args).cuda() 
        self.optimizer = optimizer_fun(self.current_network.parameters()) 
        self.update_target()

        
    def _init_seed(self):
        torch.manual_seed(self.args['seed'])
        torch.cuda.manual_seed(self.args['seed'])
        random.seed(self.args['seed'])
        np.random.seed(self.args['seed'])

    def update_target(self):
        self.target_network.load_state_dict(self.current_network.state_dict())
    
    def line_schedule(self, steps_idx):
        eps = self.args['eps_end'] + (self.args['eps_start'] - self.args['eps_end']) * (1 - min(steps_idx,self.args['eps_decay_steps']) / self.args['eps_decay_steps'])
        return eps
    
    def train(self):
        last_train_steps_idx, ep_idx = 1, 1
        ep_reward_list = deque(maxlen=self.args['ep_reward_avg_number'])
        loss  = torch.tensor(0)
        tic   = time.time()
        self.train_actor.update_policy(network = self.current_network)
        for train_steps_idx in range(1, self.args['train_steps'] + 1, self.args['train_freq']):
            eps = self.line_schedule(train_steps_idx-self.args['start_training_steps']) if train_steps_idx > self.args['start_training_steps'] else 1
            data = self.train_actor.collect(steps_number = self.args['train_freq'], eps=eps)
            for frames_idx, (action, obs, reward, done, info) in enumerate(data):
                self.replay_buffer.add(action, obs, reward, done)
                if info is not None and info['episodic_return'] is not None:
                    episodic_steps = train_steps_idx + frames_idx - last_train_steps_idx
                    ep_reward_list.append(info['episodic_return'])
                    toc = time.time()
                    fps = episodic_steps / (toc-tic)
                    tic = time.time()
                    logger.add({'train_steps':train_steps_idx ,'ep': ep_idx, 'ep_steps': episodic_steps, 'ep_reward': info['episodic_return'], 'ep_reward_avg': mean(ep_reward_list), 'loss': loss.item(), 'eps': eps, 'fps': fps})
                    logger.wandb_print('(Training Agent) ', step=train_steps_idx) if train_steps_idx > self.args['start_training_steps'] else logger.wandb_print('(Collecting Data) ', step=train_steps_idx)
                    ep_idx += 1
                    last_train_steps_idx = train_steps_idx + frames_idx

            if train_steps_idx > self.args['start_training_steps']:
                loss = self.compute_td_loss()

            if (train_steps_idx-1) % self.args['update_target_freq'] == 0:
                self.update_target()

            if (train_steps_idx-1) % self.args['eval_freq'] == 0:
                self.eval_actor.update_policy(network = deepcopy(self.current_network).share_memory())
                self.eval_actor.eval(eval_idx = train_steps_idx, eval_number = self.args['eval_number'], eval_max_steps = self.args['eval_max_steps'], eps = self.args['eval_eps'])
                self.eval_actor.save_policy(name = train_steps_idx)
                self.eval_actor.render(name=train_steps_idx, render_max_steps=self.args['eval_max_steps'], render_mode='rgb_array',fps=self.args['eval_video_fps'], is_show=self.args['eval_display'], eps = self.args['eval_eps'])

    def compute_td_loss(self):
        state, action, reward, next_state, done = self.replay_buffer.sample()

        with torch.no_grad():
            q_next = self.target_network(next_state).max(1)[0]
            q_target = reward + self.args['gamma'] * (~done) * q_next 
        
        q = self.current_network(state).gather(1, action.unsqueeze(-1)).squeeze(-1)
        loss = nn.MSELoss()(q_target, q)
        self.optimizer.zero_grad()
        loss.backward()
        nn.utils.clip_grad_norm_(self.current_network.parameters(), self.args['clip_gradient'])
        gradient_norm = nn.utils.clip_grad_norm_(self.current_network.parameters(), self.args['clip_gradient'])
        logger.add({'gradient_norm': gradient_norm.item()})
        with self.network_lock:
            self.optimizer.step()
        return loss

# %%
