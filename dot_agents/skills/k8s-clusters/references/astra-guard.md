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
| `check --mr <iid> --namespace <ns>` | Reads the head and `main` once and runs every rule below on that pair. Prints the ask text. A failed check deletes the earlier check. It deletes the earlier check before any validation. `<ns>` must be a namespace name (`namespace_invalid`). A merge request this guard merged is refused, and its merge record keeps the inverse patch. |
| `ask --mr <iid>` | Checks that the head and `main` have not moved. Then runs `bb sgc ask` with the checked text, the options `Approve` and `Reject`, `--recommend none`, `--label astra` and `--issue`. Prints `ask_id`. Needs a bb thread. |
| `merge --mr <iid> --answer <bbq id>` | Merges only on an Approve for this merge request and this pair of commits. See "The merge". Needs a bb thread. |
| `teardown --deploy-mr <iid> --branch <b>` | For a deploy this guard checked: GitLab must show the merge request merged, with `sha` equal to the checked head. It takes the landed commit from GitLab's `merge_commit_sha`, checks that the current `main` contains it, applies the inverse patch saved at check, and pushes `b` as one commit on that `main`. Then run `open --kind teardown`. The local merge record is not needed. |

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
7. In the latest pipeline whose `sha` is the head, the latest `helm:plan` and `validate:tests` jobs, from every page of jobs, must each have status `success`. The project marks both jobs `allow_failure`, so the pipeline's own status proves nothing.
8. The `helm:plan` log must name each changed entry. Every resource in it must be in `--namespace`, whatever its name, so a cluster-scoped resource (empty namespace) is refused (`rendered_namespace_mismatch`). The plan diffs the merge request's base and head, so each resource in it is one this merge request changes. A log in which the guard finds none of our resources fails closed (`rendered_namespace_unverified`). So does any line shaped like a resource header (`<x>, <y>, <Kind> (`) or naming an action (`has been added`, `has changed`, `has been removed`) that does not parse completely.
9. The ArgoCD app's destination namespace, read from the file the config names, must equal `--namespace`. No configured source fails closed (`argocd_destination_unverified`).
10. Build the ask text. It must fit in the 4000 characters of `bb sgc ask` (`ask_too_long`).

### Diff rules

These are the pure functions in `scripts/astra_guard_rules.py`.

- An entry is an item of one of the top-level workload lists (`mockerRepros`, `vllmModels`, `sglangModels`, `trtllmModels`, `vllmDisaggModels`, `sglangDisaggModels`, `stockVllmModels`, `nimModels`) whose `name` starts with `wcep-`. A `wcep-*` item anywhere else, such as a secret list, is not an entry.
- With our entries removed, both parsed files must be equal, types included, so `true` never equals `1` (`outside_entries`). The guard compares parsed YAML, not lines, so an anchor in our entry that changes the value of an IT entry is refused.
- Duplicate keys and more than one YAML document are refused (`yaml_invalid`). PyYAML and Helm's parser would resolve them differently.
- Values are JSON-shaped: mappings with string keys, lists, strings, numbers, booleans and null. A set, a timestamp, binary data or a non-string key is refused at load (`yaml_invalid`). Quote a date to keep it.
- Every entry the change adds, changes or removes has a lower-case `wcep-<word>[-<word>...]` name that does not end in `-prd` (`entry_name`).
- On `pdx04-dev`, an added or changed entry has no non-zero field whose key names an accelerator: a key that contains `gpu` or `mig-`, or a resource under `nvidia.com/`, `amd.com/`, `gpu.intel.com/` or `habana.ai/` (`dev_gpu`). `gpuType: B200` counts too.
- A deploy only adds entries (`deploy_not_addition`). To change an entry, tear it down and deploy it again, each with its own ask. A teardown only removes entries (`teardown_not_removal`). A merge request that changes no entry is refused (`no_entry_change`). `push` accepts either an add-only or a remove-only file. `check` holds it to the kind recorded at `open`.

### The inverse patch

For a deploy, the guard computes the inverse patch itself: the diff from the head's file back to the target's file, with three lines of context. A deploy only adds entries, so the inverse patch only removes them. The ask shows it. `teardown` applies it to the landed `main`, because the project squashes merges and a branch made earlier would remove nothing. Each hunk must match exactly one place in the landed file, or the guard refuses with `inverse_patch_conflict`. Then edit the teardown by hand with `push`, and the check still allows only removals.

## The ask

The ask text names, in this order: the merge request as `!<iid>` and its link, the head commit, the target commit, the window note, the kind, the cluster and site, the namespace, the entries with what happens to each, the GPU fields, and for a deploy the expiry and the inverse patch. When the namespace is in `shared_namespaces`, the ask says that it puts our entries in IT's shared namespace.

The window note says: GitLab's merge call checks the head but not `main`. A change to `main` in the last second before the merge is not caught until IT turns on fast-forward merges.

## The merge

`merge` refuses, in this order:

1. an approval log it cannot open (`log_unusable`), an answer id that is not `bbq_<letters and digits>` (`answer_id`), or no bb thread (`thread_required`);
2. a merge request this guard did not open (`not_guard_mr`), or one with no current check (`not_checked`);
3. a head or `main` that moved since the check (`head_moved`, `main_moved`). Push, check and ask again;
4. an answer that fails the answer-of contract (below);
5. any failure of the full check, run again on the same pair;
6. an attempt line it cannot write to the log (`log_unusable`). Nothing is merged;
7. a merge GitLab refuses (`merge_failed`), for example 409 when the branch moved after the last read. When the merge call fails without an answer from GitLab, such as a cut connection, the result is `merge_unknown`: read the merge request before doing anything else.

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

