# coding=utf-8
# Copyright 2018 The Tensor2Tensor Authors.
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

"""Utilities for player.py."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy
import os

import gym
import numpy as np
import six

from tensor2tensor.data_generators.gym_env import T2TGymEnv
from tensor2tensor.models.research.rl import get_policy
from tensor2tensor.models.research.rl import make_simulated_env_fn_from_hparams
from tensor2tensor.rl import rl_utils
from tensor2tensor.rl.envs.simulated_batch_gym_env import FlatBatchEnv
from tensor2tensor.utils import trainer_lib
import tensorflow as tf


flags = tf.flags
FLAGS = flags.FLAGS


def make_simulated_gym_env(real_env, world_model_dir, hparams, random_starts):
  """Gym environment with world model."""
  initial_frame_chooser = rl_utils.make_initial_frame_chooser(
      real_env, hparams.frame_stack_size,
      simulation_random_starts=random_starts,
      simulation_flip_first_random_for_beginning=False
  )
  env_fn = make_simulated_env_fn_from_hparams(
      real_env, hparams,
      batch_size=1,
      initial_frame_chooser=initial_frame_chooser,
      model_dir=world_model_dir
  )
  env = env_fn(in_graph=False)
  flat_env = FlatBatchEnv(env)
  return flat_env


def load_data_and_make_simulated_env(
    data_dir, wm_dir, hparams, which_epoch_data="last", random_starts=True
):
  hparams = copy.deepcopy(hparams)
  t2t_env = T2TGymEnv.setup_and_load_epoch(
      hparams, data_dir=data_dir,
      which_epoch_data=which_epoch_data)
  return make_simulated_gym_env(
      t2t_env, world_model_dir=wm_dir,
      hparams=hparams, random_starts=random_starts)


class ExtendToEvenDimentions(gym.ObservationWrapper):
  """Force even dimentions of both height and width by adding zeros."""
  HW_AXES = (0, 1)

  def __init__(self, env):
    gym.ObservationWrapper.__init__(self, env)

    orig_shape = env.observation_space.shape
    extended_shape = list(orig_shape)
    for axis in self.HW_AXES:
      if self.if_odd(orig_shape[axis]):
        extended_shape[axis] += 1

    assert env.observation_space.dtype == np.uint8
    self.observation_space = gym.spaces.Box(
        low=0,
        high=255,
        shape=extended_shape,
        dtype=np.uint8)

  def observation(self, frame):
    """Add single zero row/column to observation if needed."""
    if frame.shape == self.observation_space.shape:
      return frame
    else:
      extended_frame = np.zeros(self.observation_space.shape,
                                self.observation_space.dtype)
      assert self.HW_AXES == (0, 1)
      extended_frame[:frame.shape[0], :frame.shape[1]] = frame
      return extended_frame

  def if_odd(self, n):
    return n % 2


class RenderObservations(gym.Wrapper):
  """Add observations rendering in 'rgb_array' mode."""

  def __init__(self, env):
    super(RenderObservations, self).__init__(env)
    if "rgb_array" not in self.metadata["render.modes"]:
      self.metadata["render.modes"].append("rgb_array")

  def step(self, action):
    ret = self.env.step(action)
    self.last_observation = ret[0]
    return ret

  def reset(self, **kwargs):
    self.last_observation = self.env.reset(**kwargs)
    return self.last_observation

  def render(self, mode="human", **kwargs):
    assert mode == "rgb_array"
    return self.last_observation


def wrap_with_monitor(env, video_dir):
  """Wrap environment with gym.Monitor.

  Video recording provided by Monitor requires
    1) both height and width of observation to be even numbers.
    2) rendering of environment

  Args:
    env: environment.
    video_dir: video directory.

  Returns:
    wrapped environment.
  """
  env = ExtendToEvenDimentions(env)
  env = RenderObservations(env)  # pylint: disable=redefined-variable-type
  env = gym.wrappers.Monitor(env, video_dir, force=True,
                             video_callable=lambda idx: True,
                             write_upon_reset=True)
  return env


def create_simulated_env(
    output_dir, grayscale, resize_width_factor, resize_height_factor,
    frame_stack_size, generative_model, generative_model_params,
    random_starts=True, which_epoch_data="last", **other_hparams
):
  """"Create SimulatedEnv with minimal subset of hparams."""
  # We need these, to initialize T2TGymEnv, but these values (hopefully) have
  # no effect on player.
  a_bit_risky_defaults = {
      "game": "pong",  # assumes that T2TGymEnv has always reward_range (-1,1)
      "real_batch_size": 1,
      "rl_env_max_episode_steps": -1,
      "max_num_noops": 0
  }

  for key in a_bit_risky_defaults:
    if key not in other_hparams:
      other_hparams[key] = a_bit_risky_defaults[key]

  hparams = tf.contrib.training.HParams(
      grayscale=grayscale,
      resize_width_factor=resize_width_factor,
      resize_height_factor=resize_height_factor,
      frame_stack_size=frame_stack_size,
      generative_model=generative_model,
      generative_model_params=generative_model_params,
      **other_hparams
  )
  return load_data_and_make_simulated_env(
      output_dir, wm_dir=None, hparams=hparams,
      which_epoch_data=which_epoch_data,
      random_starts=random_starts)


