# train.py
# Script to train policies in Isaac Gym
#
# Copyright (c) 2018-2023, NVIDIA Corporation
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import hydra
import yaml

from omegaconf import DictConfig, OmegaConf


def preprocess_train_config(cfg, config_dict):
    """
    Adding common configuration parameters to the rl_games train config.
    An alternative to this is inferring them in task-specific .yaml files, but that requires repeating the same
    variable interpolations in each config.
    """

    train_cfg = config_dict['params']['config']

    train_cfg['device'] = cfg.rl_device

    train_cfg['population_based_training'] = cfg.pbt.enabled
    train_cfg['pbt_idx'] = cfg.pbt.policy_idx if cfg.pbt.enabled else None

    train_cfg['full_experiment_name'] = cfg.get('full_experiment_name')

    print(f'Using rl_device: {cfg.rl_device}')
    print(f'Using sim_device: {cfg.sim_device}')
    print(train_cfg)

    try:
        model_size_multiplier = config_dict['params']['network']['mlp']['model_size_multiplier']
        if model_size_multiplier != 1:
            units = config_dict['params']['network']['mlp']['units']
            for i, u in enumerate(units):
                units[i] = u * model_size_multiplier
            print(f'Modified MLP units by x{model_size_multiplier} to {config_dict["params"]["network"]["mlp"]["units"]}')
    except KeyError:
        pass

    return config_dict


