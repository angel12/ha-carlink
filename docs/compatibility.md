# Verified development baseline

Tasks 1–6 implement UI configuration/reauthentication, lifecycle, command-only
entities, packaging, and an offline HA test harness. Offline integration behavior
is verified; this is not live-device or target-installation acceptance. No live
login or vehicle command was run.

## Versions and platform

Verified locally on macOS 26.6.2, arm64:

| Component | Verified version |
| --- | --- |
| Python (uv-managed CPython) | 3.14.7 |
| Home Assistant | 2026.9.3 |
| pytest-homeassistant-custom-component | 0.13.366 |
| pytest | 9.0.3 |
| pytest-asyncio | 1.4.0 |
| pytest-socket | 0.8.0 |
| pip | 26.2 |
| uv | 0.12.5 |
| pycarlink distribution metadata | 0.1.0; exact Git commit below |

Spencer reports HA 2026.9.3. PyPI metadata for that exact release requires
Python >=3.14.2; plugin 0.13.366 requires Python >=3.14 and pins
`homeassistant==2026.9.3`. This matches his **HA version**, not a verified
installation on his hardware. Neither HA OS/Linux installation nor HACS
installation has been exercised. HACS minimum `2026.9.3` means the exact tested
baseline, not a compatibility promise for arbitrary newer versions.

Direct test pins are in `pyproject.toml`. `requirements-test-macos.lock` freezes
all 140 installed packages, including transitive dependencies and the immutable
Git reference. It is a macOS development snapshot, **not a cross-platform lock**;
it includes PyObjC dependencies. It is version-pinned, not a hash-verified supply
chain lock. A second clean virtualenv successfully reproduced this snapshot and
passed the packaging/HA tests and `pip check`.

## Dependency provenance

Approved development/test requirement (also in the manifest):

```text
pycarlink@git+https://github.com/angel12/PyCarLink.git@a11d551491fc8e6025c7d68314cc7666d040cbac
```

`https://pypi.org/pypi/pycarlink/json` returned HTTP 404. No PyPI release was
available through that endpoint; `0.1.0` is source metadata, **not evidence of a
published PyPI release**. Spencer explicitly approved this exact Git dependency
for the initial test build. It was fetched and built by uv, not installed editable
from the adjacent checkout. Installation needs Git/network access; target HA
installation remains unverified. No library publication was attempted.

The installed distribution's PEP 610 `direct_url.json` records the above GitHub
URL and exact `commit_id` and `requested_revision`. Both installed source files
were compared byte-for-byte with `git show` at the approved commit:

| File | Installed SHA-256 (matches Git source) |
| --- | --- |
| `pycarlink/__init__.py` | `8000578e94c38ab04dde566a3e8a92ead796da2d949b749441cd772b15a90152` |
| `pycarlink/PyCarLink.py` | `f5c7f9acf847bef9657f3e5a4962ef693bbdff411fe302e8788ba6d7db6526cc` |

Imports verified without constructing a client:

```python
from pycarlink import PyCarlink
from pycarlink.PyCarLink import MalformedResponseError, NoAssetsError
```

All six upstream test files were extracted from that exact commit into scratch
storage without the package source. They ran against the **installed artifact**:
**28 tests and 646 subtests passed**. Coverage includes command success and
terminal/unknown outcomes, finite timeouts/no resubmission, response validation
and empty assets, public imports, and atomic status snapshots. These are mocked
offline regression checks, not live service validation.

## Reproduce locally

From the repository root, with uv and Git available:

```sh
uv venv --python 3.14.7 .venv
uv pip install --python .venv/bin/python -r requirements-test-macos.lock
.venv/bin/python -m pytest tests/test_packaging.py -v
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m pip check
```

For resolving the direct project/test requirements instead of replaying the
complete macOS snapshot (transitive versions may change):

```sh
uv pip install --python .venv/bin/python -r pyproject.toml --group test
```

Both installation commands were exercised in Task 1 (then **3 tests passed**).
Task 6 reruns the full implemented suite, including real config-entry setup,
reauthentication, platform forwarding and entity services; see the final
acceptance section below.

## Harness constraints and lessons

- Leave plugin autoload enabled for integration tests. No fake HA modules or
  replaced HA fixtures are used. `enable_custom_integrations` is autouse.
- `--disable-socket --allow-unix-socket` blocks internet sockets while permitting
  local Unix sockets needed by asyncio. HA also fails teardown on attempted
  sockets even if the test catches the blocked-socket exception. Do not clear or
  suppress that safety check. An intentional socket assertion during harness
  development proved the guard and was removed from the passing suite.
- `custom_components/__init__.py` and an early import in `tests/conftest.py`
  bind imports to this repository before HA mounts the plugin's bundled test
  configuration, which contains a competing `custom_components` package. The
  real-loader test caught this collision; a bare `hass` smoke test did not.
- Manifest `config_flow: true` is backed by the implemented UI flow and real HA
  flow-manager tests, including reauthentication and identity checks.
- The requested `cloud_polling` classification does not imply recurring
  telemetry polling. The approved design has none.
- The constructor performs authentication/status I/O. Never instantiate the
  real client in tests unless its transport is mocked first. No credentials are
  needed or provided for these checks.

## Task 6 final offline acceptance

On the macOS/Python/HA baseline above, both the development environment and a
new environment resolved from `pyproject.toml` passed **162 tests**, `pip check`,
and Ruff 0.15.10. Coverage is **99%** (334 statements, 4 missed):
`__init__.py` lines 31, 116, 133 and `runtime.py` line 195. This is statement
coverage, not proof of every cancellation interleaving or physical behavior.

