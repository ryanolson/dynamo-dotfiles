# Headless machine onboarding: `fleet onboard`

Status: in progress. Steps 1 to 5 are built (`fleet onboard`, with tests). The brev-up change and the bb phase are not.
Updated: 2026-10-30

## Goal

One command on the admin laptop takes a machine that only has SSH and puts it in the fleet, with the dotfiles installed. Today it takes several commands, and some steps are not written down. The optional last phase also adds the machine to bb as an execution machine, over the fleet door.

```
fleet onboard [USER@]HOST NAME [--jump ALIAS] [--fleet-config PATH] [--no-alias]
```

## What exists now

| Step | Tool today | Gap |
|---|---|---|
| Install fleet and dotfiles, enroll, renew timer | `fleet invite NAME --dotfiles`, piped to `ssh HOST bash -s` | Works, but has no entry point except `brev-up` |
| Enroll over SSH | `fleet enroll-remote HOST NAME` | Needs `~/.local/bin/fleet` on the machine already. Does not install the timer |
| Agent and pylon | `fleet agent install`, `fleet install-pylon` | Separate commands |
| Short SSH name for other machines | `broker/ssh-hosts.json` in fleet-config | Edited by hand, then deployed to the broker host |
| Create the instance, join, start a bb server | fleet-config `provision/brev-up` | Brev only. Installs a bb server, which an execution machine does not need |

`brev-up` already does most of the work after SSH answers. The plan is to move those steps into `fleet onboard` and make `brev-up` call it, so there is one implementation.

## Steps of `fleet onboard`

Each step converges, so a failed run is repaired by running the same command again. This is the rule `fleet worker enroll` already follows.

1. Check that `ssh HOST true` works. `--jump` adds a `ProxyJump`.
2. Send `fleet invite NAME --dotfiles` to the machine on stdin, as `brev-up` does. This installs the dotfiles, installs `fleet`, enrolls, and runs `fleet timer install`.
3. Run `fleet agent install` and `fleet install-pylon` on the machine.
4. Add the machine to `broker/ssh-hosts.json` in a fleet-config checkout (`--fleet-config`, default `~/repos/fleet-config`). Do not commit or deploy. Print the next step, as `fleet worker enroll` does.
5. Check: `fleet list` shows the name, and `ssh NAME true` works once the broker has the new list.

Tailscale is not part of `fleet onboard`. The owner ruled on 2026-10-30 that joining the tailnet is a manual step and not needed by default. The fleet door and a fleet tunnel give a machine a path to the server without it. `brev-up` keeps its own optional join until it calls `fleet onboard`.

## Dotfiles on the machine

`fleet invite --dotfiles` runs `bootstrap.sh` first. Without a terminal, `bootstrap.sh` stops unless `DOTFILES_NAME` and `DOTFILES_EMAIL` are set. It then uses the prompt defaults, which means class `sudo` or `nosudo` is detected, and the 1Password question is answered no. Under `ssh bash -s` there is no terminal, so `fleet onboard` must send both variables ahead of the script. It reads them from the laptop's chezmoi data. The note does not yet say that `brev-up` does this, and it should be checked.

## Short SSH name

The short names come from the broker (`GET /v1/ssh-hosts`). Every machine writes them into `~/.ssh/fleet/config` on each `fleet renew`. The broker reads `ssh-hosts.json` from `/etc/fleet-broker/`, and `broker/install.sh` copies it there and restarts the service. A new entry needs a commit and that deploy. Whether the broker also reloads the file without a restart has not been checked.

An entry has an alias, a hostname, a user, an optional jump host, and a host key alias. A machine that is not on the tailnet has its public address or DNS name as the hostname and its own name as the host key alias.

## Hazards

- **First contact.** The fleet SSH settings trust new hosts on first use only inside the tailnet, where WireGuard authenticates the peer. A raw public address has no such protection. `fleet onboard` should print the host key fingerprint it accepted, so it can be compared with the provider's console. A stricter option is a `--host-key-fingerprint` flag that refuses any other key. This is a decision for the owner.
- **Token exposure.** The enrollment token and any Tailscale key travel on SSH standard input only.
- **No sudo.** The no-sudo class installs into `$HOME`, and `fleet door` needs sudo. A machine that is only a door client needs no sudo, because `fleet tunnel` runs as a user service.
- **Revocation.** `fleet revoke` takes up to 24 hours to end access. This is the existing rule.

## bb phase (gated)

The phase adds the machine as a bb execution machine over the fleet door. It does not start until the experiment below has an answer.

1. `fleet tunnel add bb --mode client --listen 127.0.0.1:38900 --to DOOR:8461 --peer DOOR_NAME` on the machine.
2. Make a manual-provider bundle (`bb machine create --provider manual`), put it in a private file on the machine, and run `bb machine enroll --bootstrap-file`.
3. `bb machine provider-cli status` and `install`, then `bb machine list`.

The experiment: bb has one server-wide setting, `machineServerUrl`, that the bundle uses. A machine behind the tunnel must reach the server at the tunnel's loopback address, which no other machine uses. Find out whether the bundle's URL can be set per machine. If it cannot, decide between a second bb access path and changing the server setting. Do not change `machineServerUrl` on the live server to find out, because every machine uses it.

Do not copy `bb-server-service install` from `brev-up`. That installs a server.

## Open questions

- Which machine runs the fleet door, and where it forwards. Read `/etc/fleet-door/nginx.conf` on the door machine. The alias `bb` in `ssh-hosts.json` and the tunnel peer on the laptop name different machines, so do not assume they are the same.
- Whether the broker reloads `ssh-hosts.json` without a restart.
- Whether `brev-up` sets `DOTFILES_NAME` and `DOTFILES_EMAIL`, or the invite command carries them.
- The host key decision above.

## Tests

Tests land with the code, written first, in `test/test-fleet.py` with a fake `ssh` on `PATH`:

- `onboard` sends the token on stdin and never in an argument.
- A second run after a failure in step 3 does not repeat step 2 and exits 0.
- The `ssh-hosts.json` change is added once and is not added twice.
- A host name with `user@` and with `--jump` passes validation. A name with a space or a leading dash is refused.
- `DOTFILES_NAME` and `DOTFILES_EMAIL` are in the script that the machine receives.
- No Tailscale command is sent.
- The token's key file on the laptop is removed (a first version leaked it, because `$(...)` loses the cleanup entry).

## Order of work

1. `fleet onboard` steps 1 to 5, and `brev-up` changed to call it. Tests first.
2. README section for the command, and the removal of the `brev-up` steps that moved.
3. The bb experiment, then the bb phase.

## Review

A cross-family design review, up to two rounds, before any code.
