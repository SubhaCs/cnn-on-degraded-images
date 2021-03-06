# -*- coding: utf-8 -*-
"""
Training a capsule network.
Created on Tue May 29 20:00:00 2018
Author: Prasun Roy | CVPRU-ISICAL (http://www.isical.ac.in/~cvpr)
GitHub: https://github.com/prasunroy/cnn-on-degraded-images

Original author: Xifeng Guo
Original source: https://github.com/XifengGuo/CapsNet-Keras

"""


# imports
from __future__ import division
from __future__ import print_function

import cv2
import glob
import numpy
import os
import tensorflow

from keras import backend
from keras import layers
from keras import models
from keras import optimizers
from keras.callbacks import CSVLogger
from keras.callbacks import EarlyStopping
from keras.callbacks import LearningRateScheduler
from keras.callbacks import ModelCheckpoint
from keras.callbacks import TensorBoard
from keras.utils import to_categorical
from matplotlib import pyplot
from scipy.io import loadmat

from libs.CapsuleNetwork import CapsuleLayer
from libs.CapsuleNetwork import Length
from libs.CapsuleNetwork import Mask
from libs.CapsuleNetwork import PrimaryCaps
from libs.PipelineUtils import shutdown
from mlutils.callbacks import Telegram


# configurations
# -----------------------------------------------------------------------------
PROCESS_SEED = None

ARCHITECTURE = 'capsnet'
DECODER_NAME = 'decoder'
INPUT_DSHAPE = (104, 104, 3)
NUM_TCLASSES = 10

DATASET_ID = 'synthetic_digits'
DATA_TRAIN = 'data/{}/data_train_104x104/data.mat'.format(DATASET_ID)
DATA_VALID = 'data/{}/data_valid_104x104/data.mat'.format(DATASET_ID)
IMAGE_SIZE = (104, 104, 3)
BATCH_SIZE = 50
NUM_EPOCHS = 100
LEARN_RATE = 0.001
DECAY_RATE = 0.9
RECON_COEF = 0.0005
N_ROUTINGS = 3
F_OPTIMIZE = optimizers.adam(lr=LEARN_RATE)
OUTPUT_DIR = 'output/{}/{}/'.format(DATASET_ID, ARCHITECTURE)

AUTH_TOKEN = None
TELCHAT_ID = None

F_SHUTDOWN = False
# -----------------------------------------------------------------------------


# setup seed for random number generators for reproducibility
numpy.random.seed(PROCESS_SEED)
tensorflow.set_random_seed(PROCESS_SEED)


# setup image data format
backend.set_image_data_format('channels_last')


# setup paths for model architectures
mdl_dir = os.path.join(OUTPUT_DIR, 'models')
mdl_train_file = os.path.join(mdl_dir, '{}_train.json'.format(ARCHITECTURE))
mdl_evaln_file = os.path.join(mdl_dir, '{}.json'.format(ARCHITECTURE))


# setup paths for callbacks
log_dir = os.path.join(OUTPUT_DIR, 'logs')
cpt_dir = os.path.join(OUTPUT_DIR, 'checkpoints')
tbd_dir = os.path.join(OUTPUT_DIR, 'tensorboard')

log_file = os.path.join(log_dir, 'training.csv')
cpt_best = os.path.join(cpt_dir, '{}_best.h5'.format(ARCHITECTURE))
cpt_last = os.path.join(cpt_dir, '{}_last.h5'.format(ARCHITECTURE))


# validate paths
def validate_paths():
    flag = True
    for file in [DATA_TRAIN, DATA_VALID]:
        if not os.path.isfile(file):
            print('[INFO] Data not found at {}'.format(file))
            flag = False
    for directory in [OUTPUT_DIR, mdl_dir, log_dir, cpt_dir, tbd_dir]:
        if not os.path.isdir(directory):
            os.makedirs(directory)
        elif len(glob.glob(os.path.join(directory, '*.*'))) > 0:
            print('[INFO] Output directory {} must be empty'.format(directory))
            flag = False
    
    return flag


