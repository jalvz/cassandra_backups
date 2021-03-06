from __future__ import (absolute_import, print_function)

import time
import json
import logging
from distutils.util import strtobool
from boto.s3.connection import S3Connection
from boto.s3.key import Key
from boto.exception import S3ResponseError
from datetime import datetime
from fabric.api import (env, execute, hide, run, sudo)
from fabric.context_managers import settings
from fabric.operations import local

from cassandra_backups.utils import get_s3_connection_host, nice_local


class Snapshot(object):
    """
    A Snapshot instance keeps the details about a cassandra snapshot

    Multiple snapshots can be stored in a single S3 bucket

    A Snapshot is best described by:
        - its name (which defaults to the utc time of creation)
        - the list of hostnames the snapshot runs on
        - the list of keyspaces being backed up
        - the keyspace table being backed up
        - the S3 bucket's base path where the snapshot is stored

    Snapshots data (and incremental backups) are stored using the
    following convention:

        s3_bucket_name:/<base_path>/<snapshot_name>/<node-hostname>/...

    Snapshots are represented on S3 by their manifest file, this makes
    incremental backups much easier
    """

    SNAPSHOT_TIMESTAMP_FORMAT = '%Y%m%d'

    def __init__(self, base_path, s3_bucket, hosts, keyspaces, table):
        self.s3_bucket = s3_bucket
        self.name = self.make_snapshot_name()
        self.hosts = hosts
        self.keyspaces = keyspaces
        self.table = table
        self._base_path = base_path

    def dump_manifest_file(self):
        manifest_data = {
            'name': self.name,
            'base_path': self._base_path,
            'hosts': self.hosts,
            'keyspaces': self.keyspaces,
            'table': self.table
        }
        return json.dumps(manifest_data)

    @staticmethod
    def load_manifest_file(data, s3_bucket):
        manifest_data = json.loads(data)
        snapshot = Snapshot(
            base_path=manifest_data['base_path'],
            s3_bucket=s3_bucket,
            hosts=manifest_data['hosts'],
            keyspaces=manifest_data['keyspaces'],
            table=manifest_data['table']
        )
        snapshot.name = manifest_data['name']
        return snapshot

    @property
    def base_path(self):
        return '/'.join([self._base_path, self.name])

    @staticmethod
    def make_snapshot_name():
        return datetime.utcnow().strftime(Snapshot.SNAPSHOT_TIMESTAMP_FORMAT)

    def unix_time_name(self):
        dt = datetime.strptime(self.name, self.SNAPSHOT_TIMESTAMP_FORMAT)
        return time.mktime(dt.timetuple()) * 1000

    def __cmp__(self, other):
        return self.unix_time_name() - other.unix_time_name()

    def __repr__(self):
        return self.name

    __str__ = __repr__


class RestoreWorker(object):
    def __init__(self, aws_access_key_id, aws_secret_access_key, s3_bucket_region, snapshot,
                 cassandra_tools_bin_dir, restore_dir, use_sudo, use_local):
        self.aws_secret_access_key = aws_secret_access_key
        self.aws_access_key_id = aws_access_key_id
        self.s3_host = get_s3_connection_host(s3_bucket_region)
        self.snapshot = snapshot
        self.cassandra_tools_bin_dir = cassandra_tools_bin_dir
        self.restore_dir = restore_dir
        self.use_sudo = use_sudo
        self.use_local = use_local

    def restore(self, keyspace):

        logging.info("Restoring keyspace=%(keyspace)s to host %(host)s ,\
            " % dict(keyspace=keyspace, host=env.host_string))

        restore_command = "cassandra-backups-agent " \
                          "fetch " \
                          "--keyspace=%(keyspace)s " \
                          "--snapshot-path=%(snapshot_path)s " \
                          "--aws-access-key-id=%(aws_access_key_id)s " \
                          "--aws-secret-access-key=%(aws_secret_access_key)s  " \
                          "--s3-host=%(s3_host)s  " \
                          "--s3-bucket-name=%(s3_bucket_name)s " \
                          "--host=%(host)s " \
                          "--cassandra-tools-bin-dir=%(cassandra_tools_bin_dir)s " \
                          "--restore-dir=%(restore_dir)s "

        cmd = restore_command % dict(
            keyspace=keyspace,
            snapshot_path=self.snapshot.base_path,
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
            s3_host=self.s3_host,
            s3_bucket_name=self.snapshot.s3_bucket,
            host=env.host_string,
            cassandra_tools_bin_dir=self.cassandra_tools_bin_dir,
            restore_dir=self.restore_dir,
        )

        if self.use_local and self.use_sudo:
            local("sudo " + cmd)
        elif self.use_local:
            local(cmd)
        elif self.use_sudo:
            sudo(cmd)
        else:
            run(cmd)


