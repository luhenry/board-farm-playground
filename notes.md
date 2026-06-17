# SpacemiT K3 Pico-ITX -- Bringup Notes (Raspberry Pi Edition)

This document describes how to bring up the SpacemiT K3 Pico-ITX board running Ubuntu 26.04,
using a Raspberry Pi as the network bridge and DHCP/TFTP server. It is written for consumption
by another agent and contains every detail needed to reproduce the setup from scratch.

---

## 1. Hardware topology

```
[Windows Laptop] / "Internet"
   | (USB-Ethernet dongle, 192.168.254.1/28, static)
   | <tftp64 for DHCP server>
   |
   | (eth0, 192.168.254.2/28, DHCP from upstream)
[Raspberry Pi]
   | (USB-Ethernet dongle enxc84d4433b782, 192.168.254.17/28, static)
   | (USB-serial CH340, /dev/ttyUSB0 on Pi, 115200 8N1)
   | (USB-power via Gembird SiS-PM smart plug, outlet 1)
   | <netboot.py for DHCP and TFTP server>
   |
   | (end0, 192.168.254.18, DHCP from upstream)
[SpacemiT K3 Pico-ITX]
```

Additional USB devices on the Pi:
- 0bda:8153 Realtek RTL8153 -- the USB-Ethernet dongle to the K3
- 1a86:7523 QinHeng CH340 -- serial console to the K3
- 04b4:fd15 Cypress / Energenie EG-PMS2 -- the SiS-PM power relay

The Pi's own uplink (eth0) and the dongle (enxc84d4433b782) are in DIFFERENT /28 blocks:
- eth0:             192.168.254.2/28  (covers .1 - .14, upstream network)
- enxc84d4433b782:  192.168.254.17/28 (covers .16 - .31, K3 subnet)

This separation is critical. See section 7 for what goes wrong when they share a /28.

---

## 2. Raspberry Pi OS assumptions

- Ubuntu 24.04 or 25.x, hostname ludovic-pi
- User: ubuntu, passwordless sudo
- Python 3.11+ (tested with 3.14), tomllib in stdlib
- pyserial installed: sudo apt-get install python3-serial
- expect installed: sudo apt-get install expect
- SSH key auth from the Windows laptop works (no password)

---

## 3. Configuring the USB-Ethernet dongle (enxc84d4433b782)

The dongle is a Realtek RTL8153 (0bda:8153). On the Pi it appears as enxc84d4433b782.
The interface name is derived from the MAC address by systemd/udev and is stable.

Create /etc/netplan/60-k3-dongle.yaml:

```yaml
network:
  version: 2
  ethernets:
    enxc84d4433b782:
      dhcp4: false
      addresses:
        - 192.168.254.17/28
      routes:
        - to: 0.0.0.0/0
          via: 192.168.254.1
          metric: 200
      nameservers:
        addresses: [1.1.1.1]
```

Set permissions and apply:

```bash
sudo chmod 600 /etc/netplan/60-k3-dongle.yaml
sudo netplan apply
```

The interface will show DOWN until the K3 is powered on and the cable is plugged in.
Once the K3 boots and link is established the interface comes up automatically.

Do NOT assign the dongle an IP in the same /28 as eth0. The Pi's eth0 is 192.168.254.2/28
which covers .0-.15. The dongle uses .16-.31. They are different subnets.

---

## 4. NAT and IP forwarding on the Pi

Run once, then persistent across reboots:

```bash
# Enable IP forwarding
echo "net.ipv4.ip_forward=1" | sudo tee /etc/sysctl.d/99-ipforward.conf
sudo sysctl -w net.ipv4.ip_forward=1

# MASQUERADE outbound traffic from K3 subnet through eth0
sudo iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
sudo iptables -A FORWARD -i enxc84d4433b782 -o eth0 -j ACCEPT
sudo iptables -A FORWARD -m state --state RELATED,ESTABLISHED -j ACCEPT

# Persist iptables rules across reboots
sudo apt-get install -y iptables-persistent
sudo netfilter-persistent save
```

With this setup the K3 (192.168.254.18) can reach the internet through the Pi.

