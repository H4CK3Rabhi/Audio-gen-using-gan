# Based on TensorFlow released DCGAN codes

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os, time

# Standard Imports
from absl import flags
from scipy.io.wavfile import write as wavwrite
import numpy as np
import tensorflow as tf

import bias_input
import bias_model
from tensorflow.contrib import summary
from tensorflow.python.estimator import estimator

FLAGS = flags.FLAGS

# Cloud TPU Cluster Resolvers
flags.DEFINE_string(
    'tpu', default='acheketa2-tpu',
    help='The Cloud TPU to use for training. This should be either the name '
         'used when creating the Cloud TPU, or a grpc://ip.address.of.tpu:8470 url.')
flags.DEFINE_string(
    'gcp_project', default='dlcampjeju2018',
    help='Project name for the Cloud TPU-enabled project. If not specified, we '
         'will attempt to automatically detect the GCE project from metadata.')
flags.DEFINE_string(
    'tpu_zone', default='us-central1-f',
    help='GCE zone where the Cloud TPU is located in. If not specified, we '
         'will attempt to automatically detect the GCE project from metadata.')

# Model specific paramenters
flags.DEFINE_string('model_dir', 'gs://acheketa1-ckpt', 'Output model directory')
flags.DEFINE_integer('noise_dim', 100,
                     'Number of dimensions for the noise vector')
flags.DEFINE_integer('batch_size', 512,
                     'Batch size for both generator and discriminator')
flags.DEFINE_integer('num_shards', None, 'Number of TPU chips')
flags.DEFINE_integer('train_steps', 200000, 'Number of training steps')
flags.DEFINE_integer('train_steps_per_eval', 400,
                     'Steps per eval and image generation')
flags.DEFINE_integer('iterations_per_loop', 40,
                     'Steps per interior TPU loop. Should be less than'
                     ' --train_steps_per_eval')
flags.DEFINE_float('learning_rate', 0.02, 'LR for both D and G')
flags.DEFINE_boolean('eval_loss', False,
                     'Evaluate discriminator and generator loss during eval')
flags.DEFINE_boolean('use_tpu', True, 'Use TPU for training')

_NUM_VIZ_AUDIO = 20  # For generating a 10x10 grid of generator samples
_D_Y = 10  # label
_FS = 16000
_WINDOW_LEN = 8192

# Global variables for data and model
dataset = None
model = None


def model_fn(features, labels, mode, params):
    def host_call_fn(gs, g_loss, d_loss, real_audio, generated_audio):
        """Training host call. Creates scalar summaries for training metrics.
    This function is executed on the CPU and should not directly reference
    any Tensors in the rest of the `model_fn`. To pass Tensors from the
    model to the `metric_fn`, provide as part of the `host_call`. See
    https://www.tensorflow.org/api_docs/python/tf/contrib/tpu/TPUEstimatorSpec
    for more information.
    Arguments should match the list of `Tensor` objects passed as the second
    element in the tuple passed to `host_call`.
    Args:
      gs: `Tensor with shape `[batch]` for the global_step
      g_loss: `Tensor` with shape `[batch]` for the generator loss.
      d_loss: `Tensor` with shape `[batch]` for the discriminator loss.
      real_audio: `Tensor` with shape `[batch, 8192, 1]`
      generated_audio: `Tensor` with shape `[batch, 8192, 1]`
    Returns:
      List of summary ops to run on the CPU host.
    """
        gs = gs[0]
        with summary.create_file_writer(FLAGS.model_dir).as_default():
            with summary.always_record_summaries():
                summary.scalar('g_loss', g_loss, step=gs)
                summary.scalar('d_loss', d_loss, step=gs)
                summary.audio('real_audio', real_audio, sample_rate=_FS, max_outputs=10, step=gs)
                summary.audio('generated_audio', generated_audio, sample_rate=_FS, max_outputs=10, step=gs)
        return summary.all_summary_ops()


    if mode == tf.estimator.ModeKeys.PREDICT:
        ###########
        # PREDICT #
        ###########
        # Pass only noise to PREDICT mode
        # labels
        labels = tf.range(10)
        labels = labels + tf.constant(10, name='fixed', dtype=tf.int32)
        labels = tf.tile(labels, [2])
        labels = tf.cast(labels, dtype=tf.float32)
        labels = tf.reshape(labels, [_NUM_VIZ_AUDIO, 1])
        random_noise = features['random_noise']
        predictions = {
            'generated_audio': model.generator_wavegan(random_noise, labels, train=False)
        }
        return tf.contrib.tpu.TPUEstimatorSpec(mode=mode, predictions=predictions)


    # Use params['batch_size'] for the batch size inside model_fn
    batch_size = params['batch_size']  # pylint: disable=unused-variable
    real_audio = features['real_audio']
    random_noise = features['random_noise']

    is_training = (mode == tf.estimator.ModeKeys.TRAIN)
    generated_audio = model.generator_wavegan(random_noise, labels, train=is_training)

    # Get logits from discriminator
    d_on_data_logits = tf.squeeze(model.discriminator_wavegan(real_audio, labels, reuse=False))
    d_on_g_logits = tf.squeeze(model.discriminator_wavegan(generated_audio, labels, reuse=True))

    # Calculate discriminator loss
    d_loss_on_data = tf.nn.sigmoid_cross_entropy_with_logits(
        labels=tf.ones_like(d_on_data_logits),
        logits=d_on_data_logits)
    d_loss_on_gen = tf.nn.sigmoid_cross_entropy_with_logits(
        labels=tf.zeros_like(d_on_g_logits),
        logits=d_on_g_logits)

    d_loss = d_loss_on_data + d_loss_on_gen

    # Calculate generator loss
    g_loss = tf.nn.sigmoid_cross_entropy_with_logits(
        labels=tf.ones_like(d_on_g_logits),
        logits=d_on_g_logits)

    if mode != tf.estimator.ModeKeys.PREDICT:
        global_step = tf.reshape(tf.train.get_global_step(), [1])
        g_loss_t = g_loss
        d_loss_t = d_loss
        host_call = (host_call_fn, [global_step, g_loss_t, d_loss_t, real_audio, generated_audio])


    if mode == tf.estimator.ModeKeys.TRAIN:
        #########
        # TRAIN #
        #########

        g_optimizer = tf.train.AdamOptimizer(
            learning_rate=FLAGS.learning_rate,
            beta1=0.5)
        d_optimizer = tf.train.AdamOptimizer(
            learning_rate=2e-4,
            beta1=0.5)

        if FLAGS.use_tpu:
            d_optimizer = tf.contrib.tpu.CrossShardOptimizer(d_optimizer)
            g_optimizer = tf.contrib.tpu.CrossShardOptimizer(g_optimizer)

        with tf.control_dependencies(tf.get_collection(tf.GraphKeys.UPDATE_OPS)):
            d_step = d_optimizer.minimize(
                d_loss,
                var_list=tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES,
                                           scope='Discriminator'))
            g_step = g_optimizer.minimize(
                g_loss,
                var_list=tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES,
                                           scope='Generator'))

            increment_step = tf.assign_add(tf.train.get_or_create_global_step(), 1)
            joint_op = tf.group([d_step, g_step, increment_step])

            return tf.contrib.tpu.TPUEstimatorSpec(
                mode=mode,
                loss=g_loss,
                train_op=joint_op,
                host_call=host_call)


    elif mode == tf.estimator.ModeKeys.EVAL:
        ########
        # EVAL #
        ########
        def _eval_metric_fn(d_loss, g_loss):
            # When using TPUs, this function is run on a different machine than the
            # rest of the model_fn and should not capture any Tensors defined there
            return {
                'discriminator_loss': tf.metrics.mean(d_loss),
                'generator_loss': tf.metrics.mean(g_loss)}

        return tf.contrib.tpu.TPUEstimatorSpec(
            mode=mode,
            loss=tf.reduce_mean(g_loss),
            eval_metrics=(_eval_metric_fn, [d_loss, g_loss]))

    # Should never reach here
    raise ValueError('Invalid mode provided to model_fn')


