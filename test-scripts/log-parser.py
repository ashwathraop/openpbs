# coding: utf-8

import copy
import logging
import math
import re
import os
import sys
import time
import pathlib
import traceback
import collections
from datetime import datetime, timedelta, tzinfo
from subprocess import PIPE, Popen
from concurrent.futures import ProcessPoolExecutor, wait

tm_re = r'(?P<datetime>\d\d/\d\d/\d{4}\s\d\d:\d\d:\d\d(\.\d{6})?)'
job_re = r';(?P<jobid>[\d\[\d+\]]+\..*);'
fail_re = r';(?P<jobid>[\d\[\]]+\..*);'

# Server metrics
NUR = 'node_up_rate'

# Scheduler metrics
NC = 'num_cycles'
mCD = 'cycle_duration_min'
MCD = 'cycle_duration_max'
mCT = 'min_cycle_time'
MCT = 'max_cycle_time'
CDA = 'cycle_duration_mean'
CD25 = 'cycle_duration_25p'
CDA = 'cycle_duration_mean'
CD50 = 'cycle_duration_median'
CD75 = 'cycle_duration_75p'
CST = 'cycle_start_time'
CD = 'cycle_duration'
QD = 'query_duration'
NJC = 'num_jobs_considered'
NJFR = 'num_jobs_failed_to_run'
SST = 'scheduler_solver_time'
NJCAL = 'num_jobs_calendared'
NJFP = 'num_jobs_failed_to_preempt'
NJP = 'num_jobs_preempted'
T2R = 'time_to_run'
T2D = 'time_to_discard'
TiS = 'time_in_sched'
TTC = 'time_to_calendar'

# Scheduling Estimated Start Time
EST = 'estimates'
EJ = 'estimated_jobs'
Eat = 'estimated'
DDm = 'drift_duration_min'
DDM = 'drift_duration_max'
DDA = 'drift_duration_mean'
DD50 = 'drift_duration_median'
ND = 'num_drifts'
NJD = 'num_jobs_drifted'
NJND = 'num_jobs_no_drift'
NEST = 'num_estimates'
JDD = 'job_drift_duration'
ESTR = 'estimated_start_time_range'
ESTA = 'estimated_start_time_accuracy'
JST = 'job_start_time'
ESTS = 'estimated_start_time_summary'
Ds15mn = 'drifted_sub_15mn'
Ds1hr = 'drifted_sub_1hr'
Ds3hr = 'drifted_sub_3hr'
Do3hr = 'drifted_over_3hr'

# Accounting metrics
JWTm = 'job_wait_time_min'
JWTM = 'job_wait_time_max'
JWTA = 'job_wait_time_mean'
JWT25 = 'job_wait_time_25p'
JWT50 = 'job_wait_time_median'
JWT75 = 'job_wait_time_75p'
JRTm = 'job_run_time_min'
JRT25 = 'job_run_time_25p'
JRT50 = 'job_run_time_median'
JRTA = 'job_run_time_mean'
JRT75 = 'job_run_time_75p'
JRTM = 'job_run_time_max'
JNSm = 'job_node_size_min'
JNS25 = 'job_node_size_25p'
JNS50 = 'job_node_size_median'
JNSA = 'job_node_size_mean'
JNS75 = 'job_node_size_75p'
JNSM = 'job_node_size_max'
JCSm = 'job_cpu_size_min'
JCS25 = 'job_cpu_size_25p'
JCS50 = 'job_cpu_size_median'
JCSA = 'job_cpu_size_mean'
JCS75 = 'job_cpu_size_75p'
JCSM = 'job_cpu_size_max'
CPH = 'cpu_hours'
NPH = 'node_hours'
USRS = 'unique_users'
UNCPUS = 'utilization_ncpus'
UNODES = 'utilization_nodes'

# Generic metrics
VER = 'pbs_version'
JID = 'job_id'
JRR = 'job_run_rate'
JSR = 'job_submit_rate'
JER = 'job_end_rate'
JTR = 'job_throughput'
NJQ = 'num_jobs_queued'
NJR = 'num_jobs_run'
NJE = 'num_jobs_ended'
DUR = 'duration'
RI = 'custom_interval'
IT = 'init_time'
CF = 'custom_freq'
CFC = 'custom_freq_counts'
CG = 'custom_groups'

PARSER_OK_CONTINUE = 0
PARSER_OK_STOP = 1
PARSER_ERROR_CONTINUE = 2
PARSER_ERROR_STOP = 3

class PbsTypeSize(str):

    """
    Descriptor class for memory as a numeric entity.
    Units can be one of ``b``, ``kb``, ``mb``, ``gb``, ``tb``, ``pt``

    :param unit: The unit type associated to the memory value
    :type unit: str
    :param value: The numeric value of the memory
    :type value: int or None
    :raises: ValueError and TypeError
    """

    def __init__(self, value=None):
        if value is None:
            return

        if len(value) < 2:
            raise ValueError

        if value[-1:] in ('b', 'B') and value[:-1].isdigit():
            self.unit = 'b'
            self.value = int(int(value[:-1]) / 1024)
            return

        # lower() applied to ignore case
        unit = value[-2:].lower()
        self.value = value[:-2]
        if not self.value.isdigit():
            raise ValueError
        if unit == 'kb':
            self.value = int(self.value)
        elif unit == 'mb':
            self.value = int(self.value) * 1024
        elif unit == 'gb':
            self.value = int(self.value) * 1024 * 1024
        elif unit == 'tb':
            self.value = int(self.value) * 1024 * 1024 * 1024
        elif unit == 'pb':
            self.value = int(self.value) * 1024 * 1024 * 1024 * 1024
        else:
            raise TypeError
        self.unit = 'kb'

    def encode(self, value=None, valtype='kb', precision=1):
        """
        Encode numeric memory input in kilobytes to a string, including
        unit

        :param value: The numeric value of memory to encode
        :type value: int or None.
        :param valtype: The unit of the input value, defaults to kb
        :type valtype: str
        :param precision: Precision of the encoded value, defaults to 1
        :type precision: int
        :returns: Encoded memory in kb to string
        """
        if value is None:
            value = self.value

        if valtype == 'b':
            val = value
        elif valtype == 'kb':
            val = value * 1024
        elif valtype == 'mb':
            val = value * 1024 * 1024
        elif valtype == 'gb':
            val = value * 1024 * 1024 * 1024 * 1024
        elif valtype == 'tb':
            val = value * 1024 * 1024 * 1024 * 1024 * 1024
        elif valtype == 'pt':
            val = value * 1024 * 1024 * 1024 * 1024 * 1024 * 1024

        m = (
            (1 << 50, 'pb'),
            (1 << 40, 'tb'),
            (1 << 30, 'gb'),
            (1 << 20, 'mb'),
            (1 << 10, 'kb'),
            (1, 'b')
        )

        for factor, suffix in m:
            if val >= factor:
                break

        return '%.*f%s' % (precision, float(val) / factor, suffix)

    def __cmp__(self, other):
        if self.value < other.value:
            return -1
        if self.value == other.value:
            return 0
        return 1

    def __lt__(self, other):
        if self.value < other.value:
            return True
        return False

    def __le__(self, other):
        if self.value <= other.value:
            return True
        return False

    def __gt__(self, other):
        if self.value > other.value:
            return True
        return False

    def __ge__(self, other):
        if self.value < other.value:
            return True
        return False

    def __eq__(self, other):
        if self.value == other.value:
            return True
        return False

    def __get__(self, instance, other):
        return self.value

    def __add__(self, other):
        if isinstance(other, int):
            self.value += other
        else:
            self.value += other.value
        return self

    def __mul__(self, other):
        if isinstance(other, int):
            self.value *= other
        else:
            self.value *= other.value
        return self

    def __floordiv__(self, other):
        self.value /= other.value
        return self

    def __sub__(self, other):
        self.value -= other.value
        return self

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        return self.encode(valtype=self.unit)

class PbsTypeDuration(str):

    """
    Descriptor class for a duration represented as ``hours``,
    ``minutes``, and ``seconds``,in the form of ``[HH:][MM:]SS``

    :param as_seconds: HH:MM:SS represented in seconds
    :type as_seconds: int
    :param as_str: duration represented in HH:MM:SS
    :type as_str: str
    """

    def __init__(self, val):
        if isinstance(val, str):
            if ':' in val:
                s = val.split(':')
                l = len(s)
                if l > 3:
                    raise ValueError
                hr = mn = sc = 0
                if l >= 2:
                    sc = s[l - 1]
                    mn = s[l - 2]
                    if l == 3:
                        hr = s[0]
                self.duration = int(hr) * 3600 + int(mn) * 60 + int(sc)
            elif val.isdigit():
                self.duration = int(val)
        elif isinstance(val, int) or isinstance(val, float):
            self.duration = val

    def __add__(self, other):
        self.duration += other.duration
        return self

    def __sub__(self, other):
        self.duration -= other.duration
        return self

    def __cmp__(self, other):
        if self.duration < other.duration:
            return -1
        if self.duration == other.duration:
            return 0
        return 1

    def __lt__(self, other):
        if self.duration < other.duration:
            return True
        return False

    def __le__(self, other):
        if self.duration <= other.duration:
            return True
        return False

    def __gt__(self, other):
        if self.duration > other.duration:
            return True
        return False

    def __ge__(self, other):
        if self.duration < other.duration:
            return True
        return False

    def __eq__(self, other):
        if self.duration == other.duration:
            return True
        return False

    def __get__(self, instance, other):
        return self.duration

    def __repr__(self):
        return self.__str__()

    def __int__(self):
        return int(self.duration)

    def __str__(self):
        return str(datetime.timedelta(seconds=self.duration))


