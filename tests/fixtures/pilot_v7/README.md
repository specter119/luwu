# M6 public Pi and Codex fixtures

These fixtures use synthetic public values and the local application field
names selected for the M6 pilot:

- Pi JSON: `defaultModel`, `defaultProvider`, and `lastChangelogVersion`, at
  `$HOME/.pi/agent/settings.json`.
- Codex TOML: `model` and `model_provider`, at `$HOME/.codex/config.toml`.

Each fixture is a complete version 7 manifest tree. `home/` contains `repo/`
with type-safe `tojson` templates, `inputs/` with declared global and local
values, and the required target parent directories. The baseline and
application targets begin absent. Tracked `.keep` files preserve empty target
parents and baseline directories in Git. The values and comments in these
fixtures are synthetic and public; no credentials are included.

## Copy and exercise in an isolated HOME

From the repository root, build and install this checkout's wheel into a
separate environment before running the examples. Python 3.13 was used for
the recorded acceptance; the package requires Python 3.12 or later.

```sh
uv build --wheel
trial_env="$(mktemp -d /tmp/luwu-m6-env.XXXXXX)"
uv venv --python 3.13 "$trial_env"
uv pip install --python "$trial_env/bin/python" dist/luwu-0.1.0-py3-none-any.whl
export PATH="$trial_env/bin:$PATH"
luwu --version
```

This installs into the disposable environment, leaving any existing Luwu
installation intact. Keep that environment selected for every command below.

The commands below run in Bash. Each preview is expected to return exit code 2.
`expect_preview` handles that status inside an `if`, so the snippets also work
with `set -e`; an unexpected status still stops the script. Baseline acceptance
and every write are previewed before their separate `--yes` confirmation.

### Pi

```bash
set -euo pipefail
expect_preview() {
  if "$@"; then status=0; else status=$?; fi
  if [ "$status" -ne 2 ]; then
    printf 'expected preview exit code 2; got %s\n' "$status" >&2
    return 1
  fi
}

trial_root="$(mktemp -d /tmp/luwu-m6-pi.XXXXXX)"
cp -a tests/fixtures/pilot_v7/pi/. "$trial_root/"
manifest="$trial_root/luwu.toml"
home="$trial_root/home"

HOME="$home" luwu inspect --manifest "$manifest" --json
expect_preview env HOME="$home" luwu accept --manifest "$manifest" --resource pi --from desired --field defaultModel --field defaultProvider --field lastChangelogVersion --json
HOME="$home" luwu accept --manifest "$manifest" --resource pi --from desired --field defaultModel --field defaultProvider --field lastChangelogVersion --yes --json
expect_preview env HOME="$home" luwu apply --manifest "$manifest" --json
HOME="$home" luwu apply --manifest "$manifest" --yes --json
HOME="$home" luwu plan --manifest "$manifest" --json

# Simulate a public application-side choice by editing only the target file.
python3 - "$home/.pi/agent/settings.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
document = json.loads(path.read_text(encoding="utf-8"))
document["defaultProvider"] = "synthetic-pi-provider-app"
path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
HOME="$home" luwu plan --manifest "$manifest" --json
expect_preview env HOME="$home" luwu reverse-sync --manifest "$manifest" --resource pi --field defaultProvider --json
HOME="$home" luwu reverse-sync --manifest "$manifest" --resource pi --field defaultProvider --yes --json
expect_preview env HOME="$home" luwu accept --manifest "$manifest" --resource pi --from live --field defaultProvider --json
HOME="$home" luwu accept --manifest "$manifest" --resource pi --from live --field defaultProvider --yes --json
HOME="$home" luwu plan --manifest "$manifest" --json

# Edit the source-owned global model input by replacing its one known token.
python3 - "$home/inputs/global/pi.json" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
before = path.read_bytes()
old = b'"synthetic-pi-model-v1"'
new = b'"synthetic-pi-model-trial"'
assert before.count(old) == 1, "expected one synthetic model token"
path.write_bytes(before.replace(old, new, 1))
PY
HOME="$home" luwu plan --manifest "$manifest" --json
expect_preview env HOME="$home" luwu apply --manifest "$manifest" --json
HOME="$home" luwu apply --manifest "$manifest" --yes --json
python3 - "$home/.pi/agent/settings.json" <<'PY'
import json
import sys
from pathlib import Path

document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert document["defaultModel"] == "synthetic-pi-model-trial"
assert document["defaultProvider"] == "synthetic-pi-provider-app"
PY
expect_preview env HOME="$home" luwu accept --manifest "$manifest" --resource pi --from desired --field defaultModel --json
HOME="$home" luwu accept --manifest "$manifest" --resource pi --from desired --field defaultModel --yes --json
HOME="$home" luwu plan --manifest "$manifest" --json
noop_result="$(HOME="$home" luwu apply --manifest "$manifest" --yes --json)"
printf '%s\n' "$noop_result" | python3 -c 'import json, sys; assert json.load(sys.stdin)["outcome"] == "no_changes"'
printf 'Fixture remains at %s\n' "$trial_root"
```

### Codex

