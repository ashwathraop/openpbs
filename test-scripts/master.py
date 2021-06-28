import json
import os
import pathlib
import socket
import subprocess
import sys
import argparse
import time
from concurrent.futures import ProcessPoolExecutor, wait

MYDIR = str(pathlib.Path(__file__).resolve().parent)
ME = socket.getfqdn(socket.gethostname())
TMPDIR = os.path.join(os.sep, 'run', 'user', str(os.getuid()))
if not os.path.exists(TMPDIR):
    TMPDIR = '/tmp'


def run_cmd(host, cmd):
    if host != ME:
        cmd = ['ssh', host] + cmd
    print('++++ ' + time.ctime() + " ++++ :Running: " + " ".join(cmd), flush=True)
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    return p.returncode == 0


def copy_artifacts(host, nocon):
    if host != ME:
        run_cmd(host, ['mkdir', '-p', MYDIR])
        _c = ['scp', '-p']
        if not nocon:
            _c += [os.path.join(MYDIR, 'pbs.tgz')]
        _c += [os.path.join(MYDIR, 'openpbs-server.rpm')]
        _c += [os.path.join(MYDIR, 'cleanup-pbs.sh')]
        _c += [os.path.join(MYDIR, 'entrypoint')]
        _c += [os.path.join(MYDIR, 'get-top.sh')]
        _c += [os.path.join(MYDIR, 'setup-pbs.sh')]
        _c += [os.path.join(MYDIR, 'submit-jobs.sh')]
        _c += [os.path.join(MYDIR, 'truncate-logs.sh')]
        _c += [host + ':' + MYDIR]
        subprocess.run(_c)
    if not nocon:
        _c = ['podman', 'load', '-i']
        _c += [os.path.join(MYDIR, 'pbs.tgz')]
        _c += ['pbs:latest']
        run_cmd(host, _c)


def delete_container(host, cid):
    _c = ['podman', 'rm', '-vf', str(cid), '&>/dev/null']
    run_cmd(host, _c)


def cleanup_containers(host):
    print('Cleaning previous containers on %s' % host)
    _c = ['podman', 'ps', '-aqf', 'label=pbs=1']
    _s = []
    if host != ME:
        _s = ['ssh', host]
    try:
        p = subprocess.check_output(_s + _c)
    except:
        p = ''
    p = p.splitlines()
    p = [x.strip() for x in p]
    p = [x.decode() for x in p if len(x) > 0]
    if len(p) > 0:
        with ProcessPoolExecutor(max_workers=10) as executor:
            _ps = []
            for _p in p:
                _ps.append(executor.submit(delete_container, host, _p))
            wait(_ps)
    _c = ['podman', 'run', '--network', 'host', '-it']
    _c += ['--rm', '-l', 'pbs=1', '-v', TMPDIR+':/tmp/htmp']
    _c += ['centos:8', 'rm', '-rf']
    _c += ['/tmp/htmp/pbs', '/tmp/htmp/rpms']
    _c += ['/tmp/htmp/pbssetuplogs']
    _c += ['&>/dev/null']
    run_cmd(host, _c)


def cleanup_pbs(host):
    run_cmd(host, [os.path.join(MYDIR, 'cleanup-pbs.sh')])
    _c = ['rm', '-rf', '/tmp/pbs', '/tmp/rpms', '/tmp/pbssetuplogs', '/var/spool/pbs']
    run_cmd(host, _c)


def cleanup_system(host, nocon):
    print('Cleaning system on %s' % host)
    if nocon:
        cleanup_pbs(host)
    else:
        cleanup_containers(host)
        _c = ['podman', 'rmi', '-f', 'pbs:latest']
        run_cmd(host, _c)


