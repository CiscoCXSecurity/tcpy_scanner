#!/usr/bin/env python3
# tcpy_scanner - TCP Port Scanner
# Copyright Cisco Systems, Inc. and its affiliates
#
# This tool may be used for legal purposes only.  Users take full responsibility
# for any actions performed using this tool.  The author accepts no liability
# for damage caused by this tool.  If these terms are not acceptable to you, then
# you are not permitted to use this tool.
#
# In all other respects the GPL version 2 applies:
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#

import argparse
import collections
import ipaddress
import math
import os
import re
import select
import socket
import sys
import time

class ScannerBase(object):
    def __init__(self):
        self._sleep_total = 0
        self.header = "Starting Scan"
        self.sleep_multiplier = 1.87 # if we total up the time we sleep for, it doesn't match the time cProfile reports we spent in the sleep function, so we use this multiplier to adjust the estimate
        self.reply_callback_function = None
        self.bandwidth_bits_per_second = 32000
        self.max_probes = 3
        self.probes = [] # [(None, None, None),] # List of probe tuples
        self.inter_packet_interval = None
        self.inter_packet_interval_per_host = None
        self.backoff = 1.5
        self.rtt = 0.5 # https://www.nature.com/articles/s41598-019-46208-6
        self.bytes_sent = 0
        self.resolve_names = False # TODO not implemented yet
        self.target_source = None
        self.target_list_unprocessed = []
        self.target_filename = None
        self.scan_start_time_internal = None
        self.scan_start_time = None
        self.probes_sent_count = 0
        self.replies = 0
        self.packet_rate = None
        self.packet_rate_per_host = None
        self.probe_index_to_socket_dict = {}
        self.host_count = 0
        self.next_recv_time = time.time()
        self.recv_interval = 0.1
        self.debug = False
        self.log_reply_tuples = []
        self.debug_reply_log = "debug_reply_log.txt"
        self.blocklist = []
        self.count_in_queue = {} # how many probes are in the queue for each probe type
        self.sleep_reasons = {}

    #
    # Properties
    #

    @property
    def bytes_sent_target(self):
        if self.scan_start_time_internal:
            return self.bandwidth_bits_per_second * (time.time() - self.scan_start_time_internal) / 8
        else:
            return 0

    @property
    def probes_sent_target(self):
        if self.scan_start_time_internal:
            return self.packet_rate * (time.time() - self.scan_start_time_internal)
        else:
            return 0

    @property
    def sleep_total(self):
        return self._sleep_total * self.sleep_multiplier

    #
    # Setters
    #

    def set_reply_callback(self, reply_callback):
        self.reply_callback_function = reply_callback

    def set_debug(self, debug):
        self.debug = debug

    # set max_probes
    def set_max_probes(self, n): # int
        self.max_probes = int(n)

    def set_blocklist(self, blocklist_ips):
        # check ips are valid
        for ip in blocklist_ips:
            self.add_to_blocklist(ip)

    # set bandwidth
    def set_bandwidth(self, bandwidth): # string like 250k, 1m, 1g
        self.bandwidth_bits_per_second = expand_number(bandwidth)

        if self.bandwidth_bits_per_second < 1:
            print("[E] Bandwidth %s is too low" % self.bandwidth_bits_per_second)
            sys.exit(0)

        if self.bandwidth_bits_per_second > 1000000:
            print("[W] Bandwidth %s is too high.  Continuing anyway..." % self.bandwidth_bits_per_second)

        self.set_inter_packet_interval()

    def set_inter_packet_interval(self):
        if self.packet_overhead is None or self.packet_overhead == 0:
            print("[E] Code error: Packet overhead not set prior to calculating inter-packet interval")
            sys.exit(0)
        if self.bandwidth_bits_per_second is None or self.bandwidth_bits_per_second == 0:
            print("[E] Code error: Bandwidth not set not set prior to calculating inter-packet interval")
            sys.exit(0)
        self.inter_packet_interval = 8 * (self.payload_len_estimate + self.packet_overhead) / float(self.bandwidth_bits_per_second)

    def set_packet_rate(self, packet_rate):
        self.packet_rate = expand_number(packet_rate)

    def set_packet_rate_per_host(self, packet_rate_per_host):
        self.packet_rate_per_host = packet_rate_per_host
        self.inter_packet_interval_per_host = 1 / float(self.packet_rate_per_host)

    def set_header(self, header):
        self.header = header

    def add_targets(self, targets): # list
        self.target_source = "list"
        self.target_list_unprocessed = targets

    def add_targets_from_file(self, file): # str
        self.target_source = "file"
        self.target_filename = file

    #
    # Adders
    #

    def add_to_blocklist(self, ip):
        try:
            socket.inet_aton(ip)
        except socket.error:
            print("[E] Invalid IP address in blocklist: %s" % ip)
            sys.exit(1)
        if ip not in self.blocklist:
            self.blocklist.append(ip)

    #
    # Getters
    #

    def get_probe_port(self, probe_index):
        probe = self.probes[probe_index]
        return int(probe[0])

    def get_probe_payload_hex(self, probe_index):
        probe = self.probes[probe_index]
        return probe[2]

    def get_probe_payload_bin(self, probe_index):
        probe = self.probes[probe_index]
        return probe[3]

    def get_probe_name(self, probe_index):
        probe = self.probes[probe_index]
        return probe[1]

    def get_probe_index_from_socket(self, s):
        for probe_index, socket in self.probe_index_to_socket_dict.items():
            if s == socket:
                return probe_index
        return None

    def get_available_bandwidth_quota_packets(self):

        packet_quota_left = None
        # return 100 if there is no bandwidth quota
        if self.bandwidth_bits_per_second is None:
            packet_quota_left = 100
        else:
            # return 0 if we exceed our bandwidth quota
            bytes_left = self.bytes_sent_target - self.bytes_sent
            if bytes_left <= 0:
                packet_quota_left = 0
            else:
                packet_quota_left = int(8 * bytes_left / float(self.packet_overhead))

        # return the number of packets we can send
        return packet_quota_left

    def get_available_packet_rate_quota_packets(self):
        packet_quota_left = None
        # return 100 if there is no packet rate quota
        if self.packet_rate is None or self.packet_rate == 0: # TODO messy
            packet_quota_left = 100
        else:
            # return 0 if we exceed our packet rate quota
            packets_left = self.probes_sent_target - self.probes_sent_count
            if packets_left <= 0:
                packet_quota_left = 0
            else:
                packet_quota_left = packets_left

        # return the number of packets we can send
        return packet_quota_left

    def get_available_quota_packets(self):
        return int(min(self.get_available_bandwidth_quota_packets(), self.get_available_packet_rate_quota_packets()))

    #
    # Debug
    #

    # Note that recording results in memory could use too much memory for large scans
    # so is disabled by default.  This feature is used for automated testing.
    def debug_log_reply(self, probe_name, srcip, port, data):
        self.log_reply_tuples.append((probe_name, srcip, port, data))

    def debug_write_log(self):
        with open(self.debug_reply_log, "w") as f:
            for probe_name, srcip, port, data in self.log_reply_tuples:
                f.write("%s,%s,%s,%s\n" % (probe_name, srcip, port, str_or_bytes_to_hex(data)))
        print("[i] Wrote debug log to %s" % self.debug_reply_log)

    def __repr__(self): # TODO
        return "%s()" % type(self).__name__

    def __str__(self): # TODO
        return "%s()" % type(self).__name__

    #
    # Others
    #

    def wait_for_quotas(self):
        bandwidth_quota_ok = False
        packet_rate_quota_ok = False
        probe_send_ok = False
        bandwidth_quota_packets_left = 0
        packet_quota_packets_left = 0
        wait_time = 0
        while not (packet_rate_quota_ok and bandwidth_quota_ok and probe_send_ok):

            # check if we're within bandwidth quota
            force_bandwidth_quota_wait = True
            force_packet_quota_wait = True
            force_probe_state_wait = True
            bandwidth_quota_ok = False
            packet_rate_quota_ok = False
            probe_send_ok = False
            wait_time = 0
            bandwidth_quota_packets_left = self.get_available_bandwidth_quota_packets()
            if bandwidth_quota_packets_left > 0:
                bandwidth_quota_ok = True
                force_bandwidth_quota_wait = False

                # check if we're within packet rate quota
                force_packet_quota_wait = False
                packet_quota_packets_left = self.get_available_packet_rate_quota_packets()
                if packet_quota_packets_left > 0:
                    packet_rate_quota_ok = True
                    force_packet_quota_wait = False

                    # Check all of the probe states to see if any are ready to send
                    # This is expesnive, so we only do it if we're within the other quotas
                    # if self.probe_state_ready():
                    #     probe_send_ok = True
                    #     force_probe_state_wait = False
                    if self.get_queue_length() > 0:

                        next_probe_state = self.queue_peek_first()
                        last_probe_time = next_probe_state.probe_sent_time
                        now = time.time()

                        if last_probe_time is None or now > last_probe_time + self.inter_packet_interval_per_host:
                            probe_send_ok = True
                            force_probe_state_wait = False
                        else:
                            wait_time = last_probe_time + self.inter_packet_interval_per_host - now
                    else:
                        self.probe_state_ready_last_result = True
                        return self.probe_state_ready_last_result

            # update stats
            if force_bandwidth_quota_wait:
                self.sleep_reasons["bandwidth_quota"] += 1
            elif force_packet_quota_wait:
                self.sleep_reasons["packet_quota"] += 1
            elif force_probe_state_wait:
                self.sleep_reasons["port_states"] += 1

            if not (packet_rate_quota_ok and bandwidth_quota_ok and probe_send_ok):
                # sleep for self.inter_packet_interval seconds
                wait_time = max(self.inter_packet_interval, wait_time)

                # Do an extra receive if we have spare time
                # we must not sleep for more than the receive interval or we won't check for reponses when we're supposed to
                # Without this shorter sleep, very small scans tend to miss responses because they recv too quickly after sending and then wait for the next retry.  Then the same problem occurs.
                if wait_time > self.recv_interval:
                    self.receive_packets(self.get_socket_list())
                    self._sleep_total += self.recv_interval
                    time.sleep(self.recv_interval)
                else:
                    self._sleep_total += wait_time
                    time.sleep(wait_time)

    #
    # Abstract methods # TODO is abc module portable?
    #

    def dump(self):
        raise NotImplementedError

    def set_rtt(self, rtt):
        raise NotImplementedError

    def set_probes(self, probes):
        raise NotImplementedError

    def start_scan(self):
        raise NotImplementedError

    def receive_packets(self, socket_list):
        raise NotImplementedError

    def inform_starting_probe_type(self, probe_index):
        raise NotImplementedError

    def decrease_count_in_queue(self):
        raise NotImplementedError

    def get_queue_length(self):
        raise NotImplementedError

    def queue_peek_first(self):
        raise NotImplementedError

    def get_socket_list(self):
        raise NotImplementedError

ip_regex = r"(?:(?:[0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\.){3}(?:[0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])"
cidr_regex = r"(?:(?:[0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\.){3}(?:[0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])/([0-9]{1,2})$"

class TargetGenerator(object):
    def __init__(self, make_probe_state_callback, list=None, filename=None, custom=False):
        if list is None:
            list = []
        self.target_filename = filename
        self.target_list_unprocessed = list
        self.target_source = None
        self.custom = custom
        self.make_probe_state_callback = make_probe_state_callback
        if len(self.target_list_unprocessed) > 0:
            self.target_source = "list"
        elif self.target_filename:
            self.target_source = "file"
        elif custom:
            self.target_source = "custom"
        else:
            raise Exception("[E] __init__: No target source set")

    def get_probe_state_generator(self, probes):
        if self.custom: # format: (ip, port, name, payload_bin)
            probe_index = 0
            for probe_tuple in probes:
                ip = probe_tuple[0]
                port = probe_tuple[1]
                name = probe_tuple[2]
                payload_bin = probe_tuple[3]
                cps = self.make_probe_state_callback(ip, probes, probe_index)
                cps.payload_bin = payload_bin
                yield cps
        else:
            for probe_index in range(len(probes)):
                for target in self._get_targets():
                    yield self.make_probe_state_callback(target, probes, probe_index)

    def get_generator(self):
        return self._get_targets()

    # generator in case we are passed more hosts than we can fit in memory
    def _get_targets(self):
        if self.target_source == "list":
            for t in self._get_targets_from_list(self.target_list_unprocessed):
                yield t
        elif self.target_source == "file":
            for t in self._get_targets_from_file(self.target_filename):
                yield t
        else:
            raise Exception("[E] _get_targets: No target source set")

    # unexpanded list like [ 10.0.0.1, 10.0.0.10-10.0.0.20, 10.0.2.0/24 ]
    def _get_targets_from_list(self, targets): # list
        for target in targets:
            for t in self._get_targets_from_string(target):
                yield t

    def _get_targets_from_string(self, target): # str
        if re.match(r"^%s-%s$" % (ip_regex, ip_regex), target):
            for t in self._get_targets_from_ip_range(target):
                yield t

        elif re.match(r"^%s$" % ip_regex, target):
            yield target

        elif re.match(r"^%s$" % cidr_regex, target):
            for t in self._get_target_ips_from_cidr(target):
                yield t

        else:
            print("[E] %s is not a valid ip, ip range or cidr" % target)
            sys.exit(0)

    # add targets from file
    def _get_targets_from_file(self, file): # str
        if not os.path.isfile(file):
            print("[E] File %s does not exist" % file)
            sys.exit(0)
        with open(file, 'r') as f:
            for target in f:
                # strip leading/trailing whitespace
                target = target.strip()

                # ignore comments
                if target.startswith('#'):
                    continue

                # ignore empty lines
                if not target:
                    continue

                # ignore lines with only whitespace
                if re.match(r'^\s+$', target):
                    continue

                # yield from self._get_targets_from_string(target)
                for t in self._get_targets_from_string(target):
                    yield t

    # add targets from ip range like 10.0.0.1-10.0.0.10
    def _get_targets_from_ip_range(self, ip_range): # str
        # check ip_range is in the right format
        if not re.match(r"^%s-%s$" % (ip_regex, ip_regex), ip_range):
            print("[E] IP range %s is not in the right format" % ip_range)
            sys.exit(0)

        # get ip range
        ip_range = ip_range.split('-')

        # get ip range start and end
        start_ip = ip_range[0]
        if sys.version_info.major == 2:
            start_ip = start_ip.decode("utf8")

        end_ip = ip_range[1]
        if sys.version_info.major == 2:
            end_ip = end_ip.decode("utf8")

        ip_range_start = ipaddress.ip_address(start_ip)
        ip_range_end = ipaddress.ip_address(end_ip)

        # add targets
        for ip_int in range(int(ip_range_start), int(ip_range_end) + 1):
            yield str(ipaddress.ip_address(ip_int))

    def _get_target_ips_from_cidr (self, cidr): # str
        # check cidr is in the right format
        m = re.match(cidr_regex, cidr)
        if not m:
            print("[E] CIDR %s is not in the right format" % cidr)
            sys.exit(0)
        if int(m.group(1)) > 32:
            print("[E] Netmask for %s is > 32" % cidr)
            sys.exit(0)
        if int(m.group(1)) < 8:
            print("[E] Netmask for %s is < 8" % cidr)
            sys.exit(0)

        # if running python2, cidr must be unicode, not str
        if sys.version_info.major == 2:
            cidr = cidr.decode("utf8")

        ip_range = ipaddress.ip_network(cidr, False)
        # add targets
        for ip_int in range(int(ip_range.network_address), int(ip_range.broadcast_address) + 1):
            yield str(ipaddress.ip_address(ip_int))

class SelectPoller(object):
    # These are not defined on windows, so we create our own
    POLLOUT = 1
    POLLIN = 4
    def __init__(self):
        self.fd_list = []

    def poll(self, timeout=0):
        # check if there are any packets to receive
        readable_sockets = None
        writable_sockets = None
        error_socks = None
        try:
            readable_sockets, writable_sockets, error_socks = select.select(self.fd_list, self.fd_list, self.fd_list, timeout) # TODO -r1: ValueError: filedescriptor out of range in select()
        except ValueError as e:
            print("%s" % e.with_traceback())
            print(self.fd_list)
            sys.exit(1)
        socket_count = len(readable_sockets) + len(writable_sockets) + len(error_socks)

        events = {}
        for fd in readable_sockets:
            events[fd] = SelectPoller.POLLIN

        for fd in writable_sockets:
            if fd in events:
                events[fd] |= SelectPoller.POLLOUT
            else:
                events[fd] = SelectPoller.POLLOUT

        # generate tuples from events dict
        event_list = []
        for fd in events:
            event_list.append((fd, events[fd]))

        return event_list

    def register(self, fd, event):
        # if fd > 1023:
        #     raise Exception("[E] SelectPoller: fd %s is > 1023" % fd)
        self.fd_list.append(fd)

    def unregister(self, fd):
        if fd in self.fd_list:
            self.fd_list.remove(fd)
        else:
            pass