def generate_input_fn(is_training):
    """Creates input_fn depending on whether the code is training or not."""
    return dataset.InputFunction(is_training, FLAGS.noise_dim)


def noise_input_fn(params):
    """Input function for generating samples for PREDICT mode.

  Generates a single Tensor of fixed random noise. Use tf.data.Dataset to
  signal to the estimator when to terminate the generator returned by
  predict().

  Args:
    params: param `dict` passed by TPUEstimator.

  Returns:
    1-element `dict` containing the randomly generated noise.
  """

    # random noise
    np.random.seed(0)
    noise_dataset = tf.data.Dataset.from_tensors(tf.constant(
        np.random.randn(params['batch_size'], FLAGS.noise_dim), dtype=tf.float32))
    noise = noise_dataset.make_one_shot_iterator().get_next()
    return {'random_noise': noise}, None


def main(argv):
    del argv
    tpu_cluster_resolver = tf.contrib.cluster_resolver.TPUClusterResolver(
        FLAGS.tpu,
        zone=FLAGS.tpu_zone,
        project=FLAGS.gcp_project)

    config = tf.contrib.tpu.RunConfig(
        cluster=tpu_cluster_resolver,
        model_dir=FLAGS.model_dir,
        tpu_config=tf.contrib.tpu.TPUConfig(
            num_shards=FLAGS.num_shards,
            iterations_per_loop=FLAGS.iterations_per_loop))

    # Set module-level global variable so that model_fn and input_fn can be
    # identical for each different kind of dataset and model
    global dataset, model
    dataset = bias_input
    model = bias_model

    # TPU-based estimator used for TRAIN and EVAL
    est = tf.contrib.tpu.TPUEstimator(
        model_fn=model_fn,
        use_tpu=FLAGS.use_tpu,
        config=config,
        train_batch_size=FLAGS.batch_size,
        eval_batch_size=FLAGS.batch_size)

    # CPU-based estimator used for PREDICT (generating images)
    cpu_est = tf.contrib.tpu.TPUEstimator(
        model_fn=model_fn,
        use_tpu=False,
        config=config,
        predict_batch_size=_NUM_VIZ_AUDIO)

    current_step = estimator._load_global_step_from_checkpoint_dir(
        FLAGS.model_dir)  # pylint: disable=protected-access,line-too-long
    tf.logging.info('Starting training for %d steps, current step: %d' %
                    (FLAGS.train_steps, current_step))

    # Render some generated images
    G_z = cpu_est.predict(input_fn=noise_input_fn)
    G_z = [p['generated_audio'][:, :] for p in G_z]
    G_z = np.array(G_z)
    preview_dir = './output'
    if not os.path.isdir(preview_dir):
        os.makedirs(preview_dir)

    for i in range(len(G_z)):
      audio = np.int16(G_z[i]/np.max(np.abs(G_z[i])) * 32767)
      preview_fp = os.path.join(preview_dir, '{}_{}_{}.wav'.format(str(i % 10), str(current_step), str(i)))
      wavwrite(preview_fp, _FS, audio)

    tf.logging.info('Finished generating images')


if __name__ == '__main__':
    tf.logging.set_verbosity(tf.logging.INFO)
    tf.app.run(main)
