#!/usr/bin/env python3

import argparse
import logging
import os
import sys
import platform
import threading
import pickle
import time
import queue
import uuid
import zmq
import math
import json
import shutil
import subprocess
import multiprocessing
import random
import psutil

from typing import Any
from queue import PriorityQueue

# from funcx.executors.high_throughput.funcx_worker import funcx_worker

from funcx.executors.high_throughput.worker_map import WorkerMap

from funcx.version import VERSION as FUNCX_VERSION
from parsl.version import VERSION as PARSL_VERSION
from funcx import set_file_logger


RESULT_TAG = 10
TASK_REQUEST_TAG = 11
HEARTBEAT_CODE = (2 ** 32) - 1



class Manager(object):
    """ Manager manages task execution by the workers

                |         0mq              |    Manager         |   Worker Processes
                |                          |                    |
                | <-----Request N task-----+--Count task reqs   |      Request task<--+
    Interchange | -------------------------+->Receive task batch|          |          |
                |                          |  Distribute tasks--+----> Get(block) &   |
                |                          |                    |      Execute task   |
                |                          |                    |          |          |
                | <------------------------+--Return results----+----  Post result    |
                |                          |                    |          |          |
                |                          |                    |          +----------+
                |                          |                IPC-Qeueues

    """
    def __init__(self,
                 task_q_url="tcp://127.0.0.1:50097",
                 result_q_url="tcp://127.0.0.1:50098",
                 max_queue_size=10,
                 cores_per_worker=1,
                 max_workers=float('inf'),
                 uid=None,
                 heartbeat_threshold=120,
                 heartbeat_period=30,
                 logdir=None,
                 debug=False,
                 block_id=None,
                 internal_worker_port_range=(50000,60000),
                 mode="singularity_reuse",
                 container_image=None,
                 # TODO : This should be 10ms
                 poll_period=100):
        """
        Parameters
        ----------
        worker_url : str
             Worker url on which workers will attempt to connect back

        uid : str
             string unique identifier

        cores_per_worker : float
             cores to be assigned to each worker. Oversubscription is possible
             by setting cores_per_worker < 1.0. Default=1

        max_workers : int
             caps the maximum number of workers that can be launched.
             default: infinity

        heartbeat_threshold : int
             Seconds since the last message from the interchange after which the
             interchange is assumed to be un-available, and the manager initiates shutdown. Default:120s

             Number of seconds since the last message from the interchange after which the worker
             assumes that the interchange is lost and the manager shuts down. Default:120

        heartbeat_period : int
             Number of seconds after which a heartbeat message is sent to the interchange

        internal_worker_port_range : tuple(int, int)
             Port range from which the port(s) for the workers to connect to the manager is picked.
             Default: (50000,60000)

        mode : str
             Pick between 3 supported modes for the worker:
              1. no_container : Worker launched without containers
              2. singularity_reuse : Worker launched inside a singularity container that will be reused
              3. singularity_single_use : Each worker and task runs inside a new container instance.

        container_image : str
             Path or identifier for the container to be used. Default: None

        poll_period : int
             Timeout period used by the manager in milliseconds. Default: 10ms
        """

        logger.info("Manager started")

        self.context = zmq.Context()
        self.task_incoming = self.context.socket(zmq.DEALER)
        self.task_incoming.setsockopt(zmq.IDENTITY, uid.encode('utf-8'))
        # Linger is set to 0, so that the manager can exit even when there might be
        # messages in the pipe
        self.task_incoming.setsockopt(zmq.LINGER, 0)
        self.task_incoming.connect(task_q_url)

        self.logdir = logdir
        self.debug = debug
        self.block_id = block_id
        self.result_outgoing = self.context.socket(zmq.DEALER)
        self.result_outgoing.setsockopt(zmq.IDENTITY, uid.encode('utf-8'))
        self.result_outgoing.setsockopt(zmq.LINGER, 0)
        self.result_outgoing.connect(result_q_url)
        logger.info("Manager connected")

        self.uid = uid

        self.mode = mode
        self.container_image = container_image
        self.cores_on_node = multiprocessing.cpu_count()
        self.max_workers = max_workers
        self.cores_per_workers = cores_per_worker
        self.available_mem_on_node = round(psutil.virtual_memory().available / (2**30), 1)
        self.worker_count = min(max_workers,
                                math.floor(self.cores_on_node / cores_per_worker))
        self.worker_map = WorkerMap(self.worker_count)
        logger.info("Manager will spawn {} workers".format(self.worker_count))

        self.internal_worker_port_range = internal_worker_port_range

        self.funcx_task_socket = self.context.socket(zmq.ROUTER)
        self.funcx_task_socket.set_hwm(0)
        self.address = '127.0.0.1'
        self.worker_port = self.funcx_task_socket.bind_to_random_port(
            "tcp://*",
            min_port=self.internal_worker_port_range[0],
            max_port=self.internal_worker_port_range[1])

        logger.info("Manager listening on {} port for incoming worker connections".format(self.worker_port))

        self.task_queues = {'RAW': queue.Queue()}

        self.pending_result_queue = multiprocessing.Queue()

        self.max_queue_size = max_queue_size + self.worker_count
        self.tasks_per_round = 1

        self.heartbeat_period = heartbeat_period
        self.heartbeat_threshold = heartbeat_threshold
        self.poll_period = poll_period

    def create_reg_message(self):
        """ Creates a registration message to identify the worker to the interchange
        """
        msg = {'parsl_v': PARSL_VERSION,
               'python_v': "{}.{}.{}".format(sys.version_info.major,
                                             sys.version_info.minor,
                                             sys.version_info.micro),
               'worker_count': self.worker_count,
               'cores': self.cores_on_node,
               'mem': self.available_mem_on_node,
               'block_id': self.block_id,
               'os': platform.system(),
               'hname': platform.node(),
               'dir': os.getcwd(),
        }
        b_msg = json.dumps(msg).encode('utf-8')
        return b_msg

    def heartbeat(self):
        """ Send heartbeat to the incoming task queue
        """
        heartbeat = (HEARTBEAT_CODE).to_bytes(4, "little")
        r = self.task_incoming.send(heartbeat)
        logger.debug("Return from heartbeat: {}".format(r))

    def pull_tasks(self, kill_event):
        """ Pull tasks from the incoming tasks 0mq pipe onto the internal
        pending task queue


        While :
            receive results and task requests from the workers
            receive tasks/heartbeats from the Interchange
            match tasks to workers
            if task doesn't have appropriate worker type:
                 launch worker of type.. with LRU or some sort of caching strategy.
            if workers >> tasks:
                 advertize available capacity

        Parameters:
        -----------
        kill_event : threading.Event
              Event to let the thread know when it is time to die.
        """
        logger.info("[TASK PULL THREAD] starting")
        poller = zmq.Poller()
        poller.register(self.task_incoming, zmq.POLLIN)
        poller.register(self.funcx_task_socket, zmq.POLLIN)

        # Send a registration message
        msg = self.create_reg_message()
        logger.debug("Sending registration message: {}".format(msg))
        self.task_incoming.send(msg)
        last_beat = time.time()
        last_interchange_contact = time.time()
        task_recv_counter = 0
        task_done_counter = 0

        poll_timer = self.poll_period

        # This dict holds the info on the count of workers of various types available.
        worker_map = {'slots' : self.worker_count}

        while not kill_event.is_set():
            # Disabling the check on ready_worker_queue disables batching
            logger.debug("[TASK_PULL_THREAD] Loop start")
            pending_task_count = task_recv_counter - task_done_counter
            ready_worker_count = self.worker_map.ready_worker_count()
            logger.debug("[TASK_PULL_THREAD pending_task_count: {} Ready_worker_count: {}".format(
                pending_task_count, ready_worker_count))

            if time.time() > last_beat + self.heartbeat_period:
                self.heartbeat()
                last_beat = time.time()


            if pending_task_count < self.max_queue_size and ready_worker_count > 0:
                logger.debug("[TASK_PULL_THREAD] Requesting tasks: {}".format(ready_worker_count))
                msg = ((ready_worker_count).to_bytes(4, "little"))
                self.task_incoming.send(msg)

            # Receive results from the workers, if any
            socks = dict(poller.poll(timeout=poll_timer))
            if self.funcx_task_socket in socks and socks[self.funcx_task_socket] == zmq.POLLIN:
                # logger.debug("[FUNCX] There's an incoming result")
                try:
                    w_id, m_type, message = self.funcx_task_socket.recv_multipart()
                    logger.warning(f"Got registration message")
                    if m_type == b'REGISTER':
                        reg_info = pickle.loads(message)
                        logger.info("Registration received from worker:{} {}".format(w_id, reg_info))

                        # Increment worker_type count by 1
                        self.worker_map.register_worker(w_id, reg_info['worker_type'])
                        # TODO : HERE

                    elif m_type == b'TASK_RET':
                        logger.info("Result received from worker:{}".format(w_id))
                        logger.debug("[TASK_PULL_THREAD] Got result: {}".format(message))
                        self.pending_result_queue.put(message)
                        self.worker_map.put_worker(w_id)
                        task_done_counter += 1

                        # UNCOMMENT to kill workers. 
                        # self.kill_worker(1)
                    
                    elif m_type == b'WRKR_DIE':
                        logger.debug("[KILL] Scrubbing the worker from the map!")
                        self.worker_map.scrub_worker(w_id)

                except Exception as e:
                    logger.warning("[TASK_PULL_THREAD] FUNCX : caught {}".format(e))

            # Receive task batches from Interchange and forward to workers
            if self.task_incoming in socks and socks[self.task_incoming] == zmq.POLLIN:
                poll_timer = 0
                _, pkl_msg = self.task_incoming.recv_multipart()
                tasks = pickle.loads(pkl_msg)
                last_interchange_contact = time.time()

                if tasks == 'STOP':
                    logger.critical("[TASK_PULL_THREAD] Received stop request")
                    kill_event.set()
                    break

                elif tasks == HEARTBEAT_CODE:
                    logger.debug("Got heartbeat from interchange")

                else:
                    # TODO : Update this to unpack and forward tasks to the appropriate
                    # workers.
                    task_recv_counter += len(tasks)
                    logger.debug("[TASK_PULL_THREAD] Got tasks: {} of {}".format([t['task_id'] for t in tasks],
                                                                                 task_recv_counter))

                    for task in tasks:
                        # In the FuncX model we forward tasks received directly via a DEALER socket.
                        b_task_id = task['task_id'].encode()

                        # Set default type to raw
                        task_type = task.get('task_type', 'RAW')
                        if task_type not in self.task_queues:
                            self.task_queues[task_type] = queue.Queue()
                        self.task_queues[task_type].put(task)
                        logger.debug("Task {} pushed to a task queue".format(task))
                        """
                        #logger.debug("[TASK_PULL_THREAD] FuncX attempting send")
                        i = self.funcx_task_socket.send_multipart([b'', b_task_id] + task['buffer'])
                        logger.debug("[TASK_PULL_THREAD] FUNCX Forwarded task: {}".format(task['task_id']))
                        logger.debug("[TASK_PULL_THREAD] forward returned:{}".format(i))
                        """

            else:
                logger.debug("[TASK_PULL_THREAD] No incoming tasks")
                # Limit poll duration to heartbeat_period
                # heartbeat_period is in s vs poll_timer in ms
                if not poll_timer:
                    poll_timer = self.poll_period
                poll_timer = min(self.heartbeat_period * 1000, poll_timer * 2)

                # Only check if no messages were received.
                if time.time() > last_interchange_contact + self.heartbeat_threshold:
                    logger.critical("[TASK_PULL_THREAD] Missing contact with interchange beyond heartbeat_threshold")
                    kill_event.set()
                    logger.critical("[TASK_PULL_THREAD] Exiting")
                    break

            current_worker_map = self.worker_map.get_worker_counts()
            for task_type in current_worker_map:
                if task_type == 'slots':
                    continue


                else:

                    # TODO: TYLER -- in here, make KILL actually kill. 

                    available_workers = current_worker_map[task_type]
                    logger.debug("Available workers of type {}: {}".format(task_type,
                                                                           available_workers))
                    for i in range(available_workers):
                        if task_type in self.task_queues and not self.task_queues[task_type].empty():
                            task = self.task_queues[task_type].get()
                            worker_id = self.worker_map.get_worker(task_type)
                            logger.info("Sending task {} to {}".format(task['task_id'], worker_id))                            
                            to_send = [worker_id, pickle.dumps(task['task_id']), task['buffer']]
                            self.funcx_task_socket.send_multipart(to_send)
                            logger.debug("Sending done")


    def push_results(self, kill_event, max_result_batch_size=1):
        """ Listens on the pending_result_queue and sends out results via 0mq

        Parameters:
        -----------
        kill_event : threading.Event
              Event to let the thread know when it is time to die.
        """

        logger.debug("[RESULT_PUSH_THREAD] Starting thread")

        push_poll_period = max(10, self.poll_period) / 1000    # push_poll_period must be atleast 10 ms
        logger.debug("[RESULT_PUSH_THREAD] push poll period: {}".format(push_poll_period))

        last_beat = time.time()
        items = []

        while not kill_event.is_set():
            try:
                r = self.pending_result_queue.get(block=True, timeout=push_poll_period)
                items.append(r)
            except queue.Empty:
                pass
            except Exception as e:
                logger.exception("[RESULT_PUSH_THREAD] Got an exception: {}".format(e))

            # If we have reached poll_period duration or timer has expired, we send results
            if len(items) >= self.max_queue_size or time.time() > last_beat + push_poll_period:
                last_beat = time.time()
                if items:
                    self.result_outgoing.send_multipart(items)
                    items = []

        logger.critical("[RESULT_PUSH_THREAD] Exiting")


    def launch_worker(self, worker_id=str(random.random()),
                      mode='no_container',
                      container_uri=None,
                      walltime=1):
        """ Launch the appropriate worker

        Parameters
        ----------
        worker_id : str
           Worker identifier string
        mode : str
           Valid options are no_container, singularity
        walltime : int
           Walltime in seconds before we check status

        """
        print("LAUNCH_WORKER is only partially baked")

        debug = ' --debug' if self.debug else ''
        # TODO : This should assign some meaningful worker_id rather than random
        worker_id = ' --worker_id {}'.format(worker_id)

        cmd = (f'funcx-worker {debug}{worker_id} '
               f'-a {self.address} '
               f'-p {self.worker_port} '
               f'--logdir={self.logdir}/{self.uid} ')

        print("Command string : ", cmd)
        if mode == 'no_container':
            modded_cmd = cmd
        elif mode == 'singularity':
            modded_cmd = 'singularity run --writable {container_uri} {cmd}'.format(self.container_uri)
        else:
            raise NameError("Invalid container launch mode.")

        stdout = 'STDOUT: READING FAILED'
        stderr = 'STDERR: READING FAILED'
        try:
            proc = subprocess.Popen(cmd,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    shell=True)
            print("Launched proc")

        except Exception as e:
            print("TODO : Got an error in worker launch, got error {}".format(e))

        return proc


    def kill_worker(self, worker_type):
        """
            Kill a worker of a given worker_type. 

            Add a kill message to the task_type queue.

            Assumption : All workers of the same type are uniform, and therefore don't discriminate when killing. 
        """
        
        # self.dead_workers.add(worker_type)
        # self.available_workers[worker_type] -= 1  # COMMENTED OUT because messes with assigning the KILL task to the worker...
        logger.debug("[KILL] Appending KILL message to worker queue")
        self.task_queues[worker_type].put({"task_id": "KILL", "buffer": "KILL"})
        # self.worker_map.scrub_worker(worker_type)


    def start(self):
        """
        * while True:
            Receive tasks and start appropriate workers
            Push tasks to available workers
            Forward results
        """

        self.task_queues = {}  # k-v: task_type - task_q (PriorityQueue)
        self.worker_capacities = {}  # k-v: worker_id - capacity (integer... should only ever be 0 or 1)
        # TODO: Switch ^^^ to FIFO task queue.
        self.task_to_worker_sets = {}  # k-v: task_type - workers (set)

        # Keep track of workers to whom we've sent kill messages
        self.dead_worker_set = set()

        self.workers = [self.launch_worker(worker_id=5)]

        logger.debug("Initial workers launched")
        self._kill_event = threading.Event()
        self._result_pusher_thread = threading.Thread(target=self.push_results,
                                                      args=(self._kill_event,))
        self._result_pusher_thread.start()

        self.pull_tasks(self._kill_event)
        logger.info("Waiting")






    #----------------------------------------------------------------------------------------------
    # Deprecated
    def _start(self):
        """ Start the worker processes.

        TODO: Move task receiving to a thread
        """
        start = time.time()
        self._kill_event = threading.Event()

        self.procs = {}

        # @Tyler, we should do a `which funcx_worker.py` [note: not entry point, this must be a script]
        # copy that file over the directory '.' and then have the container run with pwd visible
        # as an initial cut, while we resolve possible issues.
        orig_location = os.getcwd()

        if not os.path.isdir("NAMESPACE"):
            os.mkdir("NAMESPACE")

        context = zmq.Context()
        registration_socket = context.socket(zmq.REP)
        self.reg_port = registration_socket.bind_to_random_port("tcp://*", min_port=50001, max_port=55000)

        for worker_id in range(self.worker_count):

            if self.mode.startswith("singularity"):
                try:
                    os.mkdir("NAMESPACE/{}".format(worker_id))
                    # shutil.copyfile(worker_py_path, "NAMESPACE/{}/funcx_worker.py".format(worker_id))
                except Exception:
                    pass  # Assuming the directory already exists.

            if self.mode == "no_container":
                p = multiprocessing.Process(target=funcx_worker,
                                            args=(worker_id,
                                                  self.uid,
                                                  "tcp://localhost:{}".format(self.internal_worker_port),
                                                  "tcp://localhost:{}".format(self.reg_port),
                                                  ),
                                            # DEBUG YADU. MUST SET BACK TO False,
                                            kwargs={'no_reuse': False,
                                                    'debug': self.debug,
                                                    'logdir': self.logdir})

                p.start()
                self.procs[worker_id] = p

            elif self.mode == "singularity_reuse":

                os.chdir("NAMESPACE/{}".format(worker_id))
                # @Tyler, FuncX worker path needs to be updated to not use the run command in the container.
                # We just want to invoke with "funcx_worker.py" which is found in the $PATH
                sys_cmd = ("singularity run {singularity_img} /usr/local/bin/funcx_worker.py --worker_id {worker_id} "
                           "--pool_id {pool_id} --task_url {task_url} --reg_url {reg_url} "
                           "--logdir {logdir} ")


                sys_cmd = sys_cmd.format(singularity_img=self.container_image,
                                         worker_id=worker_id,
                                         pool_id=self.uid,
                                         task_url="tcp://localhost:{}".format(self.internal_worker_port),
                                         reg_url="tcp://localhost:{}".format(self.reg_port),
                                         logdir=self.logdir)

                logger.debug("Singularity reuse launch cmd: {}".format(sys_cmd))
                proc = subprocess.Popen(sys_cmd, shell=True)
                self.procs[worker_id] = proc

                # Update the command to say something like :
                # while :
                # do
                #     singularity run {singularity_img} funcx_worker.py --no_reuse .....
                # done

                # FuncX worker to accept new --no_reuse flag that breaks the loop after 1 task.
                os.chdir(orig_location)

            elif self.mode == "singularity_single_use":
                # raise Exception("Not supported")
                os.chdir("NAMESPACE/{}".format(worker_id))

                if self.mode.startswith("singularity"):
                    #while True:
                    logger.info("New subprocess loop!")
                    sys_cmd = ("singularity run {singularity_img} /usr/local/bin/funcx_worker.py --no_reuse --worker_id {worker_id} "
                                   "--pool_id {pool_id} --task_url {task_url} --reg_url {reg_url} "
                                   "--logdir {logdir} ")
                    sys_cmd = sys_cmd.format(singularity_img=self.container_image,
                                             worker_id=worker_id,
                                             pool_id=self.uid,
                                             task_url="tcp://localhost:{}".format(self.internal_worker_port),
                                             reg_url="tcp://localhost:{}".format(self.reg_port),
                                             logdir=self.logdir)

                    bash_cmd = """ while :
                                   do
                                      {}
                                   done """.format(sys_cmd)
                    logger.debug("Singularity NO-reuse launch cmd: {}".format(bash_cmd))
                    proc = subprocess.Popen(bash_cmd, shell=True)
                    self.procs[worker_id] = proc
                    os.chdir(orig_location)


        for worker_id in range(self.worker_count):
            msg = registration_socket.recv_pyobj()
            logger.info("Received registration message from worker: {}".format(msg))
            registration_socket.send_pyobj("ACK")

        logger.debug("Manager synced with workers")

        self._task_puller_thread = threading.Thread(target=self.pull_tasks,
                                                    args=(self._kill_event,))
        self._result_pusher_thread = threading.Thread(target=self.push_results,
                                                      args=(self._kill_event,))
        self._task_puller_thread.start()
        self._result_pusher_thread.start()

        logger.info("Loop start")

        # TODO : Add mechanism in this loop to stop the worker pool
        # This might need a multiprocessing event to signal back.
        self._kill_event.wait()
        logger.critical("[MAIN] Received kill event, terminating worker processes")

        self._task_puller_thread.join()
        self._result_pusher_thread.join()
        for proc_id in self.procs:
            self.procs[proc_id].terminate()

            if type(self.procs[proc_id]) == "subprocess.Popen":

                poll = p.poll()

                if poll == None:
                    is_alive = False
                else:
                    is_alive = True

                logger.critical("Terminating worker {}:{}".format(self.procs[proc_id],
                                                                  is_alive))
            else:
                logger.critical("Terminating worker {}:{}".format(self.procs[proc_id],
                                                                  self.procs[proc_id].is_alive()))

            self.procs[proc_id].join()
            logger.debug("Worker:{} joined successfully".format(self.procs[proc_id]))

        self.task_incoming.close()
        self.result_outgoing.close()
        self.context.term()
        delta = time.time() - start
        logger.info("FuncX Manager ran for {} seconds".format(delta))
        return