class BackupWorker(object):
    """
    Backup process is split in this steps:
        - requests cassandra to create new backups
        - uploads backup files to S3
        - clears backup files from nodes
        - updates backup meta information

    When performing a new snapshot the manifest of the snapshot is
    uploaded to S3 for later use.

    Snapshot's manifest path:
    /<snapshot_base_path>/<snapshot_name>/manifest.json

    Every time a backup is done a description of the current ring is
    saved next to the snapshot manifest file

    """

    def __init__(self, aws_secret_access_key,
                 aws_access_key_id, s3_bucket_region, s3_ssenc,
                 s3_connection_host, cassandra_conf_path, use_sudo, use_local,
                 cassandra_tools_bin_dir, cqlsh_user, cqlsh_password,
                 backup_schema, buffer_size, exclude_tables, rate_limit, quiet, nice,
                 connection_pool_size=12, reduced_redundancy=False):
        self.aws_secret_access_key = aws_secret_access_key
        self.aws_access_key_id = aws_access_key_id
        self.s3_bucket_region = s3_bucket_region
        self.s3_ssenc = s3_ssenc
        self.s3_connection_host = s3_connection_host
        self.cassandra_conf_path = cassandra_conf_path
        self.nodetool_path = "{!s}/nodetool".format(cassandra_tools_bin_dir)
        self.cqlsh_path = "{!s}/cqlsh".format(cassandra_tools_bin_dir)
        self.cqlsh_user = cqlsh_user
        self.cqlsh_password = cqlsh_password
        self.backup_schema = backup_schema
        self.connection_pool_size = connection_pool_size
        self.buffer_size = buffer_size
        self.reduced_redundancy = reduced_redundancy
        self.rate_limit = rate_limit
        self.quiet = quiet
        self.nice = nice
        if isinstance(use_sudo, basestring):
            self.use_sudo = bool(strtobool(use_sudo))
        else:
            self.use_sudo = use_sudo
        if isinstance(use_local, basestring):
            self.use_local = bool(strtobool(use_local))
        else:
            self.use_local = use_local
        self.exclude_tables = exclude_tables

    def execute_cmd(self, cmd):
        if self.use_local and self.use_sudo:
            return nice_local('sudo ' + cmd, nice=self.nice)
        elif self.use_local:
            return nice_local(cmd, nice=self.nice)
        elif self.use_sudo:
            return sudo(cmd)
        else:
            return run(cmd)

    def get_current_node_hostname(self):
        return env.host_string

    def upload_node_backups(self, snapshot, incremental_backups):
        prefix = '/'.join(snapshot.base_path.split('/') + [self.get_current_node_hostname()])

        manifest_path = '/tmp/backupmanifest'
        manifest_command = "cassandra-backups-agent " \
                           "%(incremental_backups)s create-upload-manifest " \
                           "--manifest_path=%(manifest_path)s " \
                           "--snapshot_name=%(snapshot_name)s " \
                           "--snapshot_keyspaces=%(snapshot_keyspaces)s " \
                           "--snapshot_table=%(snapshot_table)s " \
                           "--conf_path=%(conf_path)s " \
                           "--exclude_tables=%(exclude_tables)s"
        cmd = manifest_command % dict(
            manifest_path=manifest_path,
            snapshot_name=snapshot.name,
            snapshot_keyspaces=','.join(snapshot.keyspaces or ''),
            snapshot_table=snapshot.table,
            conf_path=self.cassandra_conf_path,
            exclude_tables=self.exclude_tables,
            incremental_backups=incremental_backups and '--incremental_backups' or ''
        )

        self.execute_cmd(cmd)

        upload_command = "cassandra-backups-agent %(incremental_backups)s " \
                         "put " \
                         "--s3-bucket-name=%(bucket)s " \
                         "--s3-bucket-region=%(s3_bucket_region)s %(s3_ssenc)s " \
                         "--s3-base-path=%(prefix)s " \
                         "--manifest=%(manifest)s " \
                         "--bufsize=%(bufsize)s " \
                         "--concurrency=4"

        if self.reduced_redundancy:
            upload_command += " --reduced-redundancy"

        if self.rate_limit > 0:
            upload_command += " --rate-limit=%(rate_limit)s"

        if self.quiet:
            upload_command += " --quiet"

        if self.aws_access_key_id and self.aws_secret_access_key:
            upload_command += " --aws-access-key-id=%(key)s " \
                              "--aws-secret-access-key=%(secret)s"

        cmd = upload_command % dict(
            bucket=snapshot.s3_bucket,
            s3_bucket_region=self.s3_bucket_region,
            s3_ssenc=self.s3_ssenc and '--s3-ssenc' or '',
            prefix=prefix,
            key=self.aws_access_key_id,
            secret=self.aws_secret_access_key,
            manifest=manifest_path,
            bufsize=self.buffer_size,
            rate_limit=self.rate_limit,
            incremental_backups=incremental_backups and '--incremental_backups' or ''
        )

        self.execute_cmd(cmd)

    def snapshot(self, snapshot):
        """
        Perform a snapshot
        """
        logging.info("Create {!r} snapshot".format(snapshot))
        try:
            self.start_cluster_backup(snapshot, incremental_backups=False)
            self.upload_cluster_backups(snapshot, incremental_backups=False)
        finally:
            self.clear_cluster_snapshot(snapshot)
        self.write_ring_description(snapshot)
        self.write_snapshot_manifest(snapshot)
        if self.backup_schema:
            self.write_schema(snapshot)

    def update_snapshot(self, snapshot):
        """Updates backup data changed since :snapshot was done"""
        logging.info("Update {!r} snapshot".format(snapshot))
        self.start_cluster_backup(snapshot, incremental_backups=True)
        self.upload_cluster_backups(snapshot, incremental_backups=True)
        self.write_ring_description(snapshot)
        if self.backup_schema:
            self.write_schema(snapshot)

    def get_ring_description(self):
        with settings(host_string=env.hosts[0]):
            with hide('output'):
                ring_description = self.execute_cmd(self.nodetool_path + ' ring')
        return ring_description

    def get_keyspace_schema(self, keyspace=None):
        if self.cqlsh_user and self.cqlsh_password:
            auth = "-u {!s} -p {!s}".format(self.cqlsh_user, self.cqlsh_password)
        else:
            auth = ""
        with settings(host_string=env.hosts[0]):
            with hide('output'):
                cmd = "{!s} {!s} -e 'DESCRIBE SCHEMA;'".format(
                    self.cqlsh_path, auth)
                if keyspace:
                    cmd = "{!s} -k {!s} {!s} -e 'DESCRIBE KEYSPACE {!s};'".format(
                        self.cqlsh_path, keyspace, auth, keyspace)
                output = self.execute_cmd(cmd)
        return output

    def write_on_S3(self, bucket_name, path, content):
        conn = S3Connection(
            self.aws_access_key_id,
            self.aws_secret_access_key,
            host=self.s3_connection_host)
        bucket = conn.get_bucket(bucket_name, validate=False)
        key = bucket.new_key(path)
        key.set_contents_from_string(content)

    def write_ring_description(self, snapshot):
        logging.info("Writing ring description")
        content = self.get_ring_description()
        ring_path = '/'.join([snapshot.base_path, 'ring'])
        self.write_on_S3(snapshot.s3_bucket, ring_path, content)

    def write_schema(self, snapshot):
        if snapshot.keyspaces:
            for ks in snapshot.keyspaces:
                logging.info("Writing schema for keyspace {!s}".format(ks))
                content = self.get_keyspace_schema(ks)
                schema_path = '/'.join(
                    [snapshot.base_path, "schema_{!s}.cql".format(ks)])
                self.write_on_S3(snapshot.s3_bucket, schema_path, content)
        else:
            logging.info("Writing schema for all keyspaces")
            content = self.get_keyspace_schema()
            schema_path = '/'.join([snapshot.base_path, "schema.cql"])
            self.write_on_S3(snapshot.s3_bucket, schema_path, content)

    def write_snapshot_manifest(self, snapshot):
        content = snapshot.dump_manifest_file()
        manifest_path = '/'.join([snapshot.base_path, 'manifest.json'])
        self.write_on_S3(snapshot.s3_bucket, manifest_path, content)

    def start_cluster_backup(self, snapshot, incremental_backups=False):
        logging.info("Creating snapshots")
        with settings(parallel=True, pool_size=self.connection_pool_size):
            execute(self.node_start_backup, snapshot, incremental_backups)

    def node_start_backup(self, snapshot, incremental_backups):
        """Runs snapshot command on a cassandra node"""

        def hide_exec_cmd(cmd):
            with hide('running', 'stdout', 'stderr'):
                self.execute_cmd(cmd)

        if incremental_backups:
            backup_command = "%(nodetool)s flush %(keyspace)s %(tables)s"

            if snapshot.keyspaces:
                # flush can only take one keyspace at a time.
                for keyspace in snapshot.keyspaces:
                    cmd = backup_command % dict(
                        nodetool=self.nodetool_path,
                        keyspace=keyspace,
                        tables=snapshot.table or ''
                    )
                    hide_exec_cmd(cmd)
            else:
                # If no keyspace then can't provide a table either.
                cmd = backup_command % dict(
                    nodetool=self.nodetool_path,
                    keyspace='',
                    tables=''
                )
                hide_exec_cmd(cmd)

        else:
            backup_command = "%(nodetool)s snapshot %(table_param)s \
                -t %(snapshot)s %(keyspaces)s"

            if snapshot.table:
                # Only one keyspace can be specified along with a column family.
                table_param = "-cf {!s}".format(snapshot.table)
                for keyspace in snapshot.keyspaces:
                    cmd = backup_command % dict(
                        nodetool=self.nodetool_path,
                        table_param=table_param,
                        snapshot=snapshot.name,
                        keyspaces=keyspace
                    )
                    hide_exec_cmd(cmd)
            else:
                cmd = backup_command % dict(
                    nodetool=self.nodetool_path,
                    table_param='',
                    snapshot=snapshot.name,
                    keyspaces=' '.join(snapshot.keyspaces or '')
                )
                hide_exec_cmd(cmd)

    def upload_cluster_backups(self, snapshot, incremental_backups):
        logging.info("Uploading backups")
        with settings(parallel=True, pool_size=self.connection_pool_size):
            execute(self.upload_node_backups, snapshot, incremental_backups)

    def clear_cluster_snapshot(self, snapshot):
        logging.info("Clearing snapshots")
        with settings(parallel=True, pool_size=self.connection_pool_size):
            execute(self.clear_node_snapshot, snapshot)

    def clear_node_snapshot(self, snapshot):
        """Cleans up snapshots from a cassandra node"""
        clear_command = '%(nodetool)s clearsnapshot -t "%(snapshot)s"'
        cmd = clear_command % dict(
            nodetool=self.nodetool_path,
            snapshot=snapshot.name
        )
        self.execute_cmd(cmd)