class PBSLogUtils(object):

    """
    Miscellaneous utilities to process log files
    """

    logger = logging.getLogger(__name__)

    @classmethod
    def isfloat(cls, value):
        """
        returns true if value is a float or a string representation
        of a float returns false otherwise

        :param value: value to be checked
        :type value: str or int or float
        :returns: True or False
        """
        if isinstance(value, float):
            return True
        if isinstance(value, str):
            try:
                float(value)
                return True
            except ValueError:
                return False

    @classmethod
    def decode_value(cls, value):
        """
        Decode an attribute/resource value, if a value is
        made up of digits only then return the numeric value
        of it, if it is made of alphanumeric values only, return
        it as a string, if it is of type size, i.e., with a memory
        unit such as b,kb,mb,gb then return the converted size to
        kb without the unit

        :param value: attribute/resource value
        :type value: str or int
        :returns: int or float or string
        """

        if value is None or isinstance(value, collections.Callable):
            return value

        if isinstance(value, (int, float)):
            return value

        if value.isdigit():
            return int(value)

        if value.isalpha() or value == '':
            return value

        if cls.isfloat(value):
            return float(value)

        if ':' in value:
            try:
                value = int(PbsTypeDuration(value))
            except ValueError:
                pass
            return value

        # TODO revisit:  assume (this could be the wrong type, need a real
        # data model anyway) that the remaining is a memory expression
        try:
            value = PbsTypeSize(value)
            return value.value
        except ValueError:
            pass
        except TypeError:
            # if not then we pass to return the value as is
            pass

        return value

    @classmethod
    def parse_exechost(cls, s=None):
        """
        Parse an exechost string into a dictionary representation

        :param s: String to be parsed
        :type s: str or None
        :returns: Dictionary format of the exechost string
        """
        if s is None:
            return None

        hosts = []
        hsts = s.split('+')
        for h in hsts:
            hi = {}
            ti = {}
            (host, task) = h.split('/',)
            d = task.split('*')
            if len(d) == 1:
                taskslot = d[0]
                ncpus = 1
            elif len(d) == 2:
                (taskslot, ncpus) = d
            else:
                (taskslot, ncpus) = (0, 1)
            ti['task'] = taskslot
            ti['ncpus'] = ncpus
            hi[host] = ti
            hosts.append(hi)
        return hosts

    @classmethod
    def get_hosts(cls, exechost=None):
        """
        :returns: The hosts portion of the exec_host
        """
        hosts = []
        exechosts = cls.parse_exechost(exechost)
        if exechosts:
            for h in exechosts:
                eh = list(h.keys())[0]
                if eh not in hosts:
                    hosts.append(eh)
        return hosts

    @classmethod
    def convert_date_time(cls, dt=None, fmt=None):
        """
        convert a date time string of the form given by fmt into
        number of seconds since epoch (with possible microseconds).
        it considers the current system's timezone to convert
        the datetime to epoch time

        :param dt: the datetime string to convert
        :type dt: str or None
        :param fmt: Format to which datetime is to be converted
        :type fmt: str
        :returns: timestamp in seconds since epoch,
                or None if conversion fails
        """
        if dt is None:
            return None

        micro = False
        if fmt is None:
            if '.' in dt:
                micro = True
                fmt = "%m/%d/%Y %H:%M:%S.%f"
            else:
                fmt = "%m/%d/%Y %H:%M:%S"

        try:
            # Get datetime object
            t = datetime.strptime(dt, fmt)
            # Get epoch-timestamp assuming local timezone
            tm = t.timestamp()
        except ValueError:
            cls.logger.debug("could not convert date time: " + str(dt))
            return None

        if micro is True:
            return tm
        else:
            return int(tm)

    def get_num_lines(self, log, hostname=None, sudo=False):
        """
        Get the number of lines of particular log

        :param log: the log file name
        :type log: str
        """
        f = self.open_log(log, hostname, sudo=sudo)
        nl = sum([1 for _ in f])
        f.close()
        return nl

    def open_log(self, log, hostname=None, sudo=False):
        """
        :param log: the log file name to read from
        :type log: str
        :param hostname: the hostname from which to read the file
        :type hostname: str or None
        :param sudo: Whether to access log file as a privileged user.
        :type sudo: boolean
        :returns: A file instance
        """
        return open(log)

    def get_timestamps(self, logfile=None, hostname=None, num=None,
                       sudo=False):
        """
        Helper function to parse logfile

        :returns: Each timestamp in a list as number of seconds since epoch
        """
        if logfile is None:
            return

        records = self.open_log(logfile, hostname, sudo=sudo)
        if records is None:
            return

        rec_times = []
        tm_tag = re.compile(tm_re)
        num_rec = 0
        for record in records:
            num_rec += 1
            if num is not None and num_rec > num:
                break

            if type(record) == bytes:
                record = record.decode("utf-8")

            m = tm_tag.match(record)
            if m:
                rec_times.append(
                    self.convert_date_time(m.group('datetime')))
        records.close()
        return rec_times

    def match_msg(self, lines, msg, allmatch=False, regexp=False,
                  starttime=None, endtime=None):
        """
        Returns (x,y) where x is the matching line y, or None if
        nothing is found.

        :param allmatch: If True (False by default), return a list
                         of matching tuples.
        :type allmatch: boolean
        :param regexp: If True, msg is a Python regular expression.
                       Defaults to False.
        :type regexp: bool
        :param starttime: If set ignore matches that occur before
                          specified time
        :param endtime: If set ignore matches that occur after
                        specified time
        """
        linecount = 0
        ret = []
        if lines:
            for l in lines:
                # l.split(';', 1)[0] gets the time stamp string
                dt_str = l.split(';', 1)[0]
                if starttime is not None:
                    tm = self.convert_date_time(dt_str)
                    if tm is None or tm < starttime:
                        continue
                if endtime is not None:
                    tm = self.convert_date_time(dt_str)
                    if tm is None or tm > endtime:
                        continue
                if ((regexp and re.search(msg, l)) or
                        (not regexp and l.find(msg) != -1)):
                    m = (linecount, l)
                    if allmatch:
                        ret.append(m)
                    else:
                        return m
                linecount += 1
        if len(ret) > 0:
            return ret
        return None

    @staticmethod
    def convert_resv_date_time(date_time):
        """
        Convert reservation datetime to seconds
        """
        try:
            t = time.strptime(date_time, "%a %b %d %H:%M:%S %Y")
        except:
            t = time.localtime()
        return int(time.mktime(t))

    @staticmethod
    def convert_hhmmss_time(tm):
        """
        Convert datetime in hhmmss format to seconds
        """
        if ':' not in tm:
            return tm

        hms = tm.split(':')
        return int(int(hms[0]) * 3600 + int(hms[1]) * 60 + int(hms[2]))

    def get_rate(self, l=[]):
        """
        :returns: The frequency of occurrences of array l
                  The array is expected to be sorted
        """
        if len(l) > 0:
            duration = l[len(l) - 1] - l[0]
            if duration > 0:
                tm_factor = [1, 60, 60, 24]
                _rate = float(len(l)) / float(duration)
                index = 0
                while _rate < 1 and index < len(tm_factor):
                    index += 1
                    _rate *= tm_factor[index]
                _rate = "%.2f" % (_rate)
                if index == 0:
                    _rate = str(_rate) + '/s'
                elif index == 1:
                    _rate = str(_rate) + '/mn'
                elif index == 2:
                    _rate = str(_rate) + '/hr'
                else:
                    _rate = str(_rate) + '/day'
            else:
                _rate = str(len(l)) + '/s'
            return _rate
        return 0

    def in_range(self, tm, start=None, end=None):
        """
        :param tm: time to check within a provided range
        :param start: Lower limit for the time range
        :param end: Higer limit for the time range
        :returns: True if time is in the range else return False
        """
        if start is None and end is None:
            return True

        if start is None and end is not None:
            if tm <= end:
                return True
            else:
                return False

        if start is not None and end is None:
            if tm >= start:
                return True
            else:
                return False
        else:
            if tm >= start and tm <= end:
                return True
        return False

    @staticmethod
    def _duration(val=None):
        if val is not None:
            return str(timedelta(seconds=int(float(val))))

    @staticmethod
    def get_day(tm=None):
        """
        :param tm: Time for which to get a day
        """
        if tm is None:
            tm = time.time()
        return time.strftime("%Y%m%d", time.localtime(tm))

    @staticmethod
    def percentile(N, percent):
        """
        Find the percentile of a list of values.

        :param N: A list of values. Note N MUST BE already sorted.
        :type N: List
        :param percent: A float value from 0.0 to 1.0.
        :type percent: Float
        :returns: The percentile of the values
        """
        if not N:
            return None
        k = (len(N) - 1) * percent
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return N[int(k)]
        d0 = N[int(f)] * (c - k)
        d1 = N[int(c)] * (k - f)
        return d0 + d1

    @staticmethod
    def process_intervals(intervals, groups, frequency=60):
        """
        Process the intervals
        """
        info = {}
        if not intervals:
            return info

        val = [x - intervals[i - 1] for i, x in enumerate(intervals) if i > 0]
        info[RI] = ", ".join([str(v) for v in val])
        if intervals:
            info[IT] = intervals[0]
        if frequency is not None:
            _cf = []
            j = 0
            i = 1
            while i < len(intervals):
                if (intervals[i] - intervals[j]) > frequency:
                    _cf.append(((intervals[j], intervals[i - 1]), i - j))
                    j = i
                i += 1
            if i != j + 1:
                _cf.append(((intervals[j], intervals[i - 1]), i - j))
            else:
                _cf.append(((intervals[j], intervals[j]), 1))
            info[CFC] = _cf
            info[CF] = frequency
        if groups:
            info[CG] = groups
        return info

    def get_log_files(self, hostname, path, start, end, sudo=False):
        """
        :param hostname: Hostname of the machine
        :type hostname: str
        :param path: Path for the log file
        :type path: str
        :param start: Start time for the log file
        :param end: End time for the log file
        :returns: list of log file(s) found or an empty list
        """
        paths = []
        if os.path.isdir(path):
            logs = [os.path.join(path, x) for x in os.listdir(path)]
            for f in sorted(logs):
                if start is not None or end is not None:
                    tm = self.get_timestamps(f, hostname, num=1, sudo=sudo)
                    if not tm:
                        continue
                    d1 = time.strftime("%Y%m%d", time.localtime(tm[0]))
                    if start is not None:
                        d2 = time.strftime("%Y%m%d", time.localtime(start))
                        if d1 < d2:
                            continue
                    if end is not None:
                        d2 = time.strftime("%Y%m%d", time.localtime(end))
                        if d1 > d2:
                            continue
                paths.append(f)
        elif os.path.isfile(path):
            paths = [path]

        return paths


