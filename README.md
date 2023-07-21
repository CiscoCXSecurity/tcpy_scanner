# tcpy_scanner
Fast cross-platform TCP Connect Scanner written in Python

A tool for identifying open and closed TCP services on remote hosts. This tool may be of use to those performing security testing - e.g. during penetration testing, vulnerability assessments.

The main use-case for tcpy_scanner is scanning from an unprivileged pivot.  When you're pivoting, you don't always want to upload your normal port scanner - because you might get blocked/detected, or because it's difficult to install dependencies.  When you're unprivileged, connect scanning is your only option - you can't run a SYN scan as you'd normally do from your own system.  tcpy_scanner is designed to be copy-pasted to the pivot and run without dependencies.  It has mainly been tested on Linux and Windows using python3, but there are plans to make it compaitible with python2 so it works from more pivots.  It should work from Solaris and BSD, but this hasn't been tested.

tcpy_scanner has been written with safety in mind.  It shouldn't hog resources in a way that might disrupt the pivot.  There are plenty of options to tune resource utilisation to whatever you think is safe.
## Quick Start
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
## Features and Design Goals

### A tools for pivots

I'm only considering the pivotting use-case.  If you're scanning from your own system there are much better tools you can and should use.

### Quality

On the one hand it's really easy to write a TCP connect port scanner.  Just a few lines of code and a for loop is all you need.  On the other hand it's really hard when you consider the various OS limitations and the usability features and safety features pentesters sometimes like to have.  From bad port-scanning experiences in past, I've come to value features such as:
* Knowing the port scanner is actually scanning all the hosts and ports I tell it to.
* Knowing the port scanning is going to finish the scan before the pentest ends.
* Being able to rate-limit the scan - and for the rate limit to work as described.
* The scanner shouldn't consume a lot of memory just because it's a big scan; nor all my CPU.
* It can send exactly the number of retries I want.  No more.  No less.
* I can use the scanner from a pivot without super user privs.

I want to write a scanner that hides some of the (hideous) complexities of OS quirks and limitations and uses good defaults to get the job done.  As I say, this can be a bit complex.  Feel free to (contribute)[CONTRIBUTING.md] if you find bugs.

### Speed

While scanning speed is not the primary goal of tcpy_scanner, it IS designed to not make you wait too long for scan results.  tcpy_scanner is designed to be fast as it could easily be made to be given that it's coded in python.

See quick start guide above for how to speed up scans.

Limits on the bandwidth used and packets per second are described below.  However, be cautious of setting these too high.

### Big Scans

tcpy_scanner is designed to be able to scan large numbers of hosts - hundreds of thousands or even millions of hosts.

It will scan a Class B network with one probe with no retries in about 95 seconds (for a small probe size):
```
tcpy_scanner.py -p 80 -r 0 127.0.0.1/16
```
### Safety

When pentesting badly configured networks or fragile hosts, scanning can sometimes cause outages.  This tends to be rare, but tcpy_scanner aims to give testers ways to manage the risk of outages:
* Specify maximum bandwidth in bits per section with -b or --bandwidth.  Example: `-b 1m` or `-b 32k`
* Sensible default of 250Kbit/sec for maximum bandwidth
* The script is aware that TCP SYN packets are bigger on Linux than Windows and this is taken into account when calculating bandwidth.
* Option to specify the maximum packets per second the scanner will send.  Example `-P 3000` will send no more than 3000 packets per second.

If you choose to upload tcpy_scanner to a compromised host, so you can scan from there, the following may help to manage the risk of adversely affecting that host:
* The script doesn't use forking or threading, which helps to manage the risk of accidentally swamping the target with processes or threads.
* There is a maximum amount of memory that the script will use.  Even when host lists are huge, the script will not read / generate the entire list in memory.  This helps to manage the risk that the script will consume all available memory.  tcpy_scanner will break scans up into chunks.
* The code attempts to be efficient to keep CPU utilisation low.  If CPU utilisation is too high for you (and it generally shouldn't be high with the default settings), try scanning at a slower speed (-b).
* No output is written to disk, so the script should not use up disk space unexpectedly.

tcpy_scanner will create a large number of sockets.  The number of open sockets is a resource goverened by the 'ulimit -n' / "Maximum open files" limit.  I haven't seen this cause a problem for system stability, but be cautious until you're sure.  You can limit sockets used with the -m option.

As always, if you're scanning through a stateful device (NAT device or stateful inspection Firewall), be wary of filling the device's state table.  This can have network destabalising consequences.  It's hard to know in advadvance how many connections a device can track, so it's hard to give advice on a sensible scanning rate.  I find `-P 3000` is a good compromise between network stability and getting my pentest finished.  Use `-P 2000` if you're feeling cautious.  But this is your risk to manage with your customer.  If in doubt, discuss the risk an plan accordingly.

### Verbose Output

To aid pentesters with record keeping and answering detailed questions about their scans, tcpy_scanner outputs verbose information about scan time, scan rates and configuration.  It doesn't output to a file, though, so it's recommended you use `script' or output redirection if you need to keep a record of your scan.

### Portable

The script was designed to work with python2 and python3 (but so far has only been tested with 2.7.18 and 3.10.8 on Linux and 3.10.10 on Windows 10) because you can't be sure how old a version of python you're going to find on a compromised host.

The script has no dependencies and should work with a base python install.

You should be able to copy-paste the .py file and run it. 

Note: No consideration is given to opsec.

Note: Cursory testing has been carried out on Windows.  It seems to work, but scanning is slower because of OS limitations.  On Windows, the code cannot currently handle sending to the network address (e.g. 127.0.0.0), so use the -B option to blocklist any network addresses in your target list.  You'll get a useful error if you don't and the scan will abort.

### Reliability

Retries are supported and enabled by default in case any probes are dropped on their way to the target.  Example: `-r 2` will send a probe and then 2 retries (3 packets to each host in total).

Scanning time is predictable.  The time taken for scans should depend only on the parameters used and the length of the host list.  If networks are congested or hosts are slow to respond or there's some sort of rate-limiting with replies, this will not affect scan time - although it could mean that you should scan at a lower rate.  This is a feature, not a bug so that pentesters are not left wondering if their scan their scan will ever finish.

Suitable for testing over slow links.  tcpy_scanner will wait 1 second by default for replies.  You can wait longer using RTT option: `-R 2.5`

## Risks: Beta quality code

Aside from the usual risks of scanning, the code was written around July 2023, so it will take a while to test thoroughly.  There might still be bugs that cause the scanner to behave badly.  

## Credits

Some of the code base is shared with (tcpy_scanner)[https://github.com/CiscoCXSecurity/udp-proto-scanner].
Inspiration for the scanning code was drawn from ike-scan.
The code base conclude a list of popular ports derived from a GPLv2 compatible version of (nmap)[https://nmap.org/].

## Limitations

### Python on Windows?

During pentests I've found python is rarely installed.  So you'd need to use something like (pyinstaller)[https://pyinstaller.org/en/stable/usage.html] to create an exe from this python script.

### Limitation of closed port detection from Windows

The -c option is ignored on Windows.  This is because it (isn't possible to detect closed ports)[https://stackoverflow.com/questions/63676682/windows-sockets-how-to-immediately-detect-tcp-rst-on-nonblocking-connect] from windows using standard TCP libraries.  If you had administrator rights and could install (npcap)[https://npcap.com/], you could.  But our use-case is that we're pivotting with non-admin privileges.

### Limitation of scanning speed on Windows

It's only possible to create 511 sockets from a process on Windows, so scans that need more socket will be slower than on other platforms.

### Limitations of scanning large locally attached networks as a non-root user

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