---

## 5. netboot.py -- DHCP + TFTP server

netboot.py is a pure-Python DHCP + TFTP server. It lives in ~/git/k3-host/netboot.py on
the Pi. It requires root to bind ports 67 and 69.

### 5.1 Critical fix: SO_BINDTODEVICE

The Pi's routing table routes the 192.168.254.0/28 prefix via eth0 (because eth0 has
192.168.254.2/28). When netboot.py sends a DHCP OFFER to 255.255.255.255:68, the kernel
follows the routing table and sends it out eth0 -- not the dongle. The K3 never sees the
OFFER and keeps sending DISCOVERs forever.

The fix is SO_BINDTODEVICE, which forces the socket to send on a specific interface
regardless of the routing table. In DHCPServer.run():

```python
bind_device = self.cfg.get("bind_device") or ""
if bind_device and hasattr(socket, "SO_BINDTODEVICE"):
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                    bind_device.encode() + b"\x00")
```

This is a Linux-only socket option and requires root. It works even when both interfaces
are in overlapping subnets.

### 5.2 Pi config file: netboot-pi.toml

```toml
[dhcp]
interface   = ""
bind_device = "enxc84d4433b782"
server_ip   = "192.168.254.17"
subnet_mask = "255.255.255.240"
router      = "192.168.254.17"
dns         = ["1.1.1.1"]
pool_start  = "192.168.254.18"
pool_end    = "192.168.254.30"
lease_time  = 86400
lease_file  = "/home/ubuntu/git/k3-host/leases.csv"

[tftp]
interface = "0.0.0.0"
port      = 69
root      = "/home/ubuntu/git/k3-host/tftp-root"

[pxe]
enabled   = false
boot_file = "grubriscv64.efi"
```

Key points:
- interface = "" means bind to 0.0.0.0:67 (required to receive broadcast DHCP packets)
- bind_device forces outbound traffic through the dongle (the critical fix)
- server_ip is the IP announced in DHCP option 54 (server identifier) and option 66
- subnet_mask 255.255.255.240 = /28, covers .16 to .31
- pxe.enabled = false -- no PXE boot file options in DHCP replies

### 5.3 systemd service for netboot

File: /etc/systemd/system/netboot.service

