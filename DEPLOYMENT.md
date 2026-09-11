# F3K Timer System — Deployment & Distribution

*Discussion document. Nothing here is decided. Written to frame a conversation about
what this system looks like when someone other than its author runs it.*
*Last updated: 11 September 2026*

---

## WHY THIS DOC EXISTS

This system was built for one person, on one Pi, for one club. It is now a
functioning competition management system for F3K and F5K: timers, draw, scoring,
audio callup, field display, GliderScore export. The question has changed from
"does it work" to **"what happens when a second club wants to run it."**

The stated goal, in the author's words:

> Ideally it needs to be simple and repeatable on any hardware so I do not need to
> support it. As long as the requirements are met, it should work.

That is a *product* goal, not an engineering one, and it drives everything below.
"No support burden" is achievable, but only by being ruthless about what counts as
supported hardware and by making the box able to explain its own failures.

This doc covers four things:

1. What is actually hard to deploy (it is not the application)
2. Why containerisation does not solve the Pi case, and where it does earn a place
3. `site.json` — extracting site identity from the code and the OS config
4. Tagged releases, so a live competition box and a development box can coexist

---

## 1. WHAT IS ACTUALLY HARD TO DEPLOY

**The application is the easy part.** `base_station/requirements.txt` is five
packages — fastapi, uvicorn, websockets, jinja2, python-multipart — plus a SQLite
file. That deploys anywhere in ten minutes. It is not what stops another club
running this.

What stops them is the OS layer. Every path in `apply-system-config.sh`'s
`MANAGED_PATHS`:

| Path | What it does | Why it exists |
|---|---|---|
| `/etc/hostapd/hostapd.conf` | F3K_BASE AP on wlan1 | Timer network |
| `/etc/hostapd/hostapd-ops.conf` | F3K_OPS AP on wlan0 | Phones/tablets |
| `/etc/dnsmasq.d/f3k-timer.conf` | DHCP 192.168.10.0/24 | Timer addressing |
| `/etc/dnsmasq.d/f3k-ops.conf` | DHCP 192.168.20.0/24 + DNS hijack | Captive portal |
| `/etc/modprobe.d/mt76_usb.conf` | `disable_usb_sg` | VL805/mt76x2u scatter-gather deadlock hangs the AP |
| `/etc/NetworkManager/conf.d/99-unmanaged-wlan*.conf` | Keep NM off both radios | NM fights hostapd for the interface |
| `/etc/systemd/system/wlan{0,1}-setup.service` | Poll loops for interface readiness | USB adapter enumerates too slowly for device units |
| `/etc/nftables.d/f3k-captive.conf` | :80 → :8080 redirect | Captive portal |
| `/etc/cron.d/hostapd-watchdog` | AP health probe + restart | AP can hang while looking alive to systemd |
| `/etc/systemd/journald.conf.d/50-f3k-persistent.conf` | Persistent logs | Post-event diagnosis |

Plus bluez-alsa, a paired A2DP speaker, and `amixer -D bluealsa` for volume.

**Roughly 95% of the deployment problem is host networking, radio quirks and
audio plumbing. Roughly 5% is the Python app.**

### What is already solved

More than it feels like. `apply-system-config.sh` has a versioned
`CONFIG_VERSION`, per-file backups, restart-only-if-changed with verify and
rollback, `--check` drift detection wired into the `/pi-config-check` skill, and
design rule 1 written explicitly for *"a fielded Pi in another city with no
out-of-band access."* The operations story for a deployed box is genuinely good.

### The gap

**Bootstrap.** Design rule 3 of that script says it outright:

> Only manage files that already exist (except our own watchdog), so this never
> imposes a topology the box wasn't already running.

It *converges* a Pi that is already roughly right. It cannot *build* one from
bare Raspberry Pi OS. `install.sh` and `upgrade-to-dual-ap.sh` cover part of that
path, but they are one-time, in-person scripts —  `upgrade-to-dual-ap.sh` resets
eth0 to DHCP, which the config script's own comments call "fatal for an
unattended update."

So: **converge = solved. Bootstrap = partly scripted. Reproducible image =
does not exist.**

---

## 2. CONTAINERS

### Why a container does not solve the Pi case

To run the current system in a container you would need `--net=host`,
`--privileged`, the host's D-Bus and the host's BlueZ. At that point there is no
isolation left — it is a tarball with extra steps and a worse debugging story.

Docker isolates a process from its host. **This system's entire job is to
configure its host.** The two are working against each other.

**The natural unit for the Pi deployment is an image, not a container.**

