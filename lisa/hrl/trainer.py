import torch
import torch.nn as nn
from torch.utils.data import DataLoader
import torch.nn.functional as F
import time
import imageio
import numpy as np
from tqdm import tqdm
import os
import imageio
import wandb

from env import BaseWrapper, LorlWrapper, BabyAIWrapper, MetaWorldWrapper
from eval import eval_episode
from utils import pad, LORL_EVAL_INSTRS
from metaworld_utils import METAWORLD_EVAL_INSTRS, task_list
from viz import get_tokens, viz_matrix, plot_hist


class Trainer:

    def __init__(self, args, model, tokenizer, optimizer, train_loader, env=None, env_name=None, val_loader=None,
                 state_il=True, scheduler=None, eval_episode_factor=2, eval_every=5, num_eval_episodes=10, K=10,
                 skip_words=None, device='cuda'):
        self.args = args
        self.model = model
        self.tokenizer = tokenizer
        self.optimizer = optimizer
        self.train_loader = train_loader
        self.env = env
        self.env_name = env_name
        self.val_loader = val_loader
        self.state_il = state_il  # do Imitation learning on states too?
        self.scheduler = scheduler
        self.eval_episode_factor = eval_episode_factor
        self.eval_every = eval_every
        self.num_eval_episodes = num_eval_episodes
        self.device = device
        self.K = K  # DT sequence length
        self.skip_words = skip_words

        self.start_time = time.time()

    def train_iteration(self, iter_num, print_logs=False, eval_render=False):
        train_losses, action_losses, action_errors, state_losses, option_losses = [], [], [], [], []
        state_rc_losses, lang_rc_losses = [], []
        commitment_losses = []
        entropies, lang_entropies, mutual_info = [], [], []
        logs = dict()

        # if iter_num == 1:
        #     eval_start = time.time()
        #
        #     self.model.eval()
        #     eval_outputs = self.evaluate(iter_num, render=eval_render)
        #     for k, v in eval_outputs.items():
        #         logs[f'evaluation/{k}'] = v
        #     logs['time/evaluation'] = time.time() - eval_start

        train_start = time.time()

        model = self.model
        if hasattr(self.model, 'module'):
            model = self.model.module

        discrete = model.decision_transformer.discrete
        act_dim = model.decision_transformer.act_dim
        method = model.method
        horizon = model.horizon
        iq = model.decision_transformer.predict_q

        self.model.train()

        for langs, states, actions, timesteps, dones, attention_mask in tqdm(self.train_loader):
            lm_input = self.tokenizer(text=langs, add_special_tokens=True,
                                      return_tensors='pt', padding=True).to(self.device)

            if method == 'traj_option' or method == 'option':
                # Pad sequences to allows reshape
                K = self.K
                B, L = states.shape[0], states.shape[1]
                num_seq = (L // K + 1)
                padded_length = num_seq * K

                # This ensures the DT only looks at chunks of size horizon
                states = pad(states, padded_length)
                actions = pad(actions, padded_length)
                timesteps = pad(timesteps, padded_length)
                dones = pad(dones, padded_length)
                attention_mask = pad(attention_mask, padded_length)

            action_target = torch.clone(actions).detach()
            state_target = torch.clone(states).detach()

            if discrete:
                actions = F.one_hot(actions.long(), act_dim)

            states = states.float().to(self.device)
            actions = actions.float().to(self.device)
            timesteps = timesteps.long().to(self.device)
            dones = dones.long().to(self.device)
            attention_mask = attention_mask.long().to(self.device)
            if discrete:
                action_target = action_target.long().to(self.device)
            else:
                action_target = action_target.float().to(self.device)
            state_target = state_target.float().to(self.device)

            outputs = self.model(lm_input['input_ids'], lm_input['attention_mask'],
                                 states, actions, timesteps, attention_mask=attention_mask)

            state_preds, action_preds = outputs['dt']['state_preds'], outputs['dt']['action_preds']
            state_rc_preds, state_rc_targets = outputs['state_rc']
            lang_rc_preds, lang_rc_targets = outputs['lang_rc']
            commitment_loss = outputs['commitment_loss']
            if outputs['entropy'] is not None:
                entropy, lang_entropy = outputs['entropy'][0], outputs['entropy'][1]
            else:
                entropy = None
                lang_entropy = None


            if commitment_loss is None:
                commitment_loss = torch.zeros([])
            commitment_loss = commitment_loss.mean()

            if entropy is None:
                entropy = torch.zeros([])
                lang_entropy = torch.zeros([])
            entropy = entropy.mean()
            lang_entropy = lang_entropy.mean()

            act_dim = action_preds.shape[-1]
            B = action_preds.shape[0]
            if self.state_il:
                state_dim = state_preds.shape[-1]
            action_preds = action_preds.reshape(-1, act_dim)[attention_mask.reshape(-1) > 0]
            if discrete:
                action_target = action_target.reshape(-1)[attention_mask.reshape(-1) > 0]
            else:
                action_target = action_target.reshape(-1, act_dim)[attention_mask.reshape(-1) > 0]

            attention_mask = attention_mask.reshape(B, -1)
            # Pad extra zero to mask for target state prediction to ignore s_0
            target_attention_mask = torch.cat(
                [torch.zeros(B, 1).to(self.device), attention_mask[:, :-1]], axis=-1)
            # Pred states are from s1, ... s_T (ignoring s_T+1). We remove the last element of the mask to get correct shapes.
            pred_attention_mask = attention_mask[:, :-1]

            # We actually don't do this in the old DT code
            if self.state_il:
                state_preds = state_preds.reshape(-1,
                                                  state_dim)[pred_attention_mask.reshape(-1) > 0]
                state_target = state_target.reshape(-1,
                                                    state_dim)[target_attention_mask.reshape(-1) > 0]

            dones = dones.reshape(-1)[attention_mask.reshape(-1) > 0]
            if iq:
                q_preds = outputs['dt']['q_preds']
                # Get q_values of next states (as the final state is unknown, we use a dummy value of q=0 for state after done=True)
                # IQ should skip using the last q_val
                next_q = torch.cat((q_preds[:, 1:, :], torch.zeros(
                    [B, 1, act_dim]).to(self.device)), dim=1)
                q_preds = q_preds.reshape(-1, act_dim)[attention_mask.reshape(-1) > 0]
                # In this case action_preds come from the q_network
                action_preds = model.iq_choose_action(q_preds)
                if discrete:
                    action_preds = F.one_hot(action_preds.long(), act_dim)
            else:
                q_preds, next_q = None, None

            if self.state_il:
                act_loss, state_loss = imitation_loss(
                    action_preds, action_target, state_preds, state_target, q_preds, next_q, dones, discrete,
                    loss_fn=model.iq_critic_loss, step=iter_num)
            else:
                act_loss, state_loss = imitation_loss(
                    action_preds, action_target, None, None, q_preds, next_q, dones, discrete,
                    loss_fn=model.iq_critic_loss, step=iter_num)
            
            options_loss = outputs['dt']['options_loss'] if 'options_loss' in outputs['dt'] else None
            if options_loss is None:
                options_loss = torch.zeros([])
            else:
                options_loss = options_loss.mean()  ### because we get output from several GPUs
            
            state_rc_loss = state_reconstruction_loss(state_rc_preds, state_rc_targets)
            lang_rc_loss = lang_reconstruction_loss(lang_rc_preds, lang_rc_targets)

            loss = act_loss + state_loss + options_loss + state_rc_loss + lang_rc_loss + commitment_loss

            self.optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), .25)
            self.optimizer.step()

            with torch.no_grad():
                train_losses.append(loss.detach().cpu().item())
                if discrete:
                    action_errors.append(1 - torch.mean(
                        (torch.argmax(action_preds, dim=1) == action_target).float()).detach().cpu().item())
                action_losses.append(act_loss.detach().cpu().item())
                state_losses.append(state_loss.detach().cpu().item())
                option_losses.append(options_loss.detach().cpu().item())
                state_rc_losses.append(state_rc_loss.detach().cpu().item())
                lang_rc_losses.append(lang_rc_loss.detach().cpu().item())
                commitment_losses.append(commitment_loss.detach().cpu().item())
                entropies.append(entropy.detach().cpu().item())
                lang_entropies.append(lang_entropy.detach().cpu().item())
                mutual_info.append(entropies[-1] - lang_entropies[-1])

            if self.scheduler is not None:
                self.scheduler.step()

        logs['time/training'] = time.time() - train_start

        # self.eval_every = 20
        if iter_num % self.eval_every == 0:
            eval_start = time.time()

            self.model.eval()
            eval_outputs = self.evaluate(iter_num, render=eval_render)
            for k, v in eval_outputs.items():
                logs[f'evaluation/{k}'] = v
            logs['time/evaluation'] = time.time() - eval_start

        logs['time/total'] = time.time() - self.start_time
        logs['training/train_loss_mean'] = np.mean(train_losses)
        logs['training/train_loss_std'] = np.std(train_losses)
        if discrete:
            logs['training/action_error'] = np.mean(action_errors)
        logs['training/action_pred_loss'] = np.mean(action_losses)
        logs['training/state_pred_loss'] = np.mean(state_losses)
        logs['training/options_pred_loss'] = np.mean(option_losses)
        logs['training/state_rc_loss'] = np.mean(state_rc_losses)
        logs['training/lang_rc_loss'] = np.mean(lang_rc_losses)
        logs['training/commitment_loss'] = np.mean(commitment_losses)
        logs['training/entropy'] = np.mean(entropies)
        logs['training/lang_entropy'] = np.mean(lang_entropies)
        logs['training/MI'] = np.mean(entropies) - np.mean(lang_entropies)
        logs['training/lr'] = self.optimizer.param_groups[0]['lr']  # DT lr
        logs['training/lm_lr'] = self.optimizer.param_groups[1]['lr']  # Language model lr
        logs['training/os_lr'] = self.optimizer.param_groups[2]['lr']  # option selector lr

        if print_logs:
            print('=' * 80)
            print(f'Iteration {iter_num}')
            for k, v in logs.items():
                print(f'{k}: {v}')

        return logs

    def evaluate(self, iter_num, render=False, max_ep_len=500, render_path='', render_freq=1):
        model = self.model
        if hasattr(self.model, 'module'):
            model = self.model.module

        device = self.device
        method = model.method

        no_lang = self.train_loader.dataset.no_lang  # whether to use language or not

        words_dict = {}
        if method != 'vanilla':
            num_options = model.option_selector.num_options
            words_dict = {i: [] for i in range(num_options)}  # Create a list for each option


        if self.env and 'MetaWorld' in self.args.env.name:
            # TODO: For MetaWorld env
            # use_state = self.train_loader.dataset.kwargs['use_state']
            # setting this to be sufficiently large
            # max_ep_len = self.eval_episode_factor * self.train_loader.dataset.max_length
            # max_ep_len = int(self.train_loader.dataset.max_length * 5)
            max_ep_len = 50
            if render:
                if not os.path.isdir(render_path):
                    os.makedirs(render_path, exist_ok=True)

            returns, lengths, successes = [], [], []
            # instr_wise_stats = {k: [] for k in METAWORLD_EVAL_INSTRS.keys()}
            # rephrasal_wise_stats = {k: [] for k in [
            #     'seen', 'unseen verb', 'unseen noun', 'unseen verb noun', 'human']}
            task_wise_stats = {k: [] for k in task_list.keys()}

            for i in tqdm(range(1, self.num_eval_episodes + 1)):
                seed = (i + 123456789) * 10000 % 51511
                for task in task_list.keys():
                    # task = self.args.env.name[10:]
                    env = MetaWorldWrapper(self.env, self.train_loader.dataset, task = task, seed=seed)

                    with torch.no_grad():
                        episode_return, episode_length, success, options_list, lang, images, words_dict = eval_episode(
                            env, no_lang, self.tokenizer, model, max_ep_len, self.K, words_dict, render, device,
                            render_path=render_path, render_freq=render_freq, iter_num=iter_num, i=i)

                    if render and i % render_freq == 0:
                        r = f'{iter_num}_{i}'
                        imageio.mimsave(f'{render_path}/episode_{r}.gif', images)

                        with open(f'{render_path}/{r}.txt', 'w') as fp:
                            fp.write(lang)

                        print(options_list)
                        print(f'Success: {success}')
                        print(f'Return: {episode_return}')

                        with open(f'{render_path}/episode_{r}_options.txt', 'w') as fp:
                            fp.write(str(options_list))

                    returns.append(episode_return)
                    lengths.append(episode_length)
                    successes.append(success)
                    task_wise_stats[task].append(success)

            task_wise_stats = {k: np.mean(task_wise_stats[k]) for k in task_list.keys()}

            metrics = {
                f'return_mean': np.mean(returns),
                f'return_std': np.std(returns),
                f'length_mean': np.mean(lengths),
                f'length_std': np.std(lengths),
                f'success_rate': np.mean(successes),
                f'success_std': np.std(successes),
                f'task_wise': plot_hist(task_wise_stats),
            }

        elif self.env and 'BabyAI' in self.env.unwrapped.spec.id:
            # For BabyAI env
            # setting this to be sufficiently large
            max_ep_len = self.eval_episode_factor * self.train_loader.dataset.max_length

            returns, lengths, successes = [], [], []
            for i in tqdm(range(1, self.num_eval_episodes + 1)):
                env = BabyAIWrapper(self.env, self.train_loader.dataset)

                episode_return, episode_length, success, options_list, lang, images, words_dict = eval_episode(
                    env, no_lang, self.tokenizer, model, max_ep_len, self.K, words_dict, render, device,
                    render_path=render_path, render_freq=render_freq, iter_num=iter_num, i=i)

                if render and i % render_freq == 0:
                    r = f'{iter_num}_{i}'
                    if not os.path.isdir(render_path):
                        os.makedirs(render_path, exist_ok=True)
                    images[0].save(f'{render_path}/{r}.gif', save_all=True,
                                   append_images=images[1:], optimize=False, duration=40, loop=0)
                    with open(f'{render_path}/{r}.txt', 'w') as fp:
                        fp.write(lang)

                    print(options_list)
                    print(f'Return: {episode_return}')

                    with open(f'{render_path}/{r}_options.txt', 'w') as fp:
                        fp.write(str(options_list))

                returns.append(episode_return)
                lengths.append(episode_length)
                successes.append(success)

            metrics = {
                f'return_mean': np.mean(returns),
                f'return_std': np.std(returns),
                f'length_mean': np.mean(lengths),
                f'length_std': np.std(lengths),
                f'success_rate': np.mean(successes),
            }
        elif self.env and 'Lorl' in self.env.unwrapped.spec.id:
            # For Lorl env
            # Most of this code is from https://github.com/suraj-nair-1/lorel/blob/main/run_planning.py
            use_state = self.train_loader.dataset.kwargs['use_state']
            # setting this to be sufficiently large
            # max_ep_len = self.eval_episode_factor * self.train_loader.dataset.max_length
            max_ep_len = self.train_loader.dataset.max_length

            if render:
                if not os.path.isdir(render_path):
                    os.makedirs(render_path, exist_ok=True)

            lengths, dists, successes = [], [], []
            instr_wise_stats = {k: [] for k in LORL_EVAL_INSTRS.keys()}
            rephrasal_wise_stats = {k: [] for k in [
                'seen', 'unseen verb', 'unseen noun', 'unseen verb noun', 'human']}

            for i in tqdm(range(1, self.num_eval_episodes+1)):
                for orig_instr, rephrasals in LORL_EVAL_INSTRS.items():
                    for rephrasal_type, instr_list in rephrasals.items():
                        for instr in instr_list:
                            env = LorlWrapper(self.env, self.train_loader.dataset,
                                              instr=instr, orig_instr=orig_instr)

                            episode_return, episode_length, success, options_list, lang, images, words_dict = eval_episode(
                                env, no_lang, self.tokenizer, model, max_ep_len, self.K, words_dict, render, device, render_path=render_path,
                                render_freq=render_freq, iter_num=iter_num, i=i)

                            if render and i % render_freq == 0:
                                r = f'{iter_num}_{i}'
                                imageio.mimsave(f'{render_path}/episode_{r}_{instr}.gif', images)
                                print(options_list)
                                print(f'Success: {success}')

                                with open(f'{render_path}/episode_{r}_{instr}_options.txt', 'w') as fp:
                                    fp.write(str(options_list))

                            dists.append(episode_return)
                            lengths.append(episode_length)
                            successes.append(success)
                            instr_wise_stats[orig_instr].append(success)
                            rephrasal_wise_stats[rephrasal_type].append(success)

            instr_wise_stats = {k: np.mean(instr_wise_stats[k]) for k in instr_wise_stats.keys()}
            rephrasal_wise_stats = {k: np.mean(rephrasal_wise_stats[k]) for k in rephrasal_wise_stats.keys()}

            #instr_table = wandb.Table(data=instr_wise_stats, columns=["Instruction", "Success Rate"])
            #rephrasal_table = wandb.Table(data=rephrasal_wise_stats, columns=["Rephrasal type", "Success Rate"])

            metrics = {
                f'length_mean': np.mean(lengths),
                f'dist_mean': np.mean(dists),
                f'length_std': np.std(lengths),
                f'dist_std': np.std(dists),
                f'success_rate': np.mean(successes),
                f'instr_wise': plot_hist(instr_wise_stats),
                f'rephrasal_wise': plot_hist(rephrasal_wise_stats)}

        elif self.env and 'Hopper' in self.env.unwrapped.spec.id:
            # For env
            # setting this to be sufficiently large
            max_ep_len = self.eval_episode_factor * self.train_loader.dataset.max_length

            returns, lengths, successes = [], [], []
            for i in tqdm(range(1, self.num_eval_episodes + 1)):
                env = BaseWrapper(self.env, self.train_loader.dataset)

                episode_return, episode_length, success, options_list, lang, images, words_dict = eval_episode(
                    env, no_lang, self.tokenizer, model, max_ep_len, self.K, words_dict, render, device,
                    render_path=render_path, render_freq=render_freq, iter_num=iter_num, i=i)

                if render and i % render_freq == 0:
                    r = f'{iter_num}_{i}'
                    if not os.path.isdir(render_path):
                        os.makedirs(render_path, exist_ok=True)
                    images[0].save(f'{render_path}/{r}.gif', save_all=True,
                                   append_images=images[1:], optimize=False, duration=40, loop=0)
                    with open(f'{render_path}/{r}.txt', 'w') as fp:
                        fp.write(lang)

                    print(options_list)
                    print(f'Return: {episode_return}')

                    with open(f'{render_path}/{r}_options.txt', 'w') as fp:
                        fp.write(str(options_list))

                returns.append(episode_return)
                lengths.append(episode_length)
                successes.append(success)

            metrics = {
                f'return_mean': np.mean(returns),
                f'return_std': np.std(returns),
                f'length_mean': np.mean(lengths),
                f'length_std': np.std(lengths),
                f'success_rate': np.mean(successes),
            }
        else:
            discrete = model.decision_transformer.discrete
            train_losses, action_losses, action_errors, state_losses = [], [], [], []
            state_rc_losses, lang_rc_losses = [], []

            for langs, states, actions, timesteps, dones, attention_mask in tqdm(self.val_loader):
                lm_input = self.tokenizer(text=langs, add_special_tokens=True,
                                          return_tensors='pt', padding=True).to(self.device)

                if method == 'traj_option' or method == 'option':
                    # Pad sequences to allows reshape
                    K = self.K
                    B, L, _ = states.shape
                    num_seq = (L // K + 1)
                    padded_length = num_seq * K

                    # This ensures the DT only looks at chunks of size horizon
                    states = pad(states, padded_length)
                    actions = pad(actions, padded_length)
                    timesteps = pad(timesteps, padded_length)
                    attention_mask = pad(attention_mask, padded_length)

                action_target = torch.clone(actions).detach()
                state_target = torch.clone(states).detach()

                if discrete:
                    actions = F.one_hot(actions.long(), act_dim)

                states = states.float().to(self.device)
                actions = actions.float().to(self.device)
                timesteps = timesteps.long().to(self.device)
                attention_mask = attention_mask.long().to(self.device)
                dones = dones.long().to(self.device)
                action_target = action_target.long().to(self.device)
                state_target = state_target.float().to(self.device)

                outputs = self.model(lm_input['input_ids'], lm_input['attention_mask'],
                                     states, actions, timesteps, attention_mask=attention_mask)

                state_preds, action_preds = outputs['dt']
                state_rc_preds, state_rc_targets = outputs['state_rc']
                lang_rc_preds, lang_rc_targets = outputs['lang_rc']

                act_dim = action_preds.shape[-1]
                state_dim = state_preds.shape[-1]
                action_preds = action_preds.reshape(-1, act_dim)[attention_mask.reshape(-1) > 0]
                action_target = action_target.reshape(-1)[attention_mask.reshape(-1) > 0]

                attention_mask = attention_mask.reshape(state_preds.shape[0], -1)
                B, _ = attention_mask.shape
                # Pad extra zero to mask for target state prediction to ignore s_0
                target_attention_mask = torch.cat(
                    [torch.zeros(B, 1).to(self.device), attention_mask[:, :-1]], axis=-1)
                # Pred states are from s1, ... s_T (ignoring s_T+1). We remove the last element of the mask to get correct shapes.
                pred_attention_mask = attention_mask[:, :-1]

                # We actually don't do this in the old DT code
                state_preds = state_preds.reshape(-1,
                                                  state_dim)[pred_attention_mask.reshape(-1) > 0]
                state_target = state_target.reshape(-1,
                                                    state_dim)[target_attention_mask.reshape(-1) > 0]

                dones = dones.reshape(-1)[attention_mask.reshape(-1) > 0]
                if self.state_il:
                    act_loss, state_loss = imitation_loss(
                        action_preds, action_target, state_preds, state_target, discrete,
                        loss_fn=model.iq_critic_loss, step=iter_num)
                else:
                    act_loss, state_loss = imitation_loss(
                        action_preds, action_target, None, None, discrete, loss_fn=model.iq_critic_loss, step=iter_num)

                state_rc_loss = state_reconstruction_loss(state_rc_preds, state_rc_targets)
                lang_rc_loss = lang_reconstruction_loss(lang_rc_preds, lang_rc_targets)
                loss = act_loss + state_loss + state_rc_loss + lang_rc_loss

                with torch.no_grad():
                    train_losses.append(loss.detach().cpu().item())
                    if discrete:
                        action_errors.append(1 - torch.mean(
                            (torch.argmax(action_preds, dim=1) == action_target).float()).detach().cpu().item())
                    action_losses.append(act_loss.detach().cpu().item())
                    state_losses.append(state_loss.detach().cpu().item())
                    state_rc_losses.append(state_rc_loss.detach().cpu().item())
                    lang_rc_losses.append(lang_rc_loss.detach().cpu().item())

            metrics = {
                'eval_loss_mean': np.mean(train_losses),
                'eval_loss_std': np.std(train_losses),
                'action_pred_loss': np.mean(action_losses),
                'state_pred_loss': np.mean(state_losses),
                'state_rc_loss': np.mean(state_rc_losses),
                'lang_rc_loss': np.mean(lang_rc_losses)
            }
            if discrete:
                metrics['action_error'] = np.mean(action_errors)

        if method != 'vanilla' and model.option_selector.use_vq:
            viz_matrix(words_dict, num_options, iter_num, self.skip_words)

        return metrics

    def save(self, iter_num, filepath, config):
        if hasattr(self.model, 'module'):
            model = self.model.module
        else:
            model = self.model

        torch.save({'model': model.state_dict(),
                    'optimizer': self.optimizer.state_dict(),
                    'scheduler': self.scheduler.state_dict(),
                    'iter_num': iter_num,
                    'train_dataset_max_length': self.train_loader.dataset.max_length,
                    'config': config}, filepath)

    def load(self, filepath):
        checkpoint = torch.load(filepath)
        self.model.load_state_dict(checkpoint['model'])
        if self.optimizer:
            self.optimizer.load_state_dict(checkpoint['optimizer'])
        if self.scheduler:
            self.scheduler.load_state_dict(checkpoint['scheduler'])
        return {'iter_num': checkpoint['iter_num'], 'train_dataset_max_length': checkpoint['train_dataset_max_length'], 'config': checkpoint['config']}


def state_reconstruction_loss(state_preds, state_targets):
    if state_preds and state_targets:
        return F.mse_loss(state_preds, state_targets)
    else:
        return torch.zeros([])


def lang_reconstruction_loss(lang_preds, lang_targets):
    if lang_preds and lang_targets:
        return F.mse_loss(lang_preds, lang_targets)
    else:
        return torch.zeros([])


def imitation_loss(
        action_preds, action_targets, state_preds=None, state_targets=None, q_preds=None, next_q=None, dones=None,
        discrete=True, loss_fn=None, step=0):
    if q_preds is not None and loss_fn is not None:
        act_loss = loss_fn((q_preds, next_q, action_targets, dones), step)
    else:
        if discrete:
            act_loss = F.cross_entropy(action_preds, action_targets)
        else:
            act_loss = F.mse_loss(action_preds, action_targets)
    if state_preds is not None and state_targets is not None:
        state_loss = F.mse_loss(state_preds, state_targets)
    else:
        state_loss = torch.zeros([])
    return act_loss, state_loss
