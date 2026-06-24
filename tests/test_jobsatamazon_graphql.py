from __future__ import annotations

import json
from pathlib import Path

from app.parsers.jobsatamazon_graphql import best_graphql_extract, network_extract_from_graphql_envelope
from app.utils.jobsatamazon_network import extract_from_network_json


def test_network_extract_from_graphql_fixture() -> None:
    raw = json.loads(
        (Path(__file__).parent / "fixtures" / "jobsatamazon_graphql_sample.json").read_text(encoding="utf-8")
    )
    ext = network_extract_from_graphql_envelope(raw)
    assert ext is not None
    assert ext.title == "Warehouse Operative"
    assert ext.apply_enabled is True
    assert ext.apply_url and "apply" in ext.apply_url
    assert ext.hours_per_week == "20"
    assert ext.pay and "14.30" in ext.pay
    assert ext.location and "Bognor" in ext.location
    assert ext.postcode == "PO22 9FJ"
    assert ext.first_day == "2026-05-15"


def test_best_graphql_extract_picks_richest() -> None:
    minimal = {"data": {"getJobDetails": {"jobId": "JOB-UK-1", "title": "X"}}}
    full = json.loads(
        (Path(__file__).parent / "fixtures" / "jobsatamazon_graphql_sample.json").read_text(encoding="utf-8")
    )
    ext = best_graphql_extract([minimal, full])
    assert ext is not None
    assert ext.title == "Warehouse Operative"


def test_extract_from_network_json_merges() -> None:
    full = json.loads(
        (Path(__file__).parent / "fixtures" / "jobsatamazon_graphql_sample.json").read_text(encoding="utf-8")
    )
    ext = extract_from_network_json([full, {"noise": True}])
    assert ext.title == "Warehouse Operative"
    assert ext.apply_enabled is True


def test_search_schedule_cards_parsing() -> None:
    cards_payload = {
        "data": {
            "searchScheduleCards": {
                "nextToken": None,
                "scheduleCards": [
                    {
                        "jobSchedule": {
                            "hoursPerWeek": 40,
                            "scheduleText": "Sun, Mon, Tue, Wed 07:30 - 18:00",
                            "shiftType": "DAY"
                        },
                        "startDate": "2026-07-12",
                        "compensation": {
                            "displayCompensation": "£13.50 /hr"
                        }
                    },
                    {
                        "jobSchedule": {
                            "hoursPerWeek": 20,
                            "scheduleText": "Thu, Fri 07:30 - 18:00",
                            "shiftType": "DAY"
                        },
                        "startDate": "2026-07-15",
                        "compensation": {
                            "displayCompensation": "£14.00 /hr"
                        }
                    }
                ]
            }
        }
    }

    ext = best_graphql_extract([cards_payload])
    assert ext is not None
    assert ext.schedule == "Sun, Mon, Tue, Wed 07:30 - 18:00 / Thu, Fri 07:30 - 18:00"
    assert ext.first_day == "2026-07-12 / 2026-07-15"
    assert ext.hours_per_week == "40 / 20"
    assert ext.pay == "£13.50 /hr / £14.00 /hr"

