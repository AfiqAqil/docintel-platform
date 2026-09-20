"""Parsing the S3 event notification.

Small, and every case here is one that produced a real bug somewhere at some point: the "+"
encoding, the test event, and the prefix check that stops the worker looping on its own
report writes.
"""

from __future__ import annotations

import json
import uuid

import pytest

from consumer.messages import S3TestEvent, UnparseableMessage, parse

PREFIX = "uploads/"


def notification(key: str, bucket: str = "docintel", size: int = 1024) -> str:
    return json.dumps(
        {
            "Records": [
                {
                    "eventName": "ObjectCreated:Post",
                    "s3": {
                        "bucket": {"name": bucket},
                        "object": {"key": key, "size": size},
                    },
                }
            ]
        }
    )


def test_a_normal_notification_yields_the_document_id():
    document_id = uuid.uuid4()

    event = parse(notification(f"uploads/{document_id}"), PREFIX)

    assert event.document_id == document_id
    assert event.s3_key == f"uploads/{document_id}"
    assert event.size == 1024


def test_the_test_event_is_recognised_rather_than_failing_to_parse():
    """S3 sends this once when a notification is configured. It has no Records, so without
    this the consumer logs a parse failure every time the infrastructure is recreated."""
    with pytest.raises(S3TestEvent):
        parse(json.dumps({"Service": "Amazon S3", "Event": "s3:TestEvent"}), PREFIX)


def test_url_encoding_in_the_key_is_decoded_including_plus_as_space():
    """S3 encodes spaces in notification keys as "+", not "%20", so unquote alone is wrong.

    The key carries no filename precisely so that this cannot break a lookup, but the
    decoding still has to be right for the prefix check and the id parse.
    """
    document_id = uuid.uuid4()

    event = parse(notification(f"uploads%2F{document_id}"), PREFIX)

    assert event.document_id == document_id


def test_a_key_outside_the_upload_prefix_is_refused():
    """The notification is filtered to the uploads prefix, so this should be unreachable.

    It is checked anyway, because if that filter is ever misconfigured the alternative is
    the worker triggering on its own report writes into the same bucket, in a loop.
    """
    with pytest.raises(UnparseableMessage):
        parse(notification(f"reports/{uuid.uuid4()}.json"), PREFIX)


def test_a_key_that_is_not_a_document_id_is_refused():
    with pytest.raises(UnparseableMessage):
        parse(notification("uploads/not-a-uuid"), PREFIX)


def test_a_message_with_no_records_is_refused():
    with pytest.raises(UnparseableMessage):
        parse(json.dumps({"something": "else"}), PREFIX)
