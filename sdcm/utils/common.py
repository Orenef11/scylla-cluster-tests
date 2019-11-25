# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright (c) 2017 ScyllaDB

import os
import logging
import socket
import time
import datetime
import requests
import tempfile
import re
import errno
import threading
import concurrent.futures
import select
import sys
import math
import json
import shutil

from textwrap import dedent
from functools import wraps
from collections import defaultdict
from datetime import timedelta
from enum import Enum

import boto3
from libcloud.compute.providers import get_driver
from libcloud.compute.types import Provider

logger = logging.getLogger('utils')


def _remote_get_hash(remoter, file_path):
    try:
        result = remoter.run('md5sum {}'.format(file_path), verbose=True)
        return result.stdout.strip().split()[0]
    except Exception as details:
        logger.error(str(details))
        return None


def _remote_get_file(remoter, src, dst, user_agent=None):
    cmd = 'curl -L {} -o {}'.format(src, dst)
    if user_agent:
        cmd += ' --user-agent %s' % user_agent
    result = remoter.run(cmd, ignore_status=True)


def remote_get_file(remoter, src, dst, hash_expected=None, retries=1, user_agent=None):
    _remote_get_file(remoter, src, dst, user_agent)
    if not hash_expected:
        return
    while retries > 0 and _remote_get_hash(remoter, dst) != hash_expected:
        _remote_get_file(remoter, src, dst, user_agent)
        retries -= 1
    assert _remote_get_hash(remoter, dst) == hash_expected


class retrying(object):
    """
        Used as a decorator to retry function run that can possibly fail with allowed exceptions list
    """

    def __init__(self, n=3, sleep_time=1, allowed_exceptions=(Exception,), message=""):
        assert n > 0, "Number of retries parameter should be greater then 0 (current: %s)" % n
        self.n = n  # number of times to retry
        self.sleep_time = sleep_time  # number seconds to sleep between retries
        self.allowed_exceptions = allowed_exceptions  # if Exception is not allowed will raise
        self.message = message  # string that will be printed between retries

    def __call__(self, func):
        @wraps(func)
        def inner(*args, **kwargs):
            if self.n == 1:
                # there is no need to retry
                return func(*args, **kwargs)
            for i in xrange(self.n):
                try:
                    if self.message:
                        logger.info("%s [try #%s]" % (self.message, i))
                    return func(*args, **kwargs)
                except self.allowed_exceptions as e:
                    logger.debug("'%s': failed with '%r', retrying [#%s]" % (func.func_name, e, i))
                    time.sleep(self.sleep_time)
                    if i == self.n - 1:
                        logger.error("'%s': Number of retries exceeded!", func.func_name)
                        raise
        return inner


def log_run_info(arg):
    """
        Decorator that prints BEGIN before the function runs and END when function finished running.
        Uses function name as a name of action or string that can be given to the decorator.
        If the function is a method of a class object, the class name will be printed out.

        Usage examples:
            @log_run_info
            def foo(x, y=1):
                pass
            In: foo(1)
            Out:
                BEGIN: foo
                END: foo (ran 0.000164)s

            @log_run_info("Execute nemesis")
            def disrupt():
                pass
            In: disrupt()
            Out:
                BEGIN: Execute nemesis
                END: Execute nemesis (ran 0.000271)s
    """
    def _inner(func, msg=None):
        @wraps(func)
        def inner(*args, **kwargs):
            class_name = ""
            if len(args) > 0 and func.__name__ in dir(args[0]):
                class_name = " <%s>" % args[0].__class__.__name__
            action = "%s%s" % (msg, class_name)
            start_time = datetime.datetime.now()
            logger.debug("BEGIN: %s", action)
            res = func(*args, **kwargs)
            end_time = datetime.datetime.now()
            logger.debug("END: %s (ran %ss)", action, (end_time - start_time).total_seconds())
            return res
        return inner

    if callable(arg):  # when decorator is used without a string message
        return _inner(arg, arg.__name__)
    else:
        return lambda f: _inner(f, arg)


class Distro(Enum):
    UNKNOWN = 0
    CENTOS7 = 1
    RHEL7 = 2
    UBUNTU14 = 3
    UBUNTU16 = 4
    UBUNTU18 = 5
    DEBIAN8 = 6
    DEBIAN9 = 7