# ProbeStateContainer and ProbeStateTcp depend on each other so need to be declared in the same file

class ProbeStateContainer(object):
    _instance = None

    def __new__(cls):
        if not cls._instance:
            cls._instance = super(ProbeStateContainer, cls).__new__(cls)
            self = cls._instance
            self.probe_states = collections.deque()
            self.socket_list = []
            self.probe_states_by_fd = {}
            self.count = 0
            self.next_probe_id = 0
            #self.poller = select.poll()
            self.poller = None
            self.poll_type = "auto"
            self.poll_events = None
            self.set_poll_type(self.poll_type)
        return cls._instance

    def set_poll_type(self, poll_type):
        if poll_type == "auto":
            if sys.platform == "linux" or sys.platform == "linux2":
                poll_type = "poll"  # TODO apparently epoll should be better, but it seems less relaiable
            elif sys.platform == "darwin":
                poll_type = "poll" # TODO kqueue
            elif sys.platform == "win32":
                poll_type = "select"
            # TODO solaris + devpoll()

        self.poll_type = poll_type

        # set poller to correct type
        if poll_type == "epoll":
            self.poller = select.epoll()
            select.EPOLLRDHUP = 8192 # missing from python2
            self.poll_events = select.EPOLLOUT | select.EPOLLRDHUP
        elif poll_type == "poll":
            self.poller = select.poll()
            self.poll_events = select.POLLOUT
        elif poll_type == "select":
            self.poller = SelectPoller()
            self.poll_events = None
        else:
            raise Exception("Unknown poll type: %s" % poll_type)

        return self.poll_type

    def add_probe_state(self, probe_state):
        probe_state.probe_id = self.next_probe_id
        self.next_probe_id += 1
        self.count += 1
        self.probe_states.appendleft(probe_state)

    def new_probe_state(self):
        ps = ProbeStateTcp()
        self.add_probe_state(ps)
        return ps

    def delete_probe_state(self, probe_state):
        probe_state.delete_socket()
        self.count -= 1
        self.probe_states.remove(probe_state)
        # TODO call del probe_state?

    def schedule_delete_probe_state(self, probe_state):
        probe_state.schedule_delete()

    def popleft(self):
        if len(self.probe_states) == 0:
            return None
        self.count -= 1
        return self.probe_states.popleft()

    # This allows external code to delete elements.  This is more efficient that using deque.remove() because we don't have to iterate through the entire list
    def pop(self):
        if len(self.probe_states) == 0:
            return None
        self.count -= 1
        return self.probe_states.pop()

    def sort(self, inter_packet_interval_per_host, now=time.time()):
        list_q = list(self.probe_states)

        def sort_func(ps):
            time_key = None

            if ps.probe_sent_time is None:
                time_key = 0
            else:
                time_key = ps.probe_sent_time + inter_packet_interval_per_host - now

            if ps.deleted:
                time_key = -1000

            return time_key, ps.probe_id

        list_q = sorted(list_q, key=sort_func)
        self.probe_states = collections.deque(list_q)

    # takes and element with pop() from the left and append() it to the right.  Return a the element moved.
    # This helps the caller to iterate through the queue without us having to update indexes (which would be needed if an element was removed)
    def next(self):
        if len(self.probe_states) == 0:
            return None
        ps = self.probe_states.popleft()
        self.probe_states.append(ps)
        return ps

    def peekleft(self):
        if len(self.probe_states) == 0:
            return None
        return self.probe_states[0]

# # garbage collect # TODO

class ProbeStateTcp(object):
    def __init__(self, ip, port, probe_index):
        self.target_ip = ip
        self.probe_index = probe_index # TODO used for host_count, but does that work?
        self.probe_sent_time = None
        self.probes_sent = 0
        self.target_port = port
        self.socket = None
        self.deleted = False
        self.probe_id = None
        self.container = ProbeStateContainer()
        self.container.add_probe_state(self)

    def set_socket(self, socket):
        if self.socket:
            self.delete_socket()
        self.socket = socket
        self.container.socket_list.append(socket)
        self.container.probe_states_by_fd[socket.fileno()] = self
        #self.container.poller.register(socket.fileno(), select.POLLOUT)

        if self.container.poll_type == "epoll":
            self.container.poller.register(socket.fileno(), self.container.poll_events)
        elif self.container.poll_type == "poll":
            self.container.poller.register(socket.fileno(), self.container.poll_events)
        elif self.container.poll_type == "select":
            self.container.poller.register(socket.fileno(), self.container.poll_events)
        else:
            raise Exception("Unknown poll type: %s" % self.container.poll_type)

    def delete_socket(self):
        if self.socket is not None:
            self.container.poller.unregister(self.socket.fileno())
            self.container.socket_list.remove(self.socket)
            del self.container.probe_states_by_fd[self.socket.fileno()]
            self.socket.close()
            self.socket = None

    def schedule_delete(self):
        self.delete_socket() # must delete socket within 1 second or kernel will send retries
        self.deleted = True
    def __del__(self):
        if self.socket:
            self.delete_socket()
        #self.container.delete_probe_state(self) # TODO this seems like a good idea but doesn't work

