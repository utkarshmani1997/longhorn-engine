import fcntl
import json
import struct
import os
import grpc
import tempfile
import time
import random
import subprocess


from rpc.instance_manager.engine_manager_client import EngineManagerClient  # NOQA
from rpc.instance_manager.process_manager_client import ProcessManagerClient  # NOQA

from rpc.replica.replica_client import ReplicaClient  # NOQA

from rpc.controller.controller_client import ControllerClient  # NOQA


INSTANCE_MANAGER = "localhost:8500"

INSTANCE_MANAGER_TYPE_ENGINE = "engine"
INSTANCE_MANAGER_TYPE_REPLICA = "replica"

LONGHORN_BINARY = "./bin/longhorn"
BINARY_PATH_IN_TEST = "../bin/longhorn"

RETRY_INTERVAL = 0.5
RETRY_COUNTS = 30
RETRY_COUNTS2 = 100

SIZE = 4 * 1024 * 1024
SIZE_STR = str(SIZE)

EXPANDED_SIZE = 2 * SIZE
EXPANDED_SIZE_STR = str(EXPANDED_SIZE)

TEST_PREFIX = dict(os.environ)["TESTPREFIX"]

VOLUME_NAME = TEST_PREFIX + "core-volume"
ENGINE_NAME = TEST_PREFIX + "core-engine"
REPLICA_NAME = TEST_PREFIX + "core-replica-1"
REPLICA_2_NAME = TEST_PREFIX + "core-replica-2"

PROC_STATE_STARTING = "starting"
PROC_STATE_RUNNING = "running"
PROC_STATE_STOPPING = "stopping"
PROC_STATE_STOPPED = "stopped"
PROC_STATE_ERROR = "error"

FRONTEND_TGT_BLOCKDEV = "tgt-blockdev"


def cleanup_process(pm_client):
    cleanup_engine_process(EngineManagerClient(pm_client.address))
    for name in pm_client.process_list():
        try:
            pm_client.process_delete(name)
        except grpc.RpcError as e:
            if 'cannot find process' not in e.details():
                raise e
    for i in range(RETRY_COUNTS):
        ps = pm_client.process_list()
        if len(ps) == 0:
            break
        time.sleep(RETRY_INTERVAL)

    ps = pm_client.process_list()
    assert len(ps) == 0
    return pm_client


def cleanup_engine_process(em_client):
    for _, engine in iter(em_client.engine_list().items()):
        try:
            em_client.engine_delete(engine.spec.name)
        except grpc.RpcError as e:
            if 'cannot find engine' not in e.details():
                raise e
    for i in range(RETRY_COUNTS):
        es = em_client.engine_list()
        if len(es) == 0:
            break
        time.sleep(RETRY_INTERVAL)

    es = em_client.engine_list()
    assert len(es) == 0
    return em_client


def wait_for_process_running(client, name, type):
    healthy = False
    for i in range(RETRY_COUNTS):
        if type == INSTANCE_MANAGER_TYPE_ENGINE:
            e = client.engine_get(name)
            state = e.status.process_status.state
        elif type == INSTANCE_MANAGER_TYPE_REPLICA:
            state = client.process_get(name).status.state
        else:
            # invalid type
            assert False

        if state == PROC_STATE_RUNNING:
            healthy = True
            break
        elif state != PROC_STATE_STARTING:
            # invalid state
            assert False
        time.sleep(RETRY_INTERVAL)
    assert healthy


def create_replica_process(client, name, replica_dir="",
                           binary=LONGHORN_BINARY,
                           size=SIZE, port_count=15,
                           port_args=["--listen,localhost:"]):
    if not replica_dir:
        replica_dir = tempfile.mkdtemp()
    client.process_create(
        name=name, binary=binary,
        args=["replica", replica_dir, "--size", str(size)],
        port_count=port_count, port_args=port_args)
    wait_for_process_running(client, name,
                             INSTANCE_MANAGER_TYPE_REPLICA)

    return client.process_get(name)


