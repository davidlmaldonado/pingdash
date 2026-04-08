#!/usr/bin/env python3
"""
pingdash.py — PingInfoView-style CLI ping dashboard for macOS
Uses fping for true ICMP ping. Paste IPs, ranges, hostnames — get a live color-coded table.
Staggered batching prevents false positives on large host lists.

Requirements:
  brew install fping    (macOS)

Usage:
  python3 pingdash.py                     # interactive — paste IPs when prompted
  python3 pingdash.py -f hosts.txt        # read from file
  python3 pingdash.py 10.0.0.1 10.0.0.2  # pass IPs as args
  python3 pingdash.py 10.0.0.1-5          # range shorthand
  python3 pingdash.py -i 3                # 3-second interval (default: 5)
  python3 pingdash.py -c 10               # stop after 10 cycles (default: unlimited)
  python3 pingdash.py -t 1000             # 1000ms timeout (default: 2000)
  python3 pingdash.py -r 2                # 2 retries per host (default: 1)
  python3 pingdash.py -b 12               # 12 hosts per batch (default: 8)
  python3 pingdash.py --csv results.csv   # export CSV on exit
"""

import subprocess
import sys
import os
import re
import time
import signal
import argparse
import shutil
from datetime import datetime
from collections import OrderedDict

# ── ANSI colors ──────────────────────────────────────────────────────────────
RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
RED     = "\033[91m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
CYAN    = "\033[96m"
WHITE   = "\033[97m"
GRAY    = "\033[90m"
BG_RED  = "\033[41m"
BG_GRN  = "\033[42m"

# ── IP parsing ───────────────────────────────────────────────────────────────
def parse_ips(raw_input):
    """Parse IPs, hostnames, and ranges from raw text input."""
    results = []
    # Split on newlines, commas, spaces, tabs
    tokens = re.split(r'[\n,\s\t]+', raw_input.strip())
    tokens = [t.strip() for t in tokens if t.strip()]

    for token in tokens:
        # Range: 10.0.0.1-10.0.0.5 or 10.0.0.1-5
        m = re.match(r'^(\d{1,3}\.\d{1,3}\.\d{1,3}\.)(\d{1,3})-(?:\d{1,3}\.\d{1,3}\.\d{1,3}\.)?(\d{1,3})$', token)
        if m:
            prefix, start, end = m.group(1), int(m.group(2)), int(m.group(3))
            for i in range(start, min(end + 1, 256)):
                results.append(f"{prefix}{i}")
            continue

        # Single IP or hostname
        if re.match(r'^[\w.\-:]+$', token):
            results.append(token)

    # Dedupe, preserve order
    seen = set()
    deduped = []
    for ip in results:
        if ip not in seen:
            seen.add(ip)
            deduped.append(ip)
    return deduped


# ── Host state ───────────────────────────────────────────────────────────────
class HostState:
    __slots__ = ('ip', 'sent', 'received', 'failed', 'alive',
                 'last_latency', 'last_alive', 'last_check',
                 'min_latency', 'max_latency', 'latencies')

    def __init__(self, ip):
        self.ip = ip
        self.sent = 0
        self.received = 0
        self.failed = 0
        self.alive = None
        self.last_latency = None
        self.last_alive = None
        self.last_check = None
        self.min_latency = None
        self.max_latency = None
        self.latencies = []

    def record(self, alive, latency=None):
        self.sent += 1
        self.last_check = datetime.now()
        if alive:
            self.received += 1
            self.alive = True
            self.last_latency = latency
            self.last_alive = datetime.now()
            if latency is not None:
                self.latencies.append(latency)
                if len(self.latencies) > 200:
                    self.latencies = self.latencies[-200:]
                self.min_latency = min(self.latencies)
                self.max_latency = max(self.latencies)
        else:
            self.failed += 1
            self.alive = False
            self.last_latency = None

    @property
    def avg_latency(self):
        if not self.latencies:
            return None
        return sum(self.latencies) / len(self.latencies)

    @property
    def fail_pct(self):
        if self.sent == 0:
            return 0.0
        return (self.failed / self.sent) * 100