# List of most common ports, listed in order of popularity (most popular first).
# Taken from nmap 7.80 nmap-services file, which has a GPLv2 compatible license.
# sort -r -k3 /mnt/hgfs/share/nmap-7.80/nmap-services | grep tcp | cut -f 2 | cut -f 1 -d / | xargs echo | sed 's/ /, /g' | sed 's/^/port_popularity_nmap = [/' | sed 's/$/]/'
port_popularity_nmap = [80, 23, 443, 21, 22, 25, 3389, 110, 445, 139, 143, 53, 135, 3306, 8080, 1723, 111, 995, 993, 5900, 1025, 587, 8888, 199, 1720, 465, 548, 113, 81, 6001, 10000, 514, 5060, 179, 1026, 2000, 8443, 8000, 32768, 554, 26, 1433, 49152, 2001, 515, 8008, 49154, 1027, 5666, 646, 5000, 5631, 631, 49153, 8081, 2049, 88, 79, 5800, 106, 2121, 1110, 49155, 6000, 513, 990, 5357, 427, 49156, 543, 544, 5101, 144, 7, 389, 8009, 3128, 444, 9999, 5009, 7070, 5190, 3000, 5432, 1900, 3986, 13, 1029, 9, 5051, 6646, 49157, 1028, 873, 1755, 2717, 4899, 9100, 119, 37, 1000, 3001, 5001, 82, 10010, 1030, 9090, 2107, 1024, 2103, 6004, 1801, 5050, 19, 8031, 1041, 255, 1049, 1048, 2967, 1053, 3703, 1056, 1065, 1064, 1054, 17, 808, 3689, 1031, 1044, 1071, 5901, 100, 9102, 8010, 2869, 1039, 5120, 4001, 9000, 2105, 636, 1038, 2601, 1, 7000, 1066, 1069, 625, 311, 280, 254, 4000, 1993, 1761, 5003, 2002, 2005, 1998, 1032, 1050, 6112, 3690, 1521, 2161, 6002, 1080, 2401, 4045, 902, 7937, 787, 1058, 2383, 32771, 1033, 1040, 1059, 50000, 5555, 10001, 1494, 593, 2301, 3, 1, 3268, 7938, 1234, 1022, 1074, 8002, 1036, 1035, 9001, 1037, 464, 497, 1935, 6666, 2003, 6543, 1352, 24, 3269, 1111, 407, 500, 20, 2006, 3260, 15000, 1218, 1034, 4444, 264, 2004, 33, 1042, 42510, 999, 3052, 1023, 1068, 222, 7100, 888, 4827, 1999, 563, 1717, 2008, 992, 32770, 32772, 7001, 8082, 2007, 740, 5550, 2009, 5801, 1043, 512, 2701, 7019, 50001, 1700, 4662, 2065, 2010, 42, 9535, 2602, 3333, 161, 5100, 5002, 2604, 4002, 6059, 1047, 8192, 8193, 2702, 6789, 9595, 1051, 9594, 9593, 16993, 16992, 5226, 5225, 32769, 3283, 1052, 8194, 1055, 1062, 9415, 8701, 8652, 8651, 8089, 65389, 65000, 64680, 64623, 55600, 55555, 52869, 35500, 33354, 23502, 20828, 1311, 1060, 4443, 730, 731, 709, 1067, 13782, 5902, 366, 9050, 1002, 85, 5500, 5431, 1864, 1863, 8085, 51103, 49999, 45100, 10243, 49, 3495, 6667, 90, 475, 27000, 1503, 6881, 1500, 8021, 340, 78, 5566, 8088, 2222, 9071, 8899, 6005, 9876, 1501, 5102, 32774, 32773, 9101, 5679, 163, 648, 146, 1666, 901, 83, 9207, 8001, 8083, 5004, 3476, 8084, 5214, 14238, 12345, 912, 30, 2605, 2030, 6, 541, 8007, 3005, 4, 1248, 2500, 880, 306, 4242, 1097, 9009, 2525, 1086, 1088, 8291, 52822, 6101, 900, 7200, 2809, 395, 800, 32775, 12000, 1083, 211, 987, 705, 20005, 711, 13783, 6969, 3071, 5269, 5222, 1085, 1046, 5987, 5989, 5988, 2190, 11967, 8600, 3766, 7627, 8087, 30000, 9010, 7741, 14000, 3367, 1099, 1098, 3031, 2718, 6580, 15002, 4129, 6901, 3827, 3580, 2144, 9900, 8181, 3801, 1718, 2811, 9080, 2135, 1045, 2399, 3017, 10002, 1148, 9002, 8873, 2875, 9011, 5718, 8086, 3998, 2607, 11110, 4126, 5911, 5910, 9618, 2381, 1096, 3300, 3351, 1073, 8333, 3784, 5633, 15660, 6123, 3211, 1078, 3659, 3551, 2260, 2160, 2100, 16001, 3325, 3323, 1104, 9968, 9503, 9502, 9485, 9290, 9220, 8994, 8649, 8222, 7911, 7625, 7106, 65129, 63331, 6156, 6129, 60020, 5962, 5961, 5960, 5959, 5925, 5877, 5825, 5810, 58080, 57294, 50800, 50006, 50003, 49160, 49159, 49158, 48080, 40193, 34573, 34572, 34571, 3404, 33899, 3301, 32782, 32781, 31038, 30718, 28201, 27715, 25734, 24800, 22939, 21571, 20221, 20031, 19842, 19801, 19101, 17988, 1783, 16018, 16016, 15003, 14442, 13456, 10629, 10628, 10626, 10621, 10617, 10616, 10566, 10025, 10024, 10012, 1169, 5030, 5414, 1057, 6788, 1947, 1094, 1075, 1108, 4003, 1081, 1093, 4449, 1687, 1840, 1100, 1063, 1061, 1107, 1106, 9500, 20222, 7778, 1077, 1310, 2119, 2492, 1070, 20000, 8400, 1272, 6389, 7777, 1072, 1079, 1082, 8402, 89, 691, 1001, 32776, 1999, 212, 2020, 6003, 7002, 2998, 50002, 3372, 898, 5510, 32, 2033, 4165, 3061, 5903, 99, 749, 425, 43, 5405, 6106, 13722, 6502, 7007, 458, 9666, 8100, 3737, 5298, 1152, 8090, 2191, 3011, 1580, 5200, 3851, 3371, 3370, 3369, 7402, 5054, 3918, 3077, 7443, 3493, 3828, 1186, 2179, 1183, 19315, 19283, 3995, 5963, 1124, 8500, 1089, 10004, 2251, 1087, 5280, 3871, 3030, 62078, 9091, 4111, 1334, 3261, 2522, 5859, 1247, 9944, 9943, 9877, 9110, 8654, 8254, 8180, 8011, 7512, 7435, 7103, 61900, 61532, 5922, 5915, 5904, 5822, 56738, 55055, 51493, 50636, 50389, 49175, 49165, 49163, 3546, 32784, 27355, 27353, 27352, 24444, 19780, 18988, 16012, 15742, 10778, 4006, 2126, 4446, 3880, 1782, 1296, 9998, 9040, 32779, 1021, 32777, 2021, 32778, 616, 666, 700, 5802, 4321, 545, 1524, 1112, 49400, 84, 38292, 2040, 32780, 3006, 2111, 1084, 1600, 2048, 2638, 6699, 9111, 16080, 6547, 6007, 1533, 5560, 2106, 1443, 667, 720, 2034, 555, 801, 6025, 3221, 3826, 9200, 2608, 4279, 7025, 11111, 3527, 1151, 8200, 8300, 6689, 9878, 10009, 8800, 5730, 2394, 2393, 2725, 5061, 6566, 9081, 5678, 3800, 4550, 5080, 1201, 3168, 3814, 1862, 1114, 6510, 3905, 8383, 3914, 3971, 3809, 5033, 7676, 3517, 4900, 3869, 9418, 2909, 3878, 8042, 1091, 1090, 3920, 6567, 1138, 3945, 1175, 10003, 3390, 3889, 1131, 8292, 5087, 1119, 1117, 4848, 7800, 16000, 3324, 3322, 5221, 4445, 9917, 9575, 9099, 9003, 8290, 8099, 8093, 8045, 7921, 7920, 7496, 6839, 6792, 6779, 6692, 6565, 60443, 5952, 5950, 5907, 5906, 5862, 5850, 5815, 5811, 57797, 56737, 5544, 55056, 5440, 54328, 54045, 52848, 52673, 50500, 50300, 49176, 49167, 49161, 44501, 44176, 41511, 40911, 32785, 32783, 30951, 27356, 26214, 25735, 19350, 18101, 18040, 17877, 16113, 15004, 14441, 12265, 12174, 10215, 10180, 4567, 6100, 4004, 4005, 8022, 9898, 7999, 1271, 1199, 3003, 1122, 2323, 4224, 2022, 617, 777, 417, 714, 6346, 981, 722, 1009, 4998, 70, 1076, 5999, 10082, 765, 301, 524, 668, 2041, 6009, 1417, 1434, 259, 44443, 1984, 2068, 7004, 1007, 4343, 416, 2038, 6006, 109, 4125, 1461, 9103, 911, 726, 1010, 2046, 2035, 7201, 687, 2013, 481, 125, 6669, 6668, 903, 1455, 683, 1011, 2043, 2047, 31337, 256, 9929, 5998, 406, 44442, 783, 843, 2042, 2045, 4040, 6060, 6051, 1145, 3916, 9443, 9444, 1875, 7272, 4252, 4200, 7024, 1556, 13724, 1141, 1233, 8765, 1137, 3963, 5938, 9191, 3808, 8686, 3981, 2710, 3852, 3849, 3944, 3853, 9988, 1163, 4164, 3820, 6481, 3731, 5081, 40000, 8097, 4555, 3863, 1287, 4430, 7744, 1812, 7913, 1166, 1164, 1165, 8019, 10160, 4658, 7878, 3304, 3307, 1259, 1092, 7278, 3872, 10008, 7725, 3410, 1971, 3697, 3859, 3514, 4949, 4147, 7900, 5353, 3931, 8675, 1277, 3957, 1213, 2382, 6600, 3700, 3007, 4080, 1113, 3969, 1132, 1309, 3848, 7281, 3907, 3972, 3968, 1126, 5223, 1217, 3870, 3941, 8293, 1719, 1300, 2099, 6068, 3013, 3050, 1174, 3684, 2170, 3792, 1216, 5151, 7080, 22222, 4143, 5868, 8889, 12006, 1121, 3119, 8015, 10023, 3824, 1154, 20002, 3888, 4009, 5063, 3376, 1185, 1198, 1192, 1972, 1130, 1149, 4096, 6500, 8294, 3990, 3993, 8016, 3846, 3929, 1187, 5074, 8766, 1102, 2800, 9941, 9914, 9815, 9673, 9643, 9621, 9501, 9409, 9198, 9197, 9098, 8996, 8987, 8877, 8676, 8648, 8540, 8481, 8385, 8189, 8098, 8095, 8050, 7929, 7770, 7749, 7438, 7241, 7123, 7051, 7050, 6896, 6732, 6711, 65310, 6520, 6504, 6247, 6203, 61613, 60642, 60146, 60123, 5981, 5940, 59202, 59201, 59200, 5918, 5914, 59110, 5909, 5905, 5899, 58838, 5869, 58632, 58630, 5823, 5818, 5812, 5807, 58002, 58001, 57665, 55576, 55020, 53535, 5339, 53314, 53313, 53211, 52853, 52851, 52850, 52849, 52847, 5279, 52735, 52710, 52660, 5242, 5212, 51413, 51191, 5040, 50050, 49401, 49236, 49195, 49186, 49171, 49168, 49164, 4875, 47544, 46996, 46200, 44709, 41523, 41064, 40811, 3994, 39659, 39376, 39136, 38188, 38185, 37839, 35513, 33554, 33453, 32835, 32822, 32816, 32803, 32792, 32791, 30704, 30005, 29831, 29672, 28211, 27357, 26470, 23796, 23052, 2196, 21792, 19900, 18264, 18018, 17595, 16851, 16800, 16705, 15402, 15001, 12452, 12380, 12262, 12215, 12059, 12021, 10873, 10058, 10034, 10022, 10011, 2910, 1594, 1658, 1583, 3162, 2920, 26000, 2366, 4600, 1688, 1322, 2557, 1095, 1839, 2288, 1123, 5968, 9600, 1244, 1641, 2200, 1105, 6550, 5501, 1328, 2968, 1805, 1914, 1974, 31727, 3400, 1301, 1147, 1721, 1236, 2501, 2012, 6222, 1220, 1109, 1347, 502, 701, 2232, 2241, 4559, 710, 10005, 5680, 623, 913, 1103, 780, 930, 803, 725, 639, 540, 102, 5010, 1222, 953, 8118, 9992, 1270, 27, 123, 86, 447, 1158, 442, 18000, 419, 931, 874, 856, 250, 475, 2044, 441, 210, 6008, 7003, 5803, 1008, 556, 6103, 829, 3299, 55, 713, 1550, 709, 2628, 223, 3025, 87, 57, 10083, 5520, 980, 251, 1013, 9152, 1212, 2433, 1516, 333, 2011, 748, 1350, 1526, 7010, 1241, 127, 157, 220, 1351, 2067, 684, 77, 4333, 674, 943, 904, 840, 825, 792, 732, 1020, 1006, 657, 557, 610, 1547, 523, 996, 2025, 602, 3456, 862, 600, 2903, 257, 1522, 1353, 6662, 998, 660, 729, 730, 731, 782, 1357, 3632, 3399, 6050, 2201, 971, 969, 905, 846, 839, 823, 822, 795, 790, 778, 757, 659, 225, 1015, 1014, 1012, 655, 786, 6017, 6670, 690, 388, 44334, 754, 5011, 98, 411, 1525, 3999, 740, 12346, 802, 1337, 1127, 2112, 1414, 2600, 621, 606, 59, 928, 924, 922, 921, 918, 878, 864, 859, 806, 805, 728, 252, 1005, 1004, 641, 758, 669, 38037, 715, 1413, 2104, 1229, 3817, 6063, 6062, 6055, 6052, 6030, 6021, 6015, 6010, 3220, 6115, 3940, 2340, 8006, 4141, 3810, 1565, 3511, 5986, 5985, 2723, 9202, 4036, 4035, 2312, 3652, 3280, 4243, 4298, 4297, 4294, 4262, 4234, 4220, 4206, 22555, 9300, 7121, 1927, 4433, 5070, 2148, 1168, 9979, 7998, 4414, 1823, 3653, 1223, 8201, 4876, 3240, 2644, 4020, 2436, 3906, 4375, 4024, 5581, 5580, 9694, 6251, 7345, 7325, 7320, 7300, 3121, 5473, 5475, 3600, 3943, 4912, 2142, 1976, 1975, 5202, 5201, 4016, 5111, 9911, 10006, 3923, 3930, 1221, 2973, 3909, 5814, 14001, 3080, 4158, 3526, 1911, 5066, 2711, 2187, 3788, 3796, 3922, 2292, 16161, 3102, 4881, 3979, 3670, 4174, 3483, 2631, 1750, 3897, 7500, 5553, 5554, 9875, 4570, 3860, 3712, 8052, 2083, 8883, 2271, 1208, 3319, 3935, 3430, 1215, 3962, 3368, 3964, 1128, 5557, 4010, 9400, 1605, 3291, 7400, 5005, 1699, 1195, 5053, 3813, 1712, 3002, 3765, 3806, 43000, 2371, 3532, 3799, 3790, 3599, 3850, 4355, 4358, 4357, 4356, 5433, 3928, 4713, 4374, 3961, 9022, 3911, 3396, 7628, 3200, 1753, 3967, 2505, 5133, 3658, 8471, 1314, 2558, 6161, 4025, 3089, 9021, 30001, 8472, 5014, 9990, 1159, 1157, 1308, 5723, 3443, 4161, 1135, 9211, 9210, 4090, 7789, 6619, 9628, 12121, 4454, 3680, 3167, 3902, 3901, 3890, 3842, 16900, 4700, 4687, 8980, 1196, 4407, 3520, 3812, 5012, 10115, 1615, 2902, 4118, 2706, 2095, 2096, 3363, 5137, 3795, 8005, 10007, 3515, 8003, 3847, 3503, 5252, 27017, 2197, 4120, 1180, 5722, 1134, 1883, 1249, 3311, 3837, 2804, 4558, 4190, 2463, 1204, 4056, 1184, 19333, 9333, 3913, 3672, 4342, 4877, 3586, 8282, 1861, 1752, 9592, 1701, 6085, 2081, 4058, 2115, 8900, 4328, 2958, 2957, 7071, 3899, 2531, 2691, 5052, 1638, 3419, 2551, 4029, 3603, 1336, 2082, 1143, 3602, 1176, 4100, 3486, 6077, 4800, 2062, 1918, 12001, 12002, 9084, 7072, 1156, 2313, 3952, 4999, 5023, 2069, 28017, 27019, 27018, 3439, 6324, 1188, 1125, 3908, 7501, 8232, 1722, 2988, 10500, 1136, 1162, 10020, 22128, 1211, 3530, 12009, 9005, 3057, 3956, 1191, 3519, 5235, 1144, 4745, 1901, 1807, 2425, 5912, 3210, 32767, 5015, 5013, 3622, 4039, 10101, 5233, 5152, 3983, 3982, 9616, 4369, 3728, 3621, 2291, 5114, 7101, 1315, 2087, 5234, 1635, 3263, 4121, 4602, 2224, 3949, 9131, 3310, 3937, 2253, 3882, 3831, 2376, 2375, 3876, 3362, 3663, 3334, 47624, 1825, 3868, 4302, 5721, 1279, 2606, 1173, 22125, 17500, 12005, 6113, 1973, 3793, 3637, 8954, 3742, 9667, 41795, 41794, 4300, 8445, 12865, 3365, 4665, 3190, 3577, 3823, 2261, 2262, 2812, 1190, 22350, 3374, 4135, 2598, 2567, 1167, 8470, 8116, 3830, 8880, 2734, 3505, 3388, 3669, 1871, 4325, 8025, 1958, 3681, 3014, 8999, 4415, 3414, 4101, 6503, 9700, 3683, 1150, 18333, 4376, 3991, 3989, 3992, 2302, 3415, 1179, 3946, 2203, 4192, 4418, 2712, 25565, 4065, 3915, 2080, 3103, 2265, 8202, 2304, 8060, 4119, 4401, 1560, 3904, 4534, 1835, 1116, 8023, 8474, 3879, 4087, 4112, 6350, 9950, 3506, 3948, 3825, 2325, 1800, 1153, 6379, 3839, 5672, 4689, 47806, 3975, 3980, 4113, 2847, 2070, 3425, 6628, 3997, 3513, 3656, 2335, 1182, 1954, 3996, 4599, 2391, 3479, 5021, 5020, 1558, 1924, 4545, 2991, 6065, 1290, 1559, 1317, 5423, 1707, 5055, 9975, 9971, 9919, 9915, 9912, 9910, 9908, 9901, 9844, 9830, 9826, 9825, 9823, 9814, 9812, 9777, 9745, 9683, 9680, 9679, 9674, 9665, 9661, 9654, 9648, 9620, 9619, 9613, 9583, 9527, 9513, 9493, 9478, 9464, 9454, 9364, 9351, 9183, 9170, 9133, 9130, 9128, 9125, 9065, 9061, 9044, 9037, 9013, 9004, 8925, 8898, 8887, 8882, 8879, 8878, 8865, 8843, 8801, 8798, 8790, 8772, 8756, 8752, 8736, 8680, 8673, 8658, 8655, 8644, 8640, 8621, 8601, 8562, 8539, 8531, 8530, 8515, 8484, 8479, 8477, 8455, 8454, 8453, 8452, 8451, 8409, 8339, 8308, 8295, 8273, 8268, 8255, 8248, 8245, 8144, 8133, 8110, 8092, 8064, 8037, 8029, 8018, 8014, 7975, 7895, 7854, 7853, 7852, 7830, 7813, 7788, 7780, 7772, 7771, 7688, 7685, 7654, 7637, 7600, 7555, 7553, 7456, 7451, 7231, 7218, 7184, 7119, 7104, 7102, 7092, 7068, 7067, 7043, 7033, 6973, 6972, 6956, 6942, 6922, 6920, 6897, 6877, 6780, 6734, 6725, 6710, 6709, 6650, 6647, 6644, 6606, 65514, 65488, 6535, 65311, 65048, 64890, 64727, 64726, 64551, 64507, 64438, 64320, 6412, 64127, 64080, 63803, 63675, 6349, 63423, 6323, 63156, 6310, 63105, 6309, 62866, 6274, 6273, 62674, 6259, 62570, 62519, 6250, 62312, 62188, 62080, 62042, 62006, 61942, 61851, 61827, 61734, 61722, 61669, 61617, 61616, 61516, 61473, 61402, 6126, 6120, 61170, 61169, 61159, 60989, 6091, 6090, 60794, 60789, 60783, 60782, 60753, 60743, 60728, 60713, 6067, 60628, 60621, 60612, 60579, 60544, 60504, 60492, 60485, 60403, 60401, 60377, 60279, 60243, 60227, 60177, 60111, 60086, 60055, 60003, 60002, 60000, 59987, 59841, 59829, 59810, 59778, 5975, 5974, 5971, 59684, 5966, 5958, 59565, 5954, 5953, 59525, 59510, 59509, 59504, 5949, 59499, 5948, 5945, 5939, 5936, 5934, 59340, 5931, 5927, 5926, 5924, 5923, 59239, 5921, 5920, 59191, 5917, 59160, 59149, 59122, 59107, 5908, 59087, 58991, 58970, 58908, 5888, 5887, 5881, 5878, 5875, 5874, 58721, 5871, 58699, 58634, 58622, 58610, 5860, 5858, 58570, 58562, 5854, 5853, 5852, 5849, 58498, 5848, 58468, 5845, 58456, 58446, 58430, 5840, 5839, 5838, 58374, 5836, 5834, 5831, 58310, 58305, 5827, 5826, 58252, 5824, 5821, 5820, 5817, 58164, 58109, 58107, 5808, 58072, 5806, 5804, 57999, 57988, 57928, 57923, 57896, 57891, 57733, 57730, 57702, 57681, 57678, 57576, 57479, 57398, 57387, 5737, 57352, 57350, 5734, 57347, 57335, 5732, 57325, 57123, 5711, 57103, 57020, 56975, 56973, 56827, 56822, 56810, 56725, 56723, 56681, 5667, 56668, 5665, 56591, 56535, 56507, 56293, 56259, 5622, 5621, 5620, 5612, 5611, 56055, 56016, 55948, 55910, 55907, 55901, 55781, 55773, 55758, 55721, 55684, 55652, 55635, 55579, 55569, 55568, 55556, 5552, 55527, 55479, 55426, 55400, 55382, 55350, 55312, 55227, 55187, 55183, 55000, 54991, 54987, 54907, 54873, 54741, 54722, 54688, 54658, 54605, 5458, 5457, 54551, 54514, 5444, 5442, 5441, 54323, 54321, 54276, 54263, 54235, 54127, 54101, 54075, 53958, 53910, 53852, 53827, 53782, 5377, 53742, 5370, 53690, 53656, 53639, 53633, 53491, 5347, 53469, 53460, 53370, 53361, 53319, 53240, 53212, 53189, 53178, 53085, 52948, 5291, 52893, 52675, 52665, 5261, 5259, 52573, 52506, 52477, 52391, 52262, 52237, 52230, 52226, 52225, 5219, 52173, 52071, 52046, 52025, 52003, 52002, 52001, 52000, 51965, 51961, 51909, 51906, 51809, 51800, 51772, 51771, 51658, 51582, 51515, 51488, 51485, 51484, 5147, 51460, 51423, 51366, 51351, 51343, 51300, 5125, 51240, 51235, 51234, 51233, 5122, 5121, 51139, 51118, 51067, 51037, 51020, 51011, 50997, 5098, 5096, 5095, 50945, 5090, 50903, 5088, 50887, 50854, 50849, 50836, 50835, 50834, 50833, 50831, 50815, 50809, 50787, 50733, 50692, 50585, 50577, 50576, 50545, 50529, 50513, 50356, 50277, 50258, 50246, 50224, 50205, 50202, 50198, 50189, 5017, 5016, 50101, 50040, 50019, 50016, 49927, 49803, 49765, 49762, 49751, 49678, 49603, 49597, 49522, 49521, 49520, 49519, 49500, 49498, 49452, 49398, 49372, 49352, 4931, 49302, 49275, 49241, 49235, 49232, 49228, 49216, 49213, 49211, 49204, 49203, 49202, 49201, 49197, 49196, 49191, 49190, 49189, 49179, 49173, 49172, 49170, 49169, 49166, 49132, 49048, 4903, 49002, 48973, 48967, 48966, 48925, 48813, 48783, 48682, 48648, 48631, 4860, 4859, 48434, 48356, 4819, 48167, 48153, 48127, 48083, 48067, 48009, 47969, 47966, 4793, 47860, 47858, 47850, 4778, 47777, 4771, 4770, 47700, 4767, 47634, 4760, 47595, 47581, 47567, 47448, 47372, 47348, 47267, 47197, 4712, 47119, 47029, 47012, 46992, 46813, 46593, 4649, 4644, 46436, 46418, 46372, 46310, 46182, 46171, 46115, 4609, 4606, 46069, 46034, 45960, 45864, 45777, 45697, 45624, 45602, 45463, 45438, 45413, 4530, 45226, 45220, 4517, 4516, 45164, 45136, 45050, 45038, 44981, 44965, 4476, 4471, 44711, 44704, 4464, 44628, 44616, 44541, 44505, 44479, 44431, 44410, 44380, 44200, 44119, 44101, 44004, 4388, 43868, 4384, 43823, 43734, 43690, 43654, 43425, 43242, 43231, 43212, 43143, 43139, 43103, 43027, 43018, 43002, 42990, 42906, 42735, 42685, 42679, 42675, 42632, 42590, 42575, 42560, 42559, 42452, 42449, 42322, 42276, 42251, 42158, 42127, 42035, 42001, 41808, 41773, 41632, 41551, 41442, 41398, 41348, 41345, 41342, 41318, 41281, 41250, 41142, 41123, 40951, 40834, 40812, 40754, 40732, 40712, 40628, 40614, 40513, 40489, 40457, 40400, 40393, 40306, 40011, 40005, 40003, 40002, 40001, 39917, 39895, 39883, 39869, 39795, 39774, 39763, 39732, 39630, 39489, 39482, 39433, 39380, 39293, 39265, 39117, 39067, 38936, 38805, 38780, 38764, 38761, 38570, 38561, 38546, 38481, 38446, 38358, 38331, 38313, 38270, 38224, 38205, 38194, 38029, 37855, 37789, 37777, 37674, 37647, 37614, 37607, 37522, 37393, 37218, 37185, 37174, 37151, 37121, 36983, 36962, 36950, 36914, 36824, 36823, 36748, 36710, 36694, 36677, 36659, 36552, 36530, 36508, 36436, 36368, 36275, 36256, 36105, 36104, 36046, 35986, 35929, 35906, 35901, 35900, 35879, 35731, 35593, 35553, 35506, 35401, 35393, 35392, 35349, 35272, 35217, 35131, 35116, 35050, 35033, 34875, 34833, 34783, 34765, 34728, 34683, 34510, 34507, 34401, 34381, 34341, 34317, 34189, 34096, 34036, 34021, 33895, 33889, 33882, 33879, 33841, 33605, 33604, 33550, 33523, 33522, 33444, 33395, 33367, 33337, 33335, 33327, 33277, 33203, 33200, 33192, 33175, 33124, 33087, 33070, 33017, 33011, 33000, 32976, 32961, 32960, 32944, 32932, 32911, 32910, 32908, 32905, 32904, 32898, 32897, 32888, 32871, 32869, 32868, 32858, 32842, 32837, 32820, 32815, 32814, 32807, 32799, 32798, 32797, 32790, 32789, 32788, 32765, 32764, 32261, 32260, 32219, 32200, 32102, 32088, 32031, 32022, 32006, 31728, 31657, 31522, 31438, 31386, 31339, 31072, 31058, 31033, 30896, 30705, 30659, 30644, 30599, 30519, 30299, 30195, 30087, 29810, 29507, 29243, 29152, 29045, 28967, 28924, 28851, 28850, 28717, 28567, 28374, 28142, 28114, 27770, 27537, 27521, 27372, 27351, 27350, 27316, 27204, 27087, 27075, 27074, 27055, 27016, 27015, 26972, 26669, 26417, 26340, 26007, 26001, 25847, 25717, 25703, 25486, 25473, 25445, 25327, 25288, 25262, 25260, 25174, 24999, 24616, 24552, 24416, 24392, 24218, 23953, 23887, 23723, 23451, 23430, 23382, 23342, 23296, 23270, 23228, 23219, 23040, 23017, 22969, 22959, 22882, 22769, 22727, 22719, 22711, 22563, 22341, 22290, 22223, 22200, 22177, 22100, 22063, 22022, 21915, 21891, 21728, 21634, 21631, 21473, 21078, 21011, 20990, 20940, 20934, 20883, 20734, 20473, 20280, 20228, 20227, 20226, 20225, 20224, 20223, 20180, 20179, 20147, 20127, 20125, 20118, 20111, 20106, 20102, 20089, 20085, 20080, 20076, 20052, 20039, 20032, 20021, 20017, 20011, 19996, 19995, 19852, 19715, 19634, 19612, 19501, 19464, 19403, 19353, 19201, 19200, 19130, 19010, 18962, 18910, 18887, 18874, 18669, 18569, 18517, 18505, 18439, 18380, 18337, 18336, 18231, 18148, 18080, 18015, 18012, 17997, 17985, 17969, 17867, 17860, 17802, 17801, 17715, 17702, 17701, 17700, 17413, 17409, 17255, 17251, 17129, 17089, 17070, 17017, 17016, 16901, 16845, 16797, 16725, 16724, 16723, 16464, 16372, 16349, 16297, 16286, 16283, 16273, 16270, 16048, 15915, 15758, 15730, 15722, 15677, 15670, 15646, 15645, 15631, 15550, 15448, 15344, 15317, 15275, 15191, 15190, 15145, 15050, 15005, 14916, 14891, 14827, 14733, 14693, 14545, 14534, 14444, 14443, 14418, 14254, 14237, 14218, 14147, 13899, 13846, 13784, 13766, 13730, 13723, 13695, 13580, 13502, 13359, 13340, 13318, 13306, 13265, 13264, 13261, 13250, 13229, 13194, 13193, 13192, 13188, 13167, 13149, 13142, 13140, 13132, 13130, 13093, 13017, 12962, 12955, 12892, 12891, 12766, 12702, 12699, 12414, 12340, 12296, 12275, 12271, 12251, 12243, 12240, 12225, 12192, 12171, 12156, 12146, 12137, 12132, 12097, 12096, 12090, 12080, 12077, 12034, 12031, 12019, 11940, 11863, 11862, 11813, 11735, 11697, 11552, 11401, 11296, 11288, 11250, 11224, 11200, 11180, 11100, 11089, 11033, 11032, 11031, 11026, 11019, 11007, 11003, 10900, 10878, 10852, 10842, 10754, 10699, 10602, 10601, 10567, 10565, 10556, 10555, 10554, 10553, 10552, 10551, 10550, 10535, 10529, 10509, 10494, 10443, 10414, 10387, 10357, 10347, 10338, 10280, 10255, 10246, 10245, 10238, 10093, 10064, 10045, 10042, 10035, 10019, 10018, 1327, 2330, 2580, 2700, 1584, 9020, 3281, 2439, 1250, 1607, 1736, 1330, 2270, 2728, 2888, 3803, 5250, 1645, 1303, 3636, 1251, 1243, 1291, 1297, 1200, 1811, 4442, 1118, 8401, 2101, 2889, 1694, 1730, 1912, 1745, 2250, 1306, 2997, 2449, 1262, 4007, 1101, 1268, 1735, 1858, 1264, 1711, 3118, 4601, 1321, 1598, 1305, 1632, 9995, 1307, 1981, 2532, 1808, 2435, 1194, 1622, 1239, 1799, 2882, 1683, 3063, 3062, 1340, 4447, 1806, 6888, 2438, 1261, 5969, 9343, 2583, 2031, 3798, 2269, 20001, 2622, 11001, 1207, 2850, 21201, 2908, 3936, 3023, 2280, 2623, 7099, 2372, 1318, 1339, 1276, 11000, 48619, 3497, 1209, 1331, 1240, 3856, 2987, 2326, 25001, 25000, 1792, 3919, 1299, 2984, 1715, 1703, 1677, 2086, 1708, 1228, 3787, 5502, 1620, 1316, 1569, 1210, 1691, 1282, 2124, 1791, 2150, 9909, 4022, 1324, 2584, 2300, 9287, 2806, 1566, 1713, 1592, 3749, 1302, 1709, 3485, 2418, 2472, 24554, 3146, 2134, 2898, 9161, 9160, 2930, 1319, 3811, 2456, 2901, 6579, 2550, 8403, 31416, 22273, 7005, 66, 32786, 32787, 706, 635, 6105, 400, 47, 830, 4008, 5977, 1989, 1444, 3985, 678, 27001, 591, 642, 446, 1441, 54320, 11, 769, 983, 979, 973, 967, 965, 961, 942, 935, 926, 925, 914, 863, 858, 844, 834, 817, 815, 811, 809, 789, 779, 743, 1019, 1507, 1492, 509, 762, 5632, 578, 1495, 5308, 52, 219, 525, 1420, 665, 620, 3064, 3045, 653, 158, 716, 861, 9991, 3049, 1366, 1364, 833, 91, 1680, 3398, 750, 615, 603, 6110, 101, 989, 27010, 510, 810, 1139, 4199, 76, 847, 649, 707, 68, 449, 664, 75, 104, 629, 1652, 682, 577, 985, 984, 974, 958, 952, 949, 946, 923, 916, 899, 897, 894, 889, 835, 824, 814, 807, 804, 798, 733, 727, 237, 12, 10, 501, 122, 440, 771, 1663, 828, 860, 695, 634, 538, 1359, 1358, 1517, 1370, 3900, 492, 268, 27374, 605, 8076, 1651, 1178, 6401, 761, 5145, 50, 2018, 1349, 2014, 7597, 2120, 1445, 1402, 1465, 9104, 627, 4660, 7273, 950, 1384, 1388, 760, 92, 831, 5978, 4557, 45, 112, 456, 1214, 3086, 702, 6665, 1404, 651, 5300, 6347, 5400, 1389, 647, 448, 1356, 5232, 1484, 450, 1991, 1988, 1523, 1400, 1399, 221, 1385, 5191, 1346, 2024, 2430, 988, 962, 948, 945, 941, 938, 936, 929, 927, 919, 906, 883, 881, 875, 872, 870, 866, 855, 851, 850, 841, 836, 826, 820, 819, 816, 813, 791, 745, 736, 735, 724, 719, 343, 334, 300, 28, 249, 230, 16, 1018, 1016, 658, 1474, 696, 630, 663, 2307, 1552, 609, 741, 353, 638, 1551, 661, 491, 640, 507, 673, 632, 1354, 9105, 6143, 676, 214, 14141, 182, 69, 27665, 1475, 97, 633, 560, 799, 7009, 2015, 628, 751, 4480, 1403, 8123, 1527, 723, 1466, 1486, 1650, 991, 832, 137, 1348, 685, 1762, 6701, 994, 4500, 194, 180, 1539, 1379, 51, 886, 2064, 1405, 1435, 11371, 1401, 1369, 402, 103, 1372, 704, 854, 8892, 47557, 624, 1387, 3397, 1996, 1995, 1997, 18182, 18184, 3264, 3292, 13720, 9107, 9106, 201, 1381, 35, 6588, 5530, 3141, 670, 970, 968, 964, 963, 960, 959, 951, 947, 944, 939, 933, 909, 895, 891, 879, 869, 868, 867, 837, 821, 812, 797, 796, 794, 788, 756, 734, 721, 718, 708, 703, 60, 40, 253, 231, 14, 1017, 1003, 656, 975, 2026, 1497, 553, 511, 611, 689, 1668, 1664, 15, 561, 997, 505, 1496, 637, 213, 1412, 1515, 692, 694, 681, 680, 644, 675, 1467, 454, 622, 1476, 1373, 770, 262, 654, 1535, 58, 177, 26208, 677, 1519, 1398, 3457, 401, 412, 493, 13713, 94, 1498, 871, 1390, 6145, 133, 362, 118, 193, 115, 1549, 7008, 608, 1426, 1436, 38, 74, 73, 71, 601, 136, 4144, 129, 16444, 1446, 4132, 308, 1528, 1365, 1393, 1394, 1493, 138, 5997, 397, 29, 31, 44, 2627, 6147, 1510, 568, 350, 2053, 6146, 6544, 1763, 3531, 399, 1537, 1992, 1355, 1454, 261, 887, 200, 1376, 1424, 6111, 1410, 1409, 686, 5301, 5302, 1513, 747, 9051, 1499, 7006, 1439, 1438, 8770, 853, 196, 93, 410, 462, 619, 1529, 1990, 1994, 1986, 1386, 18183, 18181, 6700, 1442, 95, 6400, 1432, 1548, 486, 1422, 114, 1397, 6142, 1827, 626, 422, 688, 206, 202, 204, 1483, 7634, 774, 699, 2023, 776, 672, 1545, 2431, 697, 982, 978, 972, 966, 957, 956, 934, 920, 915, 908, 907, 892, 890, 885, 884, 882, 877, 876, 865, 857, 852, 849, 842, 838, 827, 818, 793, 785, 784, 755, 746, 738, 737, 717, 34, 336, 325, 303, 276, 273, 236, 235, 233, 181, 604, 1362, 712, 1437, 2027, 1368, 1531, 645, 65301, 260, 536, 764, 698, 607, 1667, 1662, 1661, 404, 224, 418, 176, 848, 315, 466, 403, 1456, 1479, 355, 763, 1472, 453, 759, 437, 2432, 120, 415, 1544, 1511, 1538, 346, 173, 54, 56, 265, 1462, 13701, 1518, 1457, 117, 1470, 13715, 13714, 267, 1419, 1418, 1407, 380, 518, 65, 391, 392, 413, 1391, 614, 1408, 162, 108, 4987, 1502, 598, 582, 487, 530, 1509, 72, 4672, 189, 209, 270, 7464, 408, 191, 1459, 5714, 5717, 5713, 564, 767, 583, 1395, 192, 1448, 428, 4133, 1416, 773, 1458, 526, 1363, 742, 1464, 1427, 1482, 569, 571, 6141, 351, 3984, 5490, 2, 13718, 373, 17300, 910, 148, 7326, 271, 423, 1451, 480, 1430, 1429, 781, 383, 2564, 613, 612, 652, 5303, 1383, 128, 19150, 1453, 190, 1505, 1371, 533, 27009, 27007, 27005, 27003, 27002, 744, 1423, 1374, 141, 1440, 1396, 352, 96, 48, 552, 570, 217, 528, 452, 451, 2766, 2108, 132, 1993, 1987, 130, 18187, 216, 3421, 142, 13721, 67, 15151, 364, 1411, 205, 6548, 124, 116, 5193, 258, 485, 599, 149, 1469, 775, 2019, 516, 986, 977, 976, 955, 954, 937, 932, 8, 896, 893, 845, 768, 766, 739, 337, 329, 326, 305, 295, 294, 293, 289, 288, 277, 238, 234, 229, 228, 226, 522, 2028, 150, 572, 596, 420, 460, 1543, 358, 361, 470, 360, 457, 643, 322, 168, 753, 369, 185, 43188, 1541, 1540, 752, 496, 662, 1449, 1480, 1473, 184, 1672, 1671, 1670, 435, 434, 1532, 1360, 174, 472, 1361, 17007, 414, 535, 432, 479, 473, 151, 1542, 438, 1488, 1508, 618, 316, 1367, 439, 284, 542, 370, 2016, 248, 1491, 44123, 41230, 7173, 5670, 18136, 3925, 7088, 1425, 17755, 17756, 4072, 5841, 2102, 4123, 2989, 10051, 10050, 31029, 3726, 9978, 9925, 6061, 6058, 6057, 6056, 6054, 6053, 6049, 6048, 6047, 6046, 6045, 6044, 6043, 6042, 6041, 6040, 6039, 6038, 6037, 6036, 6035, 6034, 6033, 6032, 6031, 6029, 6028, 6027, 6026, 6024, 6023, 6022, 6020, 6019, 6018, 6016, 6014, 6013, 6012, 6011, 36462, 5793, 3423, 3424, 4095, 3646, 3510, 3722, 3651, 14500, 3865, 15345, 3763, 38422, 3877, 9092, 5344, 2341, 6116, 2157, 165, 6936, 8041, 4888, 4889, 3074, 2165, 4389, 5770, 5769, 16619, 11876, 11877, 3741, 3633, 3840, 3717, 3716, 3590, 2805, 4537, 9762, 5007, 5006, 5358, 4879, 6114, 4185, 2784, 3724, 2596, 2595, 4417, 4845, 22321, 22289, 3219, 1338, 36411, 3861, 5166, 3674, 1785, 534, 6602, 47001, 5363, 8912, 2231, 5747, 5748, 11208, 7236, 4049, 4050, 22347, 63, 3233, 3359, 4177, 48050, 3111, 3427, 5321, 5320, 3702, 2907, 8991, 8990, 2054, 4847, 9802, 9800, 4368, 5990, 3563, 5744, 5743, 12321, 12322, 9206, 9204, 9205, 9201, 9203, 2949, 2948, 6626, 8199, 4145, 3482, 2216, 13708, 3786, 3375, 7566, 2539, 2387, 3317, 2410, 2255, 3883, 4299, 4296, 4295, 4293, 4292, 4291, 4290, 4289, 4288, 4287, 4286, 4285, 4284, 4283, 4282, 4281, 4280, 4278, 4277, 4276, 4275, 4274, 4273, 4272, 4271, 4270, 4269, 4268, 4267, 4266, 4265, 4264, 4263, 4261, 4260, 4259, 4258, 4257, 4256, 4255, 4254, 4253, 4251, 4250, 4249, 4248, 4247, 4246, 4245, 4244, 4241, 4240, 4239, 4238, 4237, 4236, 4235, 4233, 4232, 4231, 4230, 4229, 4228, 4227, 4226, 4225, 4223, 4222, 4221, 4219, 4218, 4217, 4216, 4215, 4214, 4213, 4212, 4211, 4210, 4209, 4208, 4207, 4205, 4204, 4203, 4202, 4201, 2530, 5164, 28200, 3845, 3541, 4052, 21590, 1796, 25793, 8699, 8182, 4991, 2474, 5780, 3676, 24249, 1631, 6672, 6673, 3601, 5046, 3509, 1852, 2386, 8473, 7802, 4789, 3555, 12013, 12012, 3752, 3245, 3231, 16666, 6678, 17184, 9086, 9598, 3073, 2074, 1956, 2610, 3738, 2994, 2993, 2802, 1885, 14149, 13786, 10100, 9284, 14150, 10107, 4032, 2821, 3207, 14154, 24323, 2771, 5646, 2426, 18668, 2554, 4188, 3654, 8034, 5675, 15118, 4031, 2529, 2248, 1142, 19194, 433, 3534, 3664, 2537, 519, 2655, 4184, 1506, 3098, 7887, 37654, 1979, 9629, 2357, 1889, 3314, 3313, 4867, 2696, 3217, 6306, 1189, 5281, 8953, 1910, 13894, 372, 3720, 1382, 2542, 3584, 4034, 145, 27999, 3791, 21800, 2670, 3492, 24678, 34249, 39681, 1846, 5197, 5462, 5463, 2862, 2977, 2978, 3468, 2675, 3474, 4422, 12753, 13709, 2573, 3012, 4307, 4725, 3346, 3686, 4070, 9555, 4711, 4323, 4322, 10200, 7727, 3608, 3959, 2405, 3858, 3857, 24322, 6118, 4176, 6442, 8937, 17224, 17225, 33434, 1906, 22351, 2158, 5153, 3885, 24465, 3040, 20167, 8066, 474, 2739, 3308, 590, 3309, 7902, 7901, 7903, 20046, 5582, 5583, 7872, 13716, 13717, 13705, 6252, 2915, 1965, 3459, 3160, 3754, 3243, 10261, 7932, 7933, 5450, 11971, 379, 7548, 1832, 3805, 3805, 16789, 8320, 8321, 4423, 2296, 7359, 7358, 7357, 7356, 7355, 7354, 7353, 7352, 7351, 7350, 7349, 7348, 7347, 7346, 7344, 7343, 7342, 7341, 7340, 7339, 7338, 7337, 7336, 7335, 7334, 7333, 7332, 7331, 7330, 7329, 7328, 7327, 7324, 7323, 7322, 7321, 7319, 7318, 7317, 7316, 7315, 7314, 7313, 7312, 7311, 7310, 7309, 7308, 7307, 7306, 7305, 7304, 7303, 7302, 7301, 8140, 5196, 5195, 6130, 5474, 5471, 5472, 5470, 4146, 3713, 5048, 31457, 7631, 3544, 41121, 11600, 3696, 3696, 3549, 1380, 22951, 22800, 3521, 2060, 6083, 9668, 3552, 1814, 1977, 2576, 2729, 24680, 13710, 13712, 25900, 2403, 2402, 2470, 5203, 3579, 2306, 1450, 7015, 7012, 7011, 22763, 2156, 2493, 4019, 4018, 4017, 4015, 2392, 3175, 32249, 1627, 10104, 2609, 5406, 3251, 4094, 3241, 6514, 6418, 3734, 2679, 4953, 5008, 2880, 8243, 8280, 26133, 8555, 5629, 3547, 5639, 5638, 5637, 5115, 3723, 4950, 3895, 3894, 3491, 3318, 6419, 3185, 243, 3212, 9536, 1925, 11171, 8404, 8405, 8989, 6787, 6483, 3867, 3866, 1860, 1870, 5306, 3816, 7588, 6786, 2084, 11165, 11161, 11163, 11162, 11164, 3708, 4850, 7677, 16959, 247, 3478, 5349, 3854, 5397, 7411, 9612, 11173, 9293, 5027, 5026, 5705, 8778, 527, 1312, 8808, 6144, 4157, 4156, 3249, 7471, 3615, 2154, 45966, 17235, 3018, 38800, 2737, 156, 3807, 2876, 1759, 7981, 3606, 3647, 3438, 4683, 9306, 9312, 7016, 33334, 3413, 3834, 3835, 2440, 6121, 2568, 17185, 7982, 2290, 2569, 2863, 1964, 4738, 2132, 17777, 16162, 6551, 3230, 4538, 3884, 9282, 9281, 4882, 5146, 580, 1967, 2659, 2409, 5416, 2657, 3380, 5417, 2658, 5161, 5162, 10162, 10161, 33656, 7560, 2599, 2704, 2703, 4170, 7734, 9522, 3158, 4426, 4786, 2721, 1608, 3516, 4988, 4408, 1847, 36423, 2826, 2827, 3556, 6456, 6455, 3874, 3611, 2629, 2630, 166, 5059, 3110, 1733, 40404, 2257, 2278, 4750, 4303, 3688, 4751, 5794, 4752, 7626, 16950, 3273, 3896, 3635, 1959, 4753, 2857, 4163, 1659, 2905, 2904, 2733, 4936, 5032, 3048, 28240, 2320, 4742, 22335, 5043, 4105, 1257, 3841, 43210, 4366, 5163, 11106, 5434, 6444, 6445, 5634, 5636, 5635, 6343, 4546, 3242, 5568, 4057, 24666, 21221, 6488, 6484, 6486, 6485, 6487, 6443, 6480, 6489, 2603, 4787, 2367, 9212, 9213, 5445, 45824, 8351, 13711, 4076, 5099, 2316, 3588, 5093, 9450, 8056, 8055, 8054, 8059, 8058, 8057, 8053, 3090, 3255, 2254, 2479, 2477, 2478, 3496, 3495, 2089, 38865, 9026, 9025, 9024, 9023, 3480, 1905, 3550, 7801, 2189, 5361, 32635, 3782, 3432, 3978, 6629, 3143, 7784, 2342, 2309, 2705, 2310, 2384, 6315, 5343, 9899, 5168, 5167, 3927, 266, 2577, 5307, 3838, 19007, 7708, 37475, 7701, 5435, 3499, 2719, 3352, 25576, 3942, 1644, 3755, 5574, 5573, 7542, 1129, 4079, 3038, 4033, 9401, 9402, 20012, 20013, 30832, 1606, 5410, 5422, 5409, 9801, 7743, 14034, 14033, 4952, 3452, 2760, 3153, 23272, 2578, 5156, 8554, 7401, 3771, 3138, 3137, 3500, 6900, 363, 3455, 1698, 13217, 2752, 3863, 3864, 10201, 6568, 2377, 3677, 520, 2258, 4124, 8051, 2223, 3194, 4041, 48653, 8270, 5693, 25471, 2416, 9208, 7810, 7870, 2249, 7473, 4664, 4590, 2777, 2776, 2057, 6148, 3296, 4410, 4684, 8230, 5842, 1431, 12109, 4756, 4336, 324, 323, 3019, 39, 2225, 4733, 30100, 2999, 3422, 107, 1232, 3418, 3537, 5, 8184, 3789, 5231, 4731, 4373, 45045, 3974, 12302, 2373, 6084, 16665, 16385, 18635, 18634, 10253, 7227, 3572, 3032, 5786, 2346, 2348, 2347, 2349, 45002, 3553, 43191, 5313, 3707, 3706, 3736, 32811, 1942, 44553, 35001, 35002, 35005, 35006, 35003, 35004, 532, 2214, 5569, 3142, 2332, 3768, 2774, 2773, 6099, 2167, 2714, 2713, 3533, 4037, 2457, 1953, 9345, 21553, 2408, 2736, 2188, 18104, 1813, 469, 1596, 3178, 5430, 5676, 2177, 4841, 5028, 7980, 3166, 3554, 3566, 3843, 5677, 7040, 2589, 8153, 10055, 5464, 2497, 4354, 9222, 5083, 5082, 45825, 2612, 5689, 6209, 2523, 2490, 2468, 3543, 7794, 4193, 4951, 3951, 4093, 7747, 7997, 8117, 6140, 4329, 320, 319, 597, 3453, 4457, 2303, 5360, 4487, 409, 344, 1460, 5716, 5715, 9640, 7663, 7798, 7797, 4352, 15999, 34962, 34963, 34964, 4749, 8032, 4182, 4182, 1283, 1778, 3248, 2722, 2039, 3650, 3133, 2618, 4168, 10631, 1392, 3910, 6716, 47809, 4690, 9280, 6163, 2315, 3607, 5630, 4455, 4456, 1587, 28001, 5134, 13224, 13223, 5507, 2443, 4150, 7172, 3710, 9889, 6464, 7787, 6771, 6770, 3055, 2487, 16310, 16311, 3540, 34379, 34378, 2972, 7633, 6355, 188, 2790, 32400, 4351, 3934, 3933, 4659, 1819, 5586, 5863, 9318, 318, 5318, 2634, 4416, 5078, 3189, 3010, 15740, 1603, 2787, 4390, 468, 4869, 4868, 3177, 3347, 6124, 2350, 3208, 2520, 2441, 3109, 3557, 281, 1916, 4313, 5312, 4066, 345, 9630, 9631, 6817, 3582, 9279, 9278, 3587, 4747, 2178, 5112, 3135, 5443, 7880, 1980, 6086, 3254, 4012, 9597, 3253, 2274, 2299, 8444, 6655, 44322, 44321, 5351, 5350, 5172, 4172, 1332, 2256, 8129, 8128, 4097, 8161, 2665, 2664, 6162, 4189, 1333, 3735, 586, 6581, 6582, 4681, 4312, 4989, 7216, 3348, 7680, 8276, 3095, 6657, 30002, 7237, 3435, 2246, 1675, 31400, 4311, 6671, 6679, 3034, 40853, 11103, 3274, 3355, 3078, 3075, 3076, 8070, 2484, 2483, 3891, 1571, 1830, 1630, 8997, 8102, 2482, 2481, 5155, 5575, 3718, 22005, 22004, 22003, 22002, 2524, 1829, 2237, 3977, 3976, 3303, 19191, 3433, 5724, 2400, 7629, 6640, 2389, 30999, 2447, 3673, 7430, 7429, 7426, 7431, 7428, 7427, 9390, 35357, 7728, 8004, 5045, 8688, 1258, 5757, 5729, 5767, 5766, 5755, 5768, 4743, 9008, 9007, 3187, 20014, 4089, 3434, 4840, 4843, 3100, 314, 3154, 9994, 9993, 4304, 2428, 2199, 2198, 2185, 4428, 4429, 4162, 4395, 2056, 5402, 3340, 3339, 3341, 3338, 7275, 7274, 7277, 7276, 4359, 2077, 9966, 4732, 3320, 11175, 11174, 11172, 13706, 3523, 429, 2697, 18186, 3442, 3441, 29167, 36602, 7030, 1894, 28000, 126, 4420, 2184, 3780, 49001, 4128, 8711, 10810, 45001, 5415, 4453, 359, 3266, 36424, 2868, 7724, 396, 2645, 23402, 23400, 23401, 3016, 21010, 5215, 4663, 4803, 2338, 15126, 5209, 3406, 3405, 5627, 4088, 2210, 2244, 2817, 10111, 10110, 1242, 5299, 2252, 3649, 6421, 6420, 1617, 48001, 48002, 48003, 48005, 48004, 48000, 61, 4134, 38412, 20048, 7393, 4021, 178, 8457, 550, 2058, 2075, 2076, 3165, 6133, 2614, 2585, 4702, 4701, 2586, 3203, 3204, 16361, 16367, 16360, 16368, 4159, 170, 2293, 4703, 8981, 3409, 7549, 171, 20049, 1155, 537, 3196, 3195, 2411, 2788, 4127, 6777, 6778, 1879, 5421, 3440, 2128, 21846, 21849, 21847, 21848, 395, 154, 155, 4425, 2328, 3129, 3641, 3640, 1970, 2486, 2485, 6842, 6841, 3149, 3148, 3150, 3151, 1406, 218, 10116, 10114, 2219, 2735, 10117, 10113, 2220, 3725, 5229, 4350, 6513, 4335, 4334, 5681, 1676, 2971, 4409, 3131, 4441, 1612, 1616, 1613, 1614, 13785, 11104, 11105, 3829, 11095, 3507, 3213, 7474, 3886, 4043, 2730, 377, 378, 3024, 2738, 2738, 2528, 4844, 4842, 5979, 1888, 2093, 2094, 20034, 2163, 3159, 6317, 4361, 2895, 3753, 2343, 3015, 1790, 3950, 6363, 9286, 9285, 7282, 6446, 2273, 33060, 2388, 9119, 3733, 32801, 4421, 7420, 9903, 6622, 5354, 7742, 2305, 2791, 8115, 3122, 2855, 2871, 4554, 2171, 2172, 2173, 2174, 3343, 7392, 3958, 3358, 46, 6634, 8503, 3924, 2488, 10544, 10543, 10541, 10540, 10542, 4691, 8666, 1576, 4986, 6997, 3732, 4688, 7871, 9632, 7869, 2593, 3764, 5237, 4668, 4173, 4667, 8077, 4310, 7606, 5136, 4069, 21554, 7391, 9445, 2180, 3180, 2621, 4551, 3008, 7013, 7014, 5362, 6601, 1512, 5356, 6074, 5726, 5364, 5725, 6076, 6075, 2175, 3132, 5359, 2176, 5022, 4679, 4680, 6509, 2266, 6382, 2230, 6390, 6370, 6360, 393, 2311, 8787, 18, 8786, 47000, 19788, 1960, 9596, 4603, 4151, 4552, 11211, 3569, 4883, 3571, 2944, 2945, 2272, 7720, 5157, 3445, 2427, 2727, 2363, 46999, 2789, 13930, 3232, 2688, 3235, 5598, 3115, 3117, 3116, 3331, 3332, 3302, 3330, 3558, 8809, 3570, 4153, 2591, 4179, 4171, 3276, 4360, 4458, 7421, 49000, 7073, 3836, 5282, 8384, 36700, 4686, 269, 9255, 6201, 2544, 2516, 2864, 5092, 2243, 4902, 313, 3691, 2453, 4345, 44900, 36444, 3565, 36443, 4894, 3747, 3746, 5044, 6471, 3079, 4913, 4741, 10805, 3487, 3068, 8162, 4083, 4082, 4081, 7026, 1983, 2289, 1629, 1628, 1634, 8101, 6482, 5254, 5058, 4044, 3591, 3592, 1903, 5062, 6087, 2090, 2465, 2466, 6200, 8208, 8207, 8204, 31620, 8205, 8206, 3278, 2145, 2143, 2147, 2146, 3767, 46336, 10933, 4341, 1969, 10809, 12300, 8191, 517, 4670, 7365, 3028, 3027, 3029, 1203, 1886, 11430, 374, 2212, 3407, 2816, 2779, 2815, 2780, 3373, 3739, 3815, 4347, 11796, 3970, 4547, 1764, 2395, 4372, 4432, 9747, 4371, 3360, 3361, 4331, 40023, 27504, 2294, 5253, 7697, 35354, 186, 30260, 4566, 584, 5696, 6623, 6620, 6621, 2502, 3112, 36865, 2918, 4661, 31016, 26262, 26263, 3642, 48048, 5309, 3155, 4166, 27442, 6583, 3215, 3214, 8901, 19020, 4160, 3094, 3093, 3777, 1937, 1938, 1939, 1940, 2097, 1936, 1810, 6244, 6243, 6242, 6241, 4107, 19541, 3529, 3528, 5230, 4327, 5883, 2205, 7095, 3794, 3473, 3472, 7181, 5034, 3627, 8091, 1578, 5673, 5049, 4880, 3258, 2828, 3719, 7478, 7280, 1636, 1637, 3775, 24321, 499, 3205, 1950, 1949, 3226, 8148, 5047, 4075, 17223, 21000, 3504, 3206, 2632, 529, 4073, 32034, 18769, 2527, 4593, 4791, 7031, 33435, 4740, 4739, 4068, 20202, 4737, 9214, 2215, 3743, 2088, 7410, 5728, 45054, 3614, 8020, 11751, 2202, 6697, 4744, 1884, 3699, 6714, 1611, 7202, 4569, 3508, 24386, 16995, 16994, 16994, 1674, 1673, 7128, 4746, 17234, 9215, 4486, 484, 5057, 5056, 7624, 2980, 4109, 49150, 215, 23005, 23004, 23003, 23002, 23001, 23000, 2716, 3560, 5597, 134, 38001, 38000, 4067, 1428, 2480, 5029, 8067, 5069, 3156, 3139, 244, 7675, 7673, 7672, 7674, 2637, 4139, 3783, 3657, 11320, 8615, 585, 48128, 2239, 3596, 2055, 3186, 19000, 5165, 3420, 17220, 17221, 19998, 2404, 2079, 4152, 4604, 25604, 5742, 5741, 4553, 2799, 4801, 4802, 2063, 14143, 14142, 4061, 4062, 4063, 4064, 31948, 31949, 2276, 2275, 1881, 2078, 3660, 3661, 1920, 1919, 9085, 424, 1933, 1934, 9089, 9088, 3667, 3666, 12003, 12004, 3539, 3538, 3267, 385, 3494, 4594, 4595, 4596, 3898, 9614, 4169, 5674, 2374, 5105, 8313, 44323, 5628, 2570, 2113, 4591, 4592, 5228, 5224, 5227, 2207, 4484, 3037, 2209, 2448, 3101, 382, 381, 3209, 7510, 2206, 2690, 2208, 7738, 5565, 5317, 3329, 3612, 5316, 3449, 2029, 1985, 10125, 2597, 3634, 8231, 3250, 43438, 4884, 4117, 2467, 4148, 7397, 22370, 8807, 3921, 4306, 10860, 3740, 1161, 2641, 7630, 3804, 4197, 11108, 9954, 6791, 3623, 3769, 3036, 5315, 5305, 3542, 5304, 11720, 2517, 3179, 2979, 2356, 3745, 18262, 2186, 35356, 3436, 2152, 2123, 1452, 4729, 3761, 3136, 9339, 30400, 6267, 6269, 6268, 3757, 4755, 4754, 4026, 5117, 9277, 2947, 3386, 2217, 37483, 16002, 5687, 2072, 1909, 9122, 9123, 4131, 3912, 3229, 1880, 5688, 4332, 10800, 4985, 3108, 3475, 6080, 4790, 23053, 6081, 8190, 7017, 7283, 4730, 2159, 3429, 2660, 14145, 3484, 3762, 3222, 8322, 1421, 1859, 31765, 2914, 3051, 38201, 8881, 4340, 8074, 2678, 2677, 4110, 2731, 286, 3402, 3272, 1514, 3382, 1904, 1902, 3648, 2975, 574, 8502, 3488, 9217, 4130, 7726, 5556, 7244, 41111, 4411, 4084, 2242, 4396, 4901, 7545, 7544, 27008, 27006, 27004, 5579, 2884, 3035, 1193, 5618, 7018, 2673, 4086, 8043, 8044, 3192, 3729, 1855, 1856, 1784, 24922, 1887, 7164, 4349, 7394, 16021, 16020, 6715, 4915, 4122, 3216, 14250, 3152, 1776, 36524, 4320, 4727, 3225, 2819, 4038, 6417, 347, 3047, 2495, 10081, 38202, 2515, 2514, 4353, 38472, 10102, 4085, 3953, 4788, 3088, 3134, 3639, 4309, 2755, 1928, 5075, 26486, 5401, 3759, 43440, 1926, 1982, 1798, 9981, 4536, 4535, 1504, 592, 1267, 6935, 2036, 6316, 2221, 44818, 34980, 2380, 2379, 6107, 1772, 8416, 8417, 8266, 4023, 3629, 9617, 3679, 3727, 4942, 4941, 4940, 43439, 3628, 3620, 5116, 3259, 4666, 4669, 3819, 37601, 5084, 5085, 3383, 5599, 5600, 5601, 3665, 1818, 3044, 1295, 7962, 7117, 121, 17754, 6636, 6635, 20480, 23333, 3585, 6322, 6321, 4091, 4092, 140, 6656, 3693, 11623, 11723, 13218, 3682, 3218, 9083, 3197, 3198, 394, 2526, 7700, 7707, 2916, 2917, 4370, 6515, 12010, 5398, 3564, 4346, 1378, 1893, 3525, 3638, 2228, 6632, 3392, 3671, 6159, 3462, 3461, 3464, 3465, 3460, 3463, 3123, 34567, 8149, 6703, 6702, 2263, 3477, 3524, 6160, 17729, 3711, 45678, 2168, 3328, 38462, 3932, 3295, 2164, 3395, 2874, 3246, 3247, 4191, 4028, 3489, 4556, 5684, 13929, 31685, 9987, 4060, 13819, 13820, 13821, 13818, 13822, 2420, 7547, 3685, 2193, 4427, 1930, 8913, 7021, 7020, 5719, 5245, 6326, 6320, 6325, 3522, 44544, 13400, 6088, 3568, 8567, 3567, 5567, 7165, 4142, 3161, 5352, 195, 1172, 5993, 3199, 3574, 4059, 1177, 3624, 19999, 21212, 246, 5107, 14002, 7171, 3448, 3336, 3335, 3337, 198, 197, 3447, 5031, 4605, 2464, 2227, 3223, 1335, 2226, 33333, 2762, 2761, 3227, 3228, 33331, 2861, 2860, 2098, 4301, 3252, 547, 546, 6785, 8750, 4330, 3776, 24850, 8805, 2763, 4167, 2092, 3444, 8415, 3714, 1278, 5700, 3668, 7569, 365, 8894, 8893, 8891, 8890, 11202, 3988, 1160, 3938, 6117, 6624, 6625, 2073, 461, 3578, 11109, 2229, 1775, 2764, 3678, 6511, 1133, 29999, 2594, 3881, 3498, 8732, 5777, 3394, 3393, 2298, 2297, 9388, 9387, 3120, 3297, 1898, 8442, 9888, 4183, 4673, 3778, 5271, 3127, 1932, 4451, 2563, 4452, 9346, 7022, 3631, 3630, 105, 3271, 2699, 3004, 2129, 4187, 3113, 2314, 8380, 8377, 8376, 8379, 8378, 3818, 41797, 41796, 38002, 3364, 3366, 2824, 2823, 3609, 4055, 4054, 4053, 2654, 19220, 9093, 3183, 2565, 4078, 4774, 2153, 17222, 7551, 7563, 3072, 4047, 9695, 4846, 5992, 5683, 4692, 3191, 3417, 7169, 3973, 46998, 16384, 3947, 47100, 6970, 2491, 7023, 10321, 42508, 3822, 2417, 2555, 3257, 3256, 22343, 64, 7215, 20003, 4450, 3751, 3605, 2534, 3490, 4419, 7689, 7574, 3377, 3779, 44444, 3039, 2415, 2183, 26257, 3576, 3575, 2976, 7168, 8501, 164, 3384, 7550, 45514, 356, 2617, 3730, 6688, 6687, 6690, 7683, 2052, 3481, 4136, 4137, 9087, 172, 1729, 4980, 7229, 7228, 24754, 2897, 7279, 2512, 2513, 4870, 22305, 5787, 6633, 131, 15555, 4051, 4785, 43441, 5784, 7546, 3887, 5194, 1743, 2891, 3770, 1377, 4316, 4314, 3099, 1572, 1891, 1892, 3349, 18241, 18243, 18242, 18185, 5505, 562, 531, 3772, 5065, 5064, 2182, 3893, 2921, 2922, 4074, 4140, 4115, 3056, 3616, 3559, 4970, 4969, 3114, 3157, 3750, 12168, 2122, 7129, 7162, 7167, 5270, 1197, 9060, 3106, 5247, 5246, 3290, 4728, 8998, 8610, 8609, 3756, 8614, 8613, 8612, 8611, 1872, 3583, 24676, 4377, 5079, 4378, 1734, 3545, 7262, 3675, 2552, 22537, 3709, 14414, 5251, 1882, 42509, 2318, 4326, 1563, 7163, 1554, 7161, 595, 348, 282, 8026, 5249, 5248, 5154, 10880, 3626, 4990, 3107, 6410, 6409, 6408, 6407, 6406, 6405, 6404, 4677, 581, 4671, 2964, 2965, 28589, 47808, 3966, 2446, 1854, 1961, 2444, 2277, 4175, 3188, 3043, 9380, 3692, 5682, 2155, 4104, 4103, 4102, 3593, 2845, 2844, 4186, 2218, 4678, 2017, 2913, 7648, 4914, 7687, 6501, 9750, 3344, 1896, 4568, 10128, 6768, 6767, 3182, 1313, 3181, 2059, 3604, 6300, 10129, 3695, 6301, 2494, 2625, 48129, 8195, 2574, 5750, 13823, 13216, 4027, 5068, 25955, 25954, 6946, 3411, 24577, 5429, 4621, 6784, 4676, 4675, 4784, 3785, 5425, 5424, 4305, 3960, 3408, 5584, 5585, 1943, 3124, 6508, 6507, 4155, 1120, 1929, 4324, 10439, 6506, 6505, 6122, 4971, 3387, 152, 2635, 2169, 6696, 2204, 3512, 2071, 10260, 35100, 3277, 3502, 2066, 2238, 4413, 20057, 2992, 2050, 3965, 10990, 31020, 4685, 1140, 7508, 16003, 5913, 4071, 3104, 3437, 5067, 33123, 1146, 44600, 2264, 7543, 2419, 32896, 2317, 3821, 4937, 1520, 11367, 4154, 3617, 20999, 1170, 1171, 27876, 4485, 4704, 7235, 3087, 45000, 4405, 4404, 4406, 4402, 4403, 4400, 5727, 11489, 2192, 4077, 4448, 3581, 5150, 13702, 3863, 3864, 3451, 386, 8211, 7166, 3518, 27782, 3176, 9292, 3174, 9295, 9294, 3426, 8423, 3140, 7570, 421, 2114, 6344, 2581, 2582, 11321, 384, 23546, 1834, 1115, 4165, 1557, 3758, 7847, 5086, 4849, 2037, 1447, 3312, 187, 4488, 2336, 387, 208, 207, 203, 3454, 10548, 4674, 38203, 3239, 3236, 3237, 3238, 4573, 2758, 10252, 2759, 8121, 2754, 8122, 3184, 539, 6082, 18888, 9952, 9951, 7846, 7845, 6549, 5456, 5455, 5454, 4851, 5072, 3939, 2247, 1206, 3715, 2646, 3054, 5671, 8040, 376, 2640, 30004, 30003, 5192, 4393, 4392, 4391, 4394, 1931, 5506, 8301, 4563, 35355, 4011, 7799, 3265, 9209, 693, 36001, 9956, 9955, 6627, 3234, 2667, 2668, 3613, 4804, 2887, 3416, 3833, 9216, 2846, 17555, 2786, 3316, 3021, 3026, 4878, 3917, 4362, 7775, 3224, 23457, 23456, 4549, 4431, 2295, 3573, 5073, 3760, 3357, 3954, 3705, 3704, 2692, 6769, 7170, 2521, 2085, 3096, 2810, 2859, 3431, 9389, 3655, 5106, 5103, 7509, 6801, 4013, 5540, 2476, 2475, 2334, 12007, 12008, 6868, 4046, 18463, 32483, 4030, 8793, 2259, 62, 1955, 3781, 3619, 3618, 28119, 4726, 4502, 4597, 4598, 3598, 3597, 3125, 4149, 9953, 23294, 2933, 2934, 5783, 5782, 5785, 5781, 15363, 48049, 2339, 5265, 5264, 1181, 3446, 3428, 15998, 3091, 2133, 3774, 317, 3832, 508, 3721, 1619, 1716, 2279, 3412, 2327, 6558, 2130, 1760, 5413, 2396, 2923, 3378, 3466, 2504, 2720, 4871, 7395, 3926, 1727, 1326, 2518, 1890, 2781, 565, 4984, 3342, 21845, 1963, 2851, 3748, 1739, 1269, 2455, 2547, 2548, 2546, 13882, 7779, 2695, 312, 2996, 2893, 1589, 2649, 1224, 1345, 3625, 2538, 3321, 175, 1868, 4344, 1853, 3058, 3802, 78, 2770, 3270, 575, 1771, 4839, 4838, 4837, 671, 430, 431, 2745, 2648, 3356, 1957, 2820, 1978, 2927, 2499, 2437, 2138, 2110, 1797, 1737, 483, 390, 1867, 1624, 1833, 2879, 2767, 2768, 2943, 1568, 2489, 1237, 2741, 2742, 8804, 1588, 6069, 1869, 2642, 20670, 594, 2885, 2669, 476, 2798, 3083, 3082, 3081, 2361, 5104, 1758, 7491, 1728, 5428, 1946, 559, 1610, 3144, 1922, 2726, 6149, 1838, 4014, 1274, 2647, 4106, 6102, 4548, 19540, 1866, 6965, 6966, 6964, 6963, 1751, 1625, 5453, 2709, 7967, 3354, 566, 4178, 2986, 1226, 1836, 1654, 2838, 1692, 3644, 6071, 477, 478, 2507, 1923, 3193, 2653, 2636, 1621, 3379, 2533, 2892, 2452, 1684, 2333, 22000, 1553, 3536, 11201, 2775, 2942, 2941, 2940, 2939, 2938, 2613, 426, 4116, 4412, 1966, 3065, 1225, 1705, 1618, 1660, 2545, 2676, 3687, 2756, 1599, 2832, 2831, 2830, 2829, 5461, 2974, 498, 1626, 3595, 160, 153, 3326, 1714, 3172, 3173, 3171, 3170, 3169, 2235, 6108, 169, 5399, 2471, 558, 2308, 1681, 2385, 3562, 5024, 5025, 5427, 3391, 3744, 1646, 3275, 3698, 2390, 1793, 1647, 1697, 1693, 1695, 1696, 2919, 9599, 2423, 3844, 2959, 2818, 1817, 521, 3147, 3163, 2886, 283, 2837, 2543, 2928, 2240, 1343, 2321, 3467, 9753, 1530, 2872, 1595, 2900, 1341, 2935, 3059, 2724, 3385, 2765, 368, 2461, 2462, 1253, 2680, 3009, 2434, 2694, 2351, 2353, 2354, 1788, 2352, 3662, 2355, 2091, 1732, 8183, 1678, 2588, 2924, 2687, 5071, 1777, 2899, 494, 3875, 2937, 5437, 5436, 3469, 3285, 1293, 5272, 2865, 321, 1280, 1779, 6432, 1230, 2843, 3033, 2566, 1562, 3085, 3892, 1246, 1564, 8160, 1633, 9997, 9996, 7511, 5236, 3955, 2956, 2954, 2953, 5310, 2951, 2936, 6951, 2413, 2407, 1597, 1570, 2398, 1809, 1575, 1754, 1748, 22001, 3855, 2368, 8764, 6653, 5314, 2267, 3244, 2661, 2364, 506, 2322, 2498, 3305, 183, 650, 2329, 5991, 1463, 159, 8450, 1917, 1921, 2839, 2503, 25903, 25901, 25902, 2556, 2672, 1690, 2360, 2671, 1669, 1665, 1286, 4138, 2592, 61441, 61439, 61440, 2983, 5465, 1843, 1842, 1841, 2061, 1329, 2451, 3701, 3066, 2442, 5771, 2450, 489, 8834, 1285, 3262, 2881, 2883, 43189, 6064, 1591, 1744, 405, 2397, 2683, 3062, 2162, 1288, 2286, 2236, 167, 1685, 1831, 2981, 467, 1574, 2743, 19398, 2469, 2460, 1477, 1478, 5720, 3535, 1582, 1731, 679, 2684, 2686, 2681, 2685, 1952, 9397, 9344, 2952, 2579, 2561, 1235, 367, 8665, 471, 2926, 1815, 7786, 8033, 1581, 7979, 1534, 490, 3070, 349, 1824, 2511, 1897, 6070, 2118, 2117, 1231, 24003, 24004, 24006, 24000, 3594, 24002, 24001, 24005, 5418, 2698, 8763, 1820, 1899, 2587, 8911, 8910, 1593, 2535, 4181, 2559, 3069, 2620, 1298, 2540, 2541, 2125, 1487, 2283, 2284, 2285, 2281, 2282, 2813, 5355, 2814, 2795, 1555, 1968, 2611, 245, 4042, 1682, 1485, 2560, 2841, 2370, 2842, 2840, 398, 2424, 1773, 1649, 287, 2656, 2213, 2822, 1289, 3471, 3470, 3042, 4114, 6962, 6961, 1567, 2808, 1706, 2406, 2508, 2506, 1623, 13160, 2166, 2866, 2982, 1275, 1573, 4348, 1828, 3084, 1609, 2853, 3589, 147, 3501, 1643, 1642, 1245, 43190, 2962, 2963, 576, 2549, 1579, 1585, 503, 1907, 3202, 3548, 3060, 2652, 2633, 16991, 495, 1602, 1490, 2793, 18881, 2854, 2319, 2233, 3345, 2454, 8130, 8131, 2127, 2970, 2932, 3164, 1710, 11319, 27345, 2801, 1284, 2995, 3797, 2966, 2590, 549, 1725, 2337, 3130, 5813, 25008, 25007, 25006, 25005, 25004, 25003, 25002, 25009, 6850, 1344, 1604, 8733, 2572, 1260, 1586, 1726, 6999, 6998, 2140, 2139, 2141, 1577, 4180, 4827, 1877, 2715, 19412, 19410, 19411, 5404, 5403, 2985, 1803, 2744, 6790, 2575, 12172, 1789, 35000, 1281, 14937, 14936, 263, 375, 5094, 1816, 2245, 1238, 2778, 9321, 2643, 2421, 488, 1850, 2458, 41, 2519, 6109, 1774, 2833, 3862, 3381, 1590, 2626, 1738, 2732, 19539, 2849, 2358, 1786, 1787, 1657, 2429, 1747, 1746, 5408, 5407, 2359, 24677, 1874, 2946, 2509, 1873, 2747, 2751, 2750, 2748, 2749, 9396, 3067, 1848, 9374, 2510, 2615, 1689, 4682, 3350, 24242, 3401, 3294, 3293, 5503, 5504, 5746, 5745, 2344, 7437, 3353, 2689, 3873, 1561, 1915, 2792, 10103, 26260, 26261, 589, 1948, 2666, 26489, 26487, 2769, 2674, 6066, 1876, 2835, 2834, 2782, 16309, 2969, 2867, 2797, 2950, 1822, 1342, 5135, 2650, 2109, 2051, 2912, 309, 1865, 3289, 1804, 3286, 1740, 2211, 2707, 1273, 2181, 2553, 2896, 2858, 3610, 2651, 1325, 2445, 1265, 3053, 1292, 1878, 4098, 1780, 1795, 4099, 1821, 2151, 1227, 436, 2287, 32636, 1489, 1263, 5419, 3041, 2496, 3287, 6073, 2234, 242, 1844, 2362, 11112, 1941, 3046, 1945, 6072, 2960, 5426, 2753, 3298, 1702, 1256, 1254, 1266, 2562, 1656, 1655, 579, 1255, 1415, 2365, 2345, 6104, 8132, 1908, 3282, 1857, 1679, 2870, 3458, 5420, 772, 3645, 551, 1686, 3773, 4379, 1851, 3022, 2807, 2890, 1837, 2955, 3145, 1471, 1468, 40841, 40842, 40843, 1724, 2422, 6253, 455, 2746, 3201, 5984, 2324, 3288, 5412, 2137, 1648, 1802, 4308, 2459, 48556, 2757, 1757, 1294, 7174, 1944, 371, 504, 1741, 2931, 3020, 17219, 3903, 1768, 1767, 1766, 1765, 2856, 1640, 1639, 1794, 3987, 2571, 2412, 3315, 2116, 3061, 2836, 3450, 3105, 1756, 9283, 2906, 588, 1202, 1375, 2803, 2536, 1252, 2619, 1323, 2990, 1304, 2961, 6402, 6403, 3561, 1770, 1769, 2877, 10288, 2911, 2032, 2663, 2662, 1962, 310, 357, 354, 482, 2414, 2852, 1951, 1704, 3327, 573, 567, 2708, 2131, 2772, 3643, 2812, 1749, 5042, 1913, 2624, 1826, 2136, 2616, 9164, 9163, 9162, 1781, 2929, 1320, 2848, 2268, 459, 1536, 2639, 6831, 10080, 1845, 1653, 1849, 463, 2740, 2473, 2783, 1481, 2785, 2331, 7107, 1219, 3279, 5411, 2796, 2149, 7781, 1205, 4108, 4885, 1546, 2894, 1601, 2878, 5605, 5604, 5602, 5603, 3284, 1742]