Worth noting the contrast with SoarScore2 (see `SOARSCORE.md` when written): a
headless REST kernel with no hardware coupling, where a container is exactly the
right unit. Different system, different answer.

### Where containers do earn their place

**Development and CI.** A container running just `server.py` + frontend with
simulated timers would let the web UI be tested on Windows and the test suite run
in GitHub Actions with no Pi attached. This has value today, independent of any
distribution decision, and carries no field risk. Cheapest win in this document.

**A Pi-less deployment — needs a Guardrail 2 amendment.** Guardrail 2 currently
reads *"Raspberry Pi 4 is the base station compute platform. Do not propose Pi
5/CM5 or other SBCs unless Pi 4 has been proven insufficient."* A laptop target
is a deliberate change to that rule, not an exception to it, and should be
written into `GUARDRAILS.md` before any code assumes it.

The honest trade:

| | Pi 4 appliance | Laptop + container |
|---|---|---|
| Field power | 12 V battery, trivial | Laptop battery, or inverter |
| Timing display | Drives an LCD/HDMI display as a dedicated job | Laptop screen, but then it is doing double duty as the CD's console |
| Second radio | MT7612U USB, known-good | Same dongle, unknown host USB behaviour |
| Host OS control | Total — it is our image | None — it is someone's work laptop |
| Setup effort | Flash a card | Install Docker, grant host net + BT, fight the host's NetworkManager |
| Who supports it | Us, but on hardware we specified | Nobody can, realistically |

**The Pi choice was correct for the field.** Easy power, a dedicated screen, and
total control of the OS are exactly the things the laptop path gives up. The
laptop path is attractive for a *different* scenario: a club with existing WiFi
infrastructure, where the base station does not need to be an access point at
all and timers join a normal router. That removes hostapd, dnsmasq, nftables and
the radio quirks in one move — which is to say it removes 95% of the deployment
problem by removing the requirement, not by containerising it.

**That is the real question to decide: is "runs as an AP" a requirement, or a
default?** If some clubs could run against existing WiFi, that is a genuinely
simpler product and a container is a fine way to ship it. If the isolated timer
network is non-negotiable (Guardrail 7 says it is), the laptop path inherits the
whole problem and gains little.

---

## 3. `site.json` — EXTRACTING SITE IDENTITY

**This is the highest-leverage next step**, and it is useful even if this system
is never shipped to another club — because right now the Pi's identity is
scattered across files that only exist on that Pi.

A strawman is in `setup/site.example.json`. Nothing reads it yet.

### Where identity lives today

| Value | Current home | Problem |
|---|---|---|
| `F3K_BASE` / passphrase `f3ktimer` | Heredoc in `upgrade-to-dual-ap.sh:45` | Baked into a bootstrap script |
| `F3K_OPS` / passphrase `f3kmanage` | Heredoc in `upgrade-to-dual-ap.sh:64` | Same |
| Channels 6 and 11 | Same heredocs | Not site-tunable; no survey step |
| **No `country_code` at all** | — | hostapd falls back to world regulatory domain. Fine for 2.4 GHz ch 6/11 in NZ, wrong the moment anyone wants 5 GHz or deploys outside NZ |
| `192.168.10.0/24`, `192.168.20.0/24` | Heredocs + `apply-system-config.sh:236` | Collides with common home ranges |
| `192.168.20.1` captive redirect | **Hardcoded in `frontend/app.py:2998`** | Application code knows the subnet |
| wlan0 / wlan1 role assignment | Hardcoded per interface name | Breaks if USB enumerates differently |
| Web port 8080, timer port 8765 | `server.py:33`, `app.py` | Fine as defaults, should be declared |
| Speaker MAC, volume, `lead_s`, callup | `audio_config.json` | Already externalised — the model to follow |
| **Club / venue / event name** | Does not exist anywhere | Needed for exports, display headers, diagnostics |

The pattern to copy is `audio_config.json`: a small JSON file the app reads and a
settings page writes. `site.json` is the same idea applied to everything else.

### What `site.json` must satisfy

1. **One file is the source of truth for site identity.** `apply-system-config.sh`
   renders hostapd/dnsmasq/nftables *from* it; the app reads it for the captive
   portal address, ports and display headers. No literal SSID or subnet survives
   in either the scripts or the Python.
2. **Written by a first-boot wizard**, served over F3K_OPS at a known address, so
   nobody needs SSH to commission a box.
3. **Editable afterwards from the settings page**, with the same
   verify-and-rollback discipline the config script already has — a bad SSID edit
   must not strand the operator.
