#!/bin/bash

curdir=$(dirname $(readlink -f $0))
cd ${curdir}
TMPDIR="/tmp"
resultdir=${curdir}/results/$1
num_jobs=$2
jtype=$3
num_subjobs=$4
nocon=$5
if [ "x${nocon}" == "x1" ]; then
	CON_CMD=""
	export PBS_CONF_FILE=/var/spool/pbs/confs/pbs-server-1.conf
else
	CON_CMD="podman exec pbs-server-1"
	uid=`id -u`
	if [ -d "/run/user/${uid}" ]; then
		TMPDIR="/run/user/${uid}"
	fi
fi
SSH_CMD="ssh -o ControlMaster=auto -o ControlPersist=300 -o ControlPath=~/.ssh/.cm-%r@%h@%p -o StrictHostKeyChecking=no"
SCP_CMD="scp -o ControlMaster=auto -o ControlPersist=300 -o ControlPath=~/.ssh/.cm-%r@%h@%p -o StrictHostKeyChecking=no"

###############################
# utility funcs
###############################
function collect_info() {
	local _host

	for _host in $(cat ${curdir}/nodes)
	do
		echo "--- ${_host}"
		if [ "x${_host}" == "x$(hostname)" -o "x${_host}" == "x$(hostname -f)" ]; then
			uptime
			free -hw
			${curdir}/get-top.sh
		else
			${SSH_CMD} ${_host} uptime
			${SSH_CMD} ${_host} free -hw
			${SSH_CMD} ${_host} ${curdir}/get-top.sh
		fi
		echo "------"
	done
}

function wait_jobs() {
	local _ct _last=0 _nct=0

	while true
	do
		_ct=$(${CON_CMD} /opt/pbs/bin/qstat -Bf 2>/dev/null | grep total_jobs | awk -F' = ' '{ print $2 }')
		if [ "x${_ct}" == "x0" ]; then
			echo "total_jobs: ${_ct}"
			break
		elif [ "x${_ct}" == "x${_last}" ]; then
			if [ ${_nct} -gt 10 ]; then
				echo "Looks like job counts is not changing, force exiting wait loop"
				break
			fi
			_nct=$(( _nct + 1 ))
		else
			_last=${_ct}
			_nct=0
		fi
		echo "total_jobs: ${_ct}"
		collect_info
		sleep 10
	done
}

function collect_logs() {
	local _host _svrs _d _fsvr="" _destd=${resultdir}/$1
	if [ "x${nocon}" == "x1" ]; then
		workd="/var/spool/pbs"
	else
		workd="${TMPDIR}/pbs"
	fi
	mkdir -p ${_destd}
	for _host in $(cat ${curdir}/nodes)
	do
		_svrs=$(${SSH_CMD} ${_host} ls -1 ${workd} 2>/dev/null | grep pbs-server | sort -u)
		_moms=$(${SSH_CMD} ${_host} ls -1 ${workd} 2>/dev/null | grep pbs-mom | sort -u)
		if [ "x${_svrs}" == "x" -a "x${_moms}" == "x" ]; then
			continue
		fi
		for _d in ${_svrs}
		do
			echo "Saving logs from ${_d}"
			rm -rf ${_destd}/${_d}
			mkdir -p ${_destd}/${_d}
			echo "Scheduler stats from server node ${_d}:"
			if [ -d ${workd}/${_d} ]; then
				cp -rf ${workd}/${_d}/server_logs ${_destd}/${_d}/
				cp -rf ${workd}/${_d}/server_priv/accounting ${_destd}/${_d}/
				if [ "x${_fsvr}" == "x" ]; then
					cp -rf ${workd}/${_d}/sched_logs ${_destd}/${_d}/
					python3 ${curdir}/pbs_cycle_stats.py -s ${_destd}/${_d}/sched_logs > ${_destd}/${_d}/sched_stats
					_fsvr=${_d}
				fi
			else
				${SCP_CMD} -qr ${_host}:${workd}/${_d}/server_logs ${_destd}/${_d}/
				${SCP_CMD} -qr ${_host}:${workd}/${_d}/server_priv/accounting ${_destd}/${_d}/
				if [ "x${_fsvr}" == "x" ]; then
					${SCP_CMD} -qr ${_host}:${workd}/${_d}/sched_logs ${_destd}/${_d}/sched_logs
					python3 ${curdir}/pbs_cycle_stats.py -s ${_destd}/${_d}/sched_logs > ${_destd}/${_d}/sched_stats
					_fsvr=${_d}
				fi
			fi
		done
		for _d in ${_moms}
		do
			echo "Saving logs from ${_d}"
			rm -rf ${_destd}/${_d}
			mkdir -p ${_destd}/${_d}
			if [ -d ${workd}/${_d} ]; then
				cp -rf ${workd}/${_d}/mom_logs ${_destd}/${_d}/
			else
				${SCP_CMD} -qr ${_host}:${workd}/${_d}/mom_logs ${_destd}/${_d}/
			fi
		done
		${SSH_CMD} ${_host} ${curdir}/truncate-logs.sh ${workd}
	done
}
###############################
# end of utility funcs
###############################


