# Provider configuration

RayChat needs three values before it can reach a provider: a credential, a model
identifier, and an API root. Each is taken from its environment variable when
exported, and otherwise from a stored configuration the operator selects.

```
1. RAYCHAT_AUTH_TOKEN / RAYCHAT_MODEL / RAYCHAT_BASE_URL
2. the selected stored configuration
3. the interactive prompt, which writes one
```

An exported variable is never replaced. A shell, a CI job or a wrapper script
therefore keeps complete control, and nothing stored on disk can redirect a
session that already states where it is going. The values collapse into the
process environment once, before anything else starts, so plugins, workers and
subagents inherit the same single immutable identity they always have. What a
stored configuration changes is only where a missing value may come from, never
the ability to change one afterwards.

## First launch

A launch that finds no usable configuration, with a terminal on both ends, asks
for one:

```
RayChat is not configured yet.
Server URL (the API root, ending in /v1): https://provider.example/v1
API token: ********
  Asking the endpoint which models this credential can use...
  Starting with vendor/model-c. Use /models to change it.
Context role for instructions (system, developer, user) [system]: user
Name for this configuration: work
```

The token is read without echo and never appears in the transcript. Each answer
is checked with the rule its exported variable obeys, so a typo is refused while
the operator is present rather than at the first request. A credential the
endpoint refuses is reported as refused and can be entered again; it is not
reported as a provider without a catalog, because the two are indistinguishable
at the transport and only one of them means something is wrong.

No model is asked for. The catalog is only known once the endpoint answers, and
choosing from it belongs in the application where the whole list is visible, so
the last advertised model starts the session and `/models` replaces it. A
provider that advertises nothing leaves nothing to adopt, so that case asks.

Nothing here runs without a terminal on both ends. A pipeline, a CI job, an
`--exec` run and `--help` behave exactly as they did before stored
configurations existed.

## Later launches

Every launch lists the stored configurations and asks which to use:

```
Which configuration should this session use?
   1. work  [vendor/model-c] (current)
   2. lab   [vendor/model-a]
   3. Add a new configuration
Choose 1-3 [1]:
```

The list appears even with one configuration, so the identity a session runs
under is stated rather than assumed, and the one used last is marked and taken
by pressing Enter. Adding another is always offered: once any configuration is
stored the environment resolves, so the first-launch prompt never runs again.

A configuration that cannot be read is listed with its reason rather than
hidden, because silently omitting one the operator created is worse than showing
that it needs attention.

`--profile NAME` selects one outright, without asking, and records it for later
launches. `RAYCHAT_IGNORE_PROFILES` refuses stored configurations entirely, for
scripted runs that must depend on the environment alone.

## What is stored

Configurations live under the operator's RayChat home, one pair of files each,
created with the directory and file modes already configured in `raychat.json`
(`0700` and `0600`):

```
~/.raychat/
  user_info.json              names the selected configuration
  profiles/work.json          the configuration itself
  profiles/work.env           the same values, for a shell to source
  catalogs/work.json          the models this endpoint last advertised
```

`profiles/work.json` is the authority. The `.env` beside it is regenerated from
it on every save, so the two cannot drift; a hand edit to the `.env` is replaced
rather than obeyed. Load it in a shell with `set -a; . ./work.env; set +a`.
Values are written with POSIX single-quote escaping, so a credential containing
a quote or a command substitution survives verbatim instead of sourcing as
empty or running.

A configuration records the provider identity, the role instructions must carry,
and any further variables that deployment needs:

```json
{
  "schema_version": 1,
  "nickname": "work",
  "base_url": "https://provider.example/v1",
  "model": "vendor/model-c",
  "auth_token": "...",
  "instruction_role": "user",
  "environment": {"HTTPS_PROXY": "http://proxy.example:3128"}
}
```

Every field except the nickname is optional, so a configuration recording only a
chosen model stays valid and a value supplied by the environment needs no copy.
The three identity variables are refused inside `environment`: they are already
fields, and a second definition would leave no answer to which one applies.

`instruction_role` exists because some providers reject a system role and
require the same text as a user message. That is a property of the provider
rather than a preference, so it belongs beside the endpoint and the credential.
It supplies the `--instruction-role` default, which an exported value still
overrides.

## Changing a configuration

`/models` reads the catalog again, records it beside the configuration, and
reports how it differs from the one stored before, so a provider adding or
dropping models is visible rather than silent. Selecting a model stores it on
the active configuration.

`/profile` lists the stored configurations and selects one.

Neither re-points the running session. The resolved identity is immutable for
the workers and subagents already holding it, so both take effect at the next
launch, where the startup list shows the new choice already marked current. A
selection that an exported variable would override is refused with the reason
instead of being written somewhere precedence guarantees will ignore it.

## The credential

The token is stored in plaintext at `0600`, in both the configuration and its
`.env`. RayChat has no third-party runtime dependencies, so an OS keyring is not
available to it.

The token is kept out of `repr`, logs, diagnostics and error messages. Provider
diagnostics report each variable as set, missing or empty without its value, and
setup output never echoes what was typed. `--debug` remains the deliberate
exception: it writes unredacted Authorization headers to the HTTP debug
directory.

A configuration that is a symbolic link is refused. The owner-only mode
describes the file RayChat wrote, not a target it points at.

## Testing against a local provider

`tools/provider_stub.py` serves an OpenAI-compatible endpoint from the standard
library, so provider behaviour can be exercised against a real socket without a
credential or any traffic leaving the machine:

```bash
python3 -B -S -m tools.provider_stub --models "vendor/a,vendor/b"
```

It prints the values to configure. `--hide-catalog` answers 404 for the model
catalog, imitating the many compatible servers that implement chat without one.
Replies finish an agent turn by default; `--prose` answers plain text instead,
for exercising the transport alone.