def setup_pbs_con(host, c, svrs, sips, moms, ncpus, asyncdb, vnodes, firstsvr):
    _c = ['podman', 'run', '--privileged', '--network', 'host', '-itd']
    _c += ['--rm', '-l', 'pbs=1', '-v', '%s:%s' % (MYDIR, MYDIR)]
    _c += ['-v', TMPDIR+'/pbs:/var/spool/pbs']
    _c += ['--name', c[0]]
    if 'pbs-mom' in c[0]:
        _c += ['-h', c[0]]
    _c += ['--add-host=%s' % x for x in sips]
    nip = socket.gethostbyname(host)
    _c += ['--add-host=%s:%s' % (host.split('.', 1)[0],nip)]
    if 'pbs-mom' in c[0]:
        _c += ['--add-host=%s:%s' % (c[0],nip)]
    _c += ['pbs:latest']
    p = run_cmd(host, _c)
    if not p:
        print("Failed to launch %s" % c[0])
        return p
    _e = os.path.join(MYDIR, 'entrypoint')
    _c = ['podman', 'exec', c[0], _e]
    _c += c[1:]
    if c[2] == 'server':
        if asyncdb:
            _c += ['1']
        else:
            _c += ['0']
        _c += [moms]
    elif c[2] == 'mom':
        _c += [c[0]]
    _c += [str(ncpus)]
    _c += [str(vnodes)]
    _c += [firstsvr]
    if len(svrs) > 1:
        _c += [','.join(svrs)]
    p = run_cmd(host, _c)
    if p and c[2] == 'server':
        _c = ['podman', 'exec', c[0]]
        _c += [_e, 'waitsvr', c[1], str(c[3]), moms]
        _psi = firstsvr + ":" + str(c[5])
        _c += [_psi]
        p = run_cmd(host, _c)
    if p:
        print('**** ' + time.ctime() + '****: Configured %s' % c[0], flush=True)
    else:
        print('Failed to configure %s' % c[0])
        print('Command is : %s' % " ".join(_c))
    return p


def setup_pbs_nocon(host, c, svrs, sips, moms, ncpus, asyncdb, vnodes, firstsvr):
    _c = [os.path.join(MYDIR, 'setup-pbs.sh')]
    _c += c
    if c[2] == 'server':
        if asyncdb:
            _c += ['1']
        else:
            _c += ['0']
        _c += [moms]
    elif c[2] == 'mom':
        _c += [c[0]]
    _c += [str(ncpus)]
    _c += [str(vnodes)]
    _c += [firstsvr]
    if len(svrs) > 1:
        _c += [','.join(svrs)]
    p = run_cmd(host, _c)
    if p and c[2] == 'server':
        _c = [os.path.join(MYDIR, 'entrypoint')]
        _c += ['waitsvr', c[1], str(c[3]), moms]
        _psi = firstsvr + ":" + str(c[5])
        _c += [_psi]
        p = run_cmd(host, _c)
    if p:
        print('**** ' + time.ctime() + '****: Configured %s' % c[0])
    else:
        print('Failed to configure %s' % c[0])
        print('Command is : %s' % " ".join(_c))
    return p


def setup_pbs(host, c, svrs, sips, moms, ncpus, asyncdb, vnodes, nocon, firstsvr):
    if nocon:
        return setup_pbs_nocon(host, c, svrs, sips, moms, ncpus, asyncdb, vnodes, firstsvr)
    else:
        return setup_pbs_con(host, c, svrs, sips, moms, ncpus, asyncdb, vnodes, firstsvr)