```ini
[Unit]
Description=netboot DHCP+TFTP server for K3 bringup
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/ubuntu/git/k3-host/netboot.py \
  --config /home/ubuntu/git/k3-host/netboot-pi.toml \
  --log-level INFO
WorkingDirectory=/home/ubuntu/git/k3-host
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Commands:
```bash
sudo systemctl daemon-reload
sudo systemctl enable netboot.service
sudo systemctl start netboot.service
sudo journalctl -u netboot.service -f --no-pager
```

---

## 6. Serial console

### 6.1 Interactive access with tio

Connect interactively to the K3 serial console from the Pi:

```bash
sudo tio -b 115200 /dev/ttyUSB0
```

tio handles the terminal line discipline correctly and auto-reconnects on board reset.
Exit with Ctrl-T q.

The K3 serial port is /dev/ttyUSB0 at 115200 8N1. The ubuntu user is in the dialout
group but since tio is typically run as root for automation scripts, using sudo is safe.

### 6.2 Scripted serial access with expect + tio

When you need to send a sequence of commands to the K3 unattended (e.g. to apply
networking fixes without human interaction), use expect to drive tio. tio is still the
process that owns the serial port and handles the terminal correctly; expect just
automates the keystrokes.

You cannot use tio alone for scripting because tio has no way to react to output and
conditionally send different commands. expect provides that pattern matching and branching.

Template:
```tcl
set timeout 60
spawn sudo tio -b 115200 /dev/ttyUSB0
sleep 1
send "\r"
expect {
    "login:"             { send "ubuntu\r"; exp_continue }
    "Password:"          { send "ubuntu\r"; exp_continue }
    -re {ubuntu@\S+:~\$} { }
    timeout              { puts "ERROR: no shell"; exit 1 }
}
# send commands here, e.g.:
send "ip -br addr show end0\r"
expect -re {ubuntu@\S+:~\$}
# exit tio when done
send "\x14q"
expect eof
```

Before running expect, kill any process already holding the port:
```bash
sudo fuser -k /dev/ttyUSB0 2>/dev/null
sleep 1
sudo expect /path/to/script.exp
```

### 6.3 Power control via SiS-PM

```bash
sudo sispmctl -o 1   # power on outlet 1 (K3)
sudo sispmctl -f 1   # power off outlet 1 (K3)
sudo sispmctl -g 1   # get current state
```

For a hard power cycle (needed when the stmmac DMA is in a bad state -- see section 9):
```bash
sudo sispmctl -f 1
sleep 12             # at least 10 seconds for capacitor drain
sudo sispmctl -o 1
```

---

## 7. The routing table problem (root cause of DHCP failure)

The Pi's routing table after netplan apply:

```
default via 192.168.254.1 dev eth0 ...
192.168.254.0/28 dev eth0 proto kernel scope link src 192.168.254.2
192.168.254.16/28 dev enxc84d4433b782 proto kernel scope link src 192.168.254.17
```

Both eth0 and enxc84d4433b782 are in 192.168.254.0/24. When the DHCP server sends a
broadcast OFFER (destination 255.255.255.255), the kernel picks the best route. The
192.168.254.0/28 route via eth0 is a /28 route that does not cover .16-.31, but the
default route via eth0 wins for 255.255.255.255. Result: OFFERs go out eth0, the K3
never sees them, and the K3 keeps sending DISCOVERs indefinitely.

Symptoms on the Pi:
- netboot.log shows DISCOVER, OFFER, DISCOVER, OFFER, ... (no REQUEST ever)
- sudo tcpdump -i enxc84d4433b782 port 67 or port 68 shows ONLY DISCOVERs,
  never an OFFER packet

Solution: SO_BINDTODEVICE (see section 5.1). This is the critical fix.

Without this fix, the system appears to be working (netboot.py logs OFFERs) but
the K3 never completes the DHCP handshake.

---

## 8. K3 Ubuntu 26.04 networking fixes (one-time, run after first boot)

The stock Ubuntu 26.04 image for the K3 has several networking issues that prevent
DHCP from working. These must be fixed once and they persist across reboots.

### 8.1 Mask systemd-networkd

Both systemd-networkd and NetworkManager are active. They fight over the interface.
Mask networkd permanently:

```bash
sudo systemctl mask systemd-networkd
```

Do NOT use "sudo netplan apply" after masking networkd -- netplan apply tries to contact
networkd's varlink socket, which does not exist when networkd is masked, and hangs forever.
Use "sudo nmcli connection up netplan-end0 ifname end0" instead.

### 8.2 Remove 50-cloud-init.yaml

The file /etc/netplan/50-cloud-init.yaml adds dhcp6: true, causing NM to wait
indefinitely for DHCPv6. Remove it:

```bash
sudo rm -f /etc/netplan/50-cloud-init.yaml
```

### 8.3 Set dad-timeout to 0

NetworkManager performs ARP duplicate address detection (DAD) before claiming a DHCP
address. It sends an ARP probe and waits for a reply. If any host responds, NM sends
DHCP DECLINE and starts over.

In this setup the Pi responds to the ARP probe (because it owns the subnet), causing
NM to continuously decline the offered IP and re-discover. Setting dad-timeout to 0
disables this check.

The setting must go into the NM keyfile as a passthrough via netplan:

The /etc/netplan/90-NM-<uuid>.yaml file is auto-generated by NM. To make the setting
persistent, add it via nmcli (which writes it as a passthrough into the auto-generated
netplan file):

```bash
sudo nmcli con modify netplan-end0 ipv4.dad-timeout 0 ipv6.method ignore
```

This writes dad-timeout=0 into /run/NetworkManager/system-connections/netplan-end0.nmconnection
AND into the auto-generated 90-NM-<uuid>.yaml as a passthrough entry.

### 8.4 The valid netplan 01-end0.yaml

The stock 01-end0.yaml on the K3 image only contains:

```yaml
network:
  version: 2
  renderer: NetworkManager