class PBSLogAnalyzer(object):
    """
    Utility to analyze the PBS logs
    """
    logger = logging.getLogger(__name__)
    logutils = PBSLogUtils()

    generic_tag = re.compile(tm_re + ".*")
    node_type_tag = re.compile(tm_re + ".*" + "Type 58 request.*")
    queue_type_tag = re.compile(tm_re + ".*" + "Type 20 request.*")
    job_type_tag = re.compile(tm_re + ".*" + "Type 51 request.*")
    job_exit_tag = re.compile(tm_re + ".*" + job_re + "Exit_status.*")

    def __init__(self, schedlog=None, serverlog=None,
                 momlog=None, acctlog=None, genericlog=None,
                 hostname=None, show_progress=False):

        self.hostname = hostname
        self.schedlog = schedlog
        self.serverlog = serverlog
        self.acctlog = acctlog
        self.momlog = momlog
        self.genericlog = genericlog
        self.show_progress = show_progress

        self._custom_tag = None
        self._custom_freq = None
        self._custom_id = False
        self._re_interval = []
        self._re_group = {}

        self.num_conditional_matches = 0
        self.re_conditional = None
        self.num_conditionals = 0
        self.prev_records = []

        self.info = {}

        self.scheduler = None
        self.server = None
        self.mom = None
        self.accounting = None

        if schedlog:
            self.scheduler = PBSSchedulerLog(schedlog, hostname, show_progress)

        if serverlog:
            self.server = PBSServerLog(serverlog, hostname, show_progress)
        if momlog:
            self.mom = PBSMoMLog(momlog, hostname, show_progress)

        if acctlog:
            self.accounting = PBSAccountingLog(acctlog, hostname,
                                               show_progress)

    def set_custom_match(self, pattern, frequency=None):
        """
        Set the custome matching

        :param pattern: Matching pattern
        :type pattern: str
        :param frequency: Frequency of match
        :type frequency: int
        """
        self._custom_tag = re.compile(tm_re + ".*" + pattern + ".*")
        self._custom_freq = frequency

    def set_conditional_match(self, conditions):
        """
        Set the conditional match

        :param conditions: Conditions for macthing
        """
        if not isinstance(conditions, list):
            return False
        self.re_conditional = conditions
        self.num_conditionals = len(conditions)
        self.prev_records = ['' for n in range(self.num_conditionals)]
        self.info['matches'] = []

    def analyze_scheduler_log(self, filename=None, start=None, end=None,
                              hostname=None, summarize=True):
        """
        Analyze the scheduler log

        :param filename: Scheduler log file name
        :type filename: str or None
        :param start: Time from which log to be analyzed
        :param end: Time till which log to be analyzed
        :param hostname: Hostname of the machine
        :type hostname: str or None
        :param summarize: Summarize data parsed if True else not
        :type summarize: bool
        """
        if self.scheduler is None:
            self.scheduler = PBSSchedulerLog(filename, hostname=hostname)
        return self.scheduler.analyze(filename, start, end, hostname,
                                      summarize)

    def analyze_server_log(self, filename=None, start=None, end=None,
                           hostname=None, summarize=True):
        """
        Analyze the server log
        """
        if self.server is None:
            self.server = PBSServerLog(filename, hostname=hostname)

        return self.server.analyze(filename, start, end, hostname,
                                   summarize)

    def analyze_accounting_log(self, filename=None, start=None, end=None,
                               hostname=None, summarize=True):
        """
        Analyze the accounting log
        """
        if self.accounting is None:
            self.accounting = PBSAccountingLog(filename, hostname=hostname)

        return self.accounting.analyze(filename, start, end, hostname,
                                       summarize=summarize, sudo=True)

    def analyze_mom_log(self, filename=None, start=None, end=None,
                        hostname=None, summarize=True):
        """
        Analyze the mom log
        """
        if self.mom is None:
            self.mom = PBSMoMLog(filename, hostname=hostname)

        return self.mom.analyze(filename, start, end, hostname, summarize)

    def parse_conditional(self, rec, start, end):
        """
        Match a sequence of regular expressions against multiple
        consecutive lines in a generic log. Calculate the number
        of conditional matching lines.

        Example usage: to find the number of times the scheduler
        stat'ing the server causes the scheduler to miss jobs ending,
        which could possibly indicate a race condition between the
        view of resources assigned to nodes and the actual jobs
        running, one would call this function by setting
        re_conditional to
        ``['Type 20 request received from Scheduler', 'Exit_status']``
        Which can be read as counting the number of times that the
        Type 20 message is preceded by an ``Exit_status`` message
        """
        match = True
        for rc in range(self.num_conditionals):
            if not re.search(self.re_conditional[rc], self.prev_records[rc]):
                match = False
        if match:
            self.num_conditional_matches += 1
            self.info['matches'].extend(self.prev_records)
        for i in range(self.num_conditionals - 1, -1, -1):
            self.prev_records[i] = self.prev_records[i - 1]
        self.prev_records[0] = rec
        return PARSER_OK_CONTINUE

    def parse_custom_tag(self, rec, start, end):
        m = self._custom_tag.match(rec)
        if m:
            tm = self.logutils.convert_date_time(m.group('datetime'))
            if ((start is None and end is None) or
                    self.logutils.in_range(tm, start, end)):
                self._re_interval.append(tm)
                for k, v in m.groupdict().items():
                    if k in self._re_group:
                        self._re_group[k].append(v)
                    else:
                        self._re_group[k] = [v]
            elif end is not None and tm > end:
                return PARSER_OK_STOP

        return PARSER_OK_CONTINUE

    def parse_block(self, rec, start, end):
        m = self.generic_tag.match(rec)
        if m:
            tm = self.logutils.convert_date_time(m.group('datetime'))
            if self.logutils.in_range(tm, start, end):
                print(rec, end=' ')

    def comp_analyze(self, rec, start, end):
        if self.re_conditional is not None:
            return self.parse_conditional(rec, start, end)
        elif self._custom_tag is not None:
            return self.parse_custom_tag(rec, start, end)
        else:
            return self.parse_block(rec, start, end)

    def analyze(self, path=None, start=None, end=None, hostname=None,
                summarize=True, sudo=False):
        """
        Parse any log file. This method is not ``context-specific``
        to each log file type.

        :param path: name of ``file/dir`` to parse
        :type path: str or None
        :param start: optional record time at which to start analyzing
        :param end: optional record time after which to stop analyzing
        :param hostname: name of host on which to operate. Defaults to
                         localhost
        :type hostname: str or None
        :param summarize: if True, summarize data parsed. Defaults to
                          True.
        :type summarize: bool
        :param sudo: If True, access log file(s) as privileged user.
        :type sudo: bool
        """
        if hostname is None and self.hostname is not None:
            hostname = self.hostname

        if not isinstance(path, (list,)):
            path = [path]

        for p in path:
            for f in self.logutils.get_log_files(hostname, p, start, end,
                                                sudo=sudo):
                self._log_parser(f, start, end, hostname, sudo=sudo)

        if summarize:
            return self.summary()

    def _log_parser(self, filename, start, end, hostname=None, sudo=False):
        if filename is not None:
            records = self.logutils.open_log(filename, hostname, sudo=sudo)
        else:
            return None

        if records is None:
            return None

        num_line = 0
        last_rec = None
        if self.show_progress:
            num_records = self.logutils.get_num_lines(filename,
                                                      hostname,
                                                      sudo=sudo)
            perc_range = list(range(10, 110, 10))
            perc_records = [num_records * x / 100 for x in perc_range]
            sys.stderr.write('Parsing ' + filename + ': |0%')
            sys.stderr.flush()

        for rec in records:
            num_line += 1
            if self.show_progress and (num_line > perc_records[0]):
                sys.stderr.write('-' + str(perc_range[0]) + '%')
                sys.stderr.flush()
                perc_range.remove(perc_range[0])
                perc_records.remove(perc_records[0])
            last_rec = rec
            rv = self.comp_analyze(rec, start, end)
            if (rv in (PARSER_OK_STOP, PARSER_ERROR_STOP) or
                    (self.show_progress and len(perc_records) == 0)):
                break
        if self.show_progress:
            sys.stderr.write('-100%|\n')
            sys.stderr.flush()
        records.close()

        self.epilogue(last_rec)

    def analyze_logs(self, schedlog=None, serverlog=None, momlog=None,
                     acctlog=None, genericlog=None, start=None, end=None,
                     hostname=None, showjob=False):
        """
        Analyze logs
        """
        if hostname is None and self.hostname is not None:
            hostname = self.hostname

        if schedlog is None and self.schedlog is not None:
            schedlog = self.schedlog
        if serverlog is None and self.serverlog is not None:
            serverlog = self.serverlog
        if momlog is None and self.momlog is not None:
            momlog = self.momlog
        if acctlog is None and self.acctlog is not None:
            acctlog = self.acctlog
        if genericlog is None and self.genericlog is not None:
            genericlog = self.genericlog

        cycles = None
        sjr = {}

        if schedlog:
            self.analyze_scheduler_log(schedlog, start, end, hostname,
                                       summarize=False)
            cycles = self.scheduler.cycles

        if serverlog:
            self.analyze_server_log(serverlog, start, end, hostname,
                                    summarize=False)
            sjr = self.server.server_job_run

        if momlog:
            self.analyze_mom_log(momlog, start, end, hostname,
                                 summarize=False)

        if acctlog:
            self.analyze_accounting_log(acctlog, start, end, hostname,
                                        summarize=False)

        if genericlog:
            self.analyze(genericlog, start, end, hostname, sudo=True,
                         summarize=False)

        if cycles is not None and len(sjr.keys()) != 0:
            for cycle in cycles:
                for jid, tm in cycle.sched_job_run.items():
                    # skip job arrays: scheduler runs a subjob
                    # but we don't keep track of which Considering job to run
                    # message it is associated with because the consider
                    # message doesn't show the subjob
                    if '[' in jid:
                        continue
                    if jid in sjr:
                        for tm in sjr[jid]:
                            if tm > cycle.start and tm < cycle.end:
                                cycle.inschedduration[jid] = \
                                    tm - cycle.consider[jid]

        return self.summary(showjob)

    def epilogue(self, line):
        pass

    def summary(self, showjob=False, writer=None):

        info = {}

        if self._custom_tag is not None:
            self.info = self.logutils.process_intervals(self._re_interval,
                                                        self._re_group,
                                                        self._custom_freq)
            return self.info

        if self.re_conditional is not None:
            self.info['num_conditional_matches'] = self.num_conditional_matches
            return self.info

        if self.scheduler is not None:
            info['scheduler'] = self.scheduler.summary(self.scheduler.cycles,
                                                       showjob)
        if self.server is not None:
            info['server'] = self.server.summary()

        if self.accounting is not None:
            info['accounting'] = self.accounting.summary()

        if self.mom is not None:
            info['mom'] = self.mom.summary()

        return info


