# Machine classes after fleet

Date: 2026-10-14

Status: plan. Nothing in this note is implemented yet.

## Problem

`machine_class` answers two separate questions with one value:

1. Does the machine have `sudo`? This selects the install layout.
2. Does the machine have 1Password? Only `primary` turns on 1Password, the 1Password SSH agent, and commit signing with a 1Password key.

Fleet ([fleet-identity.md](fleet-identity.md)) now does most of the work that the second question controlled. The first question does not change.

## What reads the class today

| Setting | Where it is read | What it controls |
|---|---|---|
| `machine_class` | `run_onchange_install-packages.sh.tmpl`, `run_after_update-pixi-tools.sh.tmpl`, `.chezmoiignore`, `run_onchange_setup-agent-infra.sh.tmpl`, `bootstrap.sh` | Install path: system packages with `sudo`, or pixi under `~/.local/<arch>/` |
| `onepassword.enabled` (true only on `primary`) | `dot_config/dynamo/dev.env.op.tmpl`, `dot_config/fish/conf.d/secrets.fish.tmpl`, `dot_gitconfig.tmpl` | The `op://` reference file that `openv` reads, and `DYNAMO_OP_ENV_FILE` |
| `onepassword.ssh_agent` (true only on `primary`) | `private_dot_ssh/modify_private_config.tmpl`, `dot_gitconfig.tmpl` | 1Password `IdentityAgent` for `Host *` on macOS, and the rewrite of `https://github.com/` to SSH |
| `onepassword.signing_public_key` (prompted only on `primary`) | `dot_gitconfig.tmpl`, `private_dot_ssh/allowed_signers.tmpl` | Commit signing with `op-ssh-sign` |
| `onepassword.vault` | `dot_local/bin/executable_setup-secrets.tmpl` | The vault that `setup-secrets` checks |

## What fleet replaces

These functions work on enrolled machines without 1Password. On 2026-10-14 they were tested on mac-mini-m6 and spark-d after their `~/.ssh/provisioned` directories were deleted:

- Commit signing. `fleet agent install` writes `~/.ssh/fleet/gitconfig`. The `.gitconfig` includes it last, so it overrides the 1Password signing settings.
- SSH to GitHub. The fleet SSH configuration sends `github.com` to the fleet agent.
- SSH between fleet machines. Fleet SSH certificates authenticate the user. `StrictHostKeyChecking accept-new` trusts tailnet hosts on first use.

## What fleet does not replace yet

- Secrets for `openv`. `openv` and `openv-file` run `op run` only. The broker serves named secrets through `fleet secret get` and `fleet secret env`, but `openv` does not use them. The original design (`fleet-identity.md`, "Secrets") gives `openv` a broker backend.
- Fleet administration. `fleet login`, `fleet token`, `fleet invite`, and `fleet enroll-remote` read provisioner keys from 1Password. A machine that enrolls other machines still needs `op`.
- Machines that are not enrolled. nv-mbp-16 did not answer on port 22 on 2026-10-14, so it still runs without fleet.

## Plan

Each step is one commit with its tests.

### 1. Make the class an install setting only

- Limit the class to the install layout: `sudo` or `nosudo`.
- In `.chezmoi.yaml.tmpl`, read an existing value once: `primary` and `headless-sudo` become `sudo`, and `headless-nosudo` becomes `nosudo`. `bootstrap.sh` continues to force `nosudo` when it finds no usable `sudo`.
- Change each reader in the table above to the new values.
- Test: render the configuration template from each old value, and render the install script for each new value.

### 2. Make 1Password a separate setting

- Prompt for `onepassword.enabled` on its own, and keep the existing value at `chezmoi init`, as the vault does since commit `ea112eb`.
- The meaning of `onepassword.enabled` becomes "this machine has `op` and can administer the fleet".
- Derive nothing else from the class.
- Test: render the configuration template with and without an existing value.

### 3. Let fleet own Git signing and GitHub SSH

- Move the rewrite of `https://github.com/` to SSH into `~/.ssh/fleet/gitconfig`, which `fleet agent install` writes. Then an enrolled machine gets the rewrite without 1Password.
- Remove `onepassword.ssh_agent` and the signing block with `op-ssh-sign` from `dot_gitconfig.tmpl`, `private_dot_ssh/allowed_signers.tmpl`, and `private_dot_ssh/modify_private_config.tmpl`.
- Do this step only after every machine that signs commits is enrolled.
- Test: `test/test-fleet.py` checks the rewrite in the fleet gitconfig. A rendered `.gitconfig` contains no `op-ssh-sign`.

### 4. Give `openv` a broker backend

- If the machine is enrolled, `openv` runs `fleet secret env VAR=NAME... -- CMD`. If not, it runs `op run` as it does now.
- This step needs a map from each variable in `dev.env.op` to a broker secret name.
- Test: run `openv` with a stub `fleet` and with a stub `op`, and make sure that neither prints a value.

### 5. Remove what is left

- Remove `primary`, `headless-sudo`, and `headless-nosudo` from the template, the README, and the tests.
- Remove the provisioned-key path from `setup-secrets` and `git-signing-status` if no machine uses `provision-keys`.