def get_data_dir_path(*args):
    import sdcm
    sdcm_path = os.path.realpath(sdcm.__path__[0])
    data_dir = os.path.join(sdcm_path, "../data_dir", *args)
    return os.path.abspath(data_dir)


def get_job_name():
    return os.environ.get('JOB_NAME', 'local_run')


def verify_scylla_repo_file(content, is_rhel_like=True):
    logger.info('Verifying Scylla repo file')
    if is_rhel_like:
        body_prefix = ['#', '[scylla', 'name=', 'baseurl=', 'enabled=', 'gpgcheck=', 'type=',
                       'skip_if_unavailable=', 'gpgkey=', 'repo_gpgcheck=', 'enabled_metadata=']
    else:
        body_prefix = ['#', 'deb']
    for line in content.split('\n'):
        valid_prefix = False
        for prefix in body_prefix:
            if line.startswith(prefix) or len(line.strip()) == 0:
                valid_prefix = True
                break
        logger.debug(line)
        assert valid_prefix, 'Repository content has invalid line: {}'.format(line)


def remove_comments(data):
    """Remove comments line from data

    Remove any string which is start from # in data

    Arguments:
        data {str} -- data expected the command output, file contents
    """
    return '\n'.join([i.strip() for i in data.split('\n') if not i.startswith('#')])


class S3Storage(object):

    bucket_name = 'cloudius-jenkins-test'

    def __init__(self, bucket=None):
        if bucket:
            self.bucket_name = bucket
        self._bucket = boto3.resource('s3').Bucket(name=self.bucket_name)

    def search_by_path(self, path=''):
        files = []
        for obj in self._bucket.objects.filter(Prefix=path):
            files.append(obj.key)
        return files

    def generate_url(self, file_path, dest_dir=''):
        bucket_name = self.bucket_name
        file_name = os.path.basename(os.path.normpath(file_path))
        return "https://{bucket_name}.s3.amazonaws.com/{dest_dir}/{file_name}".format(**locals())

    def upload_file(self, file_path, dest_dir=''):
        try:
            s3_url = self.generate_url(file_path, dest_dir)
            with open(file_path) as fh:
                mydata = fh.read()
                logger.info("Uploading '{file_path}' to {s3_url}".format(**locals()))
                response = requests.put(s3_url, data=mydata)
                logger.debug(response)
                return s3_url if response.ok else ""
        except Exception as e:
            logger.debug("Unable to upload to S3: %s" % e)
            return ""

    def download_file(self, link, dst_dir=""):
        resp = requests.get(link)
        try:
            if resp.status_code == 200:

                file_path = os.path.basename(os.path.dirname(link))

                if dst_dir:
                    dst = os.path.join(dst_dir, file_path)
                else:
                    dst = file_path

                if not os.path.exists(dst):
                    os.mkdir(dst)
                with open(os.path.join(dst, os.path.basename(link)), 'wb') as fh:
                    fh.write(resp.content)
                return os.path.join(os.path.abspath(dst), os.path.basename(link))
        except Exception as details:
            logger.warning("File {} is not downloaded by reason: {}".format(file_path), details)


def get_latest_gemini_version():
    bucket_name = 'downloads.scylladb.com'

    results = S3Storage(bucket_name).search_by_path(path='gemini')
    versions = set()
    for f in results:
        versions.add(f.split('/')[1])

    return str(sorted(versions)[-1])


def list_logs_by_test_id(test_id):
    log_types = ['db-cluster', 'loader-set', 'monitor-set',
                 'prometheus', 'grafana',
                 'job', 'monitoring_data_stack', 'events']
    results = []

    if not test_id:
        return results

    log_files = S3Storage().search_by_path(path=test_id)
    for log_file in log_files:
        for log_type in log_types:
            if log_type in log_file:
                results.append({"file_path": log_file,
                                "type": log_type,
                                "link": "https://{}.s3.amazonaws.com/{}".format(S3Storage.bucket_name, log_file)})
                break

    return results