# load data
def load_data():
    data_train = loadmat(DATA_TRAIN)['data']
    data_valid = loadmat(DATA_VALID)['data']
    
    numpy.random.shuffle(data_train)
    numpy.random.shuffle(data_valid)
    
    if INPUT_DSHAPE is None or IMAGE_SIZE == INPUT_DSHAPE:
        x_train = data_train[:, 1:].reshape((-1,) + INPUT_DSHAPE).astype('float32') / 255.0
        x_valid = data_valid[:, 1:].reshape((-1,) + INPUT_DSHAPE).astype('float32') / 255.0
    else:
        x_train = []
        x_valid = []
        
        for sample in data_train[:, 1:]:
            x = sample.reshape(IMAGE_SIZE)
            x = cv2.resize(x, INPUT_DSHAPE[:2])
            x_train.append(x)
        
        for sample in data_valid[:, 1:]:
            x = sample.reshape(IMAGE_SIZE)
            x = cv2.resize(x, INPUT_DSHAPE[:2])
            x_valid.append(x)
        
        x_train = numpy.asarray(x_train, dtype='float32') / 255.0
        x_valid = numpy.asarray(x_valid, dtype='float32') / 255.0
    
    y_train = to_categorical(data_train[:, 0]).astype('float32')
    y_valid = to_categorical(data_valid[:, 0]).astype('float32')
    
    return ((x_train, y_train), (x_valid, y_valid))


# calculate loss
def margin_loss(y_true, y_pred):
    loss = y_true * backend.square(backend.maximum(0.0, 0.9 - y_pred)) \
    + 0.5 * (1 - y_true) * backend.square(backend.maximum(0.0, y_pred - 0.1))
    
    return backend.mean(backend.sum(loss, 1))


# build model
def build_model():
    # input layer
    x = layers.Input(shape=INPUT_DSHAPE)
    
    # ~~~~~~~~ Capsule network ~~~~~~~~
    # layer 1: convolution layer
    conv1 = layers.Conv2D(filters=256, kernel_size=9, strides=2,
                          padding='valid', activation='relu',
                          name='Conv_1')(x)
    
    # layer 2: convolution layer
    conv2 = layers.Conv2D(filters=128, kernel_size=9, strides=1,
                          padding='valid', activation='relu',
                          name='Conv_2')(conv1)
    
    # layer 3: PrimaryCaps layer (convolution layer with `squash` activation)
    primarycaps = PrimaryCaps(conv2, dim_capsule=8, n_channels=32,
                              kernel_size=9, strides=2, padding='valid')
    
    # layer 4: DigitCaps layer (dynamic routing)
    digitcaps = CapsuleLayer(num_capsule=NUM_TCLASSES,
                             dim_capsule=16,
                             routings=N_ROUTINGS,
                             name='DigitCaps')(primarycaps)
    
    # layer 5: Output layer
    outputcaps = Length(name=ARCHITECTURE)(digitcaps)
    
    # ~~~~~~~~ Decoder network ~~~~~~~~
    y = layers.Input(shape=(NUM_TCLASSES,))
    
    masked_train = Mask()([digitcaps, y])
    masked_evaln = Mask()(digitcaps)
    
    decoder = models.Sequential(name=DECODER_NAME)
    decoder.add(layers.Dense(512, activation='relu', input_dim=16*NUM_TCLASSES))
    decoder.add(layers.Dense(1024, activation='relu'))
    decoder.add(layers.Dense(numpy.prod(INPUT_DSHAPE), activation='sigmoid'))
    decoder.add(layers.Reshape(target_shape=INPUT_DSHAPE, name='Reconstruction'))
    
    # ~~~~~~~~ Models for training and evaluation ~~~~~~~~
    model_train = models.Model([x, y], [outputcaps, decoder(masked_train)])
    model_evaln = models.Model(x, [outputcaps, decoder(masked_evaln)])
    
    model_train.compile(optimizer=F_OPTIMIZE,
                        loss=[margin_loss, 'mse'],
                        metrics={ARCHITECTURE: 'accuracy'},
                        loss_weights=[1.0, RECON_COEF])
    
    return [model_train, model_evaln]


