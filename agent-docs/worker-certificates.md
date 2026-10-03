# Worker client certificates

`bb-worker-cert` renews client certificates for an existing BB transport. It also supports a copy/paste exchange when the signing machine cannot reach the worker through SSH. It does not install BB, create a proxy, enroll a machine, or provision provider logins.

The tool requires `uv`. Its script metadata pins the Python cryptography dependency. The first run needs package-download access; subsequent runs can use the local cache. Worker request and installation require Linux, `findmnt`, and memory-backed storage. Signing works on Linux or macOS and requires the 1Password CLI. These operations do not need sudo.

## Trust and credentials

The worker generates a private key in memory-backed storage. The signing request and response contain no private keys. The signer reads all CA material in one `op inject` operation into process memory. No resolved template is written to disk. The script and dotfiles contain no credential values.

The response contains a client certificate and public CA certificates. Installation checks the CA fingerprint supplied by the trusted laptop, the certificate signature and lifetime, the worker key, and a signed binding to the pending request and server CA. Take the fingerprint from your own signing terminal. A fingerprint supplied by an untrusted sender provides no protection.

A client certificate authorizes whatever the server grants its issuing CA. This command does not narrow those server permissions. Protect the signing profile and select it deliberately. It issues client-only certificates and does not accept requested certificate extensions. The default lifetime is 45 days, bounded by the signing CA's expiry. Renewal does not revoke previously issued certificates.

1Password may reuse an existing authorization. The command does not guarantee a fresh biometric or MFA challenge. A separate approval mechanism remains pending.

The local proxy credential is separate from the certificate. BB sends it in `X-BB-Local-Transport` to authenticate to a local proxy. The proxy presents the client certificate to the server. A configured renewal restores this token from the worker's BB configuration into memory-backed storage. It never prints the token. Like other BB credentials, the source copy remains in BB's private data directory. It cannot isolate mutually untrusted processes running under the same OS account.

Memory-backed storage avoids an ordinary private-key file on persistent storage. The operating system may still swap or dump process memory. A reboot removes runtime files. Logout can also remove them when the user runtime directory is not retained. Certificates and BB registration can persist separately.

## Private signing profile

Create `~/.config/bb-worker-cert/signers/example.json` on the signing machine. Keep deployment addresses, machine names, and vault references outside this repository.

```json
{
  "client_ca_ref": "op://VAULT/CLIENT_CA/certificate",
  "client_key_ref": "op://VAULT/CLIENT_CA/private_key",
  "server_ca_ref": "op://VAULT/SERVER_CA/certificate",
  "days": 45
}
```

The referenced certificates must be current CA certificates. The client CA key must match its certificate. This version supports a directly issuing CA and one server trust CA certificate per profile.

## Direct SSH renewal

Install the dotfiles tool on both machines. Configure a worker profile as described below when renewing an existing transport.

```sh
bb-worker-cert renew worker-alias --profile example
bb-worker-cert renew user@worker.example --profile example --jump gateway-alias
```

SSH uses the normal user configuration, including `ProxyJump`. The optional `--jump` supplies SSH's `-J` option. The tool does not enable agent forwarding. It invokes `~/.local/bin/bb-worker-cert` on the target; `uv` must be on the remote command's PATH.

Use `--name worker-name` to override the certificate common name. Otherwise, the worker uses its hostname. Names and profile labels accept letters, digits, dots, underscores, and hyphens, with a maximum length of 64 characters. A pending request retains its name and key until installation completes.

## Copy/paste exchange

On the worker:

```sh
bb-worker-cert request --profile example
```

Copy the complete REQUEST block. The command prints the next command to run on your laptop:

```sh
bb-worker-cert sign --profile example
```

Paste the REQUEST block, including its END line, then press Enter. No end-of-file keystroke is needed. The laptop prints a RESPONSE block and an install command containing the signing CA fingerprint.

On the same worker, run the printed install command:

```sh
bb-worker-cert install --profile example --issuer-sha256 FINGERPRINT_FROM_YOUR_LAPTOP
```

Paste the RESPONSE block and press Enter after its END line. The worker must still have its pending private key. After a reboot, generate a new request and sign it again. Repeating `request` before installation prints the same pending request. To discard an unused request, remove that profile's `request.json` and `pending.key` from its runtime directory while no certificate command is running.

The laptop does not need an SSH route to the worker for this exchange. The worker still needs a route to its BB server when it connects. Certificate delivery does not establish a network tunnel through a cluster login node.

## Worker profile and activation

Without a worker profile, installation writes public certificates under `~/.config/bb-worker-cert/certificates/PROFILE/` and the private key under the profile's memory-backed runtime directory. It prints the paths. No services are started. This mode supports a separate container launcher or transport setup.

For an existing transport, create `~/.config/bb-worker-cert/workers/example.json` on the worker:

```json
{
  "cert_file": "~/.config/bb-transport/client.crt",
  "ca_file": "~/.config/bb-transport/server-ca.crt",
  "key_file": "/run/user/1000/bb-transport/client.key",
  "token_file": "/run/user/1000/bb-transport/token",
  "bb_config_file": "~/.local/share/bb-worker/config.json",
  "services": ["bb-transport.service", "bb-host-daemon-example.service"]
}
```

Replace the example UID, paths, and service names with local values. The public certificate directories must already exist. Private-key and token directories must be owned by the current user, have mode 0700, and reside on tmpfs or ramfs. The command rejects symlink destinations.

`bb_config_file` is optional. When present, installation restores the proxy token from `serverHeaders.X-BB-Local-Transport`. It then checks the daemon's `/status` response using the adjacent `host-daemon-port` and `host-id` files. Success requires the expected host ID and `connected: true` within 30 seconds. Service activation uses `systemctl --user`. Omit `services` for a launcher that manages its own processes.

The tool validates a response before changing active credentials. If activation fails after installation, the new certificate remains installed and the pending request remains available for a retry. Check the service logs, fix the activation problem, and repeat installation with the same response.

## Verification

```sh
timeout 300 uv run --with cryptography==49.0.0 python test/test-worker-cert.py
```