class PBSServerLog(PBSLogAnalyzer):
    """
    :param filename: Server log filename
    :type filename: str or None
    :param hostname: Hostname of the machine
    :type hostname: str or None
    """
    tm_tag = re.compile(tm_re)
    server_run_tag = re.compile(tm_re + ".*" + job_re + "Job Run at.*")
    server_nodeup_tag = re.compile(tm_re + ".*Node;.*;node up.*")
    server_enquejob_tag = re.compile(tm_re + ".*" + job_re +
                                     "enqueuing into.*state Q .*")
    server_endjob_tag = re.compile(tm_re + ".*" + job_re +
                                   "Exit_status.*")

    def __init__(self, filename=None, hostname=None, show_progress=False):

        self.server_job_queued = {}
        self.server_job_run = {}
        self.server_job_end = {}
        self.records = None
        self.nodeup = []
        self.enquejob = []
        self.record_tm = []
        self.jobsrun = []
        self.jobsend = []
        self.wait_time = []
        self.run_time = []

        self.hostname = hostname

        self.info = {}
        self.version = []

        self.filename = filename
        self.show_progress = show_progress

    def parse_runjob(self, line):
        """
        Parse server log for run job records.
        For each record keep track of the job id, and time in a
        dedicated array
        """
        m = self.server_run_tag.match(line)
        if m:
            tm = self.logutils.convert_date_time(m.group('datetime'))
            self.jobsrun.append(tm)
            jobid = str(m.group('jobid'))
            if jobid in self.server_job_run:
                self.server_job_run[jobid].append(tm)
            else:
                self.server_job_run[jobid] = [tm]
            if jobid in self.server_job_queued:
                self.wait_time.append(tm - self.server_job_queued[jobid])

    def parse_endjob(self, line):
        """
        Parse server log for run job records.
        For each record keep track of the job id, and time in a
        dedicated array
        """
        m = self.server_endjob_tag.match(line)
        if m:
            tm = self.logutils.convert_date_time(m.group('datetime'))
            self.jobsend.append(tm)
            jobid = str(m.group('jobid'))
            if jobid in self.server_job_end:
                self.server_job_end[jobid].append(tm)
            else:
                self.server_job_end[jobid] = [tm]
            if jobid in self.server_job_run:
                self.run_time.append(tm - self.server_job_run[jobid][-1:][0])

    def parse_nodeup(self, line):
        """
        Parse server log for nodes that are up
        """
        m = self.server_nodeup_tag.match(line)
        if m:
            tm = self.logutils.convert_date_time(m.group('datetime'))
            self.nodeup.append(tm)

    def parse_enquejob(self, line):
        """
        Parse server log for enqued jobs
        """
        m = self.server_enquejob_tag.match(line)
        if m:
            tm = self.logutils.convert_date_time(m.group('datetime'))
            self.enquejob.append(tm)
            jobid = str(m.group('jobid'))
            self.server_job_queued[jobid] = tm

    def comp_analyze(self, rec, start=None, end=None):
        m = self.tm_tag.match(rec)
        if m:
            tm = self.logutils.convert_date_time(m.group('datetime'))
            self.record_tm.append(tm)
            if not self.logutils.in_range(tm, start, end):
                if end and tm > end:
                    return PARSER_OK_STOP
                return PARSER_OK_CONTINUE

        if 'pbs_version=' in rec:
            version = rec.split('pbs_version=')[1].strip()
            if version not in self.version:
                self.version.append(version)
        self.parse_enquejob(rec)
        self.parse_nodeup(rec)
        self.parse_runjob(rec)
        self.parse_endjob(rec)

        return PARSER_OK_CONTINUE

    def epilogue(self, line):
        self.record_tm = sorted(self.record_tm)
        self.enquejob = sorted(self.enquejob)
        self.nodeup = sorted(self.nodeup)
        self.jobsrun = sorted(self.jobsrun)
        self.jobsend = sorted(self.jobsend)
        self.wait_time = sorted(self.wait_time)
        self.run_time = sorted(self.run_time)

    def summary(self):
        self.info[JSR] = self.logutils.get_rate(self.enquejob)
        self.info[NJE] = len(self.server_job_end)
        self.info[NJQ] = len(self.enquejob)
        self.info[NUR] = self.logutils.get_rate(self.nodeup)
        self.info[JRR] = self.logutils.get_rate(self.jobsrun)
        self.info[JER] = self.logutils.get_rate(self.jobsend)
        if len(self.server_job_end) > 0:
            tjr = self.jobsend[-1] - self.enquejob[0]
            self.info[JTR] = "%.2f/s" % (len(self.server_job_end) / tjr)
        if len(self.wait_time) > 0:
            wt = self.wait_time
            wta = float(sum(wt)) / len(wt)
            self.info[JWTm] = self.logutils._duration(min(wt))
            self.info[JWTM] = self.logutils._duration(max(wt))
            self.info[JWTA] = self.logutils._duration(wta)
            self.info[JWT25] = self.logutils._duration(
                self.logutils.percentile(wt, .25))
            self.info[JWT50] = self.logutils._duration(
                self.logutils.percentile(wt, .5))
            self.info[JWT75] = self.logutils._duration(
                self.logutils.percentile(wt, .75))
        njr = 0
        for v in self.server_job_run.values():
            njr += len(v)
        self.info[NJR] = njr
        self.info[VER] = ",".join(self.version)

        if len(self.run_time) > 0:
            rt = self.run_time
            self.info[JRTm] = self.logutils._duration(min(rt))
            self.info[JRT25] = self.logutils._duration(
                self.logutils.percentile(rt, 0.25))
            self.info[JRT50] = self.logutils._duration(
                self.logutils.percentile(rt, 0.50))
            self.info[JRTA] = self.logutils._duration(
                str(sum(rt) / len(rt)))
            self.info[JRT75] = self.logutils._duration(
                self.logutils.percentile(rt, 0.75))
            self.info[JRTM] = self.logutils._duration(max(rt))
        return self.info


class JobEstimatedStartTimeInfo(object):
    """
    Information regarding Job estimated start time
    """

    def __init__(self, jobid):
        self.jobid = jobid
        self.started_at = None
        self.estimated_at = []
        self.num_drifts = 0
        self.num_estimates = 0
        self.drift_time = 0

    def add_estimate(self, tm):
        """
        Add a job's new estimated start time
        If the new estimate is now later than any preivous one, we
        add that difference to the drift time. If the new drift time
        is pulled earlier it is not added to the drift time.

        drift time is a measure of ``"negative perception"`` that
        comes along a job being estimated to run at a later date than
        earlier ``"advertised"``.
        """
        if self.estimated_at:
            prev_tm = self.estimated_at[len(self.estimated_at) - 1]
            if tm > prev_tm:
                self.num_drifts += 1
                self.drift_time += tm - prev_tm

        self.estimated_at.append(tm)
        self.num_estimates += 1

    def __repr__(self):
        estimated_at_str = [str(t) for t in self.estimated_at]
        return " ".join([str(self.jobid), 'started: ', str(self.started_at),
                         'estimated: ', ",".join(estimated_at_str)])

    def __str__(self):
        return self.__repr__()