def setup_cluster(tconf, hosts, ips, conf, nocon):
    _ts = tconf['total_num_svrs']
    _tm = tconf['total_num_moms']
    _mph = tconf['num_moms_per_host']
    _sph = tconf['num_svrs_per_host']
    _cpm = tconf['num_cpus_per_mom']
    _dbt = tconf['async_db']
    _vnd = tconf['num_vnodes_per_mom']

    if _tm == 0 and _mph == 0:
        print('Invalid setup configuration for no. of mom')
        return False
    elif _tm != 0 and _mph != 0:
        print('Invalid setup configuration for no. of mom')
        return False

    _hl = len(hosts)
    _confs = dict(
        [(_h, {'ns': 0, 'nm': _mph, 'svrs': [], 'moms': []}) for _h in hosts])
    _hi = 0
    print("Total servers:", _ts, "Total moms:", _tm)
    # Starting from host 0, set the number of server containers to be launched on each host
    # e.g - i) if we have 2 servers and servers per host is 1 and 2+ hosts, 
    # hosts 0 and 1 will launch 1 srv container each
    # e.g - ii) if we have 2 servers and servers per host is 2 and 2+ hosts, 
    # hosts 0 will launch 2 srv containers
    # this is done by setting 'ns' key of the host's conf map
    for i in range(1, _ts + 1):
        _confs[hosts[_hi]]['ns'] += 1
        # if (total svrs / svrs per host) is less than total hosts and
        # num servers allotted for this host "ns" == svrs per host
        #  increase the _hi counter.
        if (_ts/_sph) < _hl and _confs[hosts[_hi]]['ns'] == _sph:
            _hi += 1
        if _hi == _hl:
            _hi = 0

    if _tm != 0:
        print("Total moms requested: %d" % _tm)
        print("*************************************")
        _hi = 0
        i = 0
        # Starting from host 0, set the number of mom containers to launch on each host
        # hosts which have server containers running will be skipped
        # done by setting the 'nm' key of a host's conf map
        while i < _tm:
            # If num of hosts > (total servers/ servers per host), then don't launch moms on server hosts
            if _hl > (_ts/_sph):
                if _confs[hosts[_hi]]['ns'] == 0:
                    _confs[hosts[_hi]]['nm'] += 1
                    i += 1
            else:
                _confs[hosts[_hi]]['nm'] += 1
                i += 1
            _hi += 1
            if _hi == _hl:
                _hi = 0
        print("Confs after deciding mom and server counts:", _confs)
    _lps = 18000
    _svrs = []
    _sips = []
    _scnt = 1
    _svrhost = {}
    _firstsvr = 'pbs-server-1'
    # this will add, for svr: [container name, pbs home/'default', 'server', <svr count>,  '1'/'0', svrport, dbport]
    # for mom: [container name, pbs home/'default', 'mom', mom port]
    # to _confs[_h]['svrs']/_confs[_h]['moms']
    for i, _h in enumerate(hosts):
        no_moms = 0
        # For each server container the host
        for _ in range(_confs[_h]['ns']):
            print("Setup pbs-server-%d on: %s" % (_scnt,_h))
            _svrhost.setdefault(str(_scnt), _h) # <server count: hostname>
            _c = ['pbs-server-%d' % _scnt]
            if nocon:
                _c += ['/var/spool/pbs']    # isn't this also default?
            else:
                _c += ['default']
            _c += ['server', str(_scnt)]    # Add 'server' and str(server count)
            _c.append('1' if _scnt == 1 else '0')   # Add '1' for the first server, '0' for others
            _p = _lps
            _lps += 2
            _c.append(str(_p))  # Adding port number for server
            _c.append(str(_p + 1))  # Port number for db
            _svr_name = _h.split('.')[0]    # PBS_SERVER_NAME, but UNUSED?
            _svrs.append('pbs-server-%d:%d' % (_scnt, _p)) # used for PSI, sets svrname:port
            _sips.append('pbs-server-%d:%s' % (_scnt, ips[i]))  # server_host:ip address
            _confs[_h]['svrs'].append(_c)   # a host's 'svrs' key will contain each server container's '_c' created above
            if _scnt == 1 and nocon:
                _firstsvr = _h.split('.')[0]    # Set to the first server's short hostname
            _scnt += 1
            if _hl > _ts:   # If num hosts > num servers then server hosts won't have any moms
                no_moms = 1
        if no_moms == 1:
            continue
        print("Mom count for host %s: %d" % (_h, _confs[hosts[i]]['nm']))
        for _ in range(_confs[_h]['nm']):
            _c = ['']
            if nocon:
                _c += ['/var/spool/pbs']    # isn't this also the default?
            else:
                _c += ['default']
            _c += ['mom', str(_lps)]    # Port number for moms start +2 after servers
            _lps += 2
            _confs[_h]['moms'].append(_c)

    # This is where we add the associated server to each mom's conf
    for _h in hosts:
        _mc = len(_confs[_h]['moms'])
        i = 0
        while i != _mc:
            # _mc = no of moms on host _h, distribute them among server hosts
            for __h in hosts:
                if i == _mc:
                    break
                if _confs[__h]['ns'] == 0:
                    continue
                for s in _confs[__h]['svrs']:
                    _confs[_h]['moms'][i].append(s[3])  # Add 'svr count' of the server to mom's conf
                    _confs[_h]['moms'][i].append(s[5])  # Add the server's port to mom's conf
                    i += 1
                    if i == _mc:
                        break
    # Now, mom's conf has: [container name, pbs home/'default', 'mom', mom port, <svr count>, svr port]

    svrs = dict([(_h, []) for _h in hosts]) # {'host1': [], 'host2': [], ...}
    moms = dict([(_h, []) for _h in hosts])
    _mcs = dict([(str(i), 0) for i in range(1, _ts + 1)])   # mom count for each server {'1': 0, '2': 0} for 2 servers
    _moms = dict([(_h, {}) for _h in hosts])    # {'host1': {}, 'host2': {}, ...}

    # Sets up mom counts for each server and adds server host info to mom conf and creates extra dicts
    for _h in hosts:
        # Adds all server conf info for _h [container name, pbs home/'default', 'server', <svr count>,  '1'/'0', svrport, dbport]
        svrs[_h].extend(_confs[_h]['svrs'])
        for _c in _confs[_h]['moms']:
            _s = _c[4]  # Server count for the mom's server
            _c[0] = 'pbs-mom-%s-%d' % (_s, _mcs[_s])    # pbs-mom-<svr count>-<mom count on that server>
            if nocon:
                _c[4] = _svrhost[_s]    # seems like we are replacing <svr count> with <server hostname>
            else:
                _c[4] = 'pbs-server-%s' % _s    # Or, the server's container name
            if _s not in _moms[_h]:
                _moms[_h].setdefault(_s, [])    # _moms = {'momhost1': {<svr count1>: []}, 'momhost2': .. }
            # _moms = {'momhost1': {<svr count1>: ["<mom count on svr>=<mom port offset>"]}, 'momhost2': .. }
            _moms[_h][_s].append('%d=%s' % (_mcs[_s], int(_c[3]) - 18000))
            moms[_h].append(_c) # {'momhost1': [mom1's conf], 'momhost2': ...}
            # mom conf is now: [container name, pbs home/'default', 'mom', mom port, <svr hostname>/<svr container>, svr port]
            _mcs[_s] += 1

    # I think we are finally going to add mom info to the server confs
    # Will refer to server count as <svrid num> and mom count on server as <momid num on svrid>
    __moms = []
    for _h, _ss in _moms.items():
        # _h : mom hostname
        # _ss: {<svr1 num>: ["<mom1 num on svr1>=<mom1 port offset>", "<mom2 num on svr1>=<mom2 port offset>", ..],
        # <svr2 num>: [... ]}
        # for all the servers which the host's moms are distributed to
        _t = []
        for _si, _ms in _ss.items():
            # si: svrid num
            # _ms: ["<mom1 num on svr1>=<mom1 port offset>", "<mom2 num on svr1>=<mom2 port offset>", ...]
            if len(_ms) > 0:
                # e.g - if _si is 1 and there are 2 moms, and 2 servers in total:
                # _t = ["1@0=4,1=8", "2@0=6,1=10"]
                _t.append(_si + '@' + ','.join(_ms))
        if len(_t) > 0:
            # __moms = ["<mom host>:1@0=4,1=6+2@0=8,1=10"]  for the above example
            __moms.append(_h + ':' + '+'.join(_t))
    _moms = '_'.join(__moms)    # Overwriting _moms

    with ProcessPoolExecutor(max_workers=len(hosts)) as executor:
        _ps = []
        for _h in hosts:
            if nocon:
                _ps.append(executor.submit(cleanup_pbs, _h))
            else:
                _ps.append(executor.submit(cleanup_containers, _h))
        wait(_ps)

    for _h in hosts:
        run_cmd(_h, ['mkdir', '-p', TMPDIR+'/pbs'])
        if nocon:
            run_cmd(_h, [os.path.join(MYDIR, 'install-pbs.sh')])

    r = [False]
    with ProcessPoolExecutor(max_workers=10) as executor:
        _ps = []
        for _h, _cs in moms.items():
            for _c in _cs:
                _ps.append(executor.submit(setup_pbs, _h, _c, _svrs,
                                           _sips, _moms, _cpm, _dbt, _vnd, nocon, _firstsvr))
        for _h, _cs in svrs.items():
            for _c in _cs:
                _ps.append(executor.submit(setup_pbs, _h, _c, _svrs,
                                           _sips, _moms, _cpm, _dbt, _vnd, nocon, _firstsvr))
        r = list(set([_p.result() for _p in wait(_ps)[0]]))
    if len(r) != 1:
        return False
    return r[0]