@hydra.main(version_base="1.1", config_name="config", config_path="./cfg")
def launch_rlg_hydra(cfg: DictConfig):

    import logging
    import os
    from datetime import datetime

    # noinspection PyUnresolvedReferences
    import isaacgym
    from isaacgymenvs.pbt.pbt import PbtAlgoObserver, initial_pbt_check
    from isaacgymenvs.utils.rlgames_utils import multi_gpu_get_rank
    from hydra.utils import to_absolute_path
    from isaacgymenvs.tasks import isaacgym_task_map
    import gym
    from isaacgymenvs.utils.reformat import omegaconf_to_dict, print_dict
    from isaacgymenvs.utils.utils import set_np_formatting, set_seed

    if cfg.pbt.enabled:
        initial_pbt_check(cfg)

    from isaacgymenvs.utils.rlgames_utils import RLGPUEnv, RLGPUAlgoObserver, MultiObserver, ComplexObsRLGPUEnv
    from isaacgymenvs.utils.wandb_utils import WandbAlgoObserver
    from rl_games_twk.common import env_configurations, vecenv
    from rl_games_twk.torch_runner import Runner
    from rl_games_twk.algos_torch import model_builder
    from isaacgymenvs.learning import amp_continuous
    from isaacgymenvs.learning import amp_players
    from isaacgymenvs.learning import amp_models
    from isaacgymenvs.learning import amp_network_builder
    import isaacgymenvs

    # for multi-agent RL
    from isaacgymenvs.utils.marl_utils import MultiAgentRLGPUEnv

    time_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_name = f"{cfg.wandb_name}_{time_str}"

    if hasattr(cfg.task.env, 'multiAgent'):
        # Testing on CPU due to a reset error during random object respawn
        cfg.pipeline = 'cpu'    # bugs found in gpu pipeline

    # ensure checkpoints can be specified as relative paths
    if cfg.checkpoint:
        cfg.checkpoint = to_absolute_path(cfg.checkpoint)

    cfg_dict = omegaconf_to_dict(cfg)
    print_dict(cfg_dict)

    # set numpy formatting for printing only
    set_np_formatting()

    # global rank of the GPU
    global_rank = int(os.getenv("RANK", "0"))

    # sets seed. if seed is -1 will pick a random one
    cfg.seed = set_seed(cfg.seed, torch_deterministic=cfg.torch_deterministic, rank=global_rank)

    def create_isaacgym_env(**kwargs):
        envs = isaacgymenvs.make(
            cfg.seed, 
            cfg.task_name, 
            cfg.task.env.numEnvs, 
            cfg.sim_device,
            cfg.rl_device,
            cfg.graphics_device_id,
            cfg.headless,
            cfg.multi_gpu,
            cfg.capture_video,
            cfg.force_render,
            cfg,
            **kwargs,
        )
        if cfg.capture_video:
            envs.is_vector_env = True
            envs = gym.wrappers.RecordVideo(
                envs,
                f"videos/{run_name}",
                step_trigger=lambda step: step % cfg.capture_video_freq == 0,
                video_length=cfg.capture_video_len,
            )
        return envs

    env_configurations.register('rlgpu', {
        'vecenv_type': 'MARLGPU' if cfg.train.params.algo.name == "a2c_multi_agent" else "RLGPU",
        'env_creator': lambda **kwargs: create_isaacgym_env(**kwargs),
    })

    ige_env_cls = isaacgym_task_map[cfg.task_name]
    dict_cls = ige_env_cls.dict_obs_cls if hasattr(ige_env_cls, 'dict_obs_cls') and ige_env_cls.dict_obs_cls else False

    if dict_cls:
        
        obs_spec = {}
        actor_net_cfg = cfg.train.params.network
        obs_spec['obs'] = {'names': list(actor_net_cfg.inputs.keys()), 'concat': not actor_net_cfg.name == "complex_net", 'space_name': 'observation_space'}
        if "central_value_config" in cfg.train.params.config:
            critic_net_cfg = cfg.train.params.config.central_value_config.network
            obs_spec['states'] = {'names': list(critic_net_cfg.inputs.keys()), 'concat': not critic_net_cfg.name == "complex_net", 'space_name': 'state_space'}
        
        vecenv.register('RLGPU', lambda config_name, num_actors, **kwargs: ComplexObsRLGPUEnv(config_name, num_actors, obs_spec, **kwargs))
    else:
        vecenv.register('RLGPU', lambda config_name, num_actors, **kwargs: RLGPUEnv(config_name, num_actors, **kwargs))
        vecenv.register('MARLGPU', lambda config_name, num_actors, **kwargs: MultiAgentRLGPUEnv(config_name, num_actors, **kwargs))

    rlg_config_dict = omegaconf_to_dict(cfg.train)
    rlg_config_dict = preprocess_train_config(cfg, rlg_config_dict)
    if hasattr(cfg.task.env, 'multiAgent'):
        cfg.task.env.multiAgent.isMultiAgent = True if cfg.train.params.algo.name == "a2c_multi_agent" else False

    observers = [RLGPUAlgoObserver()]

    if cfg.pbt.enabled:
        pbt_observer = PbtAlgoObserver(cfg)
        observers.append(pbt_observer)

    if cfg.wandb_activate:
        cfg.seed += global_rank
        if global_rank == 0:
            # initialize wandb only once per multi-gpu run
            wandb_observer = WandbAlgoObserver(cfg)
            observers.append(wandb_observer)

    # register new AMP network builder and agent
    def build_runner(algo_observer):
        runner = Runner(algo_observer)
        runner.algo_factory.register_builder('amp_continuous', lambda **kwargs : amp_continuous.AMPAgent(**kwargs))
        runner.player_factory.register_builder('amp_continuous', lambda **kwargs : amp_players.AMPPlayerContinuous(**kwargs))
        model_builder.register_model('continuous_amp', lambda network, **kwargs : amp_models.ModelAMPContinuous(network))
        model_builder.register_network('amp', lambda **kwargs : amp_network_builder.AMPBuilder())

        return runner

    # convert CLI arguments into dictionary
    # create runner and set the settings
    runner = build_runner(MultiObserver(observers))
    runner.load(rlg_config_dict)
    runner.reset()

    # dump config dict
    cfg.test = True
    if not cfg.test:
        experiment_dir = os.path.join('runs', cfg.train.params.config.name +
                                      '_{date:%Y-%m-%d_%H-%M-%S}'.format(date=datetime.now()))
        os.makedirs(experiment_dir, exist_ok=True)
        with open(os.path.join(experiment_dir, 'config.yaml'), 'w') as f:
            f.write(OmegaConf.to_yaml(cfg))

    import re

    def find_latest_last_element(path, best=False):
        def extract_project_name(_path):
            # Extract the path between 'runs' and 'nn'.
            project_name_with_numbers = re.search(r'runs/(.*?)/nn', _path)
            if project_name_with_numbers:
                project_name_with_numbers = project_name_with_numbers.group(1)

                # Remove numbers and anything following them that start with '_' from the project name.
                # project_name = re.sub(r'_(\d+).*$', '', project_name_with_numbers)
                project_name = project_name_with_numbers
                return project_name + '.pth'
            return None

        if best:
            return extract_project_name(path)

        # Filter files starting with 'last' in the given directory
        last_files = [file for file in os.listdir(path) if file.startswith('last')]

        # Extract the highest episode number using a lambda function
        def extract_episode_number(filename):
            match = re.search(r'ep_(\d+)', filename)
            return int(match.group(1)) if match else -1

        # Find the file with the highest episode number or return None if no matches
        return max(last_files, key=extract_episode_number, default=None)

    # Test Config
    """
    * Experiment model tags
        * Single-Agent:
            'SA', 'SA_pbt',
        * Multi-Agent fixed alpha:
            'MA_fix_1.0', 'MA_fix_0.9', 'MA_fix_0.8', 'MA_fix_0.7', 'MA_fix_0.6', 'MA_fix_0.5', 
        * Multi-Agent fixed alpha with only big objects(gymball, board)
            'MA_only_big_fix_0.7', 
        * Multi-Agent decay alpha
            'MA_decay_0.7', 'MA_decay_0.5',
        * Hetero-Agent fixed alpha 
            'HA_fix_0.9', 'HA_fix_0.8', 'HA_fix_0.7', 'HA_fix_0.6', 'HA_fix_0.5'
    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    path_to_maps = os.path.join(current_dir, 'evaluation', 'experimentMapAll.yaml')
    with open(path_to_maps, 'r') as file:
        exp_model_dict = yaml.safe_load(file)['map']
    target_tag = 'HA_fix_0.7'

    # folder = 'SA_AllegroKukaPPO_2024-09-19_15-38-32'
    folder = exp_model_dict[target_tag]
    path = os.path.dirname(os.path.abspath(__file__)) + '/runs/' + folder + '/nn/'
    cfg.checkpoint = path + find_latest_last_element(path=path, best=True)
    cfg.task.env.numEnvs = 64
    cfg.headless = False

    # Uniform Test mode setup
    cfg.task.env.uniformTest = True

    # Tensor board
    print_log = True
    if print_log:
        # http://localhost:6006

        from tensorboard import program
        log_path = os.path.dirname(os.path.abspath(__file__)) + '/runs/' + folder + '/summaries/'

        tb = program.TensorBoard()
        tb.configure(argv=[None, '--logdir', log_path])

        url = tb.launch()
        print(f"TensorBoard is running at {url}")

    runner.run({
        'train': not cfg.test,
        'play': cfg.test,
        'checkpoint': cfg.checkpoint,
        'sigma': cfg.sigma if cfg.sigma != '' else None
    })


if __name__ == "__main__":
    launch_rlg_hydra()
