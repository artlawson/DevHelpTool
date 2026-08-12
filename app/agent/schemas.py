TOOL_SCHEMAS: list[dict] = [
    {
        "name": "jira.get_my_high_priority_issues",
        "description": (
            "Fetch the current user's assigned, unresolved High/Highest "
            "priority Jira issues."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "jira.get_issues_without_prs",
        "description": (
            "Fetch the current user's unresolved Jira issues that have no "
            "linked GitHub pull request."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "github.get_my_open_prs",
        "description": "Fetch the current user's open, authored GitHub pull requests.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "github.get_prs_awaiting_my_review",
        "description": (
            "Fetch open GitHub pull requests where the current user's "
            "review has been requested."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]