# ── fping runner ─────────────────────────────────────────────────────────────
BATCH_SIZE = 8  # hosts per fping call — avoids timeout contention on large lists

def parse_fping_output(output, hosts):
    """Parse fping stderr output into {ip: (alive, latency_ms)} dict."""
    results = {}
    for line in output.strip().split('\n'):
        if not line.strip():
            continue
        # Parse: "hostname : 1.23" or "hostname : -"
        m = re.match(r'^([\w.\-:]+)\s*:\s*(.+)$', line.strip())
        if m:
            host = m.group(1).strip()
            val = m.group(2).strip()
            if val == '-':
                results[host] = (False, None)
            else:
                try:
                    latency = float(val)
                    results[host] = (True, latency)
                except ValueError:
                    results[host] = (False, None)

    # Any hosts not in output = unreachable
    for h in hosts:
        if h not in results:
            results[h] = (False, None)

    return results


def run_fping_batch(batch, timeout_ms=2000, retries=1):
    """Run fping against a single batch of hosts."""
    try:
        result = subprocess.run(
            ['fping', '-C', '1', '-q', '-r', str(retries), '-t', str(timeout_ms)] + batch,
            capture_output=True,
            text=True,
            timeout=(timeout_ms / 1000) * (retries + 1) + 5
        )
    except FileNotFoundError:
        print(f"\n{RED}{BOLD}Error: fping not found.{RESET}")
        print(f"{GRAY}Install it:{RESET}")
        print(f"  macOS:  {CYAN}brew install fping{RESET}")
        print(f"  Linux:  {CYAN}sudo apt install fping{RESET}")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        return {h: (False, None) for h in batch}

    output = result.stderr if result.stderr else result.stdout
    return parse_fping_output(output, batch)


def run_fping(hosts, timeout_ms=2000, retries=1):
    """Run fping in staggered batches to avoid timeout contention on large host lists."""
    if len(hosts) <= BATCH_SIZE:
        return run_fping_batch(hosts, timeout_ms, retries)

    all_results = {}
    for i in range(0, len(hosts), BATCH_SIZE):
        batch = hosts[i:i + BATCH_SIZE]
        batch_results = run_fping_batch(batch, timeout_ms, retries)
        all_results.update(batch_results)

    return all_results


# ── Display ──────────────────────────────────────────────────────────────────
def fail_color(pct):
    if pct == 0:
        return GREEN
    elif pct < 10:
        return GREEN
    elif pct < 30:
        return YELLOW
    elif pct < 60:
        return YELLOW
    else:
        return RED

def status_icon(alive):
    if alive is None:
        return f"{GRAY}○{RESET}"
    elif alive:
        return f"{GREEN}●{RESET}"
    else:
        return f"{RED}●{RESET}"

def fmt_latency(val):
    if val is None:
        return f"{GRAY}{'—':>7}{RESET}"
    return f"{WHITE}{val:>7.1f}{RESET}"

def fmt_time(dt):
    if dt is None:
        return f"{GRAY}{'—':>11}{RESET}"
    return f"{GRAY}{dt.strftime('%H:%M:%S'):>11}{RESET}"

def fail_bar(pct, width=12):
    filled = int(pct / 100 * width)
    filled = max(1, filled) if pct > 0 else 0
    color = fail_color(pct)
    bar = f"{color}{'█' * filled}{GRAY}{'░' * (width - filled)}{RESET}"
    return bar

