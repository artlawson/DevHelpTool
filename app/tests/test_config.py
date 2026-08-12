import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_loads_all_fields_from_env(monkeypatch, tmp_path):
    # conftest.py seeds these as ambient env vars for the rest of the suite;
    # env vars take precedence over .env file values in pydantic-settings, so
    # they must be cleared here to prove the .env file itself is being read.
    for var in (
        "ANTHROPIC_API_KEY",
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_USERNAME",
    ):
        monkeypatch.delenv(var, raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "ANTHROPIC_API_KEY=abc\n"
        "JIRA_BASE_URL=https://example.atlassian.net\n"
        "JIRA_EMAIL=me@example.com\n"
        "JIRA_API_TOKEN=jira-token\n"
        "GITHUB_TOKEN=gh-token\n"
        "GITHUB_USERNAME=octocat\n"
    )

    settings = Settings(_env_file=env_file)

    assert settings.anthropic_api_key == "abc"
    assert settings.jira_base_url == "https://example.atlassian.net"
    assert settings.jira_email == "me@example.com"
    assert settings.jira_api_token == "jira-token"
    assert settings.github_token == "gh-token"
    assert settings.github_username == "octocat"


def test_settings_missing_required_field_raises(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ANTHROPIC_API_KEY=abc\n"
        "JIRA_BASE_URL=https://example.atlassian.net\n"
        "JIRA_EMAIL=me@example.com\n"
        "JIRA_API_TOKEN=jira-token\n"
        "GITHUB_TOKEN=gh-token\n"
        # GITHUB_USERNAME intentionally omitted
    )
    # Ensure no ambient env var backfills the missing field for this test.
    monkeypatch.delenv("GITHUB_USERNAME", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=env_file)
