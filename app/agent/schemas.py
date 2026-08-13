TOOL_SCHEMAS: list[dict] = [
    {
        "name": "jira_get_my_high_priority_issues",
        "description": (
            "Fetch the current user's assigned, unresolved High/Highest "
            "priority Jira issues."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "jira_get_issues_without_prs",
        "description": (
            "Fetch the current user's unresolved Jira issues that have no "
            "linked GitHub pull request."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "jira_get_my_issues_with_linked_prs",
        "description": (
            "Fetch the current user's unresolved Jira issues, each annotated "
            "with the GitHub pull request that references its issue key in "
            "the PR title or body, if one exists."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "jira_get_incomplete_issues_from_last_sprint",
        "description": (
            "Fetch the current user's unresolved Jira issues from the most "
            "recently closed sprint on their project's board(s) - i.e. "
            "what's still incomplete from last sprint."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "github_get_my_open_prs",
        "description": "Fetch the current user's open, authored GitHub pull requests.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "github_get_prs_awaiting_my_review",
        "description": (
            "Fetch open GitHub pull requests where the current user's "
            "review has been requested."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]
