"""Calendar evidence for MYmeet tasks from meetings the user does not organize."""
import logging
import re
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock

from test_my_tasks import _load_main_async


PRIMARY = "participant@example.test"
EXTERNAL = "organizer@example.test"
ATTENDANCE = [{"self": True, "responseStatus": "accepted"}]


def _event(event_id, organizer=EXTERNAL, attendance=ATTENDANCE, **changes):
    return {
        "id": event_id,
        "organizer": {"email": organizer},
        "attendees": attendance,
        **changes,
    }


def _load_calendar(pages, *, calendar="primary", primary=PRIMARY):
    service = Mock()
    listed = service.events.return_value.list
    listed.side_effect = [NS(execute=Mock(return_value=page)) for page in pages]
    get = service.calendars.return_value.get
    if isinstance(primary, Exception):
        get.side_effect = primary
    else:
        get.return_value.execute.return_value = {"id": primary}
    ns = _load_main_async([
        "_calendar_person_email", "_primary_calendar_participation", "_get_gcal_organizer_data_checked",
    ], {
        "db": NS(get_user=lambda _: {"calendar_id": calendar}),
        "_get_calendar_id": lambda user: user["calendar_id"],
        "auth": NS(get_credentials_checked=lambda _: (object(), None),
                   classify_google_error=lambda _: NS(category="calendar_error")),
        "build": lambda *_args, **_kwargs: service,
        "logger": logging.getLogger(__name__),
        "re": re,
        "timedelta": timedelta,
    })
    return ns["_get_gcal_organizer_data_checked"], get, listed


def test_primary_calendar_requires_organizer_and_participation_evidence():
    owned = {
        "id": "owned", "organizer": {"self": True, "email": PRIMARY},
        "start": {"dateTime": "2026-11-20T10:02:15Z"},
    }
    pages = [{
        "nextPageToken": "fixture-page-2",
        "items": [
            owned,
            _event("foreign-with-self"),
            _event("foreign-with-primary", attendance=[{"email": PRIMARY.upper(), "responseStatus": "tentative"}]),
            _event("invited-needs-action", attendance=[{"email": PRIMARY, "responseStatus": "needsAction"}]),
            _event("declined", attendance=[{"self": True, "responseStatus": "declined"}]),
            _event("contradictory-attendance", attendance=[
                {"self": True, "responseStatus": "accepted"},
                {"email": PRIMARY, "responseStatus": "declined"},
            ]),
            _event("cancelled", status="cancelled"),
            _event("organizer-is-room", organizer="fixture@resource.calendar.google.com"),
            _event("organizer-is-group", organizer="fixture@group.calendar.google.com"),
            _event("missing-organizer", organizer=None),
            _event("not-a-person", organizer="arbitrary ID"),
            _event("organizer-by-email-is-primary", organizer=PRIMARY.upper()),
            _event("no-attendee", attendance=None),
            _event("no-status", attendance=[{"self": True}]),
            _event("someone-else", attendance=[{"email": "other@example.test", "responseStatus": "accepted"}]),
            _event("resource-self", attendance=[{"self": True, "resource": True, "responseStatus": "accepted"}]),
        ],
    }, {"items": [_event("owned"), _event("foreign-page-2")]}]
    run, get, listed = _load_calendar(pages)
    data, reason = run(1, 3, include_personal=True)
    assert reason is None
    assert data["ids"] == {"owned"}
    assert len(data["times"]) == 1
    assert data["other_ids"] == {
        "foreign-with-self", "foreign-with-primary", "invited-needs-action", "foreign-page-2",
    }
    get.assert_called_once_with(calendarId="primary")
    assert "pageToken" not in listed.call_args_list[0].kwargs
    assert listed.call_args_list[1].kwargs["pageToken"] == "fixture-page-2"


def test_shared_calendar_and_unknown_primary_disable_other_evidence_only():
    owned = _event("owned")
    owned["organizer"] = {"self": True}
    pages = [{"items": [owned, _event("foreign")]}]
    run, get, listed = _load_calendar(pages, calendar="shared@example.test")
    assert run(1, 3, include_personal=True) == ({"ids": {"owned"}, "times": set(), "other_ids": set()}, None)
    get.assert_not_called()
    assert listed.call_args.kwargs["calendarId"] == "shared@example.test"

    run, get, _ = _load_calendar(pages, primary=RuntimeError("fixture primary lookup unavailable"))
    assert run(1, 3, include_personal=True) == ({"ids": {"owned"}, "times": set(), "other_ids": set()}, None)
    get.assert_called_once_with(calendarId="primary")


if __name__ == "__main__":
    test_primary_calendar_requires_organizer_and_participation_evidence()
    test_shared_calendar_and_unknown_primary_disable_other_evidence_only()
    print("Calendar identity fixtures passed")