def restore_monitoring_stack(test_id):
    from sdcm.remote import LocalCmdRunner

    lr = LocalCmdRunner()
    logger.info("Checking that docker is available...")
    result = lr.run('docker ps', ignore_status=True, verbose=False)
    if result.ok:
        logger.info('Docker is available')
    else:
        logger.warning('Docker is not available on your computer. Please install docker software before continue')
        return False

    monitor_stack_base_dir = tempfile.mkdtemp()
    stored_files_by_test_id = list_logs_by_test_id(test_id)
    monitor_stack_archives = []
    for f in stored_files_by_test_id:
        if f['type'] in ['monitoring_data_stack', 'prometheus']:
            monitor_stack_archives.append(f)
    if not monitor_stack_archives or len(monitor_stack_archives) < 2:
        logger.warning('There is no available archive files for monitoring data stack restoring for test id : {}'.format(test_id))
        return False

    for arch in monitor_stack_archives:
        logger.info('Download file {} to directory {}'.format(arch['link'], monitor_stack_base_dir))
        local_path_monitor_stack = S3Storage().download_file(arch['link'], dst_dir=monitor_stack_base_dir)
        monitor_stack_workdir = os.path.dirname(local_path_monitor_stack)
        monitoring_stack_archive_file = os.path.basename(local_path_monitor_stack)
        logger.info('Extracting data from archive {}'.format(arch['file_path']))
        if arch['type'] == 'prometheus':
            monitoring_stack_data_dir = os.path.join(monitor_stack_workdir, 'monitor_data_dir')
            cmd = dedent("""
                mkdir -p {data_dir}
                cd {data_dir}
                cp ../{archive} ./
                tar -xvf {archive}
                chmod -R 777 {data_dir}
                """.format(data_dir=monitoring_stack_data_dir,
                           archive=monitoring_stack_archive_file))
            result = lr.run(cmd, ignore_status=True)
        else:
            branches = re.search('(?P<monitoring_branch>branch-[\d]+\.[\d]+?)_(?P<scylla_version>[\d]+\.[\d]+?)',
                                 monitoring_stack_archive_file)
            monitoring_branch = branches.group('monitoring_branch')
            scylla_version = branches.group('scylla_version')
            cmd = dedent("""
                cd {workdir}
                tar -xvf {archive}
                """.format(workdir=monitor_stack_workdir, archive=monitoring_stack_archive_file))
            result = lr.run(cmd, ignore_status=True)
        if not result.ok:
            logger.warning("During restoring file {} next errors occured:\n {}".format(arch['link'], result))
            return False
        logger.info("Extracting data finished")

    logger.info('Monitoring stack files available {}'.format(monitor_stack_workdir))

    monitoring_dockers_dir = os.path.join(monitor_stack_workdir, 'scylla-monitoring-{}'.format(monitoring_branch))

    def upload_sct_dashboards():
        sct_dashboard_file_name = "scylla-dash-per-server-nemesis.{}.json".format(scylla_version)
        sct_dashboard_file = os.path.join(monitoring_dockers_dir, 'sct_monitoring_addons', sct_dashboard_file_name)
        if not os.path.exists(sct_dashboard_file):
            logger.info('There is no dashboard {}. Skip load dashboard'.format(sct_dashboard_file_name))
            return False

        dashboard_url = 'http://localhost:3000/api/dashboards/db'
        with open(sct_dashboard_file, "r") as f:
            dashboard_config = json.load(f)

        res = requests.post(dashboard_url, data=json.dumps(dashboard_config), headers={'Content-Type': 'application/json'})
        if res.status_code != 200:
            logger.info('Error uploading dashboard {}. Error message {}'.format(sct_dashboard_file, res.text))
            return False
        logger.info('Dashboard {} loaded successfully'.format(sct_dashboard_file))

    def upload_annotations():
        annotations_file = os.path.join(monitoring_dockers_dir, 'sct_monitoring_addons', 'annotations.json')
        if not os.path.exists(annotations_file):
            logger.info('There is no annotations file.Skip loading annotations')
            return False

        with open(annotations_file, "r") as f:
            annotations = json.load(f)

        annotations_url = "http://localhost:3000/api/annotations"
        for an in annotations:
            res = requests.post(annotations_url, data=json.dumps(an), headers={'Content-Type': 'application/json'})
            if res.status_code != 200:
                logger.info('Error during uploading annotation {}. Error message {}'.format(an, res.text))
                return False
        logger.info('Annotations loaded successfully')

    @retrying(n=3, sleep_time=1, message='Start docker containers')
    def start_dockers(monitoring_dockers_dir, monitoring_stack_data_dir, scylla_version):
        lr.run('cd {}; ./kill-all.sh'.format(monitoring_dockers_dir))
        cmd = dedent("""cd {monitoring_dockers_dir};
                ./start-all.sh \
                -s {monitoring_dockers_dir}/config/scylla_servers.yml \
                -n {monitoring_dockers_dir}/config/node_exporter_servers.yml \
                -d {monitoring_stack_data_dir} -v {scylla_version}""".format(**locals()))
        res = lr.run(cmd, ignore_status=True)
        if res.ok:
            r = lr.run('docker ps')
            logger.info(r.stdout.encode('utf-8'))
            return True
        else:
            raise Exception('dockers start failed. {}'.format(res))

    status = False
    status = start_dockers(monitoring_dockers_dir, monitoring_stack_data_dir, scylla_version)
    upload_sct_dashboards()
    upload_annotations()
    return status