class ScannerTCP(ScannerBase):
    def __init__(self):
        super(ScannerTCP, self).__init__()

        #
        # Common to TCP and UDP, but set to different values
        #

        self.packet_overhead = 74 # 14 bytes for ethernet frame + 20 bytes IP header + 20 bytes TCP header + 20 bytes of TCP options (on linux)
        if sys.platform == "win32":
            self.packet_overhead = 54 # 14 bytes for ethernet frame + 20 bytes IP header + 20 bytes TCP header + 0 bytes of TCP options (on windows)
        self.payload_len_estimate = 0
        self.host_count_high_water = 100
        self.host_count_low_water = 90

        #
        # Specific to TCP
        #

        self.scan_start_time_internal = None
        self.show_closed_ports = False
        self.probe_state_ready_last_result = None
        self.probe_state_ready_last_check = None
        self.probe_states_container = ProbeStateContainer()
        self.poll_result_count = 0
        self.poll_deleted_packet_count = 0
        self.max_socks_multiplier = 1.5 # tuned for 65k ports against a localhost (which sends resets)
        self.set_inter_packet_interval()
        self.poll_type = "auto"
        self.max_sockets_on_windows = 511
        self.max_sockets_on_non_windows = 1021
        self.warned_about_unreachable_network = []
        self.warned_about_socket_events = []

        # Read max open files from /proc
        # $ cat /proc/self/limits
        # Limit                     Soft Limit           Hard Limit           Units
        # ...
        # Max processes             31406                31406                processes
        # Max open files            1024                 1048576              files
        # Max locked memory         1040084992           1040084992           bytes
        # ...
        limits_filename = "/proc/self/limits"
        self.soft_open_files_limit = None
        self.hard_open_files_limit = None
        # check if file exists
        try:
            with open(limits_filename, "r") as f:
                for line in f.readlines():
                    if line.startswith("Max open files"):
                        self.soft_open_files_limit = int(line.split()[3])
                        self.hard_open_files_limit = int(line.split()[4])
        except:
            pass

    #
