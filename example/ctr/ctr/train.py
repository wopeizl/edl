from __future__ import print_function

import argparse
import logging
import os
import subprocess
import time

import numpy as np

import paddle
import paddle.fluid as fluid

import reader
from network_conf import ctr_dnn_model
from multiprocessing import cpu_count


# disable gpu training for this example
os.environ["CUDA_VISIBLE_DEVICES"] = ""

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("fluid")
logger.setLevel(logging.INFO)


def parse_args():
    parser = argparse.ArgumentParser(description="PaddlePaddle CTR example")
    parser.add_argument(
        '--train_data_path',
        type=str,
        default='./data/train.txt',
        help="The path of training dataset")
    parser.add_argument(
        '--test_data_path',
        type=str,
        default='./data/valid.txt',
        help="The path of testing dataset")
    parser.add_argument(
        '--batch_size',
        type=int,
        default=1000,
        help="The size of mini-batch (default:1000)")
    parser.add_argument(
        '--embedding_size',
        type=int,
        default=10,
        help="The size for embedding layer (default:10)")
    parser.add_argument(
        '--num_passes',
        type=int,
        default=10,
        help="The number of passes to train (default: 10)")
    parser.add_argument(
        '--model_output_dir',
        type=str,
        default='models',
        help='The path for model to store (default: models)')
    parser.add_argument(
        '--sparse_feature_dim',
        type=int,
        default=1000001,
        help='sparse feature hashing space for index processing')
    parser.add_argument(
        '--is_local',
        type=int,
        default=1,
        help='Local train or distributed train (default: 1)')
    parser.add_argument(
        '--cloud_train',
        type=int,
        default=0,
        help='Local train or distributed train on paddlecloud (default: 0)')
    parser.add_argument(
        '--async_mode',
        action='store_true',
        default=False,
        help='Whether start pserver in async mode to support ASGD')
    parser.add_argument(
        '--no_split_var',
        action='store_true',
        default=False,
        help='Whether split variables into blocks when update_method is pserver')
    # the following arguments is used for distributed train, if is_local == false, then you should set them
    parser.add_argument(
        '--role',
        type=str,
        default='pserver', # trainer or pserver
        help='The path for model to store (default: models)')
    parser.add_argument(
        '--endpoints',
        type=str,
        default='127.0.0.1:6000',
        help='The pserver endpoints, like: 127.0.0.1:6000,127.0.0.1:6001')
    parser.add_argument(
        '--current_endpoint',
        type=str,
        default='127.0.0.1:6000',
        help='The path for model to store (default: 127.0.0.1:6000)')
    parser.add_argument(
        '--trainer_id',
        type=int,
        default=0,
        help='The trainer id (default: models)')
    parser.add_argument(
        '--pserver_id',
        type=int,
        default=0,
        help='The pserver id (default: models)')
    parser.add_argument(
        '--trainers',
        type=int,
        default=1,
        help='The num of trianers, (default: 1)')

    return parser.parse_args()


