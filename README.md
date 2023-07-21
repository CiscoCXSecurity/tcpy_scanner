# tcpy_scanner
Fast cross-platform TCP Connect Scanner written in Python

A tool for identifying open and closed TCP services on remote hosts. This tool may be of use to those performing security testing - e.g. during penetration testing, vulnerability assessments.

The main use-case for tcpy_scanner is scanning from an unprivileged pivot.  When you're pivoting, you don't always want to upload your normal port scanner - because you might get blocked/detected, or because it's difficult to install dependencies.  When you're unprivileged, connect scanning is your only option - you can't run a SYN scan as you'd normally do from your own system.  tcpy_scanner is designed to be copy-pasted to the pivot and run without dependencies.  It has mainly been tested on Linux and Windows using python3, but there are plans to make it compaitible with python2 so it works from more pivots.  It should work from Solaris and BSD, but this hasn't been tested.

tcpy_scanner has been written with safety in mind.  It shouldn't hog resources in a way that might disrupt the pivot.  There are plenty of options to tune resource utilisation to whatever you think is safe.

## Usage
```
usage: tcpy_scanner.py [options] -f ipsfile
       tcpy_scanner.py [options] [ -p 80,90-100 ] 10.0.0.0/16 10.1.0.0-10.1.1.9 192.168.0.1

options:
  -h, --help            show this help message and exit
  -f FILE, --file FILE  File of ips
  -p PORTS_STR_LIST, --ports PORTS_STR_LIST
                        Port list (e.g. 80,443,1000-2000) or "all". Default: 1-65535
  -b BANDWIDTH, --bandwidth BANDWIDTH
                        Bandwidth to use in bits/sec. Default 250k
  -P PACKETRATE, --packetrate PACKETRATE
                        Max packets/sec to send. Default unlimited
  -R RTT, --rtt RTT     Max round trip time for probe. Default 0.5s
  -m MAX_SOCKETS, --max MAX_SOCKETS
                        Max parallel probes. Default auto
  -r RETRIES, --retries RETRIES
                        No of packets to sent to each host. Default 2
  -d, --debug           Debug mode
  -t POLL_TYPE, --polltype POLL_TYPE
                        Poll type: poll, epoll, auto. Default auto
  -c, --closed          Show closed ports. Default False
  -B BLOCKLIST, --blocklist BLOCKLIST
                        List of blacklisted ips. Useful on windows to blocklist network addresses. Separate with commas: 127.0.0.0,192.168.0.0. Default None
```
## Examples
Scan all ports on a host:
```
tcpy_scanner.py 10.0.0.1 # defaults to -p 1-65535
tcpy_scanner.py -p 1-65535 10.0.0.1 # same thing
```

Scan selected ports on a network:
```
tcpy_scanner.py -p 22,445,3389 10.0.0.0/24
```

Scan faster by by reducing retries from 2 (default) to 0:
```
tcpy_scanner.py -r 0 -p 22,445,3389 10.0.0.0/24
```

Scan even faster by increasing bandwidth limit (default 250kbit/sec):
```
tcpy_scanner.py -b 1m -r 0 -p 22,445,3389 10.0.0.0/24
```
Allow use of more open sockets to make your scans go faster (Linux only):
```
$ cat /proc/self/limits 
Limit                     Soft Limit           Hard Limit           Units     
...
Max open files            1024                 1048576              files     
...
$ ulimit -n 1048576
```
Avoid errors relating to scanning the network address (10.0.0.0 in this example) on windows:
```
tcpy_scanner.py -p 22,445,3389 -B 10.0.0.0 10.0.0.0/24
```
## Python on Windows?

During pentests I've found python is rarely installed.  So you'd need to use something like (pyinstaller)[https://pyinstaller.org/en/stable/usage.html] to create an exe from this python script.

## Limitation of closed port detection from Windows

The -c option is ignored on Windows.  This is because it (isn't possible to detect closed ports)[https://stackoverflow.com/questions/63676682/windows-sockets-how-to-immediately-detect-tcp-rst-on-nonblocking-connect] from windows using standard TCP libraries.  If you had administrator rights and could install (npcap)[https://npcap.com/], you could.  But our use-case is that we're pivotting with non-admin privileges.

## Limitations of scanning large locally attached networks as a non-root user

There are some inherent limitations (unrelated to tcpy_scanner) to scanning large locally attached networks as a non-root user.  One of these is that Linux effectively rate-limits ARP resolutions.  Here "large" means >1024 IPs.

If your targets are on a locally attached ethernet network (i.e. packets don't go through a router), the kernel needs to find the MAC address of each target using [ARP](https://en.wikipedia.org/wiki/Address_Resolution_Protocol).  The linux kernel can perform up to a 1024 ARP resolutions in parallel (distros may differ).  The setting governing this limit is [gc_thresh3](https://www.kernel.org/doc/Documentation/networking/ip-sysctl.txt):

```
neigh/default/gc_thresh3 - INTEGER
	Maximum number of non-PERMANENT neighbor entries allowed.  Increase
	this when using large numbers of interfaces and when communicating
	with large numbers of directly-connected peers.
	Default: 1024
 ```
A failed ARP resolution will typically take 3 seconds (during my testing): 3 ARP who-has requests, 1 second apart.

This means that at best, you can hope to scan at 341 (1024/3) hosts per second.  So scanning at 341 packets per second should be reliable - even if all these packets go do different hosts.

But 341 packets/second may be too slow for some use-cases.

A good workaround is to do an ARP scan first to identify target hosts; then only scan those hosts.  e.g. if you have a /20 network (4096 IPs) and your ARP scan shows you only have 10 hosts on the network, only port scan those 10 hosts.

If we were root, we could use (arp-scan)[https://www.kali.org/tools/arp-scan/).  This would be a great solution as it crafts packets and therefore bypasses the rate limit outlined above...  But as we've resorted to port-scanning with python, we're probably not root, so we need another solution. 

Here's how we can do a crude ARP scan using tcpy_scanner.  We'll scan 1 TCP port on the whole local subnet (just to trigger the ARP lookup), then immediately inspect the ARP cache to note live hosts:
```
$ ifconfig -a # and note details of your locally attached network - 10.0.0.0/20 in this case (4096 IPs)
$ tcpy_scanner.py -r0 -p 80 -P 300 10.0.0.0/22; arp -an > arp-cache-snapshot.txt 
```
When the scan finishes, you'll save a snapshot of your ARP cache in arp-cache-snapshot.txt.  Extract your target hosts from this and port-scan as normal.