aws_regions = ['us-east-1', 'eu-west-1', 'us-west-2']
gce_regions = ['us-east1-b', 'us-west1-b', 'us-east4-b']


def clean_cloud_instances(tags_dict):
    """
    Remove all instances with specific tags from both AWS/GCE

    :param tags_dict: a dict of the tag to select the instances, e.x. {"TestId": "9bc6879f-b1ef-47e1-99ab-020810aedbcc"}
    :return: None
    """
    clean_instances_aws(tags_dict)
    clean_instances_gce(tags_dict)


def clean_instances_aws(tags_dict):
    """
    Remove all instances with specific tags AWS

    :param tags_dict: a dict of the tag to select the instances, e.x. {"TestId": "9bc6879f-b1ef-47e1-99ab-020810aedbcc"}
    :return: None
    """
    for region in aws_regions:
        logger.info('going to cleanup aws region=%s', region)
        client = boto3.client('ec2', region_name=region)
        custom_filter = [{'Name': 'tag:{}'.format(key), 'Values': [value]} for key, value in tags_dict.items()]

        response = client.describe_instances(Filters=custom_filter)
        instance_ids = [instance['InstanceId'] for reservation in response['Reservations'] for instance in reservation['Instances']]

        if instance_ids:
            logger.info("going to delete instance_ids={}".format(instance_ids))
            response = client.terminate_instances(InstanceIds=instance_ids)
            for instance in response['TerminatingInstances']:
                assert instance['CurrentState']['Name'] in ['terminated', 'shutting-down']


def clean_instances_gce(tags_dict):
    """
    Remove all instances with specific tags GCE

    :param tags_dict: a dict of the tag to select the instances, e.x. {"TestId": "9bc6879f-b1ef-47e1-99ab-020810aedbcc"}
    :return: None
    """

    # avoid cyclic dependency issues, since too many things import utils.py
    from sdcm.keystore import KeyStore

    gcp_credentials = KeyStore().get_gcp_credentials()
    compute_engine = get_driver(Provider.GCE)
    for region in gce_regions:
        logger.info('going to cleanup gce region=%s', region)
        driver = compute_engine(gcp_credentials["project_id"] + "@appspot.gserviceaccount.com",
                                gcp_credentials["private_key"],
                                datacenter=region,
                                project=gcp_credentials["project_id"])

        for node in driver.list_nodes():
            tags = node.extra['metadata'].get('items', [])
            # since libcloud list_nodes() doesn't offer any filtering
            # we go over all the expected tags, and check if they exist in on the node/instance
            # if all of them exists we delete that node
            if all([any([t['key'] == key and t['value'] == value for t in tags]) for key, value in tags_dict.items()]):
                if not node.state == 'STOPPED':
                    logger.info("going to delete: {}".format(node.name))
                    res = driver.destroy_node(node)
                    logger.info("{} deleted. res={}".format(node.name, res))