class PBSSchedulerLog(PBSLogAnalyzer):

    tm_tag = re.compile(tm_re)
    startcycle_tag = re.compile(tm_re + ".*Starting Scheduling.*")
    endcycle_tag = re.compile(tm_re + ".*Leaving [(the )]*[sS]cheduling.*")
    alarm_tag = re.compile(tm_re + ".*alarm.*")
    considering_job_tag = re.compile(tm_re + ".*" + job_re +
                                     "Considering job to run.*")
    sched_job_run_tag = re.compile(tm_re + ".*" + job_re + "Job run.*")
    estimated_tag = re.compile(tm_re + ".*" + job_re +
                               "Job is a top job and will run at "
                               "(?P<est_tm>.*)")
    run_failure_tag = re.compile(tm_re + ".*" + fail_re + "Failed to run.*")
    calendarjob_tag = re.compile(
        tm_re +
        ".*" +
        job_re +
        "Job is a top job.*")
    preempt_failure_tag = re.compile(tm_re + ".*;Job failed to be preempted.*")
    preempt_tag = re.compile(tm_re + ".*" + job_re + "Job preempted.*")
    record_tag = re.compile(tm_re + ".*")

    def __init__(self, filename=None, hostname=None, show_progress=False):

        self.filename = filename
        self.hostname = hostname
        self.show_progress = show_progress

        self.record_tm = []
        self.version = []

        self.cycle = None
        self.cycles = []

        self.estimated_jobs = {}
        self.estimated_parsing_enabled = False
        self.parse_estimated_only = False

        self.info = {}
        self.summary_info = {}

    def _parse_line(self, line):
        """
        Parse scheduling cycle Starting, Leaving, and alarm records
        From each record, keep track of the record time in a
        dedicated array
        """
        m = self.startcycle_tag.match(line)

        if m:
            tm = self.logutils.convert_date_time(m.group('datetime'))
            # if cycle was interrupted assume previous cycle ended now
            if self.cycle is not None and self.cycle.end == -1:
                self.cycle.end = tm
            self.cycle = PBSCycleInfo()
            self.cycles.append(self.cycle)
            self.cycle.start = tm
            self.cycle.end = -1
            return PARSER_OK_CONTINUE

        m = self.endcycle_tag.match(line)
        if m is not None and self.cycle is not None:
            tm = self.logutils.convert_date_time(m.group('datetime'))
            self.cycle.end = tm
            self.cycle.duration = tm - self.cycle.start
            if (self.cycle.lastjob is not None and
                    self.cycle.lastjob not in self.cycle.sched_job_run and
                    self.cycle.lastjob not in self.cycle.calendared_jobs):
                self.cycle.cantrunduration[self.cycle.lastjob] = (
                    tm - self.cycle.consider[self.cycle.lastjob])
            return PARSER_OK_CONTINUE

        m = self.alarm_tag.match(line)
        if m is not None and self.cycle is not None:
            tm = self.logutils.convert_date_time(m.group('datetime'))
            self.cycle.end = tm
            return PARSER_OK_CONTINUE

        m = self.considering_job_tag.match(line)
        if m is not None and self.cycle is not None:
            self.cycle.num_considered += 1
            jid = str(m.group('jobid'))
            tm = self.logutils.convert_date_time(m.group('datetime'))
            self.cycle.consider[jid] = tm
            self.cycle.political_order.append(jid)
            if (self.cycle.lastjob is not None and
                    self.cycle.lastjob not in self.cycle.sched_job_run and
                    self.cycle.lastjob not in self.cycle.calendared_jobs):
                self.cycle.cantrunduration[self.cycle.lastjob] = (
                    tm - self.cycle.consider[self.cycle.lastjob])
            self.cycle.lastjob = jid
            if self.cycle.queryduration == 0:
                self.cycle.queryduration = tm - self.cycle.start
            return PARSER_OK_CONTINUE

        m = self.sched_job_run_tag.match(line)
        if m is not None and self.cycle is not None:
            jid = str(m.group('jobid'))
            tm = self.logutils.convert_date_time(m.group('datetime'))
            self.cycle.sched_job_run[jid] = tm
            # job arrays require special handling because the considering
            # job to run message does not have the subjob index but only []
            if '[' in jid:
                subjid = jid
                if subjid not in self.cycle.consider:
                    jid = jid.split('[')[0] + '[]'
                    self.cycle.consider[subjid] = self.cycle.consider[jid]
                self.cycle.runduration[subjid] = tm - self.cycle.consider[jid]
            # job rerun due to preemption failure aren't considered, skip
            elif jid in self.cycle.consider:
                self.cycle.runduration[jid] = tm - self.cycle.consider[jid]
            return PARSER_OK_CONTINUE

        m = self.run_failure_tag.match(line)
        if m is not None:
            if self.cycle is not None:
                jid = str(m.group('jobid'))
                tm = self.logutils.convert_date_time(m.group('datetime'))
                self.cycle.run_failure[jid] = tm
            return PARSER_OK_CONTINUE

        m = self.preempt_failure_tag.match(line)
        if m is not None:
            if self.cycle is not None:
                self.cycle.num_preempt_failure += 1
            return PARSER_OK_CONTINUE

        m = self.preempt_tag.match(line)
        if m is not None:
            if self.cycle is not None:
                jid = str(m.group('jobid'))
                if self.cycle.lastjob in self.cycle.preempted_jobs:
                    self.cycle.preempted_jobs[self.cycle.lastjob].append(jid)
                else:
                    self.cycle.preempted_jobs[self.cycle.lastjob] = [jid]
                self.cycle.num_preempted += 1
            return PARSER_OK_CONTINUE

        m = self.calendarjob_tag.match(line)
        if m is not None:
            if self.cycle is not None:
                jid = str(m.group('jobid'))
                tm = self.logutils.convert_date_time(m.group('datetime'))
                self.cycle.calendared_jobs[jid] = tm
                if jid in self.cycle.consider:
                    self.cycle.calendarduration[jid] = \
                        (tm - self.cycle.consider[jid])
                elif '[' in jid:
                    arrjid = re.sub(r"(\[\d+\])", '[]', jid)
                    if arrjid in self.cycle.consider:
                        self.cycle.consider[jid] = self.cycle.consider[arrjid]
                        self.cycle.calendarduration[jid] = \
                            (tm - self.cycle.consider[arrjid])
            return PARSER_OK_CONTINUE

    def get_cycles(self, start=None, end=None):
        """
        Get the scheduler cycles

        :param start: Start time
        :param end: End time
        :returns: Scheduling cycles
        """
        if start is None and end is None:
            return self.cycles

        cycles = []
        if end is None:
            end = time.time()
        for c in self.cycles:
            if c.start >= start and c.end < end:
                cycles.append(c)
        return cycles

    def comp_analyze(self, rec, start, end):
        if self.estimated_parsing_enabled:
            rv = self.estimated_info_parsing(rec)
            if self.parse_estimated_only:
                return rv
        return self.scheduler_parsing(rec, start, end)

    def scheduler_parsing(self, rec, start, end):
        m = self.tm_tag.match(rec)
        if m:
            tm = self.logutils.convert_date_time(m.group('datetime'))
            self.record_tm.append(tm)
            if self.logutils.in_range(tm, start, end):
                rv = self._parse_line(rec)
                if rv in (PARSER_OK_STOP, PARSER_ERROR_STOP):
                    return rv
            if 'pbs_version=' in rec:
                version = rec.split('pbs_version=')[1].strip()
                if version not in self.version:
                    self.version.append(version)
        elif end is not None and tm > end:
            PARSER_OK_STOP

        return PARSER_OK_CONTINUE

    def estimated_info_parsing(self, line):
        """
        Parse Estimated start time information for a job
        """
        m = self.sched_job_run_tag.match(line)
        if m is not None:
            jid = str(m.group('jobid'))
            tm = self.logutils.convert_date_time(m.group('datetime'))
            if jid in self.estimated_jobs:
                self.estimated_jobs[jid].started_at = tm
            else:
                ej = JobEstimatedStartTimeInfo(jid)
                ej.started_at = tm
                self.estimated_jobs[jid] = ej

        m = self.estimated_tag.match(line)
        if m is not None:
            jid = str(m.group('jobid'))
            try:
                tm = self.logutils.convert_date_time(m.group('est_tm'),
                                                     "%a %b %d %H:%M:%S %Y")
            except:
                logging.error('error converting time: ' +
                              str(m.group('est_tm')))
                return PARSER_ERROR_STOP

            if jid in self.estimated_jobs:
                self.estimated_jobs[jid].add_estimate(tm)
            else:
                ej = JobEstimatedStartTimeInfo(jid)
                ej.add_estimate(tm)
                self.estimated_jobs[jid] = ej

        return PARSER_OK_CONTINUE

    def epilogue(self, line):
        # if log ends in the middle of a cycle there is no 'Leaving cycle'
        # message, in this case the last cycle duration is computed as
        # from start to the last record in the log file
        if (line is not None and self.cycle is not None and
                self.cycle.end <= 0):
            m = self.record_tag.match(line)
            if m:
                self.cycle.end = self.logutils.convert_date_time(
                    m.group('datetime'))

    def summarize_estimated_analysis(self, estimated_jobs=None):
        """
        Summarize estimated job analysis
        """
        if estimated_jobs is None and self.estimated_jobs is not None:
            estimated_jobs = self.estimated_jobs

        einfo = {EJ: []}
        sub15mn = 0
        sub1hr = 0
        sub3hr = 0
        sup3hr = 0
        total_drifters = 0
        total_nondrifters = 0
        drift_times = []
        for e in estimated_jobs.values():
            info = {}
            if len(e.estimated_at) > 0:
                info[JID] = e.jobid
                e_sorted = sorted(e.estimated_at)
                info[Eat] = e.estimated_at
                if e.started_at is not None:
                    info[JST] = e.started_at
                    e_diff = e_sorted[len(e_sorted) - 1] - e_sorted[0]
                    e_accuracy = (e.started_at -
                                  e.estimated_at[len(e.estimated_at) - 1])
                    info[ESTR] = e_diff
                    info[ESTA] = e_accuracy

                info[NEST] = e.num_estimates
                info[ND] = e.num_drifts
                info[JDD] = e.drift_time
                drift_times.append(e.drift_time)

                if e.drift_time > 0:
                    total_drifters += 1
                    if e.drift_time < 15 * 60:
                        sub15mn += 1
                    elif e.drift_time < 3600:
                        sub1hr += 1
                    elif e.drift_time < 3 * 3600:
                        sub3hr += 1
                    else:
                        sup3hr += 1
                else:
                    total_nondrifters += 1
                einfo[EJ].append(info)

        info = {}
        info[Ds15mn] = sub15mn
        info[Ds1hr] = sub1hr
        info[Ds3hr] = sub3hr
        info[Do3hr] = sup3hr
        info[NJD] = total_drifters
        info[NJND] = total_nondrifters
        if drift_times:
            info[DDm] = min(drift_times)
            info[DDM] = max(drift_times)
            info[DDA] = (sum(drift_times) / len(drift_times))
            info[DD50] = sorted(drift_times)[len(drift_times) / 2]
        einfo[ESTS] = info

        return einfo

    def summary(self, cycles=None, showjobs=False):
        """
        Scheduler log summary
        """
        if self.estimated_parsing_enabled:
            self.info[EST] = self.summarize_estimated_analysis()
            if self.parse_estimated_only:
                return self.info

        if cycles is None and self.cycles is not None:
            cycles = self.cycles

        num_cycle = 0
        run = 0
        failed = 0
        total_considered = 0
        run_tm = []
        cycle_duration = []
        min_duration = None
        max_duration = None
        mint = maxt = None
        calendarduration = 0
        schedsolvertime = 0

        for c in cycles:
            c.summary(showjobs)
            self.info[str(num_cycle)] = c.info
            run += len(c.sched_job_run.keys())
            run_tm.extend(list(c.sched_job_run.values()))
            failed += len(c.run_failure.keys())
            total_considered += c.num_considered

            if max_duration is None or c.duration > max_duration:
                max_duration = c.duration
                maxt = time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(c.start))

            if min_duration is None or c.duration < min_duration:
                min_duration = c.duration
                mint = time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(c.start))

            cycle_duration.append(c.duration)
            num_cycle += 1
            calendarduration += sum(c.calendarduration.values())
            schedsolvertime += c.scheduler_solver_time

        run_rate = self.logutils.get_rate(sorted(run_tm))

        sorted_cd = sorted(cycle_duration)

        self.summary_info[NC] = len(cycles)
        self.summary_info[NJR] = run
        self.summary_info[NJFR] = failed
        self.summary_info[JRR] = run_rate
        self.summary_info[NJC] = total_considered
        self.summary_info[mCD] = self.logutils._duration(min_duration)
        self.summary_info[MCD] = self.logutils._duration(max_duration)
        self.summary_info[CD25] = self.logutils._duration(
            self.logutils.percentile(sorted_cd, .25))
        if len(sorted_cd) > 0:
            self.summary_info[CDA] = self.logutils._duration(
                sum(sorted_cd) / len(sorted_cd))
        self.summary_info[CD50] = self.logutils._duration(
            self.logutils.percentile(sorted_cd, .5))
        self.summary_info[CD75] = self.logutils._duration(
            self.logutils.percentile(sorted_cd, .75))

        if mint is not None:
            self.summary_info[mCT] = mint
        if maxt is not None:
            self.summary_info[MCT] = maxt
        self.summary_info[DUR] = self.logutils._duration(sum(cycle_duration))
        self.summary_info[TTC] = self.logutils._duration(calendarduration)
        self.summary_info[SST] = self.logutils._duration(schedsolvertime)
        self.summary_info[VER] = ",".join(self.version)

        self.info['summary'] = dict(self.summary_info.items())
        return self.info


