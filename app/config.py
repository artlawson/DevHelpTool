from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    anthropic_api_key: str
    jira_base_url: str
    jira_email: str
    jira_api_token: str
    github_token: str
    github_username: str

    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()  # type: ignore[call-arg]  # fields are populated from the environment