```

Do not add ipv6-method to this file -- that is not a valid netplan key and causes a
"Error in network definition: unknown key 'ipv6-method'" error on every connection
activation.

The valid form if you need to add dhcp4-overrides:

```yaml
network:
  version: 2
  renderer: NetworkManager
  ethernets:
    end0:
      dhcp4: true
      dhcp4-overrides:
        use-dns: false
```

### 8.5 Clear stale NM lease files

On a fresh K3 that was previously configured to use a different DHCP server (e.g. with
a pool starting at 192.168.1.x), NM has a cached lease file. On reconnect NM sends a
DHCP REQUEST for the old IP (e.g. 192.168.1.150) instead of sending a DISCOVER. Since
the Pi's server does not know that IP and offers 192.168.254.18, the exchange fails.

Symptoms: tcpdump shows REQUEST packets with Requested-IP: 192.168.1.150

Fix:
```bash
sudo find /var/lib/NetworkManager -name "*.lease" -delete
sudo find /var/lib/NetworkManager -name "internal-*" -delete
```

The file is named:
  /var/lib/NetworkManager/internal-<uuid>-end0.lease

After deleting, NM will send a plain DISCOVER with no Requested-IP on the next attempt.

### 8.6 Full fix sequence (run once after first boot)

```bash
# 1. Stop networkd fighting NM
sudo systemctl mask systemd-networkd
sudo rm -f /etc/netplan/50-cloud-init.yaml

# 2. Clear stale lease so NM does a fresh DISCOVER
sudo find /var/lib/NetworkManager -name "*.lease" -delete
sudo find /var/lib/NetworkManager -name "internal-*" -delete

# 3. Disable DAD
sudo nmcli con modify netplan-end0 ipv4.dad-timeout 0 ipv6.method ignore

# 4. Reconnect (do NOT use netplan apply)
sudo nmcli connection up netplan-end0 ifname end0

# 5. Fix DNS (after each reconnect, or permanently via /etc/resolv.conf)
sudo resolvectl dns end0 1.1.1.1
sudo resolvectl default-route end0 yes
# Or more robustly:
echo "nameserver 1.1.1.1" | sudo tee /etc/resolv.conf
```

After step 4 the board should have inet 192.168.254.18/28 on end0 and a default route
via 192.168.254.17.

### 8.7 DNS: use-dns: false means you must set DNS manually

The netplan file has dhcp4-overrides: use-dns: false. This prevents NM from pushing the
DHCP-provided DNS server (1.1.1.1) to systemd-resolved. After each reboot you need to
either run:

```bash
sudo resolvectl dns end0 1.1.1.1
sudo resolvectl default-route end0 yes
```

Or bypass resolved entirely (simpler and persistent):

```bash
echo "nameserver 1.1.1.1" | sudo tee /etc/resolv.conf
```

The second approach works even if systemd-resolved is in a bad state.

---

## 9. The stmmac DMA bug -- never bring end0 down

The dwmac-spacemit-ethqos (stmmac) DMA controller on the K3 cannot recover from the
interface being brought down while open. If you run:

```bash
sudo ip link set end0 down
```

The DMA controller gets stuck. On the next open attempt you see:

```
dwmac-spacemit-ethqos cac80000.ethernet end0: Failed to reset the dma
dwmac-spacemit-ethqos cac80000.ethernet end0: stmmac_hw_setup: DMA engine initialization failed
dwmac-spacemit-ethqos cac80000.ethernet end0: failed reopening the interface after MTU change
```

After this, end0 shows as DOWN with NO-CARRIER and never recovers via software. Even
a soft reboot (sudo reboot) does not fix it. The only recovery is a hard power cycle
with at least 10 seconds off to drain the capacitors.

This means:
- Never run ip link set end0 down
- Never change the MTU on end0
- Never use ifdown on end0
- When in doubt after a failed networking attempt, do a hard power cycle:
    sudo sispmctl -f 1 && sleep 12 && sudo sispmctl -o 1

---

## 10. SSH access to the K3 from the Windows laptop

The K3 (192.168.254.18) is not directly reachable from the Windows laptop. It is behind
the Pi (192.168.254.2). Use a ProxyJump:

```
ssh -J ubuntu@192.168.254.2 ubuntu@192.168.254.18
```

The K3's ubuntu user requires a password on first SSH (default: ubuntu). To set up
key-based auth, push your key via the serial console or from the Pi:

From the Pi (once you can ping 192.168.254.18):
```bash
ssh-copy-id -i ~/.ssh/authorized_keys ubuntu@192.168.254.18
# or:
ssh ubuntu@192.168.254.18 'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys' < ~/.ssh/authorized_keys
```

After the first login you are forced to change the password. The new password replaces
"ubuntu" and is required for future sudo and SSH password auth.

---

## 11. Running the expect script from the Pi

The expect script (k3-fix-network.exp) lives at ~/git/k3-host/k3-fix-network.exp on
the Pi and automates running commands on the K3 over serial. Usage:

```bash
# Kill any process holding the port (stale tio, expect, etc.)
sudo fuser -k /dev/ttyUSB0 2>/dev/null
sleep 1