def render_table(hosts_od, cycle, term_width):
    """Render the table to stdout."""
    # Move cursor to top of table area
    lines = []

    # Header
    up = sum(1 for h in hosts_od.values() if h.alive is True)
    down = sum(1 for h in hosts_od.values() if h.alive is False)
    pending = sum(1 for h in hosts_od.values() if h.alive is None)

    header = (
        f" {BOLD}{WHITE}PING DASHBOARD{RESET}  "
        f"{GREEN}▲ {up} up{RESET}  "
        f"{RED if down > 0 else GRAY}▼ {down} down{RESET}  "
        f"{GRAY}Cycle: {cycle}{RESET}"
    )
    lines.append(header)
    lines.append(f"{GRAY}{'─' * min(term_width, 120)}{RESET}")

    # Determine host column width
    max_host = max((len(h.ip) for h in hosts_od.values()), default=15)
    host_w = max(max_host, 15)

    # Column header
    col_hdr = (
        f"  {GRAY}{'':>2} {'Host':<{host_w}}  {'Sent':>5} {'Recv':>5} {'Fail':>5} "
        f"{'Fail%':>6}  {'':>12}  {'Last':>7} {'Avg':>7} {'Min':>7} {'Max':>7}  "
        f"{'Last Seen':>11}{RESET}"
    )
    lines.append(col_hdr)
    lines.append(f"{GRAY}{'─' * min(term_width, 120)}{RESET}")

    # Rows
    for h in hosts_od.values():
        fc = fail_color(h.fail_pct)
        row = (
            f"  {status_icon(h.alive)} {WHITE}{h.ip:<{host_w}}{RESET}  "
            f"{WHITE}{h.sent:>5}{RESET} "
            f"{GREEN}{h.received:>5}{RESET} "
            f"{RED if h.failed > 0 else GRAY}{h.failed:>5}{RESET} "
            f"{fc}{h.fail_pct:>5.1f}%{RESET}  "
            f"{fail_bar(h.fail_pct)}  "
            f"{fmt_latency(h.last_latency)} "
            f"{fmt_latency(h.avg_latency)} "
            f"{fmt_latency(h.min_latency)} "
            f"{fmt_latency(h.max_latency)}  "
            f"{fmt_time(h.last_alive)}"
        )
        lines.append(row)

    lines.append(f"{GRAY}{'─' * min(term_width, 120)}{RESET}")
    lines.append(f"  {GRAY}Ctrl+C to stop{RESET}")

    return '\n'.join(lines)


def clear_and_draw(hosts_od, cycle, term_width, total_lines_ref):
    """Clear screen and redraw. Uses cursor home + erase to avoid ghost headers."""
    sys.stdout.write("\033[H\033[J")  # cursor home, erase entire screen
    output = render_table(hosts_od, cycle, term_width)
    print(output, flush=True)
    total_lines_ref[0] = output.count('\n') + 1