# Methods that are implemented differently for TCP and UDP Scanners
#
    def dump(self):
        print("")
        print_header(self.header)
        if self.target_filename:
            print("Targets file: ................ %s" % self.target_filename)
        if self.target_list_unprocessed:
            print("Targets: ..................... %s" % ", ".join(self.target_list_unprocessed))
        if self.target_ports_unprocessed:
            print("Target ports: ................ %s" % self.target_ports_unprocessed)
        if self.soft_open_files_limit:
            print("Soft open files limit: ....... %s" % self.soft_open_files_limit)
        if self.hard_open_files_limit:
            print("Hard open files limit: ....... %s" % self.hard_open_files_limit)
        print("Target port count: ........... %s" % len(self.probes))
        print("Retries: ..................... %s" % (self.max_probes - 1))
        print("Show closed ports: ........... %s" % self.show_closed_ports)
        print("Bandwidth: ................... %s bits/second" % self.bandwidth_bits_per_second)
        if self.packet_rate:
            print("Packet rate: ................. %s packets/second" % self.packet_rate)
        print("RTT: ......................... %s seconds" % self.inter_packet_interval_per_host)
        print("Inter-packet interval: ....... %s seconds" % self.inter_packet_interval)
        print("Max sockets: ................. %s" % self.host_count_high_water)
        print("Packet overhead: ............. %s bytes" % self.packet_overhead)
        print("Poll type: ................... %s" % self.probe_states_container.poll_type)
        # Note that we can't print targets / target_count here because we'd drain the generator (which could contain millions of targets)
        print_footer()

    def set_rtt(self, rtt):
        rtt = float(rtt)
        if rtt < self.recv_interval * 4:
            self.recv_interval = rtt / float(4)
            print("[W] RTT is quite low (%s).  Setting recv_interval to %s to make sure we won't miss replies - but this causes extra CPU utilisation." % (rtt, self.recv_interval))
        self.inter_packet_interval_per_host = float(rtt)

    def set_probes(self, port_list_str): # string like "80,443,8080-9000"
        self.probes = []
        self.target_ports_unprocessed = port_list_str
        for ports_str in port_list_str.split(","):
            if "-" in ports_str:
                port_range = ports_str.split("-")
                for port in range(int(port_range[0]), int(port_range[1]) + 1):
                    self.probes.append(port)
            else:
                self.probes.append(int(ports_str))

        popularity_dict = {}
        index = 0
        for port in port_popularity_nmap:
            popularity_dict[port] = index
            index += 1
        def port_sort(port):
            if port in popularity_dict:
                return popularity_dict[port]
            return 100000

        self.probes = sorted(self.probes, key=port_sort)

    def start_scan(self):
        # check we have probes
        if not self.probes:
            print("[E] No probes set.  Call set_probes() method before starting scan.")
            sys.exit(0)

        self.set_poll_type(self.poll_type)

        def make_probe_state_callback(target, probes, probe_index):
            return ProbeStateTcp(target, probes[probe_index], probe_index)

        # Set up target generator
        target_generator = None
        if self.target_source == "file":
            target_generator = TargetGenerator(make_probe_state_callback, filename=self.target_filename)
        elif self.target_source == "list":
            target_generator = TargetGenerator(make_probe_state_callback, list=self.target_list_unprocessed)
        else:
            print("[E] Unknown target source: %s.  Call add_targets_from_file() or add_targets() method before starting scan." % self.target_source)
            sys.exit(0)
        probes_state_generator_function = target_generator.get_probe_state_generator(self.probes)

        # Initialize stats for how many of each probe type are in the queue
        for probe_index in range(len(self.probes)):
            self.count_in_queue[probe_index] = 0

        last_send_time = None

        self.dump()

        self.scan_start_time = time.time()
        self.scan_start_time_internal = time.time()
        scan_running = True
        more_hosts = True
        highest_probe_index_seen = -1
        self.sleep_reasons["packet_quota"] = 0
        self.sleep_reasons["bandwidth_quota"] = 0
        self.sleep_reasons["port_states"] = 0
        while scan_running:
            #
            # add probes to queue
            #
            # if queue has capacity, create more probestate objects for up to host_count_high_water hosts; add them to queue
            if more_hosts and self.probe_states_container.count < self.host_count_low_water:
                more_hosts = False  # if we complete the for loop, there are no more probes to add
                for ps in probes_state_generator_function: # has side effect of creating probe state and adding it to container

                    # Don't add to queue if target is in blocklist
                    if ps.target_ip in self.blocklist:
                        print("[i] Skipping target %s:%s because it is in the blocklist" % (ps.target_ip, ps.target_port))
                        self.probe_states_container.delete_probe_state(ps)
                        continue

                    # Count the number of hosts we are scanning
                    if ps.probe_index == 0:
                        self.host_count += 1

                    # Inform user when we start scanning a new probe type
                    if ps.probe_index > highest_probe_index_seen:
                        self.inform_starting_probe_type(ps.probe_index)
                        highest_probe_index_seen = ps.probe_index

                    # Increment count of probes of this type in queue
                    self.count_in_queue[ps.probe_index] += 1  # TODO is this important?  add to class if so

                    # If we've reached the high watermark, exit the for loop
                    # if len(self.probe_states_queue) >= self.host_count_high_water:
                    if self.probe_states_container.count >= self.host_count_high_water:
                        # If we exit the for loop early, there are more probes to add
                        more_hosts = True
                        break

                self.probe_states_container.sort(self.inter_packet_interval_per_host, time.time())

            # If we're not within quotas, wait until we are:
            # * bandwidth quota
            # * packet rate quota
            # * probe state < at least one probe state is ready to send
            self.wait_for_quotas()

            # if queue has items, pop one off
            packet_count_to_send = self.get_available_quota_packets()

            #
            # Send Loop
            #
            for packet_counter in range(min(packet_count_to_send, self.probe_states_container.count)):
                now = time.time()
                if self.probe_states_container.count > 0:
                    ps = self.probe_states_container.peekleft()

                    # check if we've exceeded the max probes for this host AND RTT windows has passed, delete the probe state
                    if not ps.deleted and ps.probes_sent >= self.max_probes and now > ps.probe_sent_time + self.inter_packet_interval_per_host:
                        ps.schedule_delete()

                    # remove elements from left side of queue that have been flagged for deletion
                    if ps.deleted:
                        self.decrease_count_in_queue(ps.probe_index)
                        self.probe_states_container.popleft()
                        continue

                    if ps.probes_sent >= self.max_probes:
                        # We are waiting on a retry.  Do nothing for this packet.
                        # Given the list is sorted, we can terminate the loop immediately
                        break

                    # Terminate for loop immediately if we find a packet that we can't send yet
                    elif ps.probe_sent_time is not None and (
                            ps.probe_sent_time + self.inter_packet_interval_per_host > now):
                        # self.probe_states_queue.appendleft(ps)  # add back to queue
                        break

                    # We need to send a packet.  Also add back to queue so we can check for replies later
                    # At this point either probe_sent_time is None, or we're beyond the inter-packet interval
                    else:
                        # self.probe_states_queue.append(ps)  # add back to queue

                        # Check if probe is due for this host: i.e. if we're past the inter-packet interval for this host; or we never sent a probe; or no inter-packet interval is configured
                        # if (ps.probe_sent_time is None) or (self.packet_rate_per_host and (time.time() > ps.probe_sent_time + self.inter_packet_interval_per_host)):

                        # We don't need to check if we can send.  wait_for_quotas guarentees that we can send the first packet in the queue.
                        # Send probe
                        sent = False
                        while not sent:
                            # For the last few probes, start noting the time we send the last probe.  For the stats.
                            # if more_hosts == 0 and ps.probes_sent == self.max_probes - 1: # This doesn't work if we get a reply before we send the last probe
                            # if not more_hosts: # TODO optimize this.  There's a problem if the number of probes exactly equals the high water mark.  last_send_time will be set to None
                            last_send_time = time.time()

                            sock = None
                            try:
                                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                            # catch: OSError: [Errno 24] Too many open files
                            except OSError as e:
                                if e.errno == 24:
                                    print(
                                        "[E] Failed to create socket.  Too many open files (sockets).  Check 'ulimit -n', try higher limit with 'ulimit -n NNNN' or limit max sockets (-m option).")
                                    sys.exit(1)

                            sock.setblocking(False)

                            # if python version 2
                            if sys.version_info[0] == 2:
                                try:
                                    sock.connect((ps.target_ip, ps.target_port))
                                except socket.error:
                                    sent = True

                            # python version 3
                            else:
                                try:
                                    sock.connect((ps.target_ip, ps.target_port))

                                # These are expected / desired:
                                # BlockingIOError: [Errno 115] Operation now in progress 
                                # BlockingIOError: [Errno 10035] A non-blocking socket operation could not be completed immediately.
                                except BlockingIOError as e:  # not available in python2
                                    if e.errno == 115 or e.errno == 10035:
                                        sent = True
                                    else:
                                        raise e

                                except OSError as e:
                                    # OSError: [Errno 101] Network is unreachable (normal error for sending to broadcast address)
                                    if e.errno == 101:
                                        if not ps.target_ip in self.warned_about_unreachable_network:
                                            self.warned_about_unreachable_network.append(ps.target_ip)
                                            print("[I] Failed to connect to %s:%s.  Network is unreachable (probably broadcast address).  Suppressing further warning about this host." % (ps.target_ip, ps.target_port))
                                        sent = True
                                    else:
                                        raise e
                                # get socket opts for socket.SO_SNDBUF
                                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, int(1000000))

                            ps.set_socket(sock)  # will also delete the old socket if there is one

                        # Update stats
                        self.probes_sent_count += 1
                        ps.probes_sent += 1
                        ps.probe_sent_time = time.time()
                        self.bytes_sent += 0 + self.packet_overhead

                        # move element from start of queue to end of queue
                        ps = self.probe_states_container.next()

                else:
                    if not more_hosts:
                        scan_running = False
                        break
            #
            # recv some packets
            #
            # for efficiency we only receive after every 10 packets sent, or if we're past the next recv time
            # whichever is sooner
            now = time.time()
            # if self.probes_sent_count % 10 == 0 or self.next_recv_time < now: # TODO
            if self.next_recv_time < now:
                self.next_recv_time = now + self.recv_interval
                scan_running = self.receive_packets(self.probe_states_container.socket_list) or more_hosts

        # recv any remaining packets
        self.receive_packets(self.probe_states_container.socket_list)

        self.scan_duration = last_send_time - self.scan_start_time

        # scan_duration can be 0 for quick scans on windows
        if self.scan_duration == 0:
            self.scan_duration = 0.001
        self.scan_rate_bits_per_second = int(8 * self.bytes_sent / self.scan_duration)

        if self.debug:
            self.debug_write_log()

    def receive_packets(self, socket_list):
        if socket_list:
            # check if there are any packets to receive

            p = self.probe_states_container.poller
            fd_event_tuples = p.poll(0)
            for fd, event in fd_event_tuples:
                self.poll_result_count += 1
                probe_state = self.probe_states_container.probe_states_by_fd[fd]

                if probe_state is None:
                    print("[E] monitored socket does not appear in queue")
                    sys.exit(1)

                if probe_state.deleted:
                    self.poll_deleted_packet_count += 1
                    continue

                if self.probe_states_container.poll_type == "epoll":
                    if event & select.EPOLLRDHUP:
                        if self.show_closed_ports:
                            print("Received RST for %s:%s" % (probe_state.target_ip, probe_state.target_port))
                    else:
                        print("Received SYN/ACK for %s:%s" % (probe_state.target_ip, probe_state.target_port))
                        self.replies += 1
                        if self.debug:
                            self.debug_log_reply("TCP Scan", probe_state.target_ip, probe_state.target_port, b"")

                elif self.probe_states_container.poll_type == "poll":
                    # Sockets for closed ports are readable and writable; raddr is None
                    if (event & select.POLLHUP) and (event & select.POLLERR): #select.EPOLLOUT | select.EPOLLRDHUP
                        if self.show_closed_ports:
                            print("Received RST for %s:%s" % (probe_state.target_ip, probe_state.target_port))

                    # Sockets for open ports are writable and not readable; raddr is not None
                    elif not (event & select.POLLHUP) and not (event & select.POLLERR):
                        print("Received SYN/ACK for %s:%s" % (probe_state.target_ip, probe_state.target_port))
                        self.replies += 1
                        if self.debug:
                            self.debug_log_reply("TCP Scan", probe_state.target_ip, probe_state.target_port, b"")

                    else:
                        if event not in self.warned_about_socket_events:
                            self.warned_about_socket_events.append(event)
                            print("[W] Socket found with unexpected event: %s (20 relates to sending to broadcast address).  Warnings about events of same type suppressed." % event)

                elif self.probe_states_container.poll_type == "select":
                    # Sockets for closed ports are readable and writable; raddr is None
                    if (event & SelectPoller.POLLIN) and (event & SelectPoller.POLLOUT): #select.EPOLLOUT | select.EPOLLRDHUP
                        if self.show_closed_ports:
                            print("Received RST for %s:%s" % (probe_state.target_ip, probe_state.target_port))

                    # Sockets for open ports are writable and not readable; raddr is not None
                    elif (event & SelectPoller.POLLOUT) and not (event & SelectPoller.POLLIN):
                        print("Received SYN/ACK for %s:%s" % (probe_state.target_ip, probe_state.target_port))
                        sys.stdout.flush()
                        self.replies += 1
                        if self.debug:
                            self.debug_log_reply("TCP Scan", probe_state.target_ip, probe_state.target_port, b"")

                    else:
                        print("[W] Socket found with unexpected event: %s" % event)

                else:
                    raise Exception("Unknown poll type: %s" % self.probe_states_container.poll_type)

                #self.remove_socket(probe_state.socket)
                self.decrease_count_in_queue(probe_state.target_port)

                # self.probe_states_queue.remove(probe_state) # TODO expensive
                #probe_state.deleted = True
                probe_state.schedule_delete() # user sees multiple results for the same response, but lower CPU utilization
                #self.probe_states_container.delete_probe_state(probe_state) # quicker scan, user sees each result once, higher CPU utilization

        if self.probe_states_container.count == 0:
            return False
        return True

    def inform_starting_probe_type(self, probe_index):
        pass

    def decrease_count_in_queue(self, probe_index):
        return # TODO
        # self.count_in_queue[probe_index] -= 1
        # if self.count_in_queue[probe_index] == 0:
        #     self.close_socket_for_probe_index(probe_index)

    def get_queue_length(self):
        return self.probe_states_container.count

    def queue_peek_first(self):
        return self.probe_states_container.peekleft()

    def get_socket_list(self):
        return self.probe_states_container.socket_list
