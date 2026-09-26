"""Fail-closed secret guards for non-throwaway environments (audit I1, I24).

Strict envs (anything not an explicit local/CI throwaway) must REJECT weak
secret values instead of silently receiving the repo-public fallback
constants. The five throwaway env names keep the local fallbacks so local
dev and CI keep working unchanged.
"""

import pytest
from pydantic import ValidationError

# Single source of truth for every parametrization in this module: the
# recognised environment names split into strict (shared/deployed/unknown)
# vs throwaway (local/CI) buckets, matching MEMORY_FALLBACK_ENVIRONMENTS
# and _is_strict_environment in config.py. "" is a strict env: an empty
# value is an unknown name, so it fails closed.
STRICT_ENVS = [
    "production",
    "staging",
    # Same-bug-class spellings the old exact-match gate missed.
    "prod",
    "prod-eu",
    "Production",
    "dev-shared",
    "",
]
THROWAWAY_ENVS = ["development", "testing", "local", "test", "ci"]

# Meets the 32-char minimum and contains no weak substring, so it can never
# itself trip a secret guard.
STRONG_SECRET = "x" * 48


def _error_fields(exc: ValidationError) -> set:
    """Field names (pydantic v2 error ``loc`` heads) a ValidationError hit.

    Every strict-env reject test asserts on this, not just on
    ``pytest.raises(ValidationError)``: a bare raises passed vacuously when
    an earlier-declared field (SECRET_KEY) failed before the field under
    test was ever validated.
    """
    return {tuple(err.get("loc", ())) for err in exc.errors()}


def _cfg(**kw):
    """Build Settings hermetically.

    NOTE: fields not passed here still fall through to OS-env passthrough
    (pydantic-settings reads os.environ for every field), so unrelated
    process env can leak in on a developer machine or full-suite run. The
    DATABASE_URL / SUPABASE_DB_URL defaults below pin the infra fields that
    no test in this module asserts on (and keep the strict-env localhost-DB
    ban out of the way); secrets are deliberately NOT defaulted — masking
    the field under test with a strong filler is exactly the vacuity these
    tests exist to prevent.
    """
    from src.core.config import Settings

    env = kw.pop("env")
    return Settings(
        _env_file=None,
        ENVIRONMENT=env,
        DATABASE_URL=kw.pop("DATABASE_URL", "postgresql://u:p@db.example.com:5432/db"),
        SUPABASE_DB_URL=kw.pop("SUPABASE_DB_URL", ""),
        **kw,
    )


# ---------------------------------------------------------------------------
# Strict envs reject weak secrets — with the OTHER secrets pinned strong so
# the raised error provably targets the field under test.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env", STRICT_ENVS)
def test_strict_envs_reject_weak_jwt_secret(env):
    with pytest.raises(ValidationError) as exc_info:
        _cfg(
            env=env,
            JWT_SECRET_KEY="your-secret-key",
            SECRET_KEY=STRONG_SECRET,
            NEO4J_PASSWORD=STRONG_SECRET,
        )
    assert ("JWT_SECRET_KEY",) in _error_fields(exc_info.value)


@pytest.mark.parametrize("env", STRICT_ENVS)
def test_strict_envs_reject_weak_neo4j_password(env):
    with pytest.raises(ValidationError) as exc_info:
        _cfg(
            env=env,
            NEO4J_PASSWORD="neo4jpassword",
            SECRET_KEY=STRONG_SECRET,
            JWT_SECRET_KEY=STRONG_SECRET,
        )
    assert ("NEO4J_PASSWORD",) in _error_fields(exc_info.value)


@pytest.mark.parametrize("env", STRICT_ENVS)
def test_strict_envs_reject_weak_secret_key(env):
    with pytest.raises(ValidationError) as exc_info:
        _cfg(
            env=env,
            SECRET_KEY="change-in-production",
            JWT_SECRET_KEY=STRONG_SECRET,
            NEO4J_PASSWORD=STRONG_SECRET,
        )
    assert ("SECRET_KEY",) in _error_fields(exc_info.value)


def test_vacuous_raises_tripwire_weak_default_secret_key():
    """Tripwire for the vacuous-test bug this suite once had.

    SECRET_KEY is declared BEFORE JWT_SECRET_KEY/NEO4J_PASSWORD, so with the
    weak "" default in a strict env the SECRET_KEY validator raises FIRST —
    a bare pytest.raises(ValidationError) on the JWT/NEO4J cases passed
    without exercising those gates at all. With both other secrets strong,
    the raised error must target SECRET_KEY itself, proving the gate under
    test (not an earlier field) fired.
    """
    with pytest.raises(ValidationError) as exc_info:
        _cfg(env="prod", JWT_SECRET_KEY=STRONG_SECRET, NEO4J_PASSWORD=STRONG_SECRET)
    assert ("SECRET_KEY",) in _error_fields(exc_info.value)


# ---------------------------------------------------------------------------
# Strict envs accept strong secrets end to end.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env", ["prod", "staging", "Production"])
def test_strict_env_happy_path_with_strong_secrets(env):
    cfg = _cfg(
        env=env,
        SECRET_KEY=STRONG_SECRET,
        JWT_SECRET_KEY=STRONG_SECRET,
        NEO4J_PASSWORD=STRONG_SECRET,
    )
    assert cfg.SECRET_KEY == STRONG_SECRET
    assert cfg.JWT_SECRET_KEY == STRONG_SECRET
    assert cfg.NEO4J_PASSWORD == STRONG_SECRET


# ---------------------------------------------------------------------------
# Throwaway envs keep the local fallbacks, including strip/lower
# normalization variants (the normalization is core to the fix).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env", THROWAWAY_ENVS + ["Development", "  ci  "])
def test_throwaway_envs_get_local_fallback(env):
    cfg = _cfg(env=env, JWT_SECRET_KEY="your-secret-key")
    assert cfg.JWT_SECRET_KEY != "your-secret-key"


@pytest.mark.parametrize("env", THROWAWAY_ENVS)
def test_throwaway_env_neo4j_fallback_unchanged(env):
    cfg = _cfg(env=env, NEO4J_PASSWORD="neo4jpassword")
    assert cfg.NEO4J_PASSWORD == "neo4jpassword"


@pytest.mark.parametrize("env", ["PROD", " Staging ", "Dev"])
def test_strict_normalization_variants_reject_weak_jwt_secret(env):
    """Case/whitespace spellings of shared envs must NOT get the fallback."""
    with pytest.raises(ValidationError) as exc_info:
        _cfg(
            env=env,
            JWT_SECRET_KEY="your-secret-key",
            SECRET_KEY=STRONG_SECRET,
            NEO4J_PASSWORD=STRONG_SECRET,
        )
    assert ("JWT_SECRET_KEY",) in _error_fields(exc_info.value)
