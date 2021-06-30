from baselines.common.atari_wrappers import EpisodicLifeEnv
import torch
import numpy as np
import torch.multiprocessing as mp
import wandb
import json
from statistics import mean
from LogAsync import logger
import time
import matplotlib
import matplotlib.pyplot as plt
from matplotlib import gridspec
import matplotlib.animation as animation
from collections import deque
import os
from datetime import datetime
from PIL import Image
import io
import imageio

class EvaluationAsync(mp.Process):
    EVAL = 0
    NETWORK = 1
    EXIT = 3

    def __init__(self, env, **args):
        mp.Process.__init__(self)
        self.env = env
        self. args = args
        self.__pipe, self.__worker_pipe = mp.Pipe()

        self.eval_lock = mp.Lock()
        self.start()

    def _eval(self, ep_idx):
        #python test_async_actor.py --mode eval --model_path "save_model/CatDQN(BreakoutNoFrameskip-v4)_4_20210621-024725.pt" --seed 6  

        state = self.env.reset()
        tic   = time.time()

        for eval_steps_idx in range(1, self.eval_steps + 1):
            action = self.evaluator_network.act(state)
            state, _, done, info = self.env.step(action)
            if done:
                state = self.env.reset()
                if info['episodic_return'] is not None:
                    self.ep_return = info['episodic_return']
                    self.ep_steps = eval_steps_idx
                    break

            if ep_idx is None or ep_idx in self.args['eval_render_save_gif']:
                if eval_steps_idx % self.eval_render_freq == 1 : # every eval_render_freq frames sample 1 frame
                    self._render_frame(state)

        toc = time.time()
        fps = self.ep_steps / (toc-tic)
        self.ep_reward_list.append(self.ep_return)
        ep_reward_list_mean = mean(self.ep_reward_list)
        logger.terminal_print('\t (Evaluating Agent: %d)'%(self.train_steps), {'\t ep': ep_idx, '\t ep_steps':  self.ep_steps, '\t ep_reward': self.ep_return, '\t ep_reward_mean': ep_reward_list_mean, '\t fps': fps})

    def _fig2img(self):
        """Convert a Matplotlib figure to a PIL Image and return it"""
        buf = io.BytesIO()
        self.my_fig.savefig(buf, bbox_inches='tight')
        buf.seek(0)
        img = Image.open(buf)
        return img

    def _render_frame(self, state):
        action_prob = np.swapaxes(self.evaluator_network.action_prob[0].cpu().numpy(),0, 1)
        legends = []
        for i, action_meaning in enumerate(self.env.unwrapped.get_action_meanings()):
            legends.append(action_meaning + ' (Q=%+.2e)'%(self.evaluator_network.action_Q[0,i]))

        self.ax_left.clear()
        self.ax_left.imshow(state[-1])
        self.ax_left.axis('off')

        self.ax_right.clear()
        self.ax_right.plot(self.atoms_cpu, action_prob)
        self.ax_right.legend(legends, fontsize='xx-small')
        self.ax_right.grid(True)
        
        self.im_list.append(self._fig2img())

    def run(self):
        self.eval_steps = self.args['eval_steps']
        self.eval_number = self.args['eval_number']
        self.eval_render_freq = self.args['eval_render_freq']
        self.eval_result = dict()
        self.current_max_result = -1e10
        if not self.args['eval_display']: matplotlib.use('Agg')
        self.my_fig = plt.figure(figsize=(8, 4))
        gs = gridspec.GridSpec(1, 2)
        # gs_left = gridspec.GridSpecFromSubplotSpec(2, 2, subplot_spec=gs[0])
        self.ax_left = self.my_fig.add_subplot(gs[0])
        self.ax_right = self.my_fig.add_subplot(gs[1])
        self.atoms_cpu = torch.linspace(self.args['v_min'], self.args['v_max'], self.args['num_atoms'])
        
        while True:
            cmd, data = self.__worker_pipe.recv()
            if cmd == self.EVAL:
                with self.eval_lock:
                    self.ep_reward_list = deque(maxlen=self.eval_number)
                    self.train_steps = data
                    for ep_idx in range(1, self.eval_number+1):
                        self.im_list = []
                        self._eval(ep_idx)
                        if ep_idx is None or ep_idx in self.args['eval_render_save_gif']:
                            imageio.mimsave(self.folder_name + '%08d_%03d.gif'%(self.train_steps, ep_idx), self.im_list, duration = 0.2)

                    ep_reward_list_mean = mean(self.ep_reward_list)
                    logger.add({'eval_last': ep_reward_list_mean})
                    if ep_reward_list_mean >= self.current_max_result:
                        torch.save(self.evaluator_network.state_dict(), 'save_model/' + self.evaluator_name + '.pt')
                        self.current_max_result = ep_reward_list_mean
                        logger.add({'eval_best': self.current_max_result})

            elif cmd == self.EXIT:
                self.__worker_pipe.close()
                return 

            elif cmd == self.NETWORK:
                self.evaluator_network = data
                now = datetime.now()
                self.evaluator_name = self.evaluator_network.__class__.__name__ + '(' + self.env.unwrapped.spec.id + ')_%d_'%self.args['seed'] + now.strftime("%Y%m%d-%H%M%S")
                self.folder_name = 'frames/' + self.evaluator_name + '/'
                if not os.path.exists(self.folder_name):
                    os.makedirs(self.folder_name)
                if not os.path.exists('save_model'):
                    os.makedirs('save_model')

            else:
                raise NotImplementedError

    def init(self, netowrk_fun):
        self.evaluator_network  = netowrk_fun(self.env.observation_space.shape, self.env.action_space.n, **self.args).cuda().share_memory()
        self.__pipe.send([self.NETWORK, self.evaluator_network]) # pass network to the evaluation process

    def eval(self, train_steps = 0, state_dict = None):
        with self.eval_lock:
            if self.args['mode'] == 'eval': # if this is only an evaluation session, then load model first
                model_path = self.args['model_path']
                if model_path is None: raise Exception("Model Path for Evaluation is not given! Include --model_path! ")
                self.evaluator_network.load_state_dict(torch.load(model_path))
            else:
                self.evaluator_network.load_state_dict(state_dict)
            self.__pipe.send([self.EVAL, train_steps])

    def exit(self):
        self.__pipe.send([self.EXIT, None])
        self.__pipe.close()