def train_loop(args, train_program, py_reader, loss, auc_var, batch_auc_var,
               trainer_num, trainer_id):
    dataset = reader.CriteoDataset(args.sparse_feature_dim)
    train_reader = paddle.batch(
        paddle.reader.shuffle(
            dataset.train([args.train_data_path], trainer_num, trainer_id),
            buf_size=args.batch_size * 100),
        batch_size=args.batch_size)

    py_reader.decorate_paddle_reader(train_reader)
    data_name_list = []

    place = fluid.CPUPlace()
    exe = fluid.Executor(place)

    exec_strategy = fluid.ExecutionStrategy()
    build_strategy = fluid.BuildStrategy()

    if os.getenv("NUM_THREADS", ""):
        exec_strategy.num_threads = int(os.getenv("NUM_THREADS"))

    cpu_num = int(os.environ.get('CPU_NUM', cpu_count()))
    build_strategy.reduce_strategy = \
        fluid.BuildStrategy.ReduceStrategy.Reduce if cpu_num > 1 \
            else fluid.BuildStrategy.ReduceStrategy.AllReduce

    pe = fluid.ParallelExecutor(
        use_cuda=False,
        loss_name=loss.name,
        main_program=train_program,
        build_strategy=build_strategy,
        exec_strategy=exec_strategy)

    exe.run(fluid.default_startup_program())

    for pass_id in range(args.num_passes):
        pass_start = time.time()
        batch_id = 0
        py_reader.start()

        try:
            while True:
                loss_val, auc_val, batch_auc_val = pe.run(fetch_list=[loss.name, auc_var.name, batch_auc_var.name])
                loss_val = np.mean(loss_val)
                auc_val = np.mean(auc_val)
                batch_auc_val = np.mean(batch_auc_val)

                logger.info("TRAIN --> pass: {} batch: {} loss: {} auc: {}, batch_auc: {}"
                      .format(pass_id, batch_id, loss_val/args.batch_size, auc_val, batch_auc_val))
                if batch_id % 1000 == 0 and batch_id != 0:
                    model_dir = args.model_output_dir + '/batch-' + str(batch_id)
                    if args.trainer_id == 0:
                        fluid.io.save_inference_model(model_dir, data_name_list, [loss, auc_var], exe)
                batch_id += 1
        except fluid.core.EOFException:
            py_reader.reset()
        print("pass_id: %d, pass_time_cost: %f" % (pass_id, time.time() - pass_start))

        model_dir = args.model_output_dir + '/pass-' + str(pass_id)
        if args.trainer_id == 0:
            fluid.io.save_inference_model(model_dir, data_name_list, [loss, auc_var], exe)


def train():
    args = parse_args()

    if not os.path.isdir(args.model_output_dir):
        os.mkdir(args.model_output_dir)

    loss, auc_var, batch_auc_var, py_reader = ctr_dnn_model(args.embedding_size, args.sparse_feature_dim)
    optimizer = fluid.optimizer.Adam(learning_rate=1e-4)
    optimizer.minimize(loss)
    if args.cloud_train:
        # the port of all pservers, needed by both trainer and pserver
        port = int(os.getenv("PADDLE_PORT", "6174"))
        # comma separated ips of all pservers, needed by trainer and

        args.endpoints = os.getenv("PADDLE_PSERVERS", "")
        args.trainers = int(os.getenv("PADDLE_TRAINERS_NUM", "1"))
        args.pserver_id = int(os.getenv("PADDLE_PSERVER_ID", "0"))
        args.current_endpoint = os.getenv("POD_IP", "localhost") + ":" + str(port + args.pserver_id)
        args.role = os.getenv("TRAINING_ROLE", "TRAINER")
        args.trainer_id = int(os.getenv("PADDLE_TRAINER_ID", "0"))
        args.is_local = bool(int(os.getenv("PADDLE_IS_LOCAL", 0)))

    if args.is_local:
        logger.info("run local training")
        main_program = fluid.default_main_program()
        train_loop(args, main_program, py_reader, loss, auc_var, batch_auc_var, 1, 0)
    else:
        logger.info("run dist training")
        t = fluid.DistributeTranspiler()
        t.transpile(args.trainer_id, pservers=args.endpoints, trainers=args.trainers)
        if args.role == "pserver" or args.role == "PSERVER":
            logger.info("run pserver")
            prog = t.get_pserver_program(args.current_endpoint)
            startup = t.get_startup_program(args.current_endpoint, pserver_program=prog)
            exe = fluid.Executor(fluid.CPUPlace())
            exe.run(startup)
            exe.run(prog)
        elif args.role == "trainer" or args.role == "TRAINER":
            logger.info("download the training materials")
            file_index = args.trainer_id % 10
            address = "https://paddle-ctr-data.bj.bcebos.com/dac" + str(file_index) + ".tar.gz"
            cmd = "cd /workspace/ctr/data/ && curl -o dac.tar.gz " + address + " && tar zxf dac.tar.gz && rm dac.tar.gz"
            exit_code = subprocess.call(cmd, shell=True)
            if exit_code != 0:
                raise Exception("The download command failed, please check the network settings")
            logger.info("run trainer")
            train_prog = t.get_trainer_program()
            train_loop(args, train_prog, py_reader, loss, auc_var, batch_auc_var,
                       args.trainers, args.trainer_id)
        else:
            raise ValueError(
                'PADDLE_TRAINING_ROLE environment variable must be either TRAINER or PSERVER'
            )


if __name__ == '__main__':
    train()