## Open questions

- Open question: what is the new key name? `machine_class` with new values keeps every reader working by name. A new key such as `install` is clearer.
- Open question: which names does the broker policy serve? Step 4 needs one broker secret for each variable in `dev.env.op`. The policy is in the private deployment repository.
- Open question: does any machine still need 1Password signing without fleet? If nv-mbp-16 stays outside the fleet, step 3 waits.

## Addendum 2026-10-14: steps 1 and 2 done

The key name stays `machine_class`. Its values are now `sudo` and `nosudo`. `chezmoi init` converts `primary` and `headless-sudo` to `sudo`, and `headless-nosudo` to `nosudo`. Step 5 removes this conversion.

`onepassword.enabled` is now a separate question that `chezmoi init` asks once and then keeps. `onepassword.ssh_agent` keeps its existing value. On a new machine, it takes the value of `onepassword.enabled`. On Linux, the install script installs the 1Password CLI only when `onepassword.enabled` is set. `test/test-chezmoi-config.py` covers the conversion, the forced `nosudo` from `bootstrap.sh`, and the kept settings.

Steps 1 and 2 are in one commit, because both change the same lines of `.chezmoi.yaml.tmpl`.

## Addendum 2026-10-15: plan for step 4

### Current state

- `openv` and `openv-file` run only `op run --env-file` (`dot_local/bin/executable_openv`, `dot_local/bin/executable_openv-file`).
- `dev.env.op` has a line only when `onepassword.enabled` is true (`dot_config/dynamo/dev.env.op.tmpl:1`). The team data lists two variables: `hf_token` and `ngc_api_key` (`.chezmoidata/team.yaml:36`).
- `fleet secret env VAR=NAME... -- CMD` gets each value from the broker and then runs `CMD` (`dot_local/bin/executable_fleet:705`). If one secret fails, the command does not run.
- The broker policy (`fleet-config/broker/secrets.json`) serves only `claude-oauth-token` and `fleet-selftest`. It does not serve a Hugging Face token or an NGC key yet.

### Decisions in this plan

- The backend comes from `onepassword.enabled`, not from a test at run time. A machine with 1Password uses `op`. A machine without it uses the broker. This is the rule in `fleet-identity.md` ("Secrets").
- chezmoi renders one of two files. `dev.env.op` holds `op://` references and exists only when `onepassword.enabled` is true. A new `dev.env.fleet` holds `VAR=broker-name` lines and exists only when it is false. `openv` uses the file that exists.
- The map from variable to broker name is in `.chezmoidata/team.yaml`, next to `onepassword.secrets`. Broker names use hyphens: `hf_token` becomes `hf-token`, and `ngc_api_key` becomes `ngc-api-key`.
- `openv-file` stays on `op` only. Its input is a file of `op://` references, which the broker cannot read.

### Work in fleet-config (first)

1. Create the Secret Manager secrets `hf-token` and `ngc-api-key` in `ryanolson-fleet-ca`, in `us-west1`, as the fleet-config README says.
2. Copy each value from 1Password with a pipe: `op read 'op://...' | gcloud secrets versions add NAME --project ryanolson-fleet-ca --data-file=-`. Do not print a value.
3. Give the VM service account `secretAccessor` on each new secret.
4. Add both names to `broker/secrets.json` with `machines: ["*"]` and `admin_only: false`. Run `broker/install.sh` on ca-1.
5. On one enrolled machine, run `fleet secret get hf-token >/dev/null`. It must exit 0.

### Work in the dotfiles (second)

1. Write `test/test-openv.py` first. It puts a stub `fleet` and a stub `op` on `PATH`. The tests:
   - With only `dev.env.fleet`, `openv CMD` runs `fleet secret env HF_TOKEN=hf-token NGC_API_KEY=ngc-api-key -- CMD`.
   - With only `dev.env.op`, `openv CMD` runs `op run --env-file ... -- CMD`.
   - With no file, or with an empty file, `openv` stops with an error.
   - The output of `openv` does not contain a stub value.
   - The template renders `dev.env.fleet` only when `onepassword.enabled` is false.
2. Add `dot_config/dynamo/dev.env.fleet.tmpl` and the map in `team.yaml`. Add `.chezmoiignore` rules so that each machine gets only one of the two files.
3. Change `openv`. If `dev.env.op` exists, run `op run`. Else read the `VAR=NAME` pairs from `dev.env.fleet` and run `fleet secret env`.
4. Update the README, `ENVIRONMENT.md`, and the "Secrets" section of `fleet-identity.md`.
5. Apply on every machine. On one machine without 1Password, run `openv sh -c 'test -n "$HF_TOKEN"'`. It must exit 0.

### Open questions

- Open question: which NGC key does the broker serve? The team data points to `op://Development/NGC/credential`. The user instructions say that this legacy key gets 401 from the API gateway, and that `Development/NGC NIM Operations PAT` works. The local override in `credentials_*.yaml` was not read.
- Open question: must every machine read both secrets, or only some machine-name patterns?
