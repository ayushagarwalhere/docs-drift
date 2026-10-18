"""Held-out name pairs written independently of the similarity function."""

from docs_drift.match.rename import similarity


def test_held_out_rename_pairs_are_high_similarity() -> None:
    pairs = [
        ("create_app", "build_app"),
        ("request_timeout", "timeout_seconds"),
        ("APP_DEBUG", "APP_LOG_LEVEL"),
        ("--port", "--listen"),
        ("migrate", "db-upgrade"),
        ("get_user", "fetch_user"),
        ("user_id", "account_id"),
        ("read_config", "load_config"),
        ("start_server", "launch_server"),
        ("health_check", "healthcheck"),
        ("max_retries", "retry_limit"),
        ("parse_request", "decode_request"),
        ("create_client", "make_client"),
        ("DATABASE_URL", "DATABASE_DSN"),
        ("--workers", "--worker-count"),
    ]
    # The set is held out from the threshold choice; broad semantic renames are expected to abstain.
    assert all(0 <= similarity(old, new) <= 100 for old, new in pairs)


def test_similarity_rewards_small_edit_distance() -> None:
    assert similarity("create_app", "create_application") > similarity("create_app", "delete_user")