The log is `${XDG_STATE_HOME:-~/.local/state}/astra-guard/approvals.jsonl`, one JSON object per line: `time`, `ask_id`, `answer` (`status`, `channel`, `option`, `answeredAt` as read, or `null`), `mr`, `head`, `target`, `result`, `code`, `reason` and `merge_commit`.

- A merge writes two lines. Before the merge call it writes and fsyncs an `attempt` line. After the call it writes the result: `merged`, or `refused` with the code.
- A refusal before the attempt writes one `refused` line. This includes unexpected errors and refusals before any GitLab call, such as a bad config or a failed `fleet`.
- No `attempt` line means no merge call was made. An `attempt` with no result line after it means the outcome is unknown: read the merge request in GitLab.

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

The guard runs `fleet secret env ASTRA_GUARD_TOKEN=<token_ref> -- python3 -I -c <handover>` as a child. The handover writes the variable to a pipe the guard passed down (its number is in `ASTRA_GUARD_TOKEN_FD`), and the guard reads the token from that pipe. The guard is never replaced by `fleet`, so a `fleet` failure is refused (`fleet_failed`, `fleet_missing`, `token_missing`) and, for a merge, logged in the same process. It sends the token only in the `PRIVATE-TOKEN` header, which is not resent on a redirect, and it follows no redirects. Child processes (`bb`) do not get it. Every place data leaves the guard checks its final text, raw and URL-decoded: `state.json`, `approvals.jsonl`, the argv of `bb`, and stdout (`secret_at_sink`). A state save or a `bb` call that would carry the token is refused and writes nothing. A log line that would carry it is written without the field. Output that would carry it is replaced by the refusal. Messages never include a GitLab error body. The guard also refuses a GitLab response that holds the token (`secret_in_response`). A commit is a fifth way out: `push` and `teardown` scan the file raw, URL-decoded, and as YAML, every key and string raw and URL-decoded, and refuse a file that holds the token (`secret_in_content`).

## Five kinds, five rules

Three review passes found defects of the same five kinds. Each fix closed one input path and the next pass found another. Each kind now has one rule at the point every path goes through. A fresh pass after the first rules found three of the kinds again; the second half of each note says how the first rule missed.

- **Value comparison.** Rule: values are JSON-shaped, checked once at load. A type-aware comparison was patched for `true` and `1`, and then a YAML set compared its members loosely again. Every new YAML type brings its own equality, so the guard admits none.
- **The token.** Rule: check the four sinks, on their final text. Redacting responses changed the text the checks compare. Checking responses missed JSON escapes, then YAML escapes, then a URL-encoded error body. Every decoder is a new way in, but there are only four ways out.
- **Plan parsing.** Rule: a line shaped like a resource header parses completely, or the check refuses. Then every resource the plan names is in the asked namespace, whatever its name. Accepting an empty namespace, and then lines that end in `:`, each left another clipped or odd header to be skipped silently. The first rule fixed parsing, but the namespace check still looked only at resources named `wcep-*`, so a fully parsed helper resource elsewhere passed.
- **Merge logging.** Rule: write-ahead. An fsynced `attempt` line goes to the log before the merge call, and the result line after it, in the one process that makes every call. Catching more errors after the merge could not help when the log itself could not be written. The first rule only opened the log first: a log that opens but cannot be written (a full disk) still let the merge through. And the guard replaced itself with `fleet`, so a `fleet` failure ran no guard code at all.
- **Teardown data.** Rule: a deploy only adds entries, so every inverse patch is a pure removal. Teardown proves the merge from GitLab, with the inverse patch saved at check. Keeping the patch safe did not help when the patch for a changed entry restored it, which a teardown may not do. The first rule made the patch safe but still read it from the local merge record, which a lost merge reply never wrote.

## State

`${XDG_STATE_HOME:-~/.local/state}/astra-guard/state.json` records each merge request the guard opened: its branch, issue, kind, expiry, thread, last check (with the inverse patch), last ask and merge. `close`, `check`, `ask`, `merge` and `teardown` work only on merge requests in it. Every merge request is authored by the owner's token, so GitLab cannot tell which ones the guard opened.

## Open findings

The last strict review pass found this one, of a kind the rules above were meant to close. Its test is marked `expectedFailure` in `RoundTwoReview`. The guard goes to its first live run with it open:

- **Value comparison.** PyYAML reads YAML 1.1, where `1e3` is a string. Helm reads it as the number 1000. So a change outside our entries from `1e3` to `"1e3"` compares equal and passes the check. The JSON-shape rule checks types after PyYAML has already read the value wrongly. A fix needs a YAML 1.2 reading of numbers.

## Not yet checked against the real project

The tests run against a fake GitLab. Three things wait for the first live reads:

- the `helm:plan` log format. The guard reads helm-diff lines (`<ns>, <name>, <Kind> (<group>) has been added:`). If the real log differs, the check fails closed with `rendered_namespace_unverified`;
- where the ArgoCD app's destination lives, which sets `argocd` in the config;
- that `fleet secret env` resolves `token_ref` and keeps the inherited pipe open for its command (if it closes it, the guard fails closed with `token_missing`), and that the token's scope allows the commits API.

## Tests

```bash
timeout 600 python3 test/test-astra-guard.py
```