# create callbacks
def callbacks():
    cb_log = CSVLogger(filename=log_file, append=True)
    cb_stp = EarlyStopping(monitor='val_{}_loss'.format(ARCHITECTURE), min_delta=0, patience=10, verbose=1)
    cb_lrs = LearningRateScheduler(schedule=lambda epoch: LEARN_RATE * (DECAY_RATE**epoch), verbose=0)
    cb_cpt_best = ModelCheckpoint(filepath=cpt_best, monitor='val_{}_acc'.format(ARCHITECTURE), save_best_only=True, save_weights_only=True, verbose=1)
    cb_cpt_last = ModelCheckpoint(filepath=cpt_last, monitor='val_{}_acc'.format(ARCHITECTURE), save_best_only=False, save_weights_only=True, verbose=0)
    cb_tbd = TensorBoard(log_dir=tbd_dir, batch_size=BATCH_SIZE, write_grads=True, write_images=True)
    cb_tel = Telegram(auth_token=AUTH_TOKEN, chat_id=TELCHAT_ID, monitor='val_{}_acc'.format(ARCHITECTURE), out_dir=OUTPUT_DIR)
    
    return [cb_log, cb_stp, cb_lrs, cb_cpt_best, cb_cpt_last, cb_tbd, cb_tel]


# plot learning curve
def plot(train_history):
    # plot training and validation loss
    pyplot.figure()
    pyplot.plot(train_history.history['loss'], label='loss')
    pyplot.plot(train_history.history['val_loss'], label='val_loss')
    pyplot.plot(train_history.history['{}_loss'.format(ARCHITECTURE)],
                                      label='{}_loss'.format(ARCHITECTURE))
    pyplot.plot(train_history.history['val_{}_loss'.format(ARCHITECTURE)],
                                      label='val_{}_loss'.format(ARCHITECTURE))
    pyplot.plot(train_history.history['{}_loss'.format(DECODER_NAME)],
                                      label='{}_loss'.format(DECODER_NAME))
    pyplot.plot(train_history.history['val_{}_loss'.format(DECODER_NAME)],
                                      label='val_{}_loss'.format(DECODER_NAME))
    pyplot.title('Training and Validation Loss')
    pyplot.xlabel('epoch')
    pyplot.ylabel('loss')
    pyplot.legend()
    pyplot.savefig(os.path.join(log_dir, 'plot_loss.png'))
    pyplot.show(block=False)
    
    # plot training and validation accuracy
    pyplot.figure()
    pyplot.plot(train_history.history['{}_acc'.format(ARCHITECTURE)],
                                      label='{}_acc'.format(ARCHITECTURE))
    pyplot.plot(train_history.history['val_{}_acc'.format(ARCHITECTURE)],
                                      label='val_{}_acc'.format(ARCHITECTURE))
    pyplot.title('Training and Validation Accuracy')
    pyplot.xlabel('epoch')
    pyplot.ylabel('accuracy')
    pyplot.legend()
    pyplot.savefig(os.path.join(log_dir, 'plot_accuracy.png'))
    pyplot.show(block=False)
    
    return


# train model
def train():
    # validate paths
    if not validate_paths():
        return
    
    # load data
    print('[INFO] Loading data... ', end='')
    ((x_train, y_train), (x_valid, y_valid)) = load_data()
    print('done')
    
    # build model
    print('[INFO] Building model... ', end='')
    model_train, model_evaln = build_model()
    print('done')
    
    model_train.summary()
    model_evaln.summary()
    
    # serialize models to json
    model_train_json = model_train.to_json()
    model_evaln_json = model_evaln.to_json()
    
    with open(mdl_train_file, 'w') as file:
        file.write(model_train_json)
    with open(mdl_evaln_file, 'w') as file:
        file.write(model_evaln_json)
    
    # create callbacks
    cb_list = callbacks()
    
    # acknowledgement
    data = {'chat_id': TELCHAT_ID,
            'text': '`Received a new training request.\nTASK ID: {}\nMODEL  : {}\nDATASET: {}`'\
            .format(cb_list[-1]._task_id, ARCHITECTURE.upper(), DATASET_ID.upper()),
            'parse_mode': 'Markdown'}
    
    cb_list[-1]._send_message(data)
    
    # train model
    train_history = model_train.fit([x_train, y_train],
                                    [y_train, x_train],
                                    batch_size=BATCH_SIZE,
                                    epochs=NUM_EPOCHS,
                                    callbacks=cb_list,
                                    validation_data=[[x_valid, y_valid],
                                                     [y_valid, x_valid]])
    
    # plot learning curve
    plot(train_history)
    
    return


# main
if __name__ == '__main__':
    train()
    if F_SHUTDOWN:
        shutdown()