4. **Versioned with a schema version**, so `apply-system-config.sh` can migrate
   old files rather than reject them.
5. **Never committed.** `site.json` is per-box; `site.example.json` is the
   documented template in git.
6. **Interfaces identified by role, not name.** Map radios to roles by MAC or by
   driver (`mt76x2u` → timer AP), so a differently-enumerating USB port does not
   silently swap the two networks.

### What must NOT go in it

Anything derivable, anything per-competition (that is the DB), and anything
secret beyond the AP passphrases — which are low-value by design and already
public in `GUARDRAILS.md` terms (closed AP, hardcoded credentials).

---

## 4. TAGGED RELEASES

Today `/api/system/update` (`frontend/app.py:2848`) does a `git pull` and then
runs `apply-system-config.sh`. For a single box whose owner wrote the commits,
that is fine. For anyone else, **every club would be riding unreleased work.**

The firmware side already solved this: `firmware/releases/`, a tag per build,
packaged at the end of any session that changed firmware. The base station needs
the same discipline.

Proposed shape:

- **`main` is development.** No club tracks it.
- **Tagged releases** — `base-vX.Y.Z` — are what the update button offers.
  `CONFIG_VERSION` in `apply-system-config.sh` becomes part of the release
  contract rather than an internal counter.
- **Two update channels in the UI:** *Stable* (latest tag) and *Development*
  (HEAD, with a visible warning). The author's own box can sit on Development
  while a live competition box sits on Stable — which is the actual requirement:
  *develop and host a live version at the same time*.
- **Release notes are the support surface.** If a club cannot read what changed,
  they will ask. Each release states what changed, whether a reboot is needed,
  and whether the timer firmware must be updated to match.
- **Pin the firmware/base-station compatibility pair.** A release should declare
  the firmware version it expects, and the UI should warn on mismatch. This is
  the single most likely source of "it stopped working" reports.

---

## 5. NO-SUPPORT REQUIREMENTS

"As long as the requirements are met, it should work" only holds if the
requirements are *stated* and the box can *prove* whether they are met.

- **A published BOM and compatibility matrix.** The MT7612U is not incidental —
  it is a specific chipset with a specific kernel workaround. Any club that buys
  a different dongle is a support request that cannot be answered remotely. The
  matrix should name tested adapters, tested Pi models, tested Bluetooth
  speakers, and say plainly that anything else is unsupported.
- **A self-check page.** The box tests its own preconditions — both APs up, DHCP
  serving, speaker reachable, disk writable, config version current — and shows
  pass/fail. Most "it doesn't work" reports are one of these.
- **A diagnostic bundle button.** Journal extract, config versions, drift report
  from `apply-system-config.sh --check`, DB stats, recent errors — one zip the
  club emails. Cheap now, very expensive to retrofit after the first field
  failure at someone else's event.
- **A licence.** Currently none. Needs one before anyone else runs this.

---

## 6. THE TIERS, AND WHAT TO AIM AT

**Tier 0 — today.** Clone, run `install.sh`, SSH in when it breaks. Works for the
author. Does not travel.

**Tier 1 — flashable image + first-boot wizard.** Built with `pi-gen` or
`rpi-image-gen` in CI. A club flashes a card, boots, joins F3K_OPS, and a wizard
writes `site.json`. **This is the recommended target.** It needs: bootstrap mode
in `apply-system-config.sh`, `site.json`, and tagged releases — in that order,
and each is useful on its own.

**Tier 2 — shipping hardware.** Pre-flashed cards or assembled units. This is a
business decision, not an engineering one, and it inverts the "no support"
goal rather than serving it.

---

## OPEN QUESTIONS

1. **Is "base station is an access point" a requirement or a default?** Guardrail
   7 says requirement. If it can be relaxed for clubs with existing WiFi, a much
   simpler deployment exists and the container path becomes genuinely attractive.
2. **Does the laptop target justify amending Guardrail 2?** It is a real
   widening of scope, and doubles the test matrix permanently.
3. **What is the supported hardware list, exactly?** Everything downstream — the
   image, the BOM, the self-check, the support burden — depends on this answer.
4. **Licence?**
5. **Who is the second club, and can they be a pilot?** One real external
   deployment will find more problems than any amount of this document.

---

## RELATED

- `GUARDRAILS.md` — Guardrail 2 (Pi 4 platform), 5 (offline-first), 7 (network isolation), 8 (one base station)
- `setup/apply-system-config.sh` — the convergence script, and its design rules
- `setup/site.example.json` — the `site.json` strawman
- `PROJECT_PHASES.md` — where this work would sit in the roadmap