def list_instances_aws(tags_dict, region_name=None):
    """
    list all instances with specific tags AWS

    :param tags_dict: a dict of the tag to select the instances, e.x. {"TestId": "9bc6879f-b1ef-47e1-99ab-020810aedbcc"}
    :param region_name: name of the region to list

    :return: None
    """
    instances = []
    _aws_regions = [region_name] if region_name else aws_regions
    for region in _aws_regions:
        logger.info('going to list aws region=%s', region)
        client = boto3.client('ec2', region_name=region)
        custom_filter = [{'Name': 'tag:{}'.format(key), 'Values': [value]} for key, value in tags_dict.items()]

        response = client.describe_instances(Filters=custom_filter)
        instances += [instance for reservation in response['Reservations'] for instance in reservation['Instances']]

    return instances


def list_instances_gce(tags_dict, region_name=None):
    """
    list all instances with specific tags GCE

    :param tags_dict: a dict of the tag to select the instances, e.x. {"TestId": "9bc6879f-b1ef-47e1-99ab-020810aedbcc"}
    :param region_name: name of the region to list

    :return: None
    """

    # avoid cyclic dependency issues, since too many things import utils.py
    from sdcm.keystore import KeyStore

    instances = []
    gcp_credentials = KeyStore().get_gcp_credentials()
    compute_engine = get_driver(Provider.GCE)

    _gce_regions = [region_name] if region_name else gce_regions
    for region in _gce_regions:
        logger.info('going to list gce region=%s', region)
        driver = compute_engine(gcp_credentials["project_id"] + "@appspot.gserviceaccount.com",
                                gcp_credentials["private_key"],
                                datacenter=region,
                                project=gcp_credentials["project_id"])

        for node in driver.list_nodes():
            tags = node.extra['metadata'].get('items', [])
            # since libcloud list_nodes() doesn't offer any filtering
            # we go over all the expected tags, and check if they exist in on the node/instance
            # if all of them exists we delete that node
            if all([any([t['key'] == key and t['value'] == value for t in tags]) for key, value in tags_dict.items()]):
                if not node.state == 'STOPPED':
                    instances += [node]
    return instances


_scylla_ami_cache = defaultdict(dict)


def get_scylla_ami_versions(region):
    """
    get the list of all the formal scylla ami from specific region

    :param region: the aws region to look in
    :return: list of ami data
    :rtype: list
    """

    if _scylla_ami_cache[region]:
        return _scylla_ami_cache[region]

    EC2 = boto3.client('ec2', region_name=region)
    response = EC2.describe_images(
        Owners=['797456418907'],  # ScyllaDB
        Filters=[
            {'Name': 'name', 'Values': ['ScyllaDB *']},
        ],
    )

    _scylla_ami_cache[region] = sorted(response['Images'],
                                       key=lambda x: x['CreationDate'],
                                       reverse=True)

    return _scylla_ami_cache[region]


_s3_scylla_repos_cache = defaultdict(dict)


def get_s3_scylla_repos_mapping(dist_type='centos', dist_version=None):
    """
    get the mapping from version prefixes to rpm .repo or deb .list files locations

    :param dist_type: which distro to look up centos/ubuntu/debian
    :param dist_version: famaily name of the distro version

    :return: a mapping of versions prefixes to repos
    :rtype: dict
    """
    if (dist_type, dist_version) in _s3_scylla_repos_cache:
        return _s3_scylla_repos_cache[(dist_type, dist_version)]

    s3_client = boto3.client('s3')
    bucket = 'downloads.scylladb.com'

    if dist_type == 'centos':
        response = s3_client.list_objects(Bucket=bucket, Prefix='rpm/centos/',  Delimiter='/')

        for f in response['Contents']:
            filename = os.path.basename(f['Key'])
            # only if path look like 'rpm/centos/scylla-1.3.repo', we deem it formal one
            if filename.startswith('scylla-') and filename.endswith('.repo'):
                version_prefix = filename.replace('.repo', '').split('-')[-1]
                _s3_scylla_repos_cache[(dist_type, dist_version)][version_prefix] = "https://s3.amazonaws.com/{bucket}/{path}".format(bucket=bucket, path=f['Key'])

    elif dist_type == 'ubuntu' or dist_type == 'debian':
        response = s3_client.list_objects(Bucket=bucket, Prefix='deb/{}/'.format(dist_type),  Delimiter='/')
        for f in response['Contents']:
            filename = os.path.basename(f['Key'])

            # only if path look like 'deb/debian/scylla-3.0-jessie.list', we deem it formal one
            if filename.startswith('scylla-') and filename.endswith('-{}.list'.format(dist_version)):

                version_prefix = filename.replace('-{}.list'.format(dist_version), '').split('-')[-1]
                _s3_scylla_repos_cache[(dist_type, dist_version)][version_prefix] = "https://s3.amazonaws.com/{bucket}/{path}".format(bucket=bucket, path=f['Key'])

    else:
        raise NotImplementedError("[{}] is not yet supported".format(dist_type))
    return _s3_scylla_repos_cache[(dist_type, dist_version)]