class PBSCycleInfo(object):

    def __init__(self):

        self.info = {}

        """
        Time between end and start of a cycle, which may be on alarm,
        or signal, not only Leaving - Starting
        """
        self.duration = 0
        " Time of a Starting scheduling cycle message "
        self.start = 0
        " Time of a Leaving scheduling cycle message "
        self.end = 0
        " Time at which Considering job to run message "
        self.consider = {}
        " Number of jobs considered "
        self.num_considered = 0
        " Time at which job run message in scheduler. This includes time to "
        " start the job by the server "
        self.sched_job_run = {}
        """
        number of jobs added to the calendar, i.e.,
        number of backfilling jobs
        """
        self.calendared_jobs = {}
        " Time between Considering job to run to Job run message "
        self.runduration = {}
        " Time to determine that job couldn't run "
        self.cantrunduration = {}
        " List of jobs preempted in order to run high priority job"
        self.preempted_jobs = {}
        """
        Time between considering job to run to server logging
        'Job Run at request...
        """
        self.inschedduration = {}
        " Total time spent in scheduler solver, insched + cantrun + calendar"
        self.scheduler_solver_time = 0
        " Error 15XXX in the sched log corresponds to a failure to run"
        self.run_failure = {}
        " Job failed to be preempted"
        self.num_preempt_failure = 0
        " Job preempted by "
        self.num_preempted = 0
        " Time between start of cycle and first job considered to run "
        self.queryduration = 0
        " The order in which jobs are considered "
        self.political_order = []
        " Time to calendar "
        self.calendarduration = {}

        self.lastjob = None

    def summary(self, showjobs=False):
        """
        Summary regarding cycle
        """
        self.info[CST] = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(self.start))
        self.info[CD] = PBSLogUtils._duration(self.end - self.start)
        self.info[QD] = PBSLogUtils._duration(self.queryduration)
        # number of jobs considered may be different than length of
        # the consider dictionary due to job arrays being considered once
        # per subjob using the parent array job id
        self.info[NJC] = self.num_considered
        self.info[NJR] = len(self.sched_job_run.keys())
        self.info[NJFR] = len(self.run_failure)
        self.scheduler_solver_time = (sum(self.inschedduration.values()) +
                                      sum(self.cantrunduration.values()) +
                                      sum(self.calendarduration.values()))
        self.info[SST] = self.scheduler_solver_time
        self.info[NJCAL] = len(self.calendared_jobs.keys())
        self.info[NJFP] = self.num_preempt_failure
        self.info[NJP] = self.num_preempted
        self.info[TTC] = sum(self.calendarduration.values())

        if showjobs:
            for j in self.consider.keys():
                s = {JID: j}
                if j in self.runduration:
                    s[T2R] = self.runduration[j]
                if j in self.cantrunduration:
                    s[T2D] = self.cantrunduration[j]
                if j in self.inschedduration:
                    s[TiS] = self.inschedduration[j]
                if j in self.calendarduration:
                    s[TTC] = self.calendarduration[j]
                if 'jobs' in self.info:
                    self.info['jobs'].append(s)
                else:
                    self.info['jobs'] = [s]


class PBSMoMLog(PBSLogAnalyzer):

    """
    Container and Parser of a PBS ``MoM`` log
    """
    tm_tag = re.compile(tm_re)
    mom_run_tag = re.compile(tm_re + ".*" + job_re + "Started, pid.*")
    mom_end_tag = re.compile(tm_re + ".*" + job_re +
                             "delete job request received.*")
    mom_enquejob_tag = re.compile(tm_re + ".*;Type 5 .*")

    def __init__(self, filename=None, hostname=None, show_progress=False):

        self.filename = filename
        self.hostname = hostname
        self.show_progress = show_progress

        self.start = []
        self.end = []
        self.queued = []

        self.info = {}
        self.version = []

    def comp_analyze(self, rec, start, end):
        m = self.mom_run_tag.match(rec)
        if m:
            tm = self.logutils.convert_date_time(m.group('datetime'))
            if ((start is None and end is None) or
                    self.logutils.in_range(tm, start, end)):
                self.start.append(tm)
                return PARSER_OK_CONTINUE
            elif end is not None and tm > end:
                return PARSER_OK_STOP

        m = self.mom_end_tag.match(rec)
        if m:
            tm = self.logutils.convert_date_time(m.group('datetime'))
            if ((start is None and end is None) or
                    self.logutils.in_range(tm, start, end)):
                self.end.append(tm)
                return PARSER_OK_CONTINUE
            elif end is not None and tm > end:
                return PARSER_OK_STOP

        m = self.mom_enquejob_tag.match(rec)
        if m:
            tm = self.logutils.convert_date_time(m.group('datetime'))
            if ((start is None and end is None) or
                    self.logutils.in_range(tm, start, end)):
                self.queued.append(tm)
                return PARSER_OK_CONTINUE
            elif end is not None and tm > end:
                return PARSER_OK_STOP

        if 'pbs_version=' in rec:
            version = rec.split('pbs_version=')[1].strip()
            if version not in self.version:
                self.version.append(version)

        return PARSER_OK_CONTINUE

    def epilogue(self, line):
        self.start = sorted(self.start)
        self.queued = sorted(self.queued)
        self.end = sorted(self.end)

    def summary(self):
        """
        Mom log summary
        """
        run_rate = self.logutils.get_rate(self.start)
        queue_rate = self.logutils.get_rate(self.queued)
        end_rate = self.logutils.get_rate(self.end)

        self.info[NJQ] = len(self.queued)
        self.info[NJR] = len(self.start)
        self.info[NJE] = len(self.end)
        self.info[JRR] = run_rate
        self.info[JSR] = queue_rate
        self.info[JER] = end_rate
        self.info[VER] = ",".join(self.version)

        return self.info


