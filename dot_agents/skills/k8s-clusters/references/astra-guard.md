# astra-guard

`astra-guard` is the only way agents call the GitLab API of the Astra GitOps project. A merge to that project's `main` redeploys at once, and the project needs no approval and no green pipeline. The fleet GitLab token belongs to the owner, who can merge there. So the guard allows four writes only, and it merges only after the owner answers Approve to a `bb sgc ask` that names the merge request and both commits. Agents never call the GitLab API or `git push` with the token themselves. Agents run no `oc` or `kubectl` against Astra.

Every command prints one JSON object on stdout. A refusal exits 1 with `{"error": "...", "code": "..."}`. The code is stable. The message is for people.

## Commands

| Command | What it does |
|---|---|
| `get <api-path>` | One GET of `/api/v4<api-path>`, on any project. The path must be relative, with no `..`, no `//`, and no host. Prints `body` and `next_page`. |
| `push --branch <b> --base <main sha> --file <values path>=<local file>` | Reads `main`. It must still be `--base`. Checks the file against the diff rules, then makes one commit on `main` with that file's content, through the commits API. An existing branch is replaced, so a rebase is a new `push`. |
| `open --branch <b> --issue <ISSUE> --kind deploy\|teardown --title <t> [--expires YYYY-MM-DD]` | Opens a merge request from `b` into `main` of the configured project. A deploy needs `--expires`. The description holds `Kind:`, `Expires:` and `Issue:` lines. The guard records the merge request in its state. |
| `close --mr <iid>` | Closes a merge request that this guard opened. It sends only `state_event: close`. |
| `check --mr <iid> --namespace <ns>` | Reads the head and `main` once and runs every rule below on that pair. Prints the ask text. A failed check deletes the earlier check. |
| `ask --mr <iid>` | Checks that the head and `main` have not moved. Then runs `bb sgc ask` with the checked text, the options `Approve` and `Reject`, `--recommend none`, `--label astra` and `--issue`. Prints `ask_id`. Needs a bb thread. |
| `merge --mr <iid> --answer <bbq id>` | Merges only on an Approve for this merge request and this pair of commits. See "The merge". Needs a bb thread. |
| `teardown --deploy-mr <iid> --branch <b>` | For a deploy this guard merged: reads the current `main`, checks that it contains the landed deploy, applies the deploy's inverse patch, and pushes `b` as one commit on that `main`. Then run `open --kind teardown`. |

A branch is `wcep/<ISSUE>-<purpose>`, for example `wcep/TEAM-12-mocker-smoke`. `open` checks that the branch names the `--issue`.

## Writes

The guard sends these four writes and nothing else. `gate()` in the script refuses every other method, path or body before a request leaves, with `write_not_allowed`.

| Write | Body the gate accepts |
|---|---|
| `POST /projects/<id>/repository/commits` | A `wcep/*` branch, a full `start_sha`, and only `update` actions on one of the two values files, as text. No file mode. |
| `POST /projects/<id>/merge_requests` | A `wcep/*` source branch, target `main`, title and description. No other project. |
| `PUT /projects/<id>/merge_requests/<iid>` | Exactly `{"state_event": "close"}`. |
| `PUT /projects/<id>/merge_requests/<iid>/merge` | Exactly `{"sha": <head>}`. No `merge_when_pipeline_succeeds` and no `auto_merge`. |

The token cannot approve, rebase, comment, retarget, reopen, or set auto-merge through the guard. The guard uses the commits API, not `git push`. So no push option such as `merge_request.merge_when_pipeline_succeeds` can travel with a push, and no git configuration can send the token to another host.

## The check

`check` and `merge` run the same function on one pair of commits:

1. Read the merge request. It must be open, not a draft, from the recorded branch into `main`, in the configured project. Its `sha` is the head.
2. Read `main` once. That commit is the target.
3. The merge base of the target and the head must be the target. A branch behind `main` is refused (`behind_main`). So the merge result is the checked tree.
4. Compare the two commits' trees, level by level. The only change allowed is the content of one of `dynamo-workloads/values/pdx04-dev/values.yaml` and `dynamo-workloads/values/pdx04-stg/values.yaml`, with mode `100644` on both sides. Anything else is refused (`path_not_allowed`), and a change to both files is refused (`one_site`). The tree comparison does not depend on GitLab's diff, which can truncate.
5. Parse both versions of the file and apply the diff rules (below).
6. A deploy needs one `Expires: YYYY-MM-DD` line in the merge request description, not in the past.
7. In the latest pipeline whose `sha` is the head, the latest `helm:plan` and `validate:tests` jobs must each have status `success`. The project marks both jobs `allow_failure`, so the pipeline's own status proves nothing.
8. The `helm:plan` log must name each changed entry, and every one of our resources in it must be in `--namespace` (`rendered_namespace_mismatch`). A log in which the guard finds none of our resources fails closed (`rendered_namespace_unverified`).
9. The ArgoCD app's destination namespace, read from the file the config names, must equal `--namespace`. No configured source fails closed (`argocd_destination_unverified`).
10. Build the ask text. It must fit in the 4000 characters of `bb sgc ask` (`ask_too_long`).

### Diff rules

These are the pure functions in `scripts/astra_guard_rules.py`.

- An entry is an item of one of the top-level workload lists (`mockerRepros`, `vllmModels`, `sglangModels`, `trtllmModels`, `vllmDisaggModels`, `sglangDisaggModels`, `stockVllmModels`, `nimModels`) whose `name` starts with `wcep-`. A `wcep-*` item anywhere else, such as a secret list, is not an entry.
- With our entries removed, both parsed files must be equal (`outside_entries`). The guard compares parsed YAML, not lines, so an anchor in our entry that changes the value of an IT entry is refused.
- Duplicate keys and more than one YAML document are refused (`yaml_invalid`). PyYAML and Helm's parser would resolve them differently.
- Entry names are lower-case `wcep-<word>[-<word>...]` and never end in `-prd` (`entry_name`).
- On `pdx04-dev`, an added or changed entry has no field whose key contains `gpu` with a non-zero value (`dev_gpu`). `gpuType: B200` counts too.
- A deploy adds or changes at least one entry. A teardown only removes entries and changes nothing else (`teardown_not_removal`). A merge request that changes no entry is refused (`no_entry_change`).

### The inverse patch

For a deploy, the guard computes the inverse patch itself: the diff from the head's file back to the target's file, with three lines of context. The ask shows it. `teardown` applies it to the landed `main`, because the project squashes merges and a branch made earlier would remove nothing. Each hunk must match exactly one place in the landed file, or the guard refuses with `inverse_patch_conflict`. Then edit the teardown by hand with `push`, and the check still allows only removals.

## The ask

The ask text names, in this order: the merge request as `!<iid>` and its link, the head commit, the target commit, the window note, the kind, the cluster and site, the namespace, the entries with what happens to each, the GPU fields, and for a deploy the expiry and the inverse patch. When the namespace is in `shared_namespaces`, the ask says that it puts our entries in IT's shared namespace.

The window note says: GitLab's merge call checks the head but not `main`. A change to `main` in the last second before the merge is not caught until IT turns on fast-forward merges.

## The merge

`merge` refuses, in this order:

1. an answer id that is not `bbq_<letters and digits>` (`answer_id`), or no bb thread (`thread_required`);
2. a merge request this guard did not open (`not_guard_mr`), or one with no current check (`not_checked`);
3. a head or `main` that moved since the check (`head_moved`, `main_moved`). Push, check and ask again;
4. an answer that fails the answer-of contract (below);
5. any failure of the full check, run again on the same pair;
6. a merge GitLab refuses (`merge_failed`), for example 409 when the branch moved after the last read.

The merge sends the head as `sha`, so GitLab refuses it if the branch moved. Nothing refuses a move of `main` inside the merge call itself (the window above).