# Run the script
sudo expect /home/ubuntu/git/k3-host/k3-fix-network.exp
```

The script:
1. Spawns: sudo tio -b 115200 /dev/ttyUSB0
2. Sends a carriage return to wake the console
3. Handles login: prompt (sends "ubuntu") and already-logged-in state
4. Runs whatever commands are needed
5. Exits tio with Ctrl-T q (sent as \x14q)

---

## 12. Verifying the full boot after setup

After completing the one-time fixes in section 8, a clean boot looks like this:

1. Power on: sudo sispmctl -o 1
2. K3 boots (about 90 seconds to login prompt)
3. netboot.py logs show: DISCOVER -> OFFER -> REQUEST -> ACK
4. Pi can ping K3: ping 192.168.254.18
5. K3 has IP: ip -br addr show end0 shows UP 192.168.254.18/28
6. K3 can reach internet: ping 8.8.8.8 (after DNS is set)
7. apt-get update succeeds

If only DISCOVERs appear and no REQUEST, check:
- Is bind_device set in netboot-pi.toml? (see section 5.1)
- Are there stale lease files? (see section 8.5)
- Did something run ip link set end0 down? (see section 9, power cycle needed)

If REQUEST appears but the K3 requests the wrong IP (e.g. 192.168.1.150):
- Stale lease file -- delete /var/lib/NetworkManager/internal-*-end0.lease on the K3

If REQUEST appears and ACK is sent but K3 still has no IP:
- Check dmesg on K3 for stmmac DMA errors (means ip link set end0 down was run)
- Hard power cycle required

---

## 13. File locations on the Pi

All project files live under ~/git/k3-host/ on the Pi (never in the home directory root):

```
~/git/k3-host/
    netboot.py          -- DHCP + TFTP server (Linux-only, no Windows code)
    netboot-pi.toml     -- Pi-specific config
    leases.csv          -- DHCP lease database (auto-created)
    tftp-root/          -- Files served over TFTP
    k3-fix-network.exp  -- expect script for serial console automation