class SnapshotCollection(object):
    def __init__(
            self, aws_access_key_id,
            aws_secret_access_key, base_path, s3_bucket, s3_connection_host):
        self.s3_connection_host = s3_connection_host
        self.s3_bucket = s3_bucket
        self.base_path = base_path
        self.snapshots = None
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key

    def _read_s3(self):
        if self.snapshots:
            return

        conn = S3Connection(self.aws_access_key_id, self.aws_secret_access_key, host=self.s3_connection_host)
        bucket = conn.get_bucket(self.s3_bucket, validate=False)
        self.snapshots = []
        prefix = self.base_path
        if not self.base_path.endswith('/'):
            prefix = "{!s}/".format(self.base_path)
        snap_paths = [snap.name for snap in bucket.list(
            prefix=prefix, delimiter='/')]
        # Remove the root dir from the list since it won't have a manifest file.
        snap_paths = [x for x in snap_paths if x != prefix]
        for snap_path in snap_paths:
            mkey = Key(bucket)
            manifest_path = '/'.join([snap_path, 'manifest.json'])
            mkey.key = manifest_path
            try:
                manifest_data = mkey.get_contents_as_string()
            except S3ResponseError as e:  # manifest.json not found.
                logging.warn("Response: {!r} manifest_path: {!r}".format(
                    e.message, manifest_path))
                continue
            try:
                self.snapshots.append(
                    Snapshot.load_manifest_file(manifest_data, self.s3_bucket))
            except Exception as e:  # Invalid json format.
                logging.error("Parsing manifest.json failed. {!r}".format(
                    e.message))
                continue
        self.snapshots = sorted(self.snapshots, reverse=True)

    def get_snapshot_by_name(self, name):
        snapshots = filter(lambda s: s.name == name, self)
        return snapshots and snapshots[0]

    def get_latest(self):
        self._read_s3()
        return self.snapshots[0]

    def get_snapshot_for(self, hosts, keyspaces, table, name):
        """Returns the most recent compatible snapshot"""
        for snapshot in self:
            if snapshot.hosts != hosts:
                continue
            if snapshot.keyspaces != keyspaces:
                continue
            if snapshot.table != table:
                continue
            if snapshot.name != name:
                continue
            return snapshot

    def __iter__(self):
        self._read_s3()
        return iter(self.snapshots)
