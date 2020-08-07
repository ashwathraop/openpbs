# coding: utf-8
"""

# Copyright (C) 1994-2019 Altair Engineering, Inc.
# For more information, contact Altair at www.altair.com.
#
# This file is part of the PBS Professional ("PBS Pro") software.
#
# Open Source License Information:
#
# PBS Pro is free software. You can redistribute it and/or modify it under the
# terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# PBS Pro is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.
# See the GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Commercial License Information:
#
# For a copy of the commercial license terms and conditions,
# go to: (http://www.pbspro.com/UserArea/agreement.html)
# or contact the Altair Legal Department.
#
# Altair’s dual-license business model allows companies, individuals, and
# organizations to create proprietary derivative works of PBS Pro and
# distribute them - whether embedded or bundled with other software -
# under a commercial license agreement.
#
# Use of Altair’s trademarks, including but not limited to "PBS™",
# "PBS Professional®", and "PBS Pro™" and Altair’s logos is subject to Altair's
# trademark licensing policies.

"""
__doc__ = """
This module is be used for Azure VA systems.
"""

import os
import stat
import time
import random
import json
from subprocess import Popen, PIPE
from pbs.v1._pmi_types import BackendError
import pbs
from pbs.v1._pmi_utils import _running_excl, _pbs_conf, _get_vnode_names, \
    _svr_vnode

def launch(func, args):
    """
    Run az and return the structured output.

    :param args: arguments for capmc command
    :type args: str
    :returns: az output in json format.
    """

    cmd = "az"
    cmd = cmd + " " + args
    fail = ""

    pbs.logjobmsg(func, "launch: " + cmd)
    cmd_run = Popen(cmd, shell=True, stdout=PIPE, stderr=PIPE)
    (cmd_out, cmd_err) = cmd_run.communicate()
    exitval = cmd_run.returncode
    if exitval != 0:
        fail = "%s: exit %d" % (cmd, exitval)
    else:
        pbs.logjobmsg(func, "launch: finished")

    try:
        out = json.loads(cmd_out)
    except Exception:
        out = None
    try:
        err = cmd_err.splitlines()[0]           # first line only
    except Exception:
        err = ""

    if len(err) > 0:
        pbs.logjobmsg(func, "stderr: %s" % err.strip())

    if len(fail) > 0:
        pbs.logjobmsg(func, fail)
        raise BackendError(fail)
    return out

def load_az_config():
    # Identify the config file and read in the data
    global res_grp
    global app_id
    global tenant_id
    global secret
    global pbshome
    config_file = ''
    try:
        # This block will work for PBS Pro versions 13 and later
        pbs_conf = pbs.get_pbs_conf()
        pbshome = pbs_conf['PBS_HOME']
    except:
        pbs.logjobmsg("PBS_power",
                   "PBS_HOME needs to be defined in the config file")
        pbs.logjobmsg("PBS_power", "Exiting the power hook")
        pbs.event().accept()
    if pbshome is None:
        raise BackendError("PBS_HOME not found")
    if 'AZURE_CONFIG_FILE' in os.environ:
        config_file = os.environ["AZURE_CONFIG_FILE"]
    tmpcfg = ''
    if not config_file:
        tmpcfg = os.path.join(pbshome, 'server_priv', 'hooks', 'pbs_azure_login.cf')
    if os.path.isfile(tmpcfg):
        config_file = tmpcfg

    if not config_file:
        raise Exception("Config file not found")
    pbs.logjobmsg("PBS_power", "Azure config file is %s" % config_file)    
    pbs.logmsg(pbs.EVENT_DEBUG3, "Azure config file is %s" % config_file)
    try:
        fd = open(config_file, 'r')
        config = json.load(fd)
        fd.close()
    except IOError:
        raise Exception("I/O error reading config file")
    except:
        raise Exception("Error reading config file " + tmpcfg)
    app_id = ""
    tenant_id = ""
    res_grp = ""
    secret = ""
    if 'app_id' in config:
        app_id = config['app_id']
    if 'tenant_id' in config:
        tenant_id = config['tenant_id']
    if 'resource_group' in config:
        res_grp = config['resource_group']
    if 'secret' in config:
        secret = config['secret']
    
class Pmi:
    def __init__(self, pyhome=None):
        pbs.logmsg(pbs.LOG_WARNING, "VA: init")

    def _connect(self, endpoint, port, job):
        load_az_config()
        cmd = "login --service-principal -u " + app_id + " -p " + secret + " --tenant " + tenant_id
        func = "pmi_connect"
        out = launch(func, cmd)
        return

    def _disconnect(self, job):
        return

    def _get_usage(self, job):
        return None

    def _query(self, query_type):
        return None

    def _activate_profile(self, profile_name, job):
        return False

    def _deactivate_profile(self, job):
        return False

    def _pmi_ramp_down(self, hosts):
        return False

    def _pmi_ramp_up(self, hosts):
        return False

    def _pmi_power_off(self, hosts):
        pbs.logjobmsg("PBS_power", "VA: power off the nodes")
        az_cmd = "vm deallocate --resource-group " + str(res_grp)
        func = "pmi_power_off"
        for vname in hosts:
            pbs.logjobmsg("PBS_power", "VA: powering off the node: %s" % str(vname))
            cmd = az_cmd + " --name " + str(vname) + " --no-wait"
            out = launch(func, cmd)
        return True

    def _pmi_power_on(self, hosts):
        pbs.logjobmsg("PBS_power", "VA: power on the nodes")
        az_cmd = "vm start --resource-group " + str(res_grp)
        func = "pmi_power_on"
        for vname in hosts:
            pbs.logjobmsg("PBS_power", "VA: powering on the node: %s" % str(vname))
            cmd = az_cmd + " --name " + str(vname) + " --no-wait"
            out = launch(func, cmd)
        return True

    def _pmi_power_status(self, hosts):
        pbs.logjobmsg("PBS_power", "VA: status of the nodes")
        az_cmd = "vm get-instance-view --resource-group " + str(res_grp) + " --query instanceView.statuses[1]"
        func = "pmi_power_status"
        nidset = set()
        for vname in hosts:
            cmd = az_cmd + " --name " + str(vname)
            out = launch(func, cmd)
            if out["code"] == "PowerState/running":
                pbs.logjobmsg("PBS_power", "VA: Node is powerd on: %s" % str(vname))
                nidset.add(vname)
                time.sleep(5)
        return nidset
