# netboot.py

A minimal DHCP + TFTP server for PXE netbooting, designed for local development
and Android bringup workflows.

**Requirements:** Python 3.11+ (uses `tomllib` from stdlib), root/Administrator
privileges (ports 67 and 69).

---

## Quick start

```bash
# 1. Edit netboot.toml — set server_ip, pool range, and boot_file
# 2. Drop your PXE files into tftp-root/
# 3. Run (as root / Administrator):
sudo python netboot.py
```

Or generate a fresh config first:

```bash
python netboot.py --init          # writes netboot.toml
sudo python netboot.py            # start with defaults
```

---

## Configuration

All settings live in `netboot.toml`. CLI flags override individual values.

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `dhcp` | `server_ip` | `192.168.1.1` | IP of this machine; sent as next-server (opt 54/66) |
| `dhcp` | `pool_start` / `pool_end` | `.100` / `.200` | Assignable IP range |
| `dhcp` | `lease_time` | `86400` | Lease duration in seconds |
| `dhcp` | `lease_file` | `leases.csv` | Persistent MAC→IP binding store |
| `tftp` | `root` | `./tftp-root` | Directory served over TFTP |
| `tftp` | `port` | `69` | UDP port |
| `pxe` | `boot_file` | `pxelinux.0` | DHCP option 67 — boot filename |

### CLI overrides

```
--server-ip IP      --pool-start IP    --pool-end IP
--lease-file PATH   --lease-time SEC   --dhcp-iface IP
--tftp-root DIR     --tftp-port PORT   --tftp-iface IP
--boot-file FILE    --log-level LEVEL  --config FILE
```

---

## TFTP root layout (typical PXE / Android bringup)

```
tftp-root/
  pxelinux.0          ← or grubx64.efi / ipxe.efi
  pxelinux.cfg/
    default           ← boot menu / kernel cmdline
  vmlinuz             ← kernel image (zImage / Image)
  initrd.img          ← initramfs
```

For GRUB2 UEFI, set `boot_file = "grubx64.efi"` and place `grub.cfg` under
`tftp-root/grub/`.

For iPXE chainloading, set `boot_file = "ipxe.efi"` and serve an `ipxe.cfg`
script from the TFTP root.

---

## Lease file

`leases.csv` is created automatically and persists across restarts:

```
mac,ip,hostname,lease_start,lease_expiry
aa:bb:cc:dd:ee:ff,192.168.1.100,dut-board,1717000000.0,1717086400.0
```

Delete a row (or the whole file) to free an IP or reset all bindings.

---

## Notes

- TFTP is **read-only** (RRQ only). WRQ is rejected.
- Only **octet** (binary) mode is supported — correct for kernel/initrd transfers.
- Path traversal attempts (`../`) are rejected with TFTP error code 2.
- DHCP always sends options 66 (TFTP server) and 67 (boot file) — PXE is
  always active.