class PBSAccountingLog(PBSLogAnalyzer):

    """
    Container and Parser of a PBS accounting log
    """

    tm_tag = re.compile(tm_re)

    record_tag = re.compile(r"""
                        (?P<date>\d\d/\d\d/\d{4,4})[\s]+
                        (?P<time>\d\d:\d\d:\d\d);
                        (?P<type>[A-Z]);
                        (?P<id>[0-9\[\]].*);
                        (?P<msg>.*)
                        """, re.VERBOSE)

    Q_sub_record_tag = re.compile(r"""
                        .*user=(?P<user>[\w\d]+)[\s]+
                        .*qtime=(?P<qtime>[0-9]+)[\s]+
                        .*
                        """, re.VERBOSE)

    S_sub_record_tag = re.compile(r"""
                        .*user=(?P<user>[\w\d]+)[\s]+
                        .*qtime=(?P<qtime>[0-9]+)[\s]+
                        .*start=(?P<start>[0-9]+)[\s]+
                        .*exec_host=(?P<exechost>[\[\],\-\=\/\.\w/*\d\+]+)[\s]+
                        .*Resource_List.ncpus=(?P<ncpus>[0-9]+)[\s]+
                        .*
                        """, re.VERBOSE)

    E_sub_record_tag = re.compile(r"""
                        .*user=(?P<user>[\w\d]+)[\s]+
                        .*qtime=(?P<qtime>[0-9]+)[\s]+
                        .*start=(?P<start>[0-9]+)[\s]+
                        .*exec_host=(?P<exechost>[\[\],\-\=\/\.\w/*\d\+]+)[\s]+
                        .*Resource_List.ncpus=(?P<ncpus>[0-9]+)[\s]+
                        .*end=(?P<end>[0-9]+)[\s]+
                        .*resources_used.walltime=(?P<walltime>[0-9:]+)
                        .*
                        """, re.VERBOSE)

    sub_record_tag = re.compile(r"""
                .*qtime=(?P<qtime>[0-9]+)[\s]+
                .*start=(?P<start>[0-9]+)[\s]+
                .*exec_host=(?P<exechost>[\[\],\-\=\/\.\w/*\d\+]+)[\s]+
                .*exec_vnode=(?P<execvnode>[\(\)\[\],:\-\=\/\.\w/*\d\+]+)[\s]+
                .*Resource_List.ncpus=(?P<ncpus>[\d]+)[\s]+
                .*
                """, re.VERBOSE)

    logger = logging.getLogger(__name__)

    def __init__(self, filename=None, hostname=None, show_progress=False):

        self.filename = filename
        self.hostname = hostname
        self.show_progress = show_progress

        self.record_tm = []

        self.entries = {}
        self.queue = []
        self.start = []
        self.end = []
        self.wait_time = []
        self.run_time = []
        self.job_node_size = []
        self.job_cpu_size = []
        self.used_cph = 0
        self.nodes_cph = 0
        self.used_nph = 0
        self.jobs_started = []
        self.jobs_ended = []
        self.users = {}
        self.tmp_wait_time = {}

        self.duration = 0

        self.utilization_parsing = False
        self.running_jobs_parsing = False
        self.job_info_parsing = False
        self.accounting_workload_parsing = False

        self._total_ncpus = 0
        self._num_nodes = 0
        self._running_jobids = []
        self._server = None

        self.running_jobs = {}
        self.job_start = {}
        self.job_end = {}
        self.job_queue = {}
        self.job_nodes = {}
        self.job_cpus = {}
        self.job_rectypes = {}

        self.job_attrs = {}
        self.parser_errors = 0

        self.info = {}

    def enable_running_jobs_parsing(self):
        """
        Enable parsing for running jobs
        """
        self.running_jobs_parsing = True

    def enable_utilization_parsing(self, hostname=None, nodesfile=None,
                                   jobsfile=None):
        """
        Enable utilization parsing

        :param hostname: Hostname of the machine
        :type hostname: str or None
        :param nodesfile: optional file containing output of
                          pbsnodes -av
        :type nodesfile: str or None
        :param jobsfile: optional file containing output of
                         qstat -f
        :type jobsfile: str or None
        """
        self.utilization_parsing = True

    def enable_job_info_parsing(self):
        """
        Enable job information parsing
        """
        self.job_info_res = {}
        self.job_info_parsing = True

    def enable_accounting_workload_parsing(self):
        """
        Enable accounting workload parsing
        """
        self.accounting_workload_parsing = True

    def comp_analyze(self, rec, start, end, **kwargs):
        if self.job_info_parsing:
            return self.job_info(rec)
        else:
            return self.accounting_parsing(rec, start, end)

    def accounting_parsing(self, rec, start, end):
        """
        Parsing accounting log
        """
        r = self.record_tag.match(rec)
        if not r:
            return PARSER_ERROR_CONTINUE

        tm = self.logutils.convert_date_time(r.group('date') +
                                             ' ' + r.group('time'))
        if not self.logutils.in_range(tm, start, end):
            return PARSER_OK_STOP

        self.record_tm.append(tm)
        rec_type = r.group('type')
        jobid = r.group('id')

        if not self.accounting_workload_parsing and rec_type == 'S':
            # Precompute metrics about the S record just in case
            # it does not have an E record. The differences are
            # resolved after all records are processed
            if jobid in self._running_jobids:
                self._running_jobids.remove(jobid)
            m = self.S_sub_record_tag.match(r.group('msg'))
            if m:
                self.users[jobid] = m.group('user')
                qtime = int(m.group('qtime'))
                starttime = int(m.group('start'))
                ncpus = int(m.group('ncpus'))
                self.job_cpus[jobid] = ncpus

                if starttime != 0 and qtime != 0:
                    self.tmp_wait_time[jobid] = starttime - qtime
                    self.job_start[jobid] = starttime
                    self.job_queue[jobid] = qtime
                ehost = m.group('exechost')
                self.job_nodes[jobid] = self.logutils.get_hosts(ehost)
        elif rec_type == 'E':
            if self.accounting_workload_parsing:
                try:
                    msg = r.group('msg').split()
                    attrs = dict([l.split('=', 1) for l in msg])
                except:
                    self.parser_errors += 1
                    return PARSER_OK_CONTINUE
                for k in attrs.keys():
                    attrs[k] = self.logutils.decode_value(attrs[k])
                running_time = (int(attrs['end']) - int(attrs['start']))
                attrs['running_time'] = str(running_time)
                attrs['schedselect'] = attrs['Resource_List.select']
                if 'euser' not in attrs:
                    attrs['euser'] = 'unknown_user'

                attrs['id'] = r.group('id')
                self.job_attrs[r.group('id')] = attrs

            m = self.E_sub_record_tag.match(r.group('msg'))
            if m:
                if jobid not in self.users:
                    self.users[jobid] = m.group('user')
                ehost = m.group('exechost')
                self.job_nodes[jobid] = PBSLogUtils.get_hosts(ehost)
                ncpus = int(m.group('ncpus'))
                self.job_cpus[jobid] = ncpus
                end = int(m.group('end'))
                qtime = int(m.group('qtime'))
                starttime = int(m.group('start'))
                self.job_end[jobid] = end
                self.job_queue[jobid] = qtime
                if starttime != 0 and qtime != 0:
                    # jobs enqueued prior to start of time range
                    # considered should be reset to start of time
                    # range. Only matters when computing
                    # utilization
                    if (self.utilization_parsing and
                            qtime < self.record_tm[0]):
                        qtime = self.record_tm[0]
                        if starttime < self.record_tm[0]:
                            starttime = self.record_tm[0]
                    self.wait_time.append(starttime - qtime)
                    if m.group('walltime'):
                        try:
                            walltime = self.logutils.convert_hhmmss_time(
                                m.group('walltime').strip())
                            self.run_time.append(walltime)
                        except:
                            pass
                    else:
                        walltime = end - starttime
                        self.run_time.append(walltime)

                    if self.utilization_parsing:
                        self.used_cph += ncpus * (walltime / 60)
                        if self.logutils:
                            self.used_nph += (len(self.job_nodes[jobid]) *
                                                (walltime / 60))
        elif rec_type == 'Q':
            m = self.Q_sub_record_tag.match(r.group('msg'))
            if m:
                self.users[jobid] = m.group('user')
                self.job_queue[jobid] = int(m.group('qtime'))
        elif rec_type == 'D':
            if jobid not in self.job_end:
                self.job_end[jobid] = tm

        return PARSER_OK_CONTINUE

    def epilogue(self, line):
        if self.running_jobs_parsing or self.accounting_workload_parsing:
            return

        self.record_tm = sorted(self.record_tm)
        if len(self.record_tm) > 0:
            last_record_tm = self.record_tm[len(self.record_tm) - 1]
            self.duration = last_record_tm - self.record_tm[0]
            self.info[DUR] = self.logutils._duration(self.duration)

        self.jobs_started = list(self.job_start.keys())
        self.jobs_ended = list(self.job_end.keys())
        self.job_node_size = [len(n) for n in self.job_nodes.values()]
        self.job_cpu_size = list(self.job_cpus.values())
        self.start = sorted(self.job_start.values())
        self.end = sorted(self.job_end.values())
        self.queue = sorted(self.job_queue.values())

        # list of jobs that have not yet ended, those are jobs that
        # have an S record but no E record. We port back the precomputed
        # metrics from the S record into the data to "publish"
        sjobs = set(self.jobs_started).difference(self.jobs_ended)
        for job in sjobs:
            if job in self.tmp_wait_time:
                self.wait_time.append(self.tmp_wait_time[job])
            if job in self.job_nodes:
                self.job_node_size.append(len(self.job_nodes[job]))
            if job in self.job_cpus:
                self.job_cpu_size.append(self.job_cpus[job])
            if self.utilization_parsing:
                if job in self.job_start:
                    if job in self.job_cpus:
                        self.used_cph += self.job_cpus[job] * \
                            ((last_record_tm - self.job_start[
                             job]) / 60)
                    if job in self.job_nodes:
                        self.used_nph += len(self.job_nodes[job]) * \
                            ((last_record_tm - self.job_start[
                             job]) / 60)

        """
        # Process jobs currently running, those may have an S record
        # that is older than the time window considered or not.
        # If they have an S record, then they were already processed
        # by the S record routine, otherwise, they are processed here
        if self.utilization_parsing and self._server:
            first_record_tm = self.record_tm[0]
            a = {'job_state': (EQ, 'R'),
                 'Resource_List.ncpus': (SET, ''),
                 'exec_host': (SET, ''),
                 'stime': (SET, '')}
            alljobs = self._server.status(JOB, a)
            for job in alljobs:
                # the running_jobids is populated from the node's jobs
                # attribute. If a job id is not in the running jobids
                # list, then its S record was already processed
                if job['id'] not in self._running_jobids:
                    continue

                if ('job_state' not in job or
                        'Resource_List.ncpus' not in job or
                        'exec_host' not in job or 'stime' not in job):
                    continue
                # split to catch a customer tweak
                stime = int(job['stime'].split()[0])
                if stime < first_record_tm:
                    stime = first_record_tm
                self.used_cph += int(job['Resource_List.ncpus']) * \
                    (last_record_tm - stime)
                nodes = len(self.logutils.parse_exechost(
                    job['exec_host']))
                self.used_nph += nodes * (last_record_tm - stime)
        """

    def job_info(self, rec):
        """
        PBS Job information
        """
        m = self.record_tag.match(rec)
        if m:
            d = {}
            if m.group('type') == 'E':
                if getattr(self, 'jobid', None) != m.group('id'):
                    return PARSER_OK_CONTINUE
                if not hasattr(self, 'job_info_res'):
                    self.job_info_res = {}
                for a in m.group('msg').split():
                    (k, v) = a.split('=', 1)
                    d[k] = v
                self.job_info_res[m.group('id')] = d

        return PARSER_OK_CONTINUE

    def summary(self):
        """
        Accounting log summary
        """
        if self.running_jobs_parsing or self.accounting_workload_parsing:
            return

        run_rate = self.logutils.get_rate(self.start)
        queue_rate = self.logutils.get_rate(self.queue)
        end_rate = self.logutils.get_rate(self.end)

        self.info[NJQ] = len(self.queue)
        self.info[NJR] = len(self.start)
        self.info[NJE] = len(self.end)
        self.info[JRR] = run_rate
        self.info[JSR] = queue_rate
        self.info[JER] = end_rate
        if len(self.wait_time) > 0:
            wt = sorted(self.wait_time)
            wta = float(sum(self.wait_time)) / len(self.wait_time)
            self.info[JWTm] = self.logutils._duration(min(wt))
            self.info[JWTM] = self.logutils._duration(max(wt))
            self.info[JWTA] = self.logutils._duration(wta)
            self.info[JWT25] = self.logutils._duration(
                self.logutils.percentile(wt, .25))
            self.info[JWT50] = self.logutils._duration(
                self.logutils.percentile(wt, .5))
            self.info[JWT75] = self.logutils._duration(
                self.logutils.percentile(wt, .75))

        if len(self.run_time) > 0:
            rt = sorted(self.run_time)
            self.info[JRTm] = self.logutils._duration(min(rt))
            self.info[JRT25] = self.logutils._duration(
                self.logutils.percentile(rt, 0.25))
            self.info[JRT50] = self.logutils._duration(
                self.logutils.percentile(rt, 0.50))
            self.info[JRTA] = self.logutils._duration(
                str(sum(rt) / len(rt)))
            self.info[JRT75] = self.logutils._duration(
                self.logutils.percentile(rt, 0.75))
            self.info[JRTM] = self.logutils._duration(max(rt))

        if len(self.job_node_size) > 0:
            js = sorted(self.job_node_size)
            self.info[JNSm] = min(js)
            self.info[JNS25] = self.logutils.percentile(js, 0.25)
            self.info[JNS50] = self.logutils.percentile(js, 0.50)
            self.info[JNSA] = str("%.2f" % (float(sum(js)) / len(js)))
            self.info[JNS75] = self.logutils.percentile(js, 0.75)
            self.info[JNSM] = max(js)

        if len(self.job_cpu_size) > 0:
            js = sorted(self.job_cpu_size)
            self.info[JCSm] = min(js)
            self.info[JCS25] = self.logutils.percentile(js, 0.25)
            self.info[JCS50] = self.logutils.percentile(js, 0.50)
            self.info[JCSA] = str("%.2f" % (float(sum(js)) / len(js)))
            self.info[JCS75] = self.logutils.percentile(js, 0.75)
            self.info[JCSM] = max(js)

        if self.utilization_parsing:
            ncph = self._total_ncpus * self.duration
            nph = self._num_nodes * self.duration
            if ncph > 0:
                self.info[UNCPUS] = str("%.2f" %
                                        (100 * float(self.used_cph) / ncph) +
                                        '%')
            if nph > 0:
                self.info[UNODES] = str("%.2f" %
                                        (100 * float(self.used_nph) / nph) +
                                        '%')
            self.info[CPH] = self.used_cph
            self.info[NPH] = self.used_nph

        self.info[USRS] = len(set(self.users.values()))

        return self.info