def pid_exists(pid):
    """
    Return True if a given PID exists.

    :param pid: Process ID number.
    """
    try:
        os.kill(pid, 0)
    except OSError as detail:
        if detail.errno == errno.ESRCH:
            return False
    return True


def safe_kill(pid, signal):
    """
    Attempt to send a signal to a given process that may or may not exist.

    :param signal: Signal number.
    """
    try:
        os.kill(pid, signal)
        return True
    except Exception:
        return False


class FileFollowerIterator(object):
    def __init__(self, filename, thread_obj):
        self.filename = filename
        self.thread_obj = thread_obj

    def __iter__(self):
        with open(self.filename, 'r') as input_file:
            line = ''
            while not self.thread_obj.stopped():
                poller = select.poll()  # pylint: disable=no-member
                poller.register(input_file, select.POLLIN)  # pylint: disable=no-member
                if poller.poll(100):
                    line += input_file.readline()
                if not line or not line.endswith('\n'):
                    time.sleep(0.1)
                    continue

                yield line
                line = ''
            yield line


class FileFollowerThread(object):
    def __init__(self):
        self.executor = concurrent.futures.ThreadPoolExecutor(1)
        self._stop_event = threading.Event()
        self.future = None

    def __enter__(self):
        self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def run(self):
        raise NotImplementedError()

    def start(self):
        self.future = self.executor.submit(self.run)
        return self.future

    def stop(self):
        self._stop_event.set()

    def stopped(self):
        return self._stop_event.is_set()

    def follow_file(self, filename):
        return FileFollowerIterator(filename, self)


class ScyllaCQLSession(object):
    def __init__(self, session, cluster):
        self.session = session
        self.cluster = cluster

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cluster.shutdown()


class MethodVersionNotFound(Exception):
    pass


class version(object):
    VERSIONS = {}
    """
        Runs a method according to the version attribute of the class method
        Limitations: currently, can't work if the same method name in the same file used in different
                     classes
        Example:
                In [3]: class VersionedClass(object):
                   ...:     def __init__(self, current_version):
                   ...:         self.version = current_version
                   ...:
                   ...:     @version("1.2")
                   ...:     def setup(self):
                   ...:         return "1.2"
                   ...:
                   ...:     @version("2")
                   ...:     def setup(self):
                   ...:         return "2"

                In [4]: vc = VersionedClass("2")

                In [5]: vc.setup()
                Out[5]: '2'

                In [6]: vc = VersionedClass("1.2")

                In [7]: vc.setup()
                Out[7]: '1.2'
    """

    def __init__(self, ver):
        self.version = ver

    def __call__(self, func):
        self.VERSIONS[(self.version, func.func_name, func.func_code.co_filename)] = func

        @wraps(func)
        def inner(*args, **kwargs):
            cls_self = args[0]
            func_to_run = self.VERSIONS.get((cls_self.version, func.func_name, func.func_code.co_filename))
            if func_to_run:
                return func_to_run(*args, **kwargs)
            else:
                raise MethodVersionNotFound("Method '{}' with version '{}' not defined in '{}'!".format(
                    func.func_name,
                    cls_self.version,
                    cls_self.__class__.__name__))
        return inner


def get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('', 0))
    addr = s.getsockname()
    port = addr[1]
    s.close()
    return port


def get_my_ip():
    hostname = socket.gethostname()
    ip = socket.gethostbyname(hostname)
    return ip


