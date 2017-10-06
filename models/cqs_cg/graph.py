from ..connectorgraph import ConnectorGraph
from ..data_loader import get_loader
from ..errors import FirstInitialization
import numpy as np
import os
from ..subgraph import BuiltSubGraph, SubGraph
import tensorflow as tf


#Models
GENR = 'cqs_generator_0'
DISC = 'cqs_discriminator_0'
LSSG = 'cqs_loss_set_0'
LSSD = 'cqs_loss_set_1'
CNCT = 'cqs_concat_0'
SPLT = 'cqs_split_0'
CQST = 'cqs_train'

#Type variables
MDL_TYPE = 'z128_sz64'
LSS_TYPE = 'scaled_began_gmsm'

#Inputs
INPT = '/input:0'
G_IN = '/gen_input:0'
D_IN = '/data_input:0'
O_IN = '/orig_input:0'
A_IN = '/autoencoded_input:0'

#Outputs
OUTP = '/output:0'
GOUT = '/gen_output:0'
DOUT = '/data_output:0'

#Variables
VARS = '/trainable_variables'

connections = [
    #from_graph, to_graph, from_conn, to_conn
    [GENR, CNCT, GENR+OUTP, CNCT+G_IN],
    [CNCT, DISC, CNCT+OUTP, DISC+INPT],
    [DISC, SPLT, DISC+OUTP, SPLT+INPT],
    [GENR, LSSG, GENR+OUTP, LSSG+O_IN],
    [SPLT, LSSG, SPLT+GOUT, LSSG+A_IN],
    [SPLT, LSSD, SPLT+DOUT, LSSD+A_IN],
]

inputs = [
    GENR+INPT,
    CNCT+D_IN,
    LSSD+O_IN, #same as CNCT+D_IN
]

outputs = [
    LSSG+OUTP,
    LSSD+OUTP,
]


