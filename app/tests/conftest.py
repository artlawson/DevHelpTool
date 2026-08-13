import os

# Seed dummy credentials before any test module imports app.config, so the
# module-level `settings` singleton can instantiate without a real .env file.
# Individual tests that need to exercise validation failures use monkeypatch
# to unset/override specific vars for the duration of that test.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("JIRA_BASE_URL", "https://test.atlassian.net")
os.environ.setdefault("JIRA_EMAIL", "test@example.com")
os.environ.setdefault("JIRA_API_TOKEN", "test-jira-token")
os.environ.setdefault("JIRA_PROJECT_KEY", "TEST")
os.environ.setdefault("GITHUB_TOKEN", "test-github-token")
os.environ.setdefault("GITHUB_USERNAME", "test-user")