def create_engine_process(client, name=ENGINE_NAME,
                          volume_name=VOLUME_NAME,
                          binary=LONGHORN_BINARY,
                          listen="", listen_ip="localhost",
                          size=SIZE, frontend="tgt-blockdev",
                          replicas=[], backends=["file"]):
    client.engine_create(
        name=name, volume_name=volume_name,
        binary=binary, listen=listen, listen_ip=listen_ip,
        size=size, frontend=frontend, replicas=replicas,
        backends=backends)
    wait_for_process_running(client, name,
                             INSTANCE_MANAGER_TYPE_ENGINE)

    return client.engine_get(name)


def get_replica_address(r):
    return "localhost:" + str(r.status.port_start)


def cleanup_controller(grpc_client):
    try:
        v = grpc_client.volume_get()
    except grpc.RpcError as grpc_err:
        if "Socket closed" not in grpc_err.details() and \
                "failed to connect to all addresses" not in grpc_err.details():

            raise grpc_err
        return grpc_client

    if v.replicaCount != 0:
        grpc_client.volume_shutdown()
    for r in grpc_client.replica_list():
        grpc_client.replica_delete(r.address)
    return grpc_client


def cleanup_replica(grpc_client):
    r = grpc_client.replica_get()
    if r.state == 'initial':
        return grpc_client
    if r.state == 'closed':
        grpc_client.replica_open()
    grpc_client.replica_delete()
    r = grpc_client.replica_reload()
    assert r.state == 'initial'
    return grpc_client


def random_str():
    return 'random-{0}-{1}'.format(random_num(), int(time.time()))


def random_num():
    return random.randint(0, 1000000)


def create_backend_file():
    name = random_str()
    fo = open(name, "w+")
    fo.truncate(SIZE)
    fo.close()
    return os.path.abspath(name)


def cleanup_backend_file(paths):
    for path in paths:
        if os.path.exists(path):
            os.remove(path)


def wait_for_volume_expansion(grpc_controller_client, size):
    for i in range(RETRY_COUNTS2):
        volume = grpc_controller_client.volume_get()
        if volume.size == size:
            break
        time.sleep(RETRY_INTERVAL)
    assert volume.size == size

    device_path = get_dev_path(volume.name)
    # BLKGETSIZE64, result is bytes as unsigned 64-bit integer (uint64)
    req = 0x80081272
    buf = ' ' * 8
    with open(device_path) as dev:
        buf = fcntl.ioctl(dev.fileno(), req, buf)
    device_size = struct.unpack('L', buf)[0]
    assert device_size == size


def get_dev_path(name):
    return os.path.join("/dev/longhorn/", name)


def get_expansion_snapshot_name():
    return 'expand-{0}'.format(EXPANDED_SIZE_STR)


def get_replica_paths_from_snapshot_name(snap_name):
    replica_paths = []
    cmd = ["find", "/tmp", "-name",
           '*volume-snap-{0}.img'.format(snap_name)]
    snap_paths = subprocess.check_output(cmd).split()
    assert snap_paths
    for p in snap_paths:
        replica_paths.append(os.path.dirname(p.decode('utf-8')))
    return replica_paths


def get_snapshot_file_paths(replica_path, snap_name):
    return os.path.join(replica_path,
                        'volume-snap-{0}.img'.format(snap_name))


def get_replica_head_file_path(replica_dir):
    cmd = ["find", replica_dir, "-name",
           '*volume-head-*.img']
    return subprocess.check_output(cmd).strip()


def wait_for_rebuild_complete(bin, url):
    cmd = [bin, '--url', url, 'replica-rebuild-status']
    completed = 0
    rebuild_status = {}
    for x in range(RETRY_COUNTS2):
        completed = 0
        rebuild_status = json.loads(subprocess.check_output(cmd).strip())
        for rebuild in rebuild_status.values():
            if rebuild['state'] == "complete":
                assert rebuild['progress'] == 100
                assert not rebuild['isRebuilding']
                completed += 1
            elif rebuild['state'] == "":
                assert not rebuild['isRebuilding']
                completed += 1
            # Right now add-replica/rebuild is a blocking call.
            # Hence the state won't become `in_progress` when
            # we check the rebuild status.
            elif rebuild['state'] == "in_progress":
                assert rebuild['state'] == "in_progress"
                assert rebuild['isRebuilding']
            else:
                assert rebuild['state'] == "error"
                assert rebuild['error'] != ""
                assert not rebuild['isRebuilding']
        if completed == len(rebuild_status):
            break
        time.sleep(RETRY_INTERVAL)
    return completed == len(rebuild_status)