### The answer-of contract

The guard runs `bb sgc answer-of <id> --json`. It accepts the answer only when all of these hold:

- the command exits 0 and prints a JSON object (`answer_unknown`, `answer_invalid`);
- `status` is `answered` (`answer_not_answered`);
- `channel` is `bb` (`answer_channel`);
- `option.text` is exactly `Approve`. A free-text answer is not an approval, even when it says "approve" (`answer_not_approve`);
- `threadId` is the calling thread, `BB_THREAD_ID` (`answer_thread`);
- the question contains `!<iid>`, the full head commit and the full target commit, each as a whole token (`answer_mismatch`). So `!12` does not match `!123`, and a short commit prefix matches nothing.

The guard reads only that command's output. It never reads a Linear comment, because every agent posts in Linear as the owner. It never calls another `bb sgc` command except `ask`. It never answers a question, and it never opens the plugin's database. Any agent can call the plugin's answer RPC, and a caller can ask under another thread. So the check catches mistakes. It is not a boundary against a hostile agent.

### The approval log

Every merge and every refusal at merge time appends one JSON line to `${XDG_STATE_HOME:-~/.local/state}/astra-guard/approvals.jsonl`: `time`, `ask_id`, `answer` (`status`, `channel`, `option`, `answeredAt` as read, or `null`), `mr`, `head`, `target`, `result` (`merged` or `refused`), `code`, `reason` and `merge_commit`.

The output also holds `comment`, a signed text, and `comment_issue`. Post `comment` on that issue as it is. It names the ask, the merge request, both commits and the result. The steward compares these comments with the owner's answers.

## Config

`~/.config/k8s-clusters/astra.toml`, or `$ASTRA_GUARD_CONFIG`. It is written by hand and never committed, because it holds internal names. The values file paths and the deny rules are in the code, not the config. A made-up example:

```toml
gitlab_url = "https://gitlab.example.com"
project_id = 12345                       # the GitOps project
token_ref = "op://Vault/Item/credential" # fleet secret reference of the GitLab token
shared_namespaces = ["ns-shared"]        # IT's namespaces; the ask says so

[sites.pdx04-dev]
cluster = "dev-cluster-name"
argocd = { project = 678, file = "path/to/app.yaml", key = "spec.destination.namespace" }

[sites.pdx04-stg]
cluster = "stg-cluster-name"
argocd = { project = 678, file = "path/to/stg-app.yaml", key = "spec.destination.namespace", ref = "main" }
```

`gitlab_url` must use `https` (plain `http` works only on loopback, for tests). In `key`, a step into a list picks the item whose `name` equals the step.

## Token

The guard re-runs itself as `fleet secret env ASTRA_GUARD_TOKEN=<token_ref> -- python3 -I astra-guard ...`. It removes the token from its environment at once. It sends the token only in the `PRIVATE-TOKEN` header, which is not resent on a redirect, and it follows no redirects. Child processes (`bb`) do not get it. Output, errors, state files and the log pass through a filter that replaces the token. The token is never in an argument or a file.

## State

`${XDG_STATE_HOME:-~/.local/state}/astra-guard/state.json` records each merge request the guard opened: its branch, issue, kind, expiry, thread, last check (with the inverse patch), last ask and merge. `close`, `check`, `ask`, `merge` and `teardown` work only on merge requests in it. Every merge request is authored by the owner's token, so GitLab cannot tell which ones the guard opened.

## Not yet checked against the real project

The tests run against a fake GitLab. Three things wait for the first live reads:

- the `helm:plan` log format. The guard reads helm-diff lines (`<ns>, <name>, <Kind> (<group>) has been added:`). If the real log differs, the check fails closed with `rendered_namespace_unverified`;
- where the ArgoCD app's destination lives, which sets `argocd` in the config;
- that `fleet secret env` resolves `token_ref`, and that the token's scope allows the commits API.

## Tests

```bash
timeout 300 python3 test/test-astra-guard.py
```
