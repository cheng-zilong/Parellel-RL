
#%%
'''
layer_init + 4 step 1 gradient + async buffer
'''
import torch
import torch.nn as nn
from utils.Network import *
from utils.LogProcess import logger
from frameworks.Nature_DQN import Nature_DQN_Sync
from utils.ActorProcess import NetworkActorAsync
import matplotlib
import matplotlib.pyplot as plt
from matplotlib import gridspec
import os
import imageio
import numpy as np
import torch.multiprocessing as mp
from utils.ReplayBufferProcess import ReplayBufferProcess
class C51_DQN(Nature_DQN_Sync):
    def __init__(self, make_env_fun, network_fun, optimizer_fun, *args, **kwargs):
        super().__init__(make_env_fun, network_fun, optimizer_fun, *args, **kwargs)
        '''
        v_min
        v_max
        num_atoms
        '''
        if type(self) is C51_DQN:
            self.replay_buffer = ReplayBufferProcess(*args, **kwargs)
            self.replay_buffer.start()
            self.network_lock = mp.Lock()
            self.train_actor = C51_NetworkActorAsync(
                make_env_fun = make_env_fun, 
                replay_buffer=self.replay_buffer,
                network_lock=self.network_lock, 
                *args, **kwargs
            )
            self.train_actor.start()
            self.eval_actor = C51_NetworkActorAsync(
                make_env_fun = make_env_fun, 
                replay_buffer=None,
                network_lock=mp.Lock(), 
                *args, **kwargs
            )
            self.eval_actor.start()

            self.delta_z = float(self.kwargs['v_max'] - self.kwargs['v_min']) / (kwargs['num_atoms'] - 1)
            self.offset = torch.linspace(0, (kwargs['batch_size'] - 1) * kwargs['num_atoms'], kwargs['batch_size']).long().unsqueeze(1).expand(kwargs['batch_size'], kwargs['num_atoms']).cuda()
            self.torch_range = torch.arange(kwargs['batch_size']).long().cuda()

            self.current_network = network_fun(self.dummy_env.observation_space.shape, self.dummy_env.action_space.n, *args, **kwargs).cuda().share_memory()
            self.target_network  = network_fun(self.dummy_env.observation_space.shape, self.dummy_env.action_space.n, *args, **kwargs).cuda()
            self.optimizer = optimizer_fun(self.current_network.parameters())
            self.update_target()

    def compute_td_loss(self):
        state, action, reward, next_state, done = self.replay_buffer.sample()

        with torch.no_grad():
            prob_next = self.target_network(next_state)
            q_next = (prob_next * self.current_network.atoms_gpu).sum(-1)
            a_next = torch.argmax(q_next, dim=-1)
            prob_next = prob_next[self.torch_range, a_next, :]

        rewards = reward.unsqueeze(-1)
        atoms_target = rewards + self.kwargs['gamma'] * (~done).unsqueeze(-1) * self.current_network.atoms_gpu.view(1, -1)
        atoms_target.clamp_(self.kwargs['v_min'], self.kwargs['v_max']).unsqueeze_(1)
        
        target_prob = (1 - (atoms_target - self.current_network.atoms_gpu.view(1, -1, 1)).abs() / self.delta_z).clamp(0, 1) * prob_next.unsqueeze(1)
        target_prob = target_prob.sum(-1)

        log_prob = self.current_network.forward_log(state)
        log_prob = log_prob[self.torch_range, action, :]
        loss = (target_prob * target_prob.add(1e-5).log() - target_prob * log_prob).sum(-1).mean()

        self.optimizer.zero_grad()
        loss.backward()
        gradient_norm = nn.utils.clip_grad_norm_(self.current_network.parameters(), self.kwargs['clip_gradient'])
        logger.add({'gradient_norm': gradient_norm.item(), 'loss': loss.item()})
        with self.network_lock:
            self.optimizer.step()
        return loss

class C51_NetworkActorAsync(NetworkActorAsync):
    def _render(self, name, render_max_steps, render_mode, fps, is_show, figsize=(10, 5), dpi=160, *args, **kwargs):
        if not is_show: matplotlib.use('Agg')
        if not os.path.exists('save_video/' + logger._run_name + '/'):
            os.makedirs('save_video/' + logger._run_name + '/')
        writer = imageio.get_writer('save_video/' + logger._run_name + '/' + str(name) +'.mp4', fps = fps)
        my_fig = plt.figure(figsize=figsize, dpi=dpi)
        gs = gridspec.GridSpec(1, 2)
        ax_left, ax_right = my_fig.add_subplot(gs[0]), my_fig.add_subplot(gs[1])
        my_fig.tight_layout()
        fig_pixel_cols, fig_pixel_rows = my_fig.canvas.get_width_height()
        self._unwrapped_reset()
        for _ in range(1, render_max_steps + 1):
            action, _, _, done, info = self._sync_collect_helper(steps_number = 1, *args, **kwargs)[-1] 
            action_prob = np.swapaxes(self._network.action_prob[0].cpu().numpy(),0, 1)
            legends = []
            for i, action_meaning in enumerate(self.env.unwrapped.get_action_meanings()):
                legend_text = ' (Q=%+.2e)'%(self._network.action_Q[0,i]) if i == action else ' (Q=%+.2e)*'%(self._network.action_Q[0,i])
                legends.append(action_meaning + legend_text) 
            ax_left.clear()
            ax_left.imshow(self.env.render(mode = render_mode))
            ax_left.axis('off')
            ax_right.clear()
            ax_right.plot(self._network.atoms_cpu, action_prob)
            ax_right.legend(legends)
            ax_right.grid(True)
            my_fig.canvas.draw()
            buf = my_fig.canvas.tostring_rgb()
            writer.append_data(np.fromstring(buf, dtype=np.uint8).reshape(fig_pixel_rows, fig_pixel_cols, 3))
            if done:
                if info['episodic_return'] is not None: break
        writer.close()
# %%
