# vim: tabstop=4 shiftwidth=4 softtabstop=4
# coding=utf-8
#
# Copyright (c) 2012 NTT DOCOMO, INC
# Copyright (c) 2011 University of Southern California / ISI
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
A driver for Bare-metal platform.
"""

from oslo.config import cfg

from nova.compute import power_state
from nova import context as nova_context
from nova import exception
from nova.openstack.common import excutils
from nova.openstack.common import importutils
from nova.openstack.common import log as logging
from nova import paths
from nova.virt.baremetal import baremetal_states
from nova.virt.baremetal import db
from nova.virt import driver
from nova.virt import firewall
from nova.virt.libvirt import imagecache

opts = [
    cfg.BoolOpt('inject_password',
                default=True,
                help='Whether baremetal compute injects password or not'),
    cfg.StrOpt('injected_network_template',
               default=paths.basedir_def('nova/virt/'
                                         'baremetal/interfaces.template'),
               help='Template file for injected network'),
    cfg.StrOpt('vif_driver',
               default='nova.virt.baremetal.vif_driver.BareMetalVIFDriver',
               help='Baremetal VIF driver.'),
    cfg.StrOpt('volume_driver',
               default='nova.virt.baremetal.volume_driver.LibvirtVolumeDriver',
               help='Baremetal volume driver.'),
    cfg.ListOpt('instance_type_extra_specs',
               default=[],
               help='a list of additional capabilities corresponding to '
               'instance_type_extra_specs for this compute '
               'host to advertise. Valid entries are name=value, pairs '
               'For example, "key1:val1, key2:val2"'),
    cfg.StrOpt('driver',
               default='nova.virt.baremetal.pxe.PXE',
               help='Baremetal driver back-end (pxe or tilera)'),
    cfg.StrOpt('power_manager',
               default='nova.virt.baremetal.ipmi.IPMI',
               help='Baremetal power management method'),
    cfg.StrOpt('tftp_root',
               default='/tftpboot',
               help='Baremetal compute node\'s tftp root path'),
    ]


LOG = logging.getLogger(__name__)

baremetal_group = cfg.OptGroup(name='baremetal',
                               title='Baremetal Options')

CONF = cfg.CONF
CONF.register_group(baremetal_group)
CONF.register_opts(opts, baremetal_group)
CONF.import_opt('host', 'nova.netconf')

DEFAULT_FIREWALL_DRIVER = "%s.%s" % (
    firewall.__name__,
    firewall.NoopFirewallDriver.__name__)


def _get_baremetal_node_by_instance_uuid(instance_uuid):
    ctx = nova_context.get_admin_context()
    node = db.bm_node_get_by_instance_uuid(ctx, instance_uuid)
    if node['service_host'] != CONF.host:
        LOG.error(_("Request for baremetal node %s "
                    "sent to wrong service host") % instance_uuid)
        raise exception.InstanceNotFound(instance_id=instance_uuid)
    return node


def _update_state(context, node, instance, state):
    """Update the node state in baremetal DB

    If instance is not supplied, reset the instance_uuid field for this node.

    """
    values = {'task_state': state}
    if not instance:
        values['instance_uuid'] = None
        values['instance_name'] = None
    db.bm_node_update(context, node['id'], values)


def get_power_manager(**kwargs):
    cls = importutils.import_class(CONF.baremetal.power_manager)
    return cls(**kwargs)


class BareMetalDriver(driver.ComputeDriver):
    """BareMetal hypervisor driver."""

    capabilities = {
        "has_imagecache": True,
        }

    def __init__(self, virtapi, read_only=False):
        super(BareMetalDriver, self).__init__(virtapi)

        self.driver = importutils.import_object(
                CONF.baremetal.driver, virtapi)
        self.vif_driver = importutils.import_object(
                CONF.baremetal.vif_driver)
        self.firewall_driver = firewall.load_driver(
                default=DEFAULT_FIREWALL_DRIVER)
        self.volume_driver = importutils.import_object(
                CONF.baremetal.volume_driver, virtapi)
        self.image_cache_manager = imagecache.ImageCacheManager()

        extra_specs = {}
        extra_specs["baremetal_driver"] = CONF.baremetal.driver
        for pair in CONF.baremetal.instance_type_extra_specs:
            keyval = pair.split(':', 1)
            keyval[0] = keyval[0].strip()
            keyval[1] = keyval[1].strip()
            extra_specs[keyval[0]] = keyval[1]
        if 'cpu_arch' not in extra_specs:
            LOG.warning(
                    _('cpu_arch is not found in instance_type_extra_specs'))
            extra_specs['cpu_arch'] = ''
        self.extra_specs = extra_specs

        self.supported_instances = [
                (extra_specs['cpu_arch'], 'baremetal', 'baremetal'),
                ]

    @classmethod
    def instance(cls):
        if not hasattr(cls, '_instance'):
            cls._instance = cls()
        return cls._instance

    def init_host(self, host):
        return

    def get_hypervisor_type(self):
        return 'baremetal'

    def get_hypervisor_version(self):
        # TODO(deva): define the version properly elsewhere
        return 1

    def legacy_nwinfo(self):
        return True

    def list_instances(self):
        l = []
        context = nova_context.get_admin_context()
        for node in db.bm_node_get_associated(context, service_host=CONF.host):
            l.append(node['instance_name'])
        return l

    def _require_node(self, instance):
        """Get a node's uuid out of a manager instance dict.

        The compute manager is meant to know the node uuid, so missing uuid
        a significant issue - it may mean we've been passed someone elses data.
        """
        node_uuid = instance.get('node')
        if not node_uuid:
            raise exception.NovaException(_(
                    "Baremetal node id not supplied to driver for %r")
                    % instance['uuid'])
        return node_uuid

    def _attach_block_devices(self, instance, block_device_info):
        block_device_mapping = driver.\
                block_device_info_get_mapping(block_device_info)
        for vol in block_device_mapping:
            connection_info = vol['connection_info']
            mountpoint = vol['mount_device']
            self.attach_volume(
                    connection_info, instance['name'], mountpoint)

    def _detach_block_devices(self, instance, block_device_info):
        block_device_mapping = driver.\
                block_device_info_get_mapping(block_device_info)
        for vol in block_device_mapping:
            connection_info = vol['connection_info']
            mountpoint = vol['mount_device']
            self.detach_volume(
                    connection_info, instance['name'], mountpoint)

    def _start_firewall(self, instance, network_info):
        self.firewall_driver.setup_basic_filtering(
                instance, network_info)
        self.firewall_driver.prepare_instance_filter(
                instance, network_info)
        self.firewall_driver.apply_instance_filter(
                instance, network_info)

    def _stop_firewall(self, instance, network_info):
        self.firewall_driver.unfilter_instance(
                instance, network_info)

    def macs_for_instance(self, instance):
        context = nova_context.get_admin_context()
        node_uuid = self._require_node(instance)
        node = db.bm_node_get_by_node_uuid(context, node_uuid)
        ifaces = db.bm_interface_get_all_by_bm_node_id(context, node['id'])
        return set(iface['address'] for iface in ifaces)

    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info=None, block_device_info=None):
        node_uuid = self._require_node(instance)

        # NOTE(deva): this db method will raise an exception if the node is
        #             already in use. We call it here to ensure no one else
        #             allocates this node before we begin provisioning it.
        node = db.bm_node_associate_and_update(context, node_uuid,
                    {'instance_uuid': instance['uuid'],
                     'instance_name': instance['hostname'],
                     'task_state': baremetal_states.BUILDING})

        try:
            self._plug_vifs(instance, network_info, context=context)
            self._attach_block_devices(instance, block_device_info)
            self._start_firewall(instance, network_info)

            self.driver.cache_images(
                            context, node, instance,
                            admin_password=admin_password,
                            image_meta=image_meta,
                            injected_files=injected_files,
                            network_info=network_info,
                        )
            self.driver.activate_bootloader(context, node, instance)
            self.power_on(context, instance, network_info, block_device_info,
                          node)
            self.driver.activate_node(context, node, instance)
            _update_state(context, node, instance, baremetal_states.ACTIVE)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error(_("Error deploying instance %(instance)s "
                            "on baremetal node %(node)s.") %
                            {'instance': instance['uuid'],
                             'node': node['uuid']})

                # Do not set instance=None yet. This prevents another
                # spawn() while we are cleaning up.
                _update_state(context, node, instance, baremetal_states.ERROR)

                self.driver.deactivate_node(context, node, instance)
                self.power_off(instance, node)
                self.driver.deactivate_bootloader(context, node, instance)
                self.driver.destroy_images(context, node, instance)

                self._detach_block_devices(instance, block_device_info)
                self._stop_firewall(instance, network_info)
                self._unplug_vifs(instance, network_info)

                _update_state(context, node, None, baremetal_states.DELETED)

    def reboot(self, context, instance, network_info, reboot_type,
               block_device_info=None, bad_volumes_callback=None):
        node = _get_baremetal_node_by_instance_uuid(instance['uuid'])
        ctx = nova_context.get_admin_context()
        pm = get_power_manager(node=node, instance=instance)
        state = pm.reboot_node()
        if pm.state != baremetal_states.ACTIVE:
            raise exception.InstanceRebootFailure(_(
                "Baremetal power manager failed to restart node "
                "for instance %r") % instance['uuid'])
        _update_state(ctx, node, instance, state)

    def destroy(self, instance, network_info, block_device_info=None):
        context = nova_context.get_admin_context()

        try:
            node = _get_baremetal_node_by_instance_uuid(instance['uuid'])
        except exception.InstanceNotFound:
            LOG.warning(_("Destroy called on non-existing instance %s")
                    % instance['uuid'])
            return

        try:
            self.driver.deactivate_node(context, node, instance)
            self.power_off(instance, node)
            self.driver.deactivate_bootloader(context, node, instance)
            self.driver.destroy_images(context, node, instance)

            self._detach_block_devices(instance, block_device_info)
            self._stop_firewall(instance, network_info)
            self._unplug_vifs(instance, network_info)

            _update_state(context, node, None, baremetal_states.DELETED)
        except Exception, e:
            with excutils.save_and_reraise_exception():
                try:
                    LOG.error(_("Error from baremetal driver "
                                "during destroy: %s") % e)
                    _update_state(context, node, instance,
                                  baremetal_states.ERROR)
                except Exception:
                    LOG.error(_("Error while recording destroy failure in "
                                "baremetal database: %s") % e)

    def power_off(self, instance, node=None):
        """Power off the specified instance."""
        if not node:
            node = _get_baremetal_node_by_instance_uuid(instance['uuid'])
        pm = get_power_manager(node=node, instance=instance)
        pm.deactivate_node()
        if pm.state != baremetal_states.DELETED:
            raise exception.InstancePowerOffFailure(_(
                "Baremetal power manager failed to stop node "
                "for instance %r") % instance['uuid'])
        pm.stop_console()

    def power_on(self, context, instance, network_info, block_device_info=None,
                 node=None):
        """Power on the specified instance."""
        if not node:
            node = _get_baremetal_node_by_instance_uuid(instance['uuid'])
        pm = get_power_manager(node=node, instance=instance)
        pm.activate_node()
        if pm.state != baremetal_states.ACTIVE:
            raise exception.InstancePowerOnFailure(_(
                "Baremetal power manager failed to start node "
                "for instance %r") % instance['uuid'])
        pm.start_console()

    def get_volume_connector(self, instance):
        return self.volume_driver.get_volume_connector(instance)

    def attach_volume(self, connection_info, instance, mountpoint):
        return self.volume_driver.attach_volume(connection_info,
                                                instance, mountpoint)

    def detach_volume(self, connection_info, instance_name, mountpoint):
        return self.volume_driver.detach_volume(connection_info,
                                                instance_name, mountpoint)

    def get_info(self, instance):
        # NOTE(deva): compute/manager.py expects to get NotFound exception
        #             so we convert from InstanceNotFound
        inst_uuid = instance.get('uuid')
        node = _get_baremetal_node_by_instance_uuid(inst_uuid)
        pm = get_power_manager(node=node, instance=instance)
        ps = power_state.SHUTDOWN
        if pm.is_power_on():
            ps = power_state.RUNNING
        return {'state': ps,
                'max_mem': node['memory_mb'],
                'mem': node['memory_mb'],
                'num_cpu': node['cpus'],
                'cpu_time': 0}

    def refresh_security_group_rules(self, security_group_id):
        self.firewall_driver.refresh_security_group_rules(security_group_id)
        return True

    def refresh_security_group_members(self, security_group_id):
        self.firewall_driver.refresh_security_group_members(security_group_id)
        return True

    def refresh_provider_fw_rules(self):
        self.firewall_driver.refresh_provider_fw_rules()

    def _node_resource(self, node):
        vcpus_used = 0
        memory_mb_used = 0
        local_gb_used = 0

        vcpus = node['cpus']
        memory_mb = node['memory_mb']
        local_gb = node['local_gb']
        if node['registration_status'] != 'done' or node['instance_uuid']:
            vcpus_used = node['cpus']
            memory_mb_used = node['memory_mb']
            local_gb_used = node['local_gb']

        dic = {'vcpus': vcpus,
               'memory_mb': memory_mb,
               'local_gb': local_gb,
               'vcpus_used': vcpus_used,
               'memory_mb_used': memory_mb_used,
               'local_gb_used': local_gb_used,
               'hypervisor_type': self.get_hypervisor_type(),
               'hypervisor_version': self.get_hypervisor_version(),
               'hypervisor_hostname': str(node['uuid']),
               'cpu_info': 'baremetal cpu',
               }
        return dic

    def refresh_instance_security_rules(self, instance):
        self.firewall_driver.refresh_instance_security_rules(instance)

    def get_available_resource(self, nodename):
        context = nova_context.get_admin_context()
        resource = {}
        try:
            node = db.bm_node_get_by_node_uuid(context, nodename)
            resource = self._node_resource(node)
        except exception.NodeNotFoundByUUID:
            pass
        return resource

    def ensure_filtering_rules_for_instance(self, instance_ref, network_info):
        self.firewall_driver.setup_basic_filtering(instance_ref, network_info)
        self.firewall_driver.prepare_instance_filter(instance_ref,
                                                      network_info)

    def unfilter_instance(self, instance_ref, network_info):
        self.firewall_driver.unfilter_instance(instance_ref,
                                                network_info=network_info)

    def get_host_stats(self, refresh=False):
        caps = []
        context = nova_context.get_admin_context()
        nodes = db.bm_node_get_all(context,
                                     service_host=CONF.host)
        for node in nodes:
            res = self._node_resource(node)
            nodename = str(node['uuid'])
            data = {}
            data['vcpus'] = res['vcpus']
            data['vcpus_used'] = res['vcpus_used']
            data['cpu_info'] = res['cpu_info']
            data['disk_total'] = res['local_gb']
            data['disk_used'] = res['local_gb_used']
            data['disk_available'] = res['local_gb'] - res['local_gb_used']
            data['host_memory_total'] = res['memory_mb']
            data['host_memory_free'] = res['memory_mb'] - res['memory_mb_used']
            data['hypervisor_type'] = res['hypervisor_type']
            data['hypervisor_version'] = res['hypervisor_version']
            data['hypervisor_hostname'] = nodename
            data['supported_instances'] = self.supported_instances
            data.update(self.extra_specs)
            data['host'] = CONF.host
            data['node'] = nodename
            # TODO(NTTdocomo): put node's extra specs here
            caps.append(data)
        return caps

    def plug_vifs(self, instance, network_info):
        """Plugin VIFs into networks."""
        self._plug_vifs(instance, network_info)

    def _plug_vifs(self, instance, network_info, context=None):
        if not context:
            context = nova_context.get_admin_context()
        node = _get_baremetal_node_by_instance_uuid(instance['uuid'])
        if node:
            pifs = db.bm_interface_get_all_by_bm_node_id(context, node['id'])
            for pif in pifs:
                if pif['vif_uuid']:
                    db.bm_interface_set_vif_uuid(context, pif['id'], None)
        for (network, mapping) in network_info:
            self.vif_driver.plug(instance, (network, mapping))

    def _unplug_vifs(self, instance, network_info):
        for (network, mapping) in network_info:
            self.vif_driver.unplug(instance, (network, mapping))

    def manage_image_cache(self, context, all_instances):
        """Manage the local cache of images."""
        self.image_cache_manager.verify_base_images(context, all_instances)

    def get_console_output(self, instance):
        node = _get_baremetal_node_by_instance_uuid(instance['uuid'])
        return self.driver.get_console_output(node, instance)

    def get_available_nodes(self):
        context = nova_context.get_admin_context()
        return [str(n['uuid']) for n in
                db.bm_node_get_all(context, service_host=CONF.host)]