def process_output(info={}, summary=False):
        """
        Send analyzed log information to either the screen or to a database
        file.

        :param info: A dictionary of log analysis metrics.
        :type info: Dictionary
        :param summary: If True output summary only
        """
        if CFC in info:
            freq_info = info[CFC]
        elif 'summary' in info and CFC in info['summary']:
            freq_info = info['summary'][CFC]
        else:
            freq_info = None

        if 'matches' in info:
            for m in info['matches']:
                print(m, end=' ')
            del info['matches']

        if freq_info is not None:
            for ((l, m), n) in freq_info:
                b = time.strftime("%m/%d/%y %H:%M:%S", time.localtime(l))
                e = time.strftime("%m/%d/%y %H:%M:%S", time.localtime(m))
                print(b + ' -', end=' ')
                if b[:8] != e[:8]:
                    print(e, end=' ')
                else:
                    print(e[9:], end=' ')
                print(': ' + str(n))
            return

        if EST in info:
            einfo = info[EST]
            m = []

            for j in einfo[EJ]:
                m.append('Job ' + j[JID] + '\n\testimated:')
                if Eat in j:
                    for estimate in j[Eat]:
                        m.append('\t\t' + str(time.ctime(estimate)))
                if JST in j:
                    m.append('\tstarted:\n')
                    m.append('\t\t' + str(time.ctime(j[JST])))
                    m.append('\testimate range: ' + str(j[ESTR]))
                    m.append('\tstart to estimated: ' + str(j[ESTA]))

                if NEST in j:
                    m.append('\tnumber of estimates: ' + str(j[NEST]))
                if NJD in j:
                    m.append('\tnumber of drifts: ' + str(j[NJD]))
                if JDD in j:
                    m.append('\tdrift duration: ' + str(j[JDD]))
                m.append('\n')

            if ESTS in einfo:
                m.append('\nsummary: ')
                for k, v in sorted(einfo[ESTS].items()):
                    if 'duration' in k:
                        m.append('\t' + k + ': ' +
                                 str(PbsTypeDuration(int(v))))
                    else:
                        m.append('\t' + k + ': ' + str(v))

            print("\n".join(m))
            return

        sorted_info = sorted(info.items())
        for (k, v) in sorted_info:
            if summary and k != 'summary':
                continue
            print(str(k) + ": ", end=' ')
            if isinstance(v, dict):
                print('')
                sorted_v = sorted(v.items())
                for (k, val) in sorted_v:
                    print(str(k) + ': ' + str(val))
                print('')
            else:
                print(str(v))
        print('')

def parse_test_log(sname, tname, tpath):
    svrs = [x for x in os.scandir(tpath) if x.is_dir()]
    svr_logs = [os.path.join(x.path, 'server_logs') for x in svrs]
    sched_logs = []  # [os.path.join(x.path, 'sched_logs') for x in svrs]
    acc_logs = []  # [os.path.join(x.path, 'accounting') for x in svrs]
    pla = PBSLogAnalyzer(serverlog=svr_logs, schedlog=sched_logs,
                         acctlog=acc_logs, show_progress=True)
    info = pla.analyze_logs()
    return ('%s@%s' % (sname, tname), info)

if __name__ == '__main__':
    results = []
    with ProcessPoolExecutor(max_workers=10) as executor:
        _ps = []
        order = []
        x = pathlib.Path(sys.argv[1]).absolute()
        results_path = str(x)
        dirname = os.path.dirname(results_path)
        nodes_file = os.path.join(dirname, 'nodes')
        hosts = 1
        if os.path.exists(nodes_file):
            with open(nodes_file) as f:
                nodes_list = f.read().splitlines()
                hosts = len(nodes_list)

        _s = [x for x in os.scandir(x) if x.is_dir()]
        setups = sorted(_s, key=lambda x: x.name)
        for s in setups:
            _s = [x for x in os.scandir(s.path) if x.is_dir()]
            tests = sorted(_s, key=lambda x: x.name)
            for t in tests:
                order.append('%s@%s' % (s.name, t.name))
                _ps.append(executor.submit(parse_test_log, s.name, t.name, t.path))
        r = dict([_p.result() for _p in wait(_ps)[0]])

        for n in order:
            sname, tname = n.split('@')
            print('=' * 80)
            print('Setup: %s - Test: %s' % (sname, tname))
            print('-' * 80)
            print('Server summary:')
            process_output(r[n]['server'])
            # print('-' * 80)
            # print('Scheduler summary:')
            # process_output(r[n]['scheduler']['summary'])
            # print('-' * 80)
            # print('Scheduler cycles summary:')
            # del r[n]['scheduler']['summary']
            # process_output(r[n]['scheduler'])
            # print('-' * 80)
            # print('Accounting summary:')
            # process_output(r[n]['accounting'])


            moms = re.findall(r"(\d+)m", sname)
            if moms:
                moms = moms[0]
            else:
                print("Bad test name, couldn't determine mom count")
                sys.exit(1)
            cpus = re.findall(r"(\d+)cpu", sname)
            if cpus:
                cpus = cpus[0]
            else:
                print("Bad test name, couldn't determine cpus count")
                sys.exit(1)
            vnodes = 1

            qjobs = int(r[n]['server']['num_jobs_queued'])
            rjobs = int(r[n]['server']['num_jobs_run'])
            pjobs = qjobs - rjobs
            if pjobs == 0:
                pjobs = qjobs
                subjobs = 0
                array = "No"
            else:
                subjobs = int(rjobs / pjobs)
                array = "Yes"

            if tname == 'sched_on':
                sched = 'On'
            elif tname == 'sched_mixed':
                sched = 'Mixed'
            elif tname == 'sched_rtlimit':
                sched = 'RateLimited'
            else:
                sched = 'Off'
            
            if 'async' in sname:
                asyncdb = 'Yes'
            else:
                asyncdb = 'No'
            try:
                jsr = float(r[n]['server']['job_submit_rate'].split('/')[0])
                jt = float(r[n]['server']['job_throughput'].split('/')[0])
                jrr = float(r[n]['server']['job_run_rate'].split('/')[0])
                jer = float(r[n]['server']['job_end_rate'].split('/')[0])

                s = str(hosts) + ',' + sname + ',' + str(moms) + ',' + str(vnodes) + ',' + str(cpus) + ',' + sched + ',' + str(pjobs) + ',' + str(subjobs) + ',' + asyncdb + ',' + array + ',' + str(jsr) + ',' + str(jt) + ',' + str(jrr) + ',' + str(jer)

                sched_stat = os.path.join(results_path, sname, tname, 'pbs-server-1' ,'sched_stats')
                stat_line = ''
                print("reading %s" % sched_stat)
                if os.path.exists(sched_stat):
                    with open(sched_stat) as f:
                        print("reading %s" % sched_stat)
                        stat_line = f.read().splitlines()[11]

                stat = s + ',' + stat_line
                results.append(stat)
            except:
                pass

    print("Results in csv:")
    print('Hosts,TestName,MoMs,Vnodes/MoM,Cpus/Vnode,Scheduling,Jobs,Subjobs,AsyncDB,ArrayJob,job_submit_rate/s,job_throughput/s,job_run_rate/s,job_end_rate/s,Total Cycles,Max cycle time,90p cycle time,75p cycle time,25p cycle time,Mean cycle time,Median cycle time,Min cycle time')
    for s in results:
        print(s)