def cli_run():

    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--debug", action='store_true',
                        help="Count of apps to launch")
    parser.add_argument("-l", "--logdir", default="process_worker_pool_logs",
                        help="Process worker pool log directory")
    parser.add_argument("-u", "--uid", default=str(uuid.uuid4()).split('-')[-1],
                        help="Unique identifier string for Manager")
    parser.add_argument("-b", "--block_id", default=None,
                        help="Block identifier string for Manager")
    parser.add_argument("-c", "--cores_per_worker", default="1.0",
                        help="Number of cores assigned to each worker process. Default=1.0")
    parser.add_argument("-t", "--task_url", required=True,
                        help="REQUIRED: ZMQ url for receiving tasks")
    parser.add_argument("--max_workers", default=float('inf'),
                        help="Caps the maximum workers that can be launched, default:infinity")
    parser.add_argument("--hb_period", default=30,
                        help="Heartbeat period in seconds. Uses manager default unless set")
    parser.add_argument("--hb_threshold", default=120,
                        help="Heartbeat threshold in seconds. Uses manager default unless set")
    parser.add_argument("--poll", default=10,
                        help="Poll period used in milliseconds")
    parser.add_argument("--container_image", default=None,
                        help="Container image identifier/path")
    parser.add_argument("--mode", default="singularity_reuse",
                        help=("Select the mode of operation from "
                              "(no_container, singularity_reuse, singularity_single_use"))
    parser.add_argument("-r", "--result_url", required=True,
                        help="REQUIRED: ZMQ url for posting results")

    args = parser.parse_args()

    try:
        os.makedirs(os.path.join(args.logdir, args.uid))
    except FileExistsError:
        pass

    try:
        global logger
        logger = set_file_logger('{}/{}/manager.log'.format(args.logdir, args.uid),
                                 level=logging.DEBUG if args.debug is True else logging.INFO)

        logger.info("Python version: {}".format(sys.version))
        logger.info("Debug logging: {}".format(args.debug))
        logger.info("Log dir: {}".format(args.logdir))
        logger.info("Manager ID: {}".format(args.uid))
        logger.info("Block ID: {}".format(args.block_id))
        logger.info("cores_per_worker: {}".format(args.cores_per_worker))
        logger.info("task_url: {}".format(args.task_url))
        logger.info("result_url: {}".format(args.result_url))
        logger.info("hb_period: {}".format(args.hb_period))
        logger.info("hb_threshold: {}".format(args.hb_threshold))
        logger.info("max_workers: {}".format(args.max_workers))
        logger.info("poll_period: {}".format(args.poll))
        logger.info("mode: {}".format(args.mode))
        logger.info("container_image: {}".format(args.container_image))

        manager = Manager(task_q_url=args.task_url,
                          result_q_url=args.result_url,
                          uid=args.uid,
                          block_id=args.block_id,
                          cores_per_worker=float(args.cores_per_worker),
                          max_workers=args.max_workers if args.max_workers == float('inf') else int(args.max_workers),
                          heartbeat_threshold=int(args.hb_threshold),
                          heartbeat_period=int(args.hb_period),
                          logdir=args.logdir,
                          debug=args.debug,
                          mode=args.mode,
                          container_image=args.container_image,
                          poll_period=int(args.poll))
        manager.start()

    except Exception as e:
        logger.critical("process_worker_pool exiting from an exception")
        logger.exception("Caught error: {}".format(e))
        raise
    else:
        logger.info("process_worker_pool exiting")
        print("PROCESS_WORKER_POOL exiting")


if __name__ == "__main__":
    cli_run()