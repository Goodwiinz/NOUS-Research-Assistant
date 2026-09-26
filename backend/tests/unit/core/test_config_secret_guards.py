"""Fail-closed secret guards for non-throwaway environments (audit I1, I24).

Strict envs (anything not an explicit local/CI throwaway) must REJECT weak
secret values instead of silently receiving the repo-public fallback
constants. The five throwaway env names keep the local fallbacks so local
dev and CI keep working unchanged.
"""

import pytest
from pydantic import ValidationError


def _cfg(**kw):
    from src.core.config import Settings
    return Settings(_env_file=None, ENVIRONMENT=kw.pop("env"), **kw)


@pytest.mark.parametrize("env", ["production", "staging", "prod", "prod-eu", "Production", "dev-shared", ""])
def test_strict_envs_reject_weak_jwt_secret(env):
    with pytest.raises(ValidationError):
        _cfg(env=env, JWT_SECRET_KEY="your-secret-key")


@pytest.mark.parametrize("env", ["development", "testing", "local", "test", "ci"])
def test_throwaway_envs_get_local_fallback(env):
    cfg = _cfg(env=env, JWT_SECRET_KEY="your-secret-key")
    assert cfg.JWT_SECRET_KEY != "your-secret-key"


@pytest.mark.parametrize("env", ["prod", "staging-eu", "PROD"])
def test_strict_envs_reject_weak_neo4j_password(env):
    with pytest.raises(ValidationError):
        _cfg(env=env, NEO4J_PASSWORD="neo4jpassword")


def test_throwaway_env_neo4j_fallback_unchanged():
    cfg = _cfg(env="development", NEO4J_PASSWORD="neo4jpassword")
    assert cfg.NEO4J_PASSWORD == "neo4jpassword"


@pytest.mark.parametrize("env", ["prod", "staging", "prod-eu"])
def test_strict_envs_reject_weak_secret_key(env):
    with pytest.raises(ValidationError):
        _cfg(env=env, SECRET_KEY="change-in-production")