# ── CSV export ───────────────────────────────────────────────────────────────
def export_csv(hosts_od, filepath):
    with open(filepath, 'w') as f:
        f.write("Host,Sent,Received,Failed,Fail%,Last(ms),Avg(ms),Min(ms),Max(ms),Last Seen\n")
        for h in hosts_od.values():
            avg = f"{h.avg_latency:.1f}" if h.avg_latency else ""
            last = f"{h.last_latency:.1f}" if h.last_latency else ""
            mn = f"{h.min_latency:.1f}" if h.min_latency else ""
            mx = f"{h.max_latency:.1f}" if h.max_latency else ""
            seen = h.last_alive.strftime('%Y-%m-%d %H:%M:%S') if h.last_alive else "Never"
            f.write(f"{h.ip},{h.sent},{h.received},{h.failed},{h.fail_pct:.1f}%,{last},{avg},{mn},{mx},{seen}\n")
    print(f"\n{GREEN}Exported to {filepath}{RESET}")


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="PingInfoView-style CLI ping dashboard using fping (true ICMP)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python3 pingdash.py                          # interactive mode
  python3 pingdash.py 10.0.0.1 10.0.0.2-5     # args with range
  python3 pingdash.py -f hosts.txt -i 3        # from file, 3s interval
  echo "10.0.0.1\\n10.0.0.2" | python3 pingdash.py  # pipe input"""
    )
    parser.add_argument('hosts', nargs='*', help='IPs, hostnames, or ranges')
    parser.add_argument('-f', '--file', help='Read hosts from file')
    parser.add_argument('-i', '--interval', type=int, default=5, help='Seconds between cycles (default: 5)')
    parser.add_argument('-c', '--count', type=int, default=0, help='Stop after N cycles (default: unlimited)')
    parser.add_argument('-t', '--timeout', type=int, default=2000, help='Ping timeout in ms (default: 2000)')
    parser.add_argument('-r', '--retries', type=int, default=1, help='fping retries per host before marking failed (default: 1)')
    parser.add_argument('-b', '--batch', type=int, default=8, help='Hosts per fping batch to avoid contention (default: 8)')
    parser.add_argument('--csv', help='Export CSV to this path on exit')
    args = parser.parse_args()

    raw = ""

    # Collect input from args, file, stdin, or interactive prompt
    if args.hosts:
        raw = ' '.join(args.hosts)
    if args.file:
        with open(args.file, 'r') as f:
            raw += '\n' + f.read()
    if not raw.strip() and not sys.stdin.isatty():
        raw = sys.stdin.read()
    if not raw.strip():
        print(f"{BOLD}{WHITE}Paste IPs/hostnames/ranges (blank line to start):{RESET}")
        print(f"{GRAY}  Supports: 10.0.0.1  10.0.0.1-5  hostname.domain.com{RESET}")
        print(f"{GRAY}  One per line, or comma/space separated{RESET}")
        print()
        lines = []
        while True:
            try:
                line = input()
                if not line.strip() and lines:
                    break
                lines.append(line)
            except EOFError:
                break
        raw = '\n'.join(lines)

    ip_list = parse_ips(raw)
    if not ip_list:
        print(f"{RED}No valid hosts found in input.{RESET}")
        sys.exit(1)

    # Apply batch size
    global BATCH_SIZE
    BATCH_SIZE = args.batch

    batches = (len(ip_list) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"\n{GREEN}Monitoring {len(ip_list)} host(s) every {args.interval}s (timeout: {args.timeout}ms, retries: {args.retries}, batch: {BATCH_SIZE}){RESET}")
    if batches > 1:
        print(f"{GRAY}Staggering into {batches} batches of ~{BATCH_SIZE} to avoid contention{RESET}")
    print(f"{GRAY}Press Ctrl+C to stop{RESET}\n")
    time.sleep(0.5)

    # Initialize state
    hosts_od = OrderedDict()
    for ip in ip_list:
        hosts_od[ip] = HostState(ip)

    term_width = shutil.get_terminal_size((120, 40)).columns
    total_lines_ref = [0]
    cycle = 0
    running = True

    def handle_sigint(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        while running:
            cycle += 1
            results = run_fping(ip_list, timeout_ms=args.timeout, retries=args.retries)

            for ip in ip_list:
                alive, latency = results.get(ip, (False, None))
                hosts_od[ip].record(alive, latency)

            term_width = shutil.get_terminal_size((120, 40)).columns
            clear_and_draw(hosts_od, cycle, term_width, total_lines_ref)

            if args.count > 0 and cycle >= args.count:
                break

            # Sleep in small increments so Ctrl+C is responsive
            for _ in range(args.interval * 10):
                if not running:
                    break
                time.sleep(0.1)

    except KeyboardInterrupt:
        pass

    # Final summary
    print(f"\n\n{BOLD}Final Results after {cycle} cycle(s):{RESET}")
    for h in hosts_od.values():
        icon = f"{GREEN}●{RESET}" if h.alive else f"{RED}●{RESET}" if h.alive is not None else f"{GRAY}○{RESET}"
        avg = f"{h.avg_latency:.1f}ms" if h.avg_latency else "N/A"
        print(f"  {icon} {h.ip:<40} {h.fail_pct:>5.1f}% loss  avg {avg}")

    if args.csv:
        export_csv(hosts_od, args.csv)


if __name__ == '__main__':
    main()