```bash
set -euo pipefail
expect_preview() {
  if "$@"; then status=0; else status=$?; fi
  if [ "$status" -ne 2 ]; then
    printf 'expected preview exit code 2; got %s\n' "$status" >&2
    return 1
  fi
}

trial_root="$(mktemp -d /tmp/luwu-m6-codex.XXXXXX)"
cp -a tests/fixtures/pilot_v7/codex/. "$trial_root/"
manifest="$trial_root/luwu.toml"
home="$trial_root/home"

HOME="$home" luwu inspect --manifest "$manifest" --json
expect_preview env HOME="$home" luwu accept --manifest "$manifest" --resource codex --from desired --field model --field model_provider --json
HOME="$home" luwu accept --manifest "$manifest" --resource codex --from desired --field model --field model_provider --yes --json
expect_preview env HOME="$home" luwu apply --manifest "$manifest" --json
HOME="$home" luwu apply --manifest "$manifest" --yes --json
HOME="$home" luwu plan --manifest "$manifest" --json

# Simulate a public application-side choice by editing only the target file.
python3 - "$home/.codex/config.toml" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
before = path.read_bytes()
old = b'"synthetic-codex-provider-v1"'
new = b'"synthetic-codex-provider-app"'
assert before.count(old) == 1, "expected one synthetic provider token"
path.write_bytes(before.replace(old, new, 1))
PY
HOME="$home" luwu plan --manifest "$manifest" --json
expect_preview env HOME="$home" luwu reverse-sync --manifest "$manifest" --resource codex --field model_provider --json
HOME="$home" luwu reverse-sync --manifest "$manifest" --resource codex --field model_provider --yes --json
expect_preview env HOME="$home" luwu accept --manifest "$manifest" --resource codex --from live --field model_provider --json
HOME="$home" luwu accept --manifest "$manifest" --resource codex --from live --field model_provider --yes --json
HOME="$home" luwu plan --manifest "$manifest" --json

# Edit the source-owned global model input by replacing its one known token.
python3 - "$home/inputs/global/codex.toml" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
before = path.read_bytes()
old = b'"synthetic-codex-model-v1"'
new = b'"synthetic-codex-model-trial"'
assert before.count(old) == 1, "expected one synthetic model token"
path.write_bytes(before.replace(old, new, 1))
PY
HOME="$home" luwu plan --manifest "$manifest" --json
expect_preview env HOME="$home" luwu apply --manifest "$manifest" --json
HOME="$home" luwu apply --manifest "$manifest" --yes --json
python3 - "$home/.codex/config.toml" <<'PY'
import sys
import tomllib
from pathlib import Path

document = tomllib.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert document["model"] == "synthetic-codex-model-trial"
assert document["model_provider"] == "synthetic-codex-provider-app"
PY
expect_preview env HOME="$home" luwu accept --manifest "$manifest" --resource codex --from desired --field model --json
HOME="$home" luwu accept --manifest "$manifest" --resource codex --from desired --field model --yes --json
HOME="$home" luwu plan --manifest "$manifest" --json
noop_result="$(HOME="$home" luwu apply --manifest "$manifest" --yes --json)"
printf '%s\n' "$noop_result" | python3 -c 'import json, sys; assert json.load(sys.stdin)["outcome"] == "no_changes"'
printf 'Fixture remains at %s\n' "$trial_root"
```

### Read-only recovery observation

If a confirmed command reports an uncertain write, do not replay it or advance
the baseline automatically. Re-read the current state with these commands,
using the `trial_root`, `manifest`, and `home` from either format's snippet:

```bash
run_observation() {
  if "$@"; then status=0; else status=$?; fi
  case "$status" in
    0|2) ;;
    *) printf 'observation failed with exit code %s\n' "$status" >&2; return "$status" ;;
  esac
}

run_observation env HOME="$home" luwu inspect --manifest "$manifest" --json
run_observation env HOME="$home" luwu plan --manifest "$manifest" --json
```

A fresh no-op is evidence about the state observed now; it does not resolve the
historical durability uncertainty of the earlier write. Make an explicit
decision after reviewing the fresh observation. The [version 7 recovery
contract](../../../docs/reference.md#pilot-extension-manifest-version-7)
defines this boundary.

These commands operate only on each copied fixture HOME. They do not prove
that Pi or Codex read the file. The rehearsal simulates Dotter operator and
fixture state; it does not call real Dotter or edit Dotter configuration. A
real target requires the per-target operator handoff and permission checks in
the [maintenance workflow](../../../docs/maintenance.md#isolated-rehearsal-and-dotter-handoff).

## Feedback checklist

Report observations for each format without attaching live configuration or
secret-bearing files:

- Setup friction and the time taken for inspect, plan, and apply.
- Whether ownership labels and conflict reasons made the next safe step clear.
- Whether the selected live-owned change survived source-owned redeployment.
- Whether unrelated bytes and comments were preserved or unsupported syntax
  produced a useful review stop.
- Whether stale inputs, conflicts, and the post-write uncertainty result made
  recovery steps clear.
- High-value Pi or Codex fields missing from this bounded pilot.

Redacted command outcomes, field labels, and elapsed times are sufficient.