#
# TCP Specific Methods
#

    def dump_probe_state(self, next_probe_state):
        probe_state_str = ""
        probe_state_str += "target_ip: %s;" % next_probe_state.target_ip
        probe_state_str += "target_port: %s;" % next_probe_state.target_port
        probe_state_str += "probe_index: %s;" % next_probe_state.probe_index
        probe_state_str += "probe_sent_time: %s;" % next_probe_state.probe_sent_time
        probe_state_str += "probes_sent: %s;" % next_probe_state.probes_sent
        delay_before_send = None
        if next_probe_state.probe_sent_time is None:
            delay_before_send = 0
        else:
            delay_before_send = next_probe_state.probe_sent_time + self.inter_packet_interval_per_host - time.time()
        probe_state_str += "delay_before_send: %s;" % delay_before_send

        return probe_state_str

    def set_poll_type(self, poll_type):
        self.poll_type = self.probe_states_container.set_poll_type(poll_type)
        if self.poll_type == "select":
            if sys.platform == "win32":
                if self.host_count_high_water > self.max_sockets_on_windows:
                    print("[W] 'select' poll type (needed on windows) doesn't work for more than %s sockets.  Reducing max_sockets (-m) from %s to %s." % (self.max_sockets_on_windows, self.host_count_high_water, self.max_sockets_on_windows))
                    self.set_max_sockets(self.max_sockets_on_windows)
            else:
                if self.host_count_high_water > self.max_sockets_on_non_windows:
                    print("[W] 'select' poll type (usually only needed on windows) doesn't work for more than %s sockets.  Reducing max_sockets (-m) from %s to %s." % (self.max_sockets_on_non_windows, self.host_count_high_water, self.max_sockets_on_non_windows))
                    self.set_max_sockets(self.max_sockets_on_non_windows)

    # set max sockets to number of packets we can send in inter_packet_interval_per_host
    # multiplied by max_socks_multiplier fudge factor found empirically
    # if max_socket is set to low, the scan will be inefficient (slow)
    # if max_socket is set to high, the scanner will send unwanted retries (caused by having sockets in the queue for more than the 1 second timeout before the OS retries)
    def get_queue_length_suggestion(self):
        if self.inter_packet_interval_per_host is None:
            raise Exception("[E] Code error: RTT must set FIRST to use auto for max_sockets")
        return int(self.max_socks_multiplier * self.inter_packet_interval_per_host / float(self.inter_packet_interval))

    def set_show_closed_ports(self, param):
        if sys.platform == "win32" and param:
            # https://stackoverflow.com/questions/63676682/windows-sockets-how-to-immediately-detect-tcp-rst-on-nonblocking-connect
            print("[W] Windows does not support detecting closed ports.  Ignoring --show-closed-ports.")
            self.show_closed_ports = False
        else:
            self.show_closed_ports = param

    def set_max_sockets(self, param):
        if param == "auto":
            self.host_count_high_water = self.get_queue_length_suggestion()
        else:
            self.host_count_high_water = int(param)

        # Check we didn't exceed ulimit -n.  This can cause the scan to fail if we exceed the limit.
        if self.soft_open_files_limit and self.hard_open_files_limit and self.host_count_high_water > self.soft_open_files_limit:
            soft_limit_headroom = 10
            print("[W] max_sockets (-m) set to %s, but must be <= ulimit -n (%s).  Reducing to %s.  NB: this is a soft limit, you can increase it with 'ulimit -n NNNN' (hard limit is %s)." % (self.host_count_high_water, self.soft_open_files_limit, self.soft_open_files_limit - soft_limit_headroom, self.hard_open_files_limit))
            self.host_count_high_water = self.soft_open_files_limit - soft_limit_headroom

        # Check that incorrect values won't cause the scan to be slow or send unwanted retries
        if self.host_count_high_water > self.get_queue_length_suggestion() * 1.1:
            print("[W] max_sockets (-m) is set to %s, which is greater than the suggested value of %s for fast scanning.  You may send unwanted retries." % (self.host_count_high_water, self.get_queue_length_suggestion()))
        if self.host_count_high_water < self.get_queue_length_suggestion() * 0.7:
            print("[W] max_sockets (-m) is set to %s, which is less than the suggested value of %s for fast scanning.  Scan may be slow." % (self.host_count_high_water, self.get_queue_length_suggestion()))
            if self.inter_packet_interval_per_host > 0.1:
                print("[I] If you have a low-latency connection, try -R 0.1 (100ms RTT)")
            if self.max_probes > 1:
                print("[I] If you don't get packet loss, try -r 0 (no retries)")

        self.host_count_low_water = int(0.9 * self.host_count_high_water)

