# Synthetic profile acceptance fixture

This fixture exercises manifest v8 profiles, package dependencies, exclusions,
layered public inputs, nested JSONC and TOML fields, whole-file copies, and
directory expansion. All values and schemas are synthetic. In particular, the
`mihomo` package is only an exclusion sentinel; these files do not claim
compatibility with Mihomo, Codex, OpenCode, Droid, or another application.

The `expected/` files are independent byte oracles for rendered template
resources. `repo/assets/` and `repo/copies/` are independently checked as
literal-byte inputs. The fixture deliberately includes comments and an output
sentinel value so acceptance can check byte preservation and metadata-only CLI
output.

The WSL exclusion case keeps the `mihomo` declaration schema-valid and excludes
it through the profile. The checked-in fixture stores ordinary public
synthetic sentinels at the excluded source, target, and exclusive-input paths.
The acceptance harness replaces those three files with broken symlinks only in
its disposable copy, preserving their exact link targets while checking that
the CLI performs no stat, open, or walk on them. The isolated CLI rehearsal
below uses the ordinary sentinels as copied and does not create links or touch
the real home directory.

The ordinary settings reverse loop uses a private `settings_theme` input leaf.
The `shared_consumer` profile has a separate `settings_shared` resource and a
nested TOML consumer of the shared `theme` binding; reversing only the settings
resource must block while the nested consumer remains unchanged.

The `official_shapes` profile contains synthetic OpenCode v1.19.0 JSONC and
Codex v0.162.1 TOML shapes, plus Droid 0.240.0 settings. It covers the
declared OpenCode `$schema`, model, autoupdate, and server fields; a literal
Codex project path and hook ID with a complete synthetic `trusted_hash`; and
Droid `sessionDefaultSettings.interactionMode` / `autonomyLevel`. The fixture
drives Luwu's CLI only: it does not launch an application, execute a hook, or
calculate or grant trust. These mappings are fixed-version shape tests, not
runtime compatibility evidence. Droid settings coverage does not define or
claim workspace trust; that public file schema remains unknown and is omitted.

The `finite_batch` profile has three independent resources with separate input
files and targets. Its expected byte files support checks for a future
resource changing after an earlier write, and for a planned noop target
changing before final verification.

## Isolated CLI rehearsal

Run this Bash block from the repository root in a Python environment with this
checkout's dependencies installed. `cp -a` preserves the fixture's valid alias
symlink; its three excluded-path sentinels remain ordinary files in this guide.
The broken-link attack sentinels are created only by `ProfileCliCase` in `tests/support/profile_cli.py` in disposable
acceptance copies. The block runs in a subshell; each CLI
process receives the temporary `HOME`, while the caller's environment and
directory are restored when it exits. Every CLI call uses the copied manifest
and the `synthetic` profile; its declared target is the copied
`project/home`. All data is public and synthetic, so this drill does not read
or write real home configuration. See the [version 8 reference](../../../docs/reference.md#full-dotfiles-coverage-manifest-version-8)
and [current implementation status](../../../docs/status.md) for the contract
and current gate.

The preview commands below expect exit code `2`; the inline check lets the
block continue only for that expected preview result. Keep the printed
temporary directory until you finish inspecting it.

```bash
(
set -eu

GUIDE_REPO="$(pwd -P)"
GUIDE_TMP="$(mktemp -d /tmp/luwu-guide.XXXXXX)"
GUIDE_PROJECT="$GUIDE_TMP/project"
GUIDE_HOME="$GUIDE_TMP/home"
mkdir -p "$GUIDE_PROJECT" "$GUIDE_HOME"
cp -a "$GUIDE_REPO/tests/fixtures/dotfiles_profile/." "$GUIDE_PROJECT/"
GUIDE_PYTHONPATH="$GUIDE_REPO/src${PYTHONPATH:+:$PYTHONPATH}"
cd "$GUIDE_PROJECT"
GUIDE_MANIFEST="$GUIDE_PROJECT/luwu.toml"

luwu() {
  env HOME="$GUIDE_HOME" \
    PYTHONPATH="$GUIDE_PYTHONPATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    python -B -m luwu "$@"
}
luwu_cli() {
  local subcommand="$1"
  shift
  luwu "$subcommand" "$@" --manifest "$GUIDE_MANIFEST" --profile synthetic --json
}

luwu_cli inspect
luwu_cli plan

luwu_cli accept --resource settings --from desired --field theme --field brand || test "$?" -eq 2
luwu_cli accept --resource settings --from desired --field theme --field brand --yes
luwu_cli accept --resource nested_toml --from desired --field theme --field worker_name || test "$?" -eq 2
luwu_cli accept --resource nested_toml --from desired --field theme --field worker_name --yes

luwu_cli adopt --resource copy_profile || test "$?" -eq 2
luwu_cli adopt --resource copy_profile --yes
luwu_cli adopt --resource assets:defaults.conf || test "$?" -eq 2
luwu_cli adopt --resource assets:defaults.conf --yes
luwu_cli adopt --resource assets:nested/keymap.jsonc || test "$?" -eq 2
luwu_cli adopt --resource assets:nested/keymap.jsonc --yes

luwu_cli plan
luwu_cli apply || test "$?" -eq 2
luwu_cli apply --yes

# Simulate an application changing the declared field in the isolated target.
env HOME="$GUIDE_HOME" \
  PYTHONPATH="$GUIDE_PYTHONPATH" \
  PYTHONDONTWRITEBYTECODE=1 \
  python -B - <<'PY'
from pathlib import Path

target = Path("home/.config/demo/settings.jsonc")
data = target.read_text(encoding="utf-8")
old = '"theme": "synthetic-theme-overlay-v1"'
new = '"theme": "synthetic-theme-app-v2"'
if data.count(old) != 1:
    raise SystemExit("expected one synthetic theme marker in isolated target")
target.write_text(data.replace(old, new, 1), encoding="utf-8")
PY

luwu_cli plan
luwu_cli reverse-sync --resource settings --field theme || test "$?" -eq 2
luwu_cli reverse-sync --resource settings --field theme --yes
luwu_cli plan
luwu_cli apply || test "$?" -eq 2
luwu_cli apply --yes

printf 'Isolated fixture: %s\n' "$GUIDE_TMP"
)
```