class PPOPolicyInferencer(object):
  """Non-tensorflow API for infering policy (and value function).

  Example:
    >>> ppo = PPOPolicyInferencer(...)
    >>> ppo.reset_frame_stack()
    >>> ob = env.reset()
    >>> while not done:
    >>>   logits, value = ppo.infer(ob)
    >>>   ob, _, done, _ = env.step(action)
  """

  def __init__(self, hparams, action_space, observation_space, policy_dir):
    assert hparams.base_algo == "ppo"
    ppo_hparams = trainer_lib.create_hparams(hparams.base_algo_params)

    frame_stack_shape = (1, hparams.frame_stack_size) + observation_space.shape
    self._frame_stack = np.zeros(frame_stack_shape, dtype=np.uint8)

    with tf.Graph().as_default():
      self.obs_t = tf.placeholder(shape=self.frame_stack_shape, dtype=np.uint8)
      self.logits_t, self.value_function_t = get_policy(
          self.obs_t, ppo_hparams, action_space
      )
      model_saver = tf.train.Saver(
          tf.global_variables(scope=ppo_hparams.policy_network + "/.*")  # pylint: disable=unexpected-keyword-arg
      )
      self.sess = tf.Session()
      self.sess.run(tf.global_variables_initializer())
      trainer_lib.restore_checkpoint(policy_dir, model_saver,
                                     self.sess)

  @property
  def frame_stack_shape(self):
    return self._frame_stack.shape

  def reset_frame_stack(self, frame_stack=None):
    if frame_stack is None:
      self._frame_stack.fill(0)
    else:
      assert frame_stack.shape == self.frame_stack_shape, \
        "{}, {}".format(frame_stack.shape, self.frame_stack_shape)
      self._frame_stack = frame_stack.copy()

  def _add_to_stack(self, ob):
    stack = np.roll(self._frame_stack, shift=-1, axis=1)
    stack[0, -1, ...] = ob
    self._frame_stack = stack

  def infer(self, ob):
    """Add new observation to frame stack and infer policy.

    Args:
      ob: array of shape (height, width, channels)

    Returns:
      logits and vf.
    """
    self._add_to_stack(ob)
    logits, vf = self.infer_from_frame_stack(self._frame_stack)
    return logits, vf

  def infer_from_frame_stack(self, ob_stack):
    """Infer policy from stack of observations.

    Args:
      ob_stack: array of shape (1, frame_stack_size, height, width, channels)

    Returns:
      logits and vf.
    """
    logits, vf = self.sess.run([self.logits_t, self.value_function_t],
                               feed_dict={self.obs_t: ob_stack})
    return logits, vf


def infer_paths(output_dir, **subdirs):
  """Infers standard paths to policy and model directories.

  Example:
    >>> infer_paths("/some/output/dir/", policy="", model="custom/path")
    {"policy": "/some/output/dir/policy", "model": "custom/path",
    "output_dir":"/some/output/dir/"}

  Args:
    output_dir: output directory.
    **subdirs: sub-directories.

  Returns:
    a dictionary with the directories.
  """
  directories = dict()
  for name, path in six.iteritems(subdirs):
    directories[name] = path if path else os.path.join(output_dir, name)
  directories["output_dir"] = output_dir
  return directories