#
# Helper functions
#

# recvfrom returns bytes in python3 and str in python3.  This function converts either to hex string
def str_or_bytes_to_hex(str_or_bytes):
    return "".join("{:02x}".format(c if type(c) is int else ord(c)) for c in str_or_bytes)

def get_time():
    offset = time.timezone
    if time.localtime().tm_isdst:
        offset = time.altzone
    offset = int(offset / 60 / 60 * -1)
    if offset > 0:
        offset = "+" + str(offset)
    else:
        offset = str(offset)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + " UTC" + offset

def round_pretty(x):
    # avoid math errors
    if x < 0.01:
        x = 0.01
    if x <= 100:
        # round to 3 significant figures
        return round(x, 2-int(math.floor(math.log10(abs(x)))))
    else:
        # Otherwise, just covert to int
        return int(x)

def print_header(message, width=80):
    message_len = len(message) + 2 # a space either side
    pad_left = int((width - message_len) / 2)
    pad_right = width - message_len - pad_left
    print("%s %s %s" % ("=" * pad_left, message, "=" * pad_right))

def print_footer(width=80):
    print("=" * width)

# Convert a string to a number, with support for K, M, G suffixes
def expand_number(number): # int or str
    number_as_string = str(number)
    if number_as_string.lower().endswith('k'):
        return int(number_as_string[:-1]) * 1000
    elif number_as_string.lower().endswith('m'):
        return int(number_as_string[:-1]) * 1000000
    elif number_as_string.lower().endswith('g'):
        return int(number_as_string[:-1]) * 1000000000
    else:
        if not number_as_string.isdigit():
            print("[E] %s should be an integer or an integer with k, m or g suffix" % number_as_string)
            sys.exit(0)
        else:
            return int(number_as_string)