```

System files:
```
/etc/netplan/60-k3-dongle.yaml        -- Dongle static IP config
/etc/systemd/system/netboot.service   -- DHCP+TFTP server service
/etc/sysctl.d/99-ipforward.conf       -- ip_forward=1
/etc/iptables/rules.v4                -- NAT rules (saved by netfilter-persistent)
```

---

## 14. Reference: addresses and credentials

Pi:
- IP (uplink eth0): 192.168.254.2/28
- IP (K3 dongle): 192.168.254.17/28
- SSH: ubuntu@192.168.254.2, key auth from laptop

K3:
- IP (DHCP): 192.168.254.18/28 (assigned by netboot.py)
- Gateway: 192.168.254.17 (the Pi dongle)
- DNS: 1.1.1.1 (set manually after each boot or via /etc/resolv.conf)
- SSH: ubuntu@192.168.254.18 (via ProxyJump through 192.168.254.2)
- Default password: ubuntu (forced change on first login)
- Serial: /dev/ttyUSB0 on Pi, 115200 8N1

K3 MAC address: 50:0a:52:0b:e6:66 (end0, the RJ45 GbE port)
K3 hostname: ludovic-k3-0

Power:
- SiS-PM outlet 1 controls K3 power
- sudo sispmctl -o 1 (on), -f 1 (off), -g 1 (status)
- Hard power cycle: -f 1, sleep 12, -o 1

---

## 15. Troubleshooting quick reference

Problem: K3 keeps sending DISCOVERs, never REQUESTs
- Root cause A: OFFER not reaching K3 (wrong interface on Pi)
  Check: sudo tcpdump -i enxc84d4433b782 port 67 -- do OFFERs appear?
  If not: bind_device is missing from netboot-pi.toml
- Root cause B: stale lease requesting old IP
  Check: tcpdump -- is Requested-IP something other than 192.168.254.x?
  Fix: delete /var/lib/NetworkManager/internal-*-end0.lease on K3

Problem: nmcli connection up fails immediately "no carrier"
- ip link set end0 down was run, stmmac DMA is broken
- Hard power cycle (12s off) required

Problem: nmcli connection up fails "IP configuration could not be reserved"
- NM tried to DHCP but got no reply in 45 seconds
- Check netboot.py is running: sudo systemctl status netboot.service
- Check bind_device is set in config
- Check dongle has an IP: ip -br addr show enxc84d4433b782

Problem: apt-get update fails with DNS resolution error
- DNS not set on K3
- Fix: echo "nameserver 1.1.1.1" | sudo tee /etc/resolv.conf
  (or: sudo resolvectl dns end0 1.1.1.1 && sudo resolvectl default-route end0 yes)

Problem: expect script gets "Device file is locked by another process"
- A stale tio or expect process is holding /dev/ttyUSB0
- Fix: sudo fuser -k /dev/ttyUSB0

Problem: serial output is garbled / binary data
- Use: sudo tio -b 115200 /dev/ttyUSB0

---

## 16. What was NOT needed (common dead ends)

- dnsmasq in WSL: blocked by mirrored networking mode on Windows
- tftpd-hpa in WSL: UDP doesn't work reliably in mirrored WSL mode
- New-NetNat on Windows: NetNat CIM class not registered on Qualcomm-managed machines
- ICS on Windows: hardcodes 192.168.137.0/24, conflicts with everything
- proxy_arp: was already 0, not the cause of the DHCP loop
- arp_ignore=1: not needed, not the cause
- nmcli con modify ipv4.dad-timeout 0: necessary but not sufficient alone
  (also need to clear stale leases AND have bind_device set on the Pi)
- SO_DONTROUTE: does not help with interface selection on Linux
- tio as a systemd service: tio exits when stdin has no TTY
  (it is designed for interactive use, not daemonization)
- expect with spawn -open [open /dev/ttyUSB0 r+]: does not set up terminal correctly,
  hangs waiting for data even when the port has output

---

## 17. labgrid setup

labgrid replaces netboot.py (DHCP + TFTP), sispmctl manual commands, and the tio/expect
serial console stack. All project files live under ~/git/k3-host/ on the Pi.

### 17.1 Architecture

```
[Pi]
  labgrid-coordinator   (WebSocket, port 20408)
  labgrid-pi-0-exporter → exposes serial, power, TFTP for k3-pico-itx-0
  dnsmasq               → DHCP + TFTP on enxc84d4433b782 (replaced netboot.py)

[Client / pytest on Pi]
  → LG_COORDINATOR=localhost:20408
  → place: k3-pico-itx-0
```

### 17.2 File layout

```
~/git/k3-host/
  boards/k3-pico-itx/
    exporter.yaml       -- resource definitions (serial, power, TFTP)
    client.yaml         -- driver stack + strategy (for pytest / labgrid env)
    strategy.py         -- K3PicoITXBootStrategy (states: off, emmc)
    pytest/
      conftest.py
      pytest.ini
  dnsmasq/
    00-no-dns.conf      -- disables dnsmasq DNS (port=0), avoids systemd-resolved conflict
    k3-pico-itx.conf    -- DHCP pool .18-.30 on enxc84d4433b782, TFTP on tftp-root/
  systemd/
    labgrid-coordinator.service
    labgrid-pi-0-exporter.service
  tftp-root/
    k3-pico-itx/        -- staging directory for future PXE files
