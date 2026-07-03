---
name: system-info
description: "Quick system diagnostics: CPU, memory, disk, uptime"
metadata:
  {
    "openclaw":
      {
        "emoji": "💻",
        "requires": { "bins": ["free", "df", "uptime"] },
        "install": [],
        "sandbox": {
          "network": false,
          "filesystem": "readonly",
          "capabilities": [],
          "allowed_commands": ["free", "df", "uptime", "lscpu"]
        }
      }
  }
---

# System Info

Quick system diagnostics covering CPU, memory, disk, and uptime. Uses standard Linux utilities that are always available.

## Commands

```bash
# Show all system info (CPU, memory, disk, uptime)
system-info

# Show CPU information
system-info cpu

# Show memory usage
system-info mem

# Show disk usage
system-info disk

# Show system uptime
system-info uptime
```

## Security Notes
- This skill operates in read-only mode with no network or privilege escalation capabilities.
- All commands are allowlisted and executed under an unprivileged service account.
- Audit logs capture all invocations and outputs for compliance and anomaly detection.

## Install
No installation needed. `free` and related utilities are always present on the system.