# return list of ports from a string like "1,2,3-5,6"
def expand_port_list(ports):
    ports_list = []
    for port in ports.split(','):
        if '-' in port:
            port_range = port.split('-')
            if len(port_range) != 2:
                print("[E] Port range %s is not in the right format" % port)
                sys.exit(0)
            for p in range(int(port_range[0]), int(port_range[1]) + 1):
                if 0 < p < 65536:
                    ports_list.append(p)
                else:
                    print("[E] Port %s is not in in range 1-65535" % p)
                    sys.exit(0)
        else:
            port = int(port)
            if 0 < port < 65536:
                ports_list.append(port)
            else:
                print("[E] Port %s is not in in range 1-65535" % port)
                sys.exit(0)
    return ports_list

# hex to bytes
def hex_decode(hex_string):
    return bytes(bytearray.fromhex(hex_string))

def hex_encode(hex_bytes):
    return hex_bytes.hex()

if __name__ == "__main__":
    VERSION = "1.1"

    # Defaults
    DEFAULT_MAX_PROBES = 2
    DEFAULT_BANDWIDTH = "250k"
    DEFAULT_PACKET_RATE = 0
    DEFAULT_PACKET_HOST_RATE = 2
    DEFAULT_RTT = 0.5
    DEFAULT_RARITY = 6
    DEFAULT_PROBES = "1-65535"
    DEFAULT_SOCKETS = "auto"
    DEFAULT_SHOW_CLOSED_PORTS = False
    DEFAULT_POLL_TYPE = "auto"

    # These get overriden later
    max_probes = DEFAULT_MAX_PROBES
    bandwidth = DEFAULT_BANDWIDTH
    packet_rate = DEFAULT_PACKET_RATE
    packet_rate_per_host = DEFAULT_PACKET_HOST_RATE
    rtt = DEFAULT_RTT
    rarity = DEFAULT_RARITY
    max_sockets = DEFAULT_SOCKETS
    show_closed_ports = DEFAULT_SHOW_CLOSED_PORTS
    poll_type = DEFAULT_POLL_TYPE

    probe_dict = {}                  # populated later with all possible probes from config above
    probe_names_selected = []        # from command line
    blocklist_ips = []               # populated later with all ips in blocklist

    script_name = sys.argv[0]

    # parse command line options
    parser = argparse.ArgumentParser(usage='%s [options] -f ipsfile\n       %s [options] [ -p 80,90-100 ] 10.0.0.0/16 10.1.0.0-10.1.1.9 192.168.0.1' % (script_name, script_name))

    parser.add_argument('-f', '--file', dest='file', help='File of ips')
    parser.add_argument('-p', '--ports', dest='ports_str_list', default=DEFAULT_PROBES, type=str, help='Port list (e.g. 80,443,1000-2000) or "all".  Default: %s' % (DEFAULT_PROBES))
    parser.add_argument('-b', '--bandwidth', dest='bandwidth', default=DEFAULT_BANDWIDTH, type=str, help='Bandwidth to use in bits/sec.  Default %s' % (DEFAULT_BANDWIDTH))
    parser.add_argument('-P', '--packetrate', dest='packetrate', default=DEFAULT_PACKET_RATE, type=str, help='Max packets/sec to send.  Default unlimited')
    #parser.add_argument('-H', '--packethostrate', dest='packehosttrate', default=DEFAULT_PACKET_HOST_RATE, type=int, help='Max packets/sec to each host.  Default %s' % (DEFAULT_PACKET_HOST_RATE))
    parser.add_argument('-R', '--rtt', dest='rtt', default=DEFAULT_RTT, type=float, help='Max round trip time for probe.  Default %ss' % (DEFAULT_RTT))
    parser.add_argument('-m', '--max', dest='max_sockets', default=DEFAULT_SOCKETS, type=str, help='Max parallel probes.  Default %s' % (DEFAULT_SOCKETS))
    parser.add_argument('-r', '--retries', dest='retries', default=DEFAULT_MAX_PROBES, type=int, help='No of packets to sent to each host.  Default %s' % (DEFAULT_MAX_PROBES))
    parser.add_argument('-d', '--debug', dest='debug', action="store_true", help='Debug mode')
    parser.add_argument('-t', '--polltype', dest='poll_type', default=DEFAULT_POLL_TYPE, type=str, help='Poll type: poll, epoll, auto.  Default %s' % (DEFAULT_POLL_TYPE))
    parser.add_argument('-c', '--closed', dest='show_closed_ports', action="store_true", help='Show closed ports.  Default %s' % (DEFAULT_SHOW_CLOSED_PORTS))
    parser.add_argument('-B', '--blocklist', dest='blocklist', default=None, type=str, help='List of blacklisted ips.  Useful on windows to blocklist network addresses.  Separate with commas: 127.0.0.0,192.168.0.0.  Default None')
    args, targets = parser.parse_known_args()

    #
    # Change defaults based on command line options
    #

    # set max_probes from retries
    if args.retries is not None:
        max_probes = args.retries + 1

    # set bandwidth
    if args.bandwidth is not None:
        bandwidth = args.bandwidth

    # set packet rate
    if args.packetrate is not None:
        packet_rate = args.packetrate

    # set rtt
    if args.rtt is not None:
        rtt = args.rtt

    # set probe names
    if args.ports_str_list is not None:
        ports_str_list = args.ports_str_list

    # set blocklist
    if args.blocklist is not None:
        blocklist_ips = args.blocklist.split(',')

    # set max_sockets
    if args.max_sockets is not None:
        max_sockets = args.max_sockets

    # set show_closed_ports
    if args.show_closed_ports is not None:
        show_closed_ports = args.show_closed_ports

    # set poll_type
    if args.poll_type is not None:
        poll_type = args.poll_type

    #
    # Check for illegal command line options
    #

    # error if any targets start with - or -- as this will be interpreted as an option
    for target in targets:
        if target.startswith('-'):
            print("[E] Target \"%s\" starts with - or -- which is interpreted as an option" % target)
            sys.exit(0)

    # error if no targets were specified
    if not args.file and not targets:
        parser.print_help()
        sys.exit(0)

    # error if --file and targets were specified
    if args.file and targets:
        print("[E] You cannot specify both a file of targets and a list of targets")
        sys.exit(0)

    print("Starting tcpy_scanner v%s ( https://github.com/CiscoCXSecurity/tcpy_scanner ) at %s" % (VERSION, get_time()))

    # check max_sockets > 1
    if max_sockets != "auto" and int(max_sockets) < 1:
        print("[E] Max sockets must be > 0")
        sys.exit(0)

    # Send each type of probe separately
    scanner = ScannerTCP()

    # Set up options for scan
    if args.file:
        scanner.add_targets_from_file(args.file)
    else:
        scanner.add_targets(targets)

    scanner.set_bandwidth(bandwidth)
    scanner.set_max_probes(max_probes)
    scanner.set_rtt(rtt)
    scanner.set_packet_rate(packet_rate)
    scanner.set_probes(ports_str_list)
    scanner.set_debug(args.debug)
    scanner.set_blocklist(blocklist_ips)
    scanner.set_show_closed_ports(show_closed_ports)
    scanner.set_max_sockets(max_sockets)
    scanner.set_poll_type(poll_type)

    # Start scan
    scanner.start_scan()

    # Print stats
    print("")
    print("Scan for complete at %s" % get_time())
    print("Found: %s open ports and received %s RSTs" % (scanner.replies, scanner.poll_result_count - scanner.replies))
    print("Sent %s bytes (%s bits) in %s probes in %ss to %s hosts: %s bits/s, %s bytes/s, %s packets/s" % (scanner.bytes_sent, scanner.bytes_sent * 8, scanner.probes_sent_count, round_pretty(scanner.scan_duration), scanner.host_count, scanner.scan_rate_bits_per_second, round_pretty(scanner.bytes_sent / scanner.scan_duration), round_pretty(scanner.probes_sent_count / scanner.scan_duration)))

