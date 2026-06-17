# Board Farm Playground

Lab control for the SpacemiT K3 Pico-ITX board. Runs on the Raspberry Pi.

Hardware attached to the Pi:
- Serial console: CH340 USB-serial (`/dev/ttyUSB0`, 115200 8N1)
- Power: Gembird EG-PMS2 smart plug (`04b4:fd15`), outlet 1
- Network: USB-Ethernet dongle `enxc84d4433b782` at 192.168.254.17/28

---

## Services

Three systemd services run on the Pi:

| Service | Role |
|---------|------|
| `labgrid-coordinator` | Central place/resource registry (WebSocket on port 20408) |
| `labgrid-pi-0-exporter` | Exports serial, power, and TFTP resources to the coordinator |
| `dnsmasq` | DHCP + TFTP server on the K3 subnet (enxc84d4433b782) |

Check status:

```bash
sudo systemctl status labgrid-coordinator labgrid-pi-0-exporter dnsmasq
```

---

## Setup

Install labgrid into the venv:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install labgrid
sudo apt-get install -y ser2net
```

Set the coordinator address (add to `~/.bashrc`):

```bash
export LG_COORDINATOR=localhost:20408
```

One-time place creation (run once after coordinator first starts):

```bash
labgrid-client -p k3-pico-itx-0 create
labgrid-client -p k3-pico-itx-0 add-match 'pi-0-exporter/k3-pico-itx-0/*'
```

---

## Daily usage

### Console

```bash
labgrid-client -p k3-pico-itx-0 acquire
labgrid-client -p k3-pico-itx-0 console
```

Exit the console with `Ctrl+]`. Release the place when done:

```bash
labgrid-client -p k3-pico-itx-0 release
```

### Power

```bash
labgrid-client -p k3-pico-itx-0 power on
labgrid-client -p k3-pico-itx-0 power off
labgrid-client -p k3-pico-itx-0 power cycle   # 12 s off, then on
```

Power cycle waits 12 seconds off to drain capacitors - required by the K3's stmmac DMA
controller (see bringup notes §9).

### Inspect

```bash
labgrid-client resources          # list all exported resources
labgrid-client places             # list all places and their state
labgrid-client -p k3-pico-itx-0 show  # show place details
```

---

## pytest

Boot to Ubuntu shell and run tests:

```bash
source .venv/bin/activate
cd boards/k3-pico-itx/pytest
pytest -v
```

The `emmc` fixture transitions the board to the Ubuntu shell via power cycle + serial login.

---

## File layout

```
boards/
  k3-pico-itx/
    exporter.yaml   -- resource definitions (serial, power, TFTP)
    client.yaml     -- driver stack + strategy (for pytest / labgrid env)
    strategy.py     -- K3PicoITXBootStrategy (states: off, emmc)
    pytest/
      conftest.py
      pytest.ini
dnsmasq/
  00-no-dns.conf    -- disables dnsmasq DNS to avoid systemd-resolved conflict
  k3-pico-itx.conf  -- DHCP pool, bind to dongle, TFTP root
systemd/
  labgrid-coordinator.service
  labgrid-pi-0-exporter.service
tftp-root/
  k3-pico-itx/      -- staging directory for future PXE boot files
```