def build_graph(config):
    #TODO: fix partial loading of saved variables from this model into partial models
    generator = init_subgraph(GENR, MDL_TYPE)
    discriminator = init_subgraph(DISC, MDL_TYPE)
    disc_loss_set = init_subgraph(LSSD, LSS_TYPE)
    gen_loss_set = init_subgraph(LSSG, LSS_TYPE)
    concat_op = init_subgraph(CNCT, '')
    split_op = init_subgraph(SPLT, '')

    conngraph = ConnectorGraph()
    conngraph.add_subgraph(generator)
    conngraph.add_subgraph(discriminator)
    conngraph.add_subgraph(gen_loss_set)
    conngraph.add_subgraph(disc_loss_set)
    conngraph.add_subgraph(concat_op)
    conngraph.add_subgraph(split_op)

    conngraph.print_subgraphs()

    conngraph.quick_connect(connections)


    with tf.Session(graph=tf.Graph()) as sess:
        conngraph.connect_graph(inputs, outputs, sess)
        
        with tf.variable_scope('cqs_train'):
            k_t = tf.Variable(0., trainable=False, name='k_t')

            d_loss = sess.graph.get_tensor_by_name(LSSD+OUTP)
            g_loss = sess.graph.get_tensor_by_name(LSSG+OUTP)
            g_loss = tf.identity(g_loss, name='g_loss')

            d_out = d_loss - k_t * g_loss
            d_out = tf.identity(d_out, name='d_loss')

            g_lr = tf.Variable(config.g_lr, name='g_lr')
            d_lr = tf.Variable(config.d_lr, name='d_lr')

            g_lr_update = tf.assign(g_lr, tf.maximum(g_lr * 0.5, config.lr_lower_boundary), name='g_lr_update')
            d_lr_update = tf.assign(d_lr, tf.maximum(d_lr * 0.5, config.lr_lower_boundary), name='d_lr_update')

            g_optimizer = tf.train.AdamOptimizer(g_lr)
            d_optimizer = tf.train.AdamOptimizer(d_lr)

            g_optim = g_optimizer.minimize(g_loss, var_list=tf.get_collection(GENR+VARS))
            d_optim = d_optimizer.minimize(d_out, var_list=tf.get_collection(DISC+VARS))

            balance = config.gamma * d_loss - g_loss
            measure = d_loss + tf.abs(balance)
            measure = tf.identity(measure, name='measure')

            with tf.control_dependencies([d_optim, g_optim]):
                k_update = tf.assign(k_t, tf.clip_by_value(k_t + config.lambda_k * balance, 0, 1))
                k_update = tf.identity(k_update, name='k_update')

            summary_op = tf.summary.merge([
                tf.summary.image('G', denorm_img(sess.graph.get_tensor_by_name(GENR+OUTP), config.data_format)),
                tf.summary.image('AE_G', denorm_img(sess.graph.get_tensor_by_name(SPLT+GOUT), config.data_format)),
                tf.summary.image('AE_D', denorm_img(sess.graph.get_tensor_by_name(SPLT+DOUT), config.data_format)),

                tf.summary.scalar('loss/d_loss', d_out),
                tf.summary.scalar('loss/g_loss', g_loss),

                tf.summary.scalar('misc/measure', measure),
                tf.summary.scalar('misc/k_t', k_t),
                tf.summary.scalar('misc/g_lr', g_lr),
                tf.summary.scalar('misc/d_lr', d_lr),
                tf.summary.scalar('misc/balance', balance),
            ])

        step = tf.Variable(0, name='step', trainable=False)
        tf.add_to_collection('step', step)
        
        sess.graph.clear_collection('outputs')
        tf.add_to_collection('outputs_interim', d_out)
        tf.add_to_collection('outputs_interim', g_loss)
        tf.add_to_collection('outputs_interim', k_t)
        tf.add_to_collection('outputs_interim', summary_op)
        tf.add_to_collection('outputs', k_update)
        tf.add_to_collection('outputs', measure)
        tf.add_to_collection('outputs_lr', g_lr_update)
        tf.add_to_collection('outputs_lr', d_lr_update)
        tf.add_to_collection('summary', summary_op)

        def get_feed_dict(self, data_loader, config, sess):
            x = data_loader
            # x = norm_img(x)
            x = sess.run(x)
            x = norm_img(x) #running numpy version so don't have to modify graph
            z = np.random.uniform(-1, 1, size=(config.batch_size, config.z_num))

            feed_dict = {GENR+INPT: z, 
                         CNCT+D_IN: x,
                         LSSD+O_IN: x}
            return feed_dict
        
        conngraph.attach_feed_dict_func(get_feed_dict)
        
    return conngraph

def denorm_img(norm, data_format):
    return tf.clip_by_value(to_nhwc((norm + 1)*127.5, data_format), 0, 255)


def nchw_to_nhwc(x):
    return tf.transpose(x, [0, 2, 3, 1])


def to_nhwc(image, data_format):
    if data_format == 'NCHW':
        new_image = nchw_to_nhwc(image)
    else:
        new_image = image
    return new_image


def to_nhwc_numpy(image, data_format):
    if data_format == 'NCHW':
        new_image = image.transpose([0, 3, 1, 2])
    else:
        new_image = image
    return new_image


def to_nchw_numpy(image):
    if image.shape[3] in [1, 3]:
        new_image = image.transpose([0, 3, 1, 2])
    else:
        new_image = image
    return new_image


def norm_img(image, data_format=None):
    image = image/127.5 - 1.
    if data_format:
        image = to_nhwc_numpy(image, data_format)
    return image


def init_subgraph(subgraph_name, type_):
    try:
        with tf.Session(graph=tf.Graph()) as sess:
            subgraph = BuiltSubGraph(subgraph_name, type_, sess)
    except FirstInitialization:
        with tf.Session(graph=tf.Graph()) as sess:
            subgraph = BuiltSubGraph(subgraph_name, type_, sess)
    return subgraph
