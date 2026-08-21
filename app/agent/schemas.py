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
        "name": "jira_get_persons_open_issues",
        "description": (
            "Fetch another Jira user's assigned, unresolved issues, given a "
            "free-text name or email identifying them (not the current user)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": (
                        "The other person's name or email address, exactly "
                        "as mentioned in the question."
                    ),
                }
            },
            "required": ["person"],
        },
    },
    {
        "name": "jira_get_issues_awaiting_my_response",
        "description": (
            "Fetch the current user's unresolved Jira issues (assigned to or "
            "reported by them) where someone has @-mentioned them in a "
            "comment that they have not replied to since."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "jira_draft_comment",
        "description": (
            "Draft a Jira comment from the user's own quick note, for a "
            "specific issue. This does NOT post anything to Jira - it only "
            "packages the issue key and note text for a separate, explicit "
            "user-confirmation step. Never claim the comment has been "
            "posted after calling this."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {
                    "type": "string",
                    "description": "The exact literal Jira issue key, e.g. 'AL-45'.",
                },
                "note_text": {
                    "type": "string",
                    "description": (
                        "The user's note, in their own words - do not "
                        "reword, summarize, or embellish it."
                    ),
                },
            },
            "required": ["issue_key", "note_text"],
        },
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
