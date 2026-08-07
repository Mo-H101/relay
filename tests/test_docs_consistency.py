"""
Documentation consistency tests (first-time-user release audit fixes).

Guards the doc fixes from the v1.0.0rc1 first-time-user review:

- C1: beginner-facing commands use ``relay``, not ``python -m app.cli``.
- C2: user-facing Relay URLs use ``127.0.0.1:8000``, not ``localhost``.
- C3: user-facing docs point installed users at the real ``.env``
  location (``%LOCALAPPDATA%\\relay\\.env`` on Windows).
- C4: every API-key example uses scoped keys (``--scopes chat,v1``).

Developer-facing reports (platform plans, audits, roadmaps, release
notes) are exempt from C1/C2/C4: they document the developer entry
points and may reference ``localhost`` in design prose.
"""

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

USER_FACING_DOCS = sorted(
    [PROJECT_ROOT / "README.md"]
    + list((PROJECT_ROOT / "docs").glob("*.md"))
    + list((PROJECT_ROOT / "docs" / "clients").glob("*.md"))
)

# Developer/planning reports that legitimately discuss `python -m app.cli`
# and `localhost`. Excluded from the user-facing command checks.
_DEV_DOCS = [
    "blockers-before-public-release.md",
    "nvidia-model-benchmark-plan.md",
    "platform-architecture-report.md",
    "platform-db-schema.md",
    "platform-implementation-roadmap.md",
    "platform-missing-components-report.md",
    "platform-p1-completion-report.md",
    "platform-p1-plan.md",
    "platform-p2-design.md",
    "platform-p2-implementation-plan.md",
    "platform-p2d-plan.md",
    "platform-p3-plan.md",
    "platform-p3-risks.md",
    "platform-p4-plan.md",
    "platform-p4.2-plan.md",
    "platform-p4.3-plan.md",
    "platform-p4.3-phase1-plan.md",
    "platform-p4.3-phase2-plan.md",
    "platform-p4.3-phase3-plan.md",
    "platform-p4.3-phase4-plan.md",
    "platform-p5-phase-plan.md",
    "platform-p5-phase2-plan.md",
    "platform-p5-phase3-plan.md",
    "platform-p5-phase4-plan.md",
    "platform-p5-phase5-plan.md",
    "platform-p5-plan.md",
    "platform-p6-phase1-plan.md",
    "platform-p6-phase2-plan.md",
    "platform-p6-phase3-plan.md",
    "platform-p6-phase4-plan.md",
    "platform-p6-phase5-plan.md",
    "platform-p6-plan.md",
    "platform-p7-phase2-plan.md",
    "platform-p7-phase3-plan.md",
    "platform-p7-plan.md",
    "platform-p8-plan.md",
    "platform-p9-architecture-design.md",
    "platform-p9-implementation-plan.md",
    "platform-p9-phase2-plan.md",
    "platform-p9-phase3-audit.md",
    "platform-p9-phase4-audit.md",
    "platform-p9-phase4-plan.md",
    "platform-p9-phase5-audit.md",
    "platform-p9-research-plan.md",
    "platform-recommended-order.md",
    "post-p9-readiness-audit.md",
    "release-candidate-checklist.md",
    "release-decisions.md",
    "roadmap-post-p7-audit.md",
    "roadmap-release-alignment-audit.md",
    "roadmap-verification-audit.md",
    "v1-live-continuity-validation.md",
    "v1-release-hardening-plan.md",
    "v1-release-readiness-plan.md",
    "v1.0.0-final-audit.md",
    "v1.0.0-readiness-report.md",
    "audit-report.md",
]

# config-reference docs legitimately list the provider base-URL *defaults*
# (`LMSTUDIO_BASE_URL`, `OLLAMA_BASE_URL`) which use ``localhost``; those
# are the provider's address, not Relay's.
_LOCAL_PROVIDER_DOCS = {"configuration.md"}


def _docs(exclude_dev: bool) -> list[Path]:
    return [
        p
        for p in USER_FACING_DOCS
        if not (exclude_dev and p.name in _DEV_DOCS)
    ]


# ------------------------------------------------------------------- C1

def test_beginner_facing_docs_use_relay_not_app_cli():
    """
    C1: user-facing docs never ask beginners to run ``python -m app.cli``.
    """
    offenders = []
    for path in _docs(exclude_dev=True):
        if path.name in _LOCAL_PROVIDER_DOCS:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "python -m app.cli" in line:
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert not offenders, "user-facing docs still use `python -m app.cli`:\n" + "\n".join(offenders)


# ------------------------------------------------------------------- C2

def test_user_facing_relay_urls_use_127_0_0_1():
    """
    C2: Relay's own endpoint is written as ``127.0.0.1:8000`` everywhere a
    user is told to point a client or ``curl`` at Relay.
    """
    offenders = []
    for path in _docs(exclude_dev=True):
        if path.name in _LOCAL_PROVIDER_DOCS:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "localhost:8000" in line:
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert not offenders, "user-facing docs still reference `localhost:8000`:\n" + "\n".join(offenders)


# ------------------------------------------------------------------- C3

def test_installed_env_location_documented():
    """
    C3: at least one beginner-facing doc tells installed Windows users where
    the real ``.env`` lives (``%LOCALAPPDATA%\\relay\\.env``).
    """
    found = []
    for path in [PROJECT_ROOT / "README.md"] + list((PROJECT_ROOT / "docs" / "clients").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if "%LOCALAPPDATA%" in text and "relay\\.env" in text:
            found.append(path.name)
    assert found, "no user-facing doc documents `%LOCALAPPDATA%\\relay\\.env`"


# ------------------------------------------------------------------- C4

def test_key_examples_use_scoped_keys():
    """
    C4: every ``relay keys add`` example passes ``--scopes chat,v1``; no
    unscoped example remains in user-facing docs.
    """
    offenders = []
    for path in _docs(exclude_dev=True):
        if path.name in _LOCAL_PROVIDER_DOCS:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            is_command = stripped.startswith("relay keys add") or stripped.startswith("$ relay keys add")
            if is_command and "--scopes" not in stripped:
                offenders.append(f"{path.name}:{i}: {stripped}")
    assert not offenders, "unscoped `relay keys add` examples remain:\n" + "\n".join(offenders)


# -------------------------------------------------------------- local models

def test_local_models_walkthrough_exists_and_is_linked():
    """
    M1: the LM Studio / Ollama walkthrough exists and is linked from the
    README so a first-time local-model user can find it.
    """
    walkthrough = PROJECT_ROOT / "docs" / "local-models.md"
    assert walkthrough.is_file(), "docs/local-models.md is missing"
    text = walkthrough.read_text(encoding="utf-8")
    assert "LM Studio" in text and "Ollama" in text

    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/local-models.md" in readme, "README does not link docs/local-models.md"


@pytest.mark.parametrize(
    "base_url_var",
    ["LMSTUDIO_BASE_URL", "OLLAMA_BASE_URL"],
)
def test_local_model_base_urls_documented(base_url_var):
    """
    M1: the walkthrough documents the config overrides for non-default
    local-server ports (the wizard's connectivity check uses the defaults).
    """
    text = (PROJECT_ROOT / "docs" / "local-models.md").read_text(encoding="utf-8")
    assert base_url_var in text, f"docs/local-models.md does not mention {base_url_var}"