def get_branched_ami(ami_version, region_name):
    """
    Get a list of AMIs, based on version match

    :param ami_version: branch version to look for, ex. 'branch-2019.1:latest', 'branch-3.1:all'
    :param region_name: the region to look AMIs in
    :return: list of ec2.images
    """
    branch, build_id = ami_version.split(':')
    ec2 = boto3.resource('ec2', region_name=region_name)

    logger.info("Looking for AMI match [%s]", ami_version)
    if build_id == 'latest' or build_id == 'all':
        filters = [{'Name': 'tag:branch', 'Values': [branch]}]
    else:
        filters = [{'Name': 'tag:branch', 'Values': [branch]}, {'Name': 'tag:build-id', 'Values': [build_id]}]

    amis = list(ec2.images.filter(Filters=filters))

    amis = sorted(amis, key=lambda x: x.creation_date, reverse=True)

    assert amis, "AMI matching [{}] wasn't found on {}".format(ami_version, region_name)
    if build_id == 'all':
        return amis
    else:
        return amis[:1]


def get_non_system_ks_cf_list(loader_node, db_node, request_timeout=300, filter_out_table_with_counter=False,
                              filter_out_mv=False):
    """Get all not system keyspace.tables pairs

    Arguments:
        loader_node {BaseNode} -- LoaderNoder to send request
        db_node_ip {str} -- ip of db_node
    """
    def get_tables_columns_list(entity_type):
        if entity_type == 'view':
            cmd = "paging off; SELECT keyspace_name, view_name FROM system_schema.views"
        else:
            cmd = "paging off; SELECT keyspace_name, table_name, type FROM system_schema.columns"
        result = loader_node.run_cqlsh(cmd=cmd, timeout=request_timeout, verbose=False, target_db_node=db_node,
                                       split=True, connect_timeout=request_timeout)
        if not result:
            return []

        splitter_result = []
        for row in result[4:]:
            if '|' not in row:
                continue
            if row.startswith('system'):
                continue
            splitter_result.append(row.split('|'))
        return splitter_result

    views_list = set()
    if filter_out_mv:
        tables = get_tables_columns_list('view')

        for table in tables:
            views_list.add('.'.join([name.strip() for name in table[:2]]))
        views_list = list(views_list)

    result = get_tables_columns_list('column')
    if not result:
        return []

    avaialable_ks_cf = defaultdict(list)
    for row in result:
        ks_cf_name = '.'.join([name.strip() for name in row[:2]])

        if filter_out_mv and ks_cf_name in views_list:
            continue

        column_type = row[2].strip()
        avaialable_ks_cf[ks_cf_name].append(column_type)

    if filter_out_table_with_counter:
        for ks_cf, column_types in avaialable_ks_cf.items():
            if 'counter' in column_types:
                avaialable_ks_cf.pop(ks_cf)
    return avaialable_ks_cf.keys()


def remove_files(path):
    logger.debug("Remove path %s", path)
    try:
        if os.path.isdir(path):
            shutil.rmtree(path=path, ignore_errors=True)
        if os.path.isfile(path):
            os.remove(path)
    except Exception as details:
        logger.error("Error during remove archived logs %s", details)


def format_timestamp(timestamp):
    return datetime.datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')


def makedirs(path):
    """

    TODO: when move to python3, this function will be replaced
    with os.makedirs function:
        os.makedirs(name, mode=0o777, exist_ok=False)

    """
    try:
        os.makedirs(path)
    except OSError:
        if os.path.exists(path):
            return
        raise


def wait_ami_available(client, ami_id):
    """Wait while ami_id become available

    Wait while ami_id become available, after
    10 minutes return an error

    Arguments:
        client {boto3.EC2.Client} -- client of EC2 service
        ami_id {str} -- ami id to check availability
    """
    waiter = client.get_waiter('image_available')
    waiter.wait(ImageIds=[ami_id],
                WaiterConfig={
                    'Delay': 30,
                    'MaxAttempts': 20}
                )


def update_certificates():
    """
    Update the certificate of server encryption, which might be expired.
    """
    try:
        from sdcm.remote import LocalCmdRunner
        localrunner = LocalCmdRunner()
        localrunner.run('openssl x509 -req -in data_dir/ssl_conf/example/db.csr -CA data_dir/ssl_conf/cadb.pem -CAkey data_dir/ssl_conf/example/cadb.key -CAcreateserial -out data_dir/ssl_conf/db.crt -days 365')
        localrunner.run('openssl x509 -enddate -noout -in data_dir/ssl_conf/db.crt')
    except Exception as ex:
        raise Exception('Failed to update certificates by openssl: %s' % ex)