from __future__ import annotations

import sqlite3
import uuid

import pytest

from app.db import connect_database
from app.services.discussion_workflow import (
    DiscussionWorkflowError,
    create_issue,
    list_collaboration,
    post_message,
    transition_issue,
)
from tests.slice4_helpers import activate_private_case


def test_messages_are_party_only_idempotent_and_append_only(demo_runtime) -> None:
    context = activate_private_case(demo_runtime)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        request_id = str(uuid.uuid4())
        original = post_message(
            connection,
            context["case_id"],
            context["owner_id"],
            "The client asks whether hosting is included.",
            request_id,
        )
        replay = post_message(
            connection,
            context["case_id"],
            context["owner_id"],
            "The client asks whether hosting is included.",
            request_id,
        )
        assert replay == original.__class__(original.message_id, False)
        correction = post_message(
            connection,
            context["case_id"],
            context["owner_id"],
            "Correction: ask whether monthly hosting is included.",
            str(uuid.uuid4()),
            correction_of_id=original.message_id,
        )
        rows = connection.execute(
            "SELECT id, body, correction_of_id FROM case_messages ORDER BY server_created_at, id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["body"] == "The client asks whether hosting is included."
        assert rows[1]["id"] == correction.message_id
        assert rows[1]["correction_of_id"] == original.message_id
        with pytest.raises(sqlite3.IntegrityError, match="case_messages_append_only"):
            connection.execute("UPDATE case_messages SET body = 'changed' WHERE id = ?", (original.message_id,))
        with pytest.raises(sqlite3.IntegrityError, match="case_messages_append_only"):
            connection.execute("DELETE FROM case_messages WHERE id = ?", (original.message_id,))
        with pytest.raises(DiscussionWorkflowError, match="idempotency_conflict"):
            post_message(
                connection,
                context["case_id"],
                context["owner_id"],
                "Different body",
                request_id,
            )
    finally:
        connection.close()


def test_issue_resolution_requires_the_other_role_and_preserves_events(demo_runtime) -> None:
    context = activate_private_case(demo_runtime)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        issue_id = create_issue(
            connection,
            context["case_id"],
            context["owner_id"],
            "Who pays the recurring hosting fee?",
        )
        assert transition_issue(
            connection,
            context["case_id"],
            issue_id,
            context["owner_id"],
            "PROPOSE",
            1,
            "The client pays the provider directly.",
        ) == 2
        with pytest.raises(DiscussionWorkflowError, match="issue_requires_other_role"):
            transition_issue(
                connection,
                context["case_id"],
                issue_id,
                context["owner_id"],
                "ACCEPT",
                2,
                "Self-accept",
            )
        assert transition_issue(
            connection,
            context["case_id"],
            issue_id,
            context["contractor_id"],
            "ACCEPT",
            2,
            "Accepted by the other role.",
        ) == 3
        issue = connection.execute(
            "SELECT state, revision FROM case_issues WHERE id = ?", (issue_id,)
        ).fetchone()
        assert tuple(issue) == ("RESOLVED", 3)
        events = connection.execute(
            "SELECT action, from_state, to_state, issue_revision FROM issue_events "
            "WHERE issue_id = ? ORDER BY issue_revision",
            (issue_id,),
        ).fetchall()
        assert [tuple(row) for row in events] == [
            ("OPENED", None, "OPEN", 1),
            ("PROPOSED", "OPEN", "PROPOSED", 2),
            ("RESOLVED", "PROPOSED", "RESOLVED", 3),
        ]
        collaboration = list_collaboration(connection, context["case_id"], context["owner_id"])
        assert [event["note"] for event in collaboration["issue_events"]] == [
            "Who pays the recurring hosting fee?",
            "The client pays the provider directly.",
            "Accepted by the other role.",
        ]
        with pytest.raises(DiscussionWorkflowError, match="issue_conflict"):
            transition_issue(
                connection,
                context["case_id"],
                issue_id,
                context["contractor_id"],
                "ACCEPT",
                2,
                "Stale replay",
            )
        with pytest.raises(sqlite3.IntegrityError, match="issue_events_append_only"):
            connection.execute(
                "UPDATE issue_events SET note = 'changed' WHERE issue_id = ? AND issue_revision = 2",
                (issue_id,),
            )
    finally:
        connection.close()


def test_issue_note_limit_matches_database_and_leaves_no_partial_event(demo_runtime) -> None:
    context = activate_private_case(demo_runtime)
    connection = connect_database(context["instance_root"] / "app.sqlite3")
    try:
        issue_id = create_issue(
            connection,
            context["case_id"],
            context["owner_id"],
            "Confirm the hosting boundary.",
        )
        assert transition_issue(
            connection,
            context["case_id"],
            issue_id,
            context["owner_id"],
            "PROPOSE",
            1,
            "x" * 4_000,
        ) == 2
        before = connection.execute(
            "SELECT state, revision FROM case_issues WHERE id = ?", (issue_id,)
        ).fetchone()
        event_count = connection.execute(
            "SELECT COUNT(*) FROM issue_events WHERE issue_id = ?", (issue_id,)
        ).fetchone()[0]
        with pytest.raises(DiscussionWorkflowError, match="issue_invalid"):
            transition_issue(
                connection,
                context["case_id"],
                issue_id,
                context["contractor_id"],
                "REQUEST_REVISION",
                2,
                "x" * 4_001,
            )
        after = connection.execute(
            "SELECT state, revision FROM case_issues WHERE id = ?", (issue_id,)
        ).fetchone()
        assert tuple(after) == tuple(before)
        assert connection.execute(
            "SELECT COUNT(*) FROM issue_events WHERE issue_id = ?", (issue_id,)
        ).fetchone()[0] == event_count
    finally:
        connection.close()