```

System files installed from the above:

```
/etc/dnsmasq.d/00-no-dns.conf
/etc/dnsmasq.d/k3-pico-itx.conf
/etc/systemd/system/labgrid-coordinator.service
/etc/systemd/system/labgrid-pi-0-exporter.service
```

### 17.3 dnsmasq replaces netboot.py

dnsmasq handles DHCP and TFTP. The routing-table OFFER problem (section 7) is solved by
`interface=enxc84d4433b782` + `bind-interfaces` instead of SO_BINDTODEVICE. DNS is disabled
(`port=0`) so dnsmasq does not conflict with systemd-resolved.

netboot.service is removed entirely.

### 17.4 Power control: SiSPMPowerPort

labgrid uses its built-in `SiSPMPowerPort` resource and `SiSPMPowerDriver` to control the
EG-PMS2 (USB ID 04b4:fd15) via sispmctl. The exporter matches the device by vendor/product
ID and selects outlet 1 (`index: 1`).

The ubuntu user must be in the `sispmctl` group (added by the sispmctl package's udev rule):

```bash
sudo usermod -aG sispmctl ubuntu
```

Log out and back in for the group to take effect. The sispmctl package installs a udev rule
at /lib/udev/rules.d/ that sets GROUP=sispmctl on the device node — no custom rule needed.

The `SiSPMPowerDriver` in client.yaml has `delay: 12.0` to satisfy the stmmac DMA
capacitor-drain requirement on power cycle (see section 9).

### 17.5 Serial console: USBSerialPort

The CH340 (1a86:7523) has no USB serial number, so `@ID_SERIAL_SHORT` cannot be used.
Match by physical USB path instead:

```yaml
k3-pico-itx-serial-port-1:
  cls: 'USBSerialPort'
  match:
    '@ID_PATH': 'platform-3f980000.usb-usb-0:1.1.3.3:1.0'
```

The path is stable as long as the cable stays in the same USB port. To find it:

```bash
udevadm info --query=property /dev/ttyUSB0 | grep ID_PATH=
```

labgrid uses ser2net to expose the serial port over RFC 2217. Install it:

```bash
sudo apt-get install -y ser2net
```

The exporter service runs with `SupplementaryGroups=dialout sispmctl`.

### 17.6 LG_COORDINATOR format

The `LG_COORDINATOR` environment variable takes `host:port`, NOT a WebSocket URL.
The `ws://` prefix and `/ws` path are added internally by labgrid.

Correct:
```bash
export LG_COORDINATOR=localhost:20408
```

Wrong (causes "Misformatted domain name" error):
```bash
export LG_COORDINATOR=ws://localhost:20408/ws   # DO NOT USE
```

Add to ~/.bashrc:
```bash
echo 'export LG_COORDINATOR=localhost:20408' >> ~/.bashrc
```

### 17.7 One-time place setup

Places persist in the coordinator across restarts. Create once:

```bash
labgrid-client -p k3-pico-itx-0 create
labgrid-client -p k3-pico-itx-0 add-match 'pi-0-exporter/k3-pico-itx-0/*'
```

### 17.8 Daily usage

```bash
# Interactive console (escape: Ctrl+])
labgrid-client -p k3-pico-itx-0 acquire
labgrid-client -p k3-pico-itx-0 console

# Power control
labgrid-client -p k3-pico-itx-0 power cycle
labgrid-client -p k3-pico-itx-0 power on
labgrid-client -p k3-pico-itx-0 power off

# Release when done
labgrid-client -p k3-pico-itx-0 release
```

### 17.9 Boot chain note

The K3 boot chain is: U-Boot SPL (silent, no prompt) → EDK II → GRUB → Ubuntu.
U-Boot SPL hands off to EDK II without exposing a prompt, so labgrid's UBootDriver is not
used. The strategy (strategy.py) only implements `off` and `emmc` states. A `tftp` state
will be added once EDK II gains PXE support.