def run_tests(conf, hosts, ips, nocon):
    for _n, _s in conf['setups'].items():
        print('Configuring setup: %s' % _n)
        r = setup_cluster(_s, hosts, ips, conf, nocon)
        if not r:
            print('skipping test, see above for reason')
            sys.exit(1)
        else:
            _c = [os.path.join(MYDIR, 'run-test.sh'), _n]
            _c += [str(_s['total_num_jobs']), _s['job_type']]
            _c += [str(_s['num_subjobs'])]
            _c.append('1' if nocon else '0')
            subprocess.run(_c)


def main():
    parser = argparse.ArgumentParser(prog='master.py')
    parser.add_argument('--clean', action='store_true', help='Do cleanup')
    parser.add_argument('--nocon', action='store_true',
                        help='Run tests in non-container mode')
    args = parser.parse_args()

    if not args.nocon and os.getuid() == 0:
        print('This script must be run as non-root user!')
        sys.exit(1)
    elif args.nocon and os.getuid() != 0:
        print('This script must run as root user in non-container mode!')
        sys.exit(1)

    os.chdir(MYDIR)

    if not os.path.isfile('config.json'):
        print('Could not find config.json file')
        sys.exit(1)

    with open('config.json') as f:
        conf = json.load(f)

    if not os.path.isfile('nodes'):
        print('Could not find nodes file')
        sys.exit(1)

    if args.nocon:
        _f = 'openpbs-server.rpm'
    else:
        _f = 'pbs.tgz'
    if not os.path.isfile(_f):
        print('Could not find %s' % _f)
        sys.exit(1)

    hosts = []
    with open('nodes') as f:
        hosts = f.read().splitlines()
        hosts = [_h for _h in hosts if len(_h.strip()) > 0]

    if len(hosts) == 0:
        print('No hosts defined in nodes file')
        sys.exit(1)

    hosts = [socket.getfqdn(_h) for _h in hosts]
    ips = [socket.gethostbyname(_h) for _h in hosts]

    with ProcessPoolExecutor(max_workers=len(hosts)) as executor:
        _ps = []
        for _h in hosts:
            _ps.append(executor.submit(cleanup_system, _h, args.nocon))
        wait(_ps)

    if args.clean:
        sys.exit(0)

    subprocess.run(['rm', '-rf', 'results'])

    with ProcessPoolExecutor(max_workers=len(hosts)) as executor:
        _ps = []
        for _h in hosts:
            _ps.append(executor.submit(copy_artifacts, _h, args.nocon))
        wait(_ps)

    run_tests(conf, hosts, ips, args.nocon)

    with ProcessPoolExecutor(max_workers=len(hosts)) as executor:
        _ps = []
        for _h in hosts:
            _ps.append(executor.submit(cleanup_system, _h, args.nocon))
        wait(_ps)


if __name__ == '__main__':
    main()