############################
# test funcs
############################
function test_with_sched_off() {
	${CON_CMD} /opt/pbs/bin/qmgr -c "s s scheduling=0"
	${curdir}/submit-jobs.sh ${nocon} ${num_jobs} ${jtype} ${num_subjobs}
	${CON_CMD} /opt/pbs/bin/qmgr -c "s s scheduling=1"
	wait_jobs
	collect_logs sched_off
}

function clear_logs() {
	local _host _svrs _d

	if [ "x${nocon}" == "x1" ]; then
		workd="/var/spool/pbs"
	else
		workd="${TMPDIR}/pbs"
	fi

	for _host in $(cat ${curdir}/nodes)
	do
		_svrs=$(${SSH_CMD} ${_host} ls -1 ${workd} 2>/dev/null | grep pbs-server | sort -u)
		_moms=$(${SSH_CMD} ${_host} ls -1 ${workd} 2>/dev/null | grep pbs-mom | sort -u)
		if [ "x${_svrs}" == "x" -a "x${_moms}" == "x" ]; then
			continue
		fi
		for _d in ${_svrs}
		do
			${SSH_CMD} ${_host} podman exec ${_d} pkill pbs
			sleep 2
			${SSH_CMD} ${_host} podman exec ${_d} rm -rf ${workd}/${_d}/server_logs
			${SSH_CMD} ${_host} podman exec ${_d} rm -rf ${workd}/${_d}/accounting_logs
			${SSH_CMD} ${_host} podman exec ${_d} rm -rf ${workd}/${_d}/sched_logs
			${SSH_CMD} ${_host} podman exec ${_d} mkdir -p ${workd}/${_d}/server_logs
			${SSH_CMD} ${_host} podman exec ${_d} mkdir -p ${workd}/${_d}/accounting_logs
			${SSH_CMD} ${_host} podman exec ${_d} mkdir -p ${workd}/${_d}/sched_logs
			${SSH_CMD} ${_host} podman exec ${_d} /etc/init.d/pbs start
		done
		for _d in ${_moms}
		do
			${SSH_CMD} ${_host} podman exec ${_d} pkill pbs
			sleep 2
			${SSH_CMD} ${_host} podman exec ${_d} rm -rf ${workd}/${_d}/mom_logs
			${SSH_CMD} ${_host} podman exec ${_d} mkdir -p ${workd}/${_d}/mom_logs
			${SSH_CMD} ${_host} podman exec ${_d} /etc/init.d/pbs start
		done
	done
}

function test_with_sched_on() {
	${CON_CMD} /opt/pbs/bin/qmgr -c "s s scheduling=1"
	${curdir}/submit-jobs.sh ${nocon} ${num_jobs} ${jtype} ${num_subjobs}
	wait_jobs
	collect_logs sched_on
}

function test_with_mixed() {
	${CON_CMD} /opt/pbs/bin/qmgr -c "s s scheduling=0"
	let "half_num_jobs = ${num_jobs} / 2"
	let "rem_num_jobs = ${num_jobs} - ${half_num_jobs}"
	${curdir}/submit-jobs.sh ${nocon} ${half_num_jobs} ${jtype} ${num_subjobs}
	${CON_CMD} /opt/pbs/bin/qmgr -c "s s scheduling=1"
	${curdir}/submit-jobs.sh ${nocon} ${rem_num_jobs} ${jtype} ${num_subjobs}
	wait_jobs
	collect_logs sched_mixed
}

function test_with_rate_limit() {
	${CON_CMD} /opt/pbs/bin/qmgr -c "s s scheduling=1"
	jobs=5000
	i=0
	if [ ${num_subjobs} -eq 0 ]; then
		while [ $i -lt ${num_jobs} ]; do
			i=$[$i+$jobs]
			${curdir}/submit-jobs.sh ${nocon} ${jobs} ${jtype} ${num_subjobs}
			sleep 1 
		done
	else
		while [ $i -lt ${num_jobs} ]; do
			i=$[$i+1]
			${curdir}/submit-jobs.sh ${nocon} 1 ${jtype} ${num_subjobs}
			sleep 1 
		done
	fi
	wait_jobs
	collect_logs sched_rtlimit
}
############################
# end test funcs
############################
collect_info
test_with_sched_off
clear_logs
collect_info
test_with_sched_on
clear_logs
collect_info
test_with_mixed
clear_logs
#collect_info
#test_with_rate_limit