```sh
.venv/bin/python -m pip check
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q --cov=custom_components.carlink --cov-report=term-missing
```

A separate empty, seeded venv installed **exactly the manifest's requirement**
using pip with `--no-cache-dir`, fetching GitHub, building the wheel, importing
`PyCarlink` without construction, and verifying PEP 610 commit provenance.
`pip check` passed. That dependency-only probe used pip 26.2.1; the integration
suite environments use the declared pip 26.2. These are macOS installs, not
Linux/HA OS proof. The fresh test-group resolution has 141 packages (including
Ruff); the original 140-package macOS snapshot remains historical and unchanged.

### HACS and hassfest

- `hacs.json` uses `Carlink`, `content_in_root: false`, minimum `2026.9.3`.
- Current HACS `validate/brands.py` resolves `brand/icon.png` **inside the
  integration content directory**, despite the general documentation's shorter
  wording. `custom_components/carlink/brand/icon.png` is the installable asset;
  root `brand/icon.png` is an identical README copy. Both are original artwork.
- Actual **Core 2026.9.3 hassfest** was run from its Git tag at
  `6de5eb18cd4502f94af44cfff3a02250d88716ed` in a separate scratch checkout/env.
  It initially rejected manifest key ordering. After metadata-only sorting it
  reported **Integrations: 1; Invalid integrations: 0**. The CLI's default
  integration validators ran; optional `--requirements` installation validation
  was not enabled. The separate exact-manifest install provides that evidence.
  The validator environment additionally needed `infrared-protocols==10.3.0`
  and Core's own `ruff==0.16.3` on PATH; these are not integration dependencies.
- `.github/workflows/ci.yml` pins inspected action wrapper SHAs for checkout,
  setup-python, hassfest, and HACS. Hassfest declares no inputs; HACS uses
  `category: integration` and `comment: 'false'`, with no ignored checks.
  The upstream wrappers still use moving Docker images; SHA-pinning their
  wrappers does **not** pin the underlying validator/container versions.
  Local `actionlint .github/workflows/ci.yml` passed with actionlint 1.7.12.
- CI is offline **with respect to Carlink**: pytest blocks internet sockets.
  Dependency installation and GitHub-based HACS validation require network.
  HACS validation reads the GitHub repository, not unpublished local files.
  At Task 6 no hosted validators had run. Publication subsequently verified
  Linux tests, hosted hassfest, and HACS validation on the reviewed implementation
  commit `f0f0b0e8234efed12e338b3a9cebca11b19bf356`:
  [successful workflow](https://github.com/angel12/ha-carlink/actions/runs/39083341837).
  HACS initially failed only for absent repository topics; after the owner added
  them, its rerun passed without code changes. No local container runtime was
  installed. Hosted Linux evidence does not establish installation on HA OS.

### Compatibility and acceptance boundaries

The lifecycle implementation reads HA's private `EntityPlatform._setup_complete`
flag against 2026.9.3 because forwarding can hide platform setup failure. It
never mutates that flag. Revalidate this private dependency on HA upgrades.
Tests cover real forwarding and selected failures before/after entity scheduling;
they do **not** establish complete coverage of arbitrary entity-add failures.

Normal successful cleanup allows reload. Public failed unload and genuinely
failed partial-setup teardown require **restart-only recovery**; disabled runtime
ownership is retained rather than falsely reporting safe cleanup. Running work
can delay reload/shutdown; no hard wall-clock bound is claimed.

### Publication verification

Dusk approved the whole implementation and then the final fixes/MIT packaging
against exact file hashes before the initial commit. Final local verification
passed **165 tests with 99% branch-aware coverage**, Ruff, dependency checks,
actionlint, and tag-specific hassfest. The final fix tests cover translated
cleanup errors and retained-runtime recovery; the Task 6 results above are
historical, not the final test count.

The owner authorized MIT licensing and publication. GitHub recognizes the MIT
license; the repository is public, issues are enabled, and the required topics
are present. The initial code commit is the exact reviewed implementation.
This documentation-only publication update does not change that reviewed code.

Remaining acceptance steps are actual HACS download/dependency installation on
the target HA instance and separately approved live login/identity verification
and safe, observed physical commands. No credentials or live Carlink API were
accessed during development, review, or CI.

## Sources checked

- https://pypi.org/pypi/homeassistant/2026.9.3/json
- https://pypi.org/pypi/pytest-homeassistant-custom-component/0.13.366/json
- https://pypi.org/pypi/pycarlink/json (404)
- https://developers.home-assistant.io/docs/creating_integration_manifest/
- Approved PyCarLink Git commit and its six offline regression suites.
- https://www.hacs.xyz/docs/publish/start/
- https://www.hacs.xyz/docs/publish/integration/
- https://www.hacs.xyz/docs/publish/action/
- https://github.com/hacs/integration/blob/main/custom_components/hacs/validate/brands.py
- https://github.com/hacs/integration/blob/main/custom_components/hacs/validate/license.py
- https://github.com/hacs/action/blob/1ebf01c408f29afcb6406bd431bc98fd8cbb15aa/action.yml
- https://github.com/home-assistant/actions/blob/cd6f3635a740c84d8a14f6121a90068b8ece6bf6/hassfest/action.yml
- https://github.com/home-assistant/core/tree/2026.9.3/script/hassfest
