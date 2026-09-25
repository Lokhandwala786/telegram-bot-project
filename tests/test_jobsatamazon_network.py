from __future__ import annotations

from app.utils.jobsatamazon_network import enrich_listing, extract_from_network_json
from app.models.job import JobListing


def test_extract_from_nested_json() -> None:
    caps = [{"nested": {"weeklySchedule": "Mon-Fri 09:00 - 13:00", "hourlyRate": "14.30 /hr"}}]
    ext = extract_from_network_json(caps)
    assert ext.schedule == "Mon-Fri 09:00 - 13:00"
    assert ext.pay and "14.30" in ext.pay


def test_enrich_listing_prefers_network_shift_fields() -> None:
    job = JobListing(
        source="jobsatamazon.co.uk",
        source_url="https://www.jobsatamazon.co.uk/app#/jobDetail?jobId=JOB-UK-0000000001",
        job_id="JOB-UK-0000000001",
        url="https://www.jobsatamazon.co.uk/app#/jobDetail?jobId=JOB-UK-0000000001",
        title="Warehouse Operative",
        location="Northampton area",
        pay_gbp_per_hour=None,
        pay_text=None,
        expected_pay_text=None,
        shift="N/A",
        posted_date_text=None,
        raw_metadata={"apply_enabled": "true"},
    )
    ext = extract_from_network_json(
        [
            {
                "employmentType": "Seasonal | Part-time",
                "weeklySchedule": "Fri 9:00 - 13:00",
                "hourlyRate": "14.30 /hr",
                "firstDay": "2026-05-15",
                "hoursPerWeek": 20,
            }
        ]
    )
    out = enrich_listing(job, ext)
    assert out.raw_metadata.get("Employment type") == "Seasonal | Part-time"
    assert out.raw_metadata.get("Schedule") == "Fri 9:00 - 13:00"
    assert out.posted_date_text == "2026-05-15"
    assert out.pay_text and "14.30" in out.pay_text


def test_jobsatamazon_parser_filters_carousel_and_loading() -> None:
    from app.parsers.jobsatamazon_search import JobsAtAmazonSearchParser

    html = """
    <html>
      <body>
        <h1>Warehouse Associate</h1>
        <div>Work address: Northampton NN1 1AA</div>
        <div>Pay rate: £14.30 /hr</div>
        <div>Type: Full-time</div>
        <div>Duration: Permanent</div>
        <button aria-disabled="false">Apply</button>
        <section id="recommendations">
          <h2>Similar jobs you may like</h2>
          <div>Customer Service Associate Type: Full Time Duration: Permanent Pay rate: From £14.58 Remote</div>
          <div>Warehouse Operative Type: Reduced Duration: Fixed-term Pay rate: From £14.30 Carlisle</div>
        </section>
      </body>
    </html>
    """
    parser = JobsAtAmazonSearchParser()
    jobs = parser.parse(
        html=html,
        source_url="https://www.jobsatamazon.co.uk/app#/jobDetail?jobId=JOB-UK-0000000449",
        max_items=10,
        expected_pay_text=None,
    )
    assert len(jobs) == 1
    j = jobs[0]
    assert j.title == "Warehouse Associate"
    assert j.location == "Northampton NN1 1AA"
    assert j.pay_text == "£14.30 /hr"
    assert j.shift == "Full-time / Permanent"
    assert j.raw_metadata.get("apply_enabled") == "true"
    assert j.raw_metadata.get("Work address") == "Northampton NN1 1AA"
    # Ensure recommendation keys are NOT captured in raw_metadata
    assert "Customer Service Associate Type" not in j.raw_metadata
    assert "Warehouse Operative Type" not in j.raw_metadata


def test_jobsatamazon_parser_rejects_loading_title_and_page() -> None:
    from app.parsers.jobsatamazon_search import JobsAtAmazonSearchParser

    html = """
    <html>
      <body>
        <div id="root">Loading job details... Please wait</div>
      </body>
    </html>
    """
    parser = JobsAtAmazonSearchParser()
    jobs = parser.parse(
        html=html,
        source_url="https://www.jobsatamazon.co.uk/app#/jobDetail?jobId=JOB-UK-0000000449",
        max_items=10,
        expected_pay_text=None,
    )
    assert len(jobs) == 0


def test_jobsatamazon_parser_rejects_no_available_shifts_banner() -> None:
    from app.parsers.jobsatamazon_search import JobsAtAmazonSearchParser

    html = """
    <html>
      <body>
        <h1>8 jobs found</h1>
        <div>Error The job you selected doesn't have available shifts. Please choose another job below.</div>
        <button>Get job alerts</button>
      </body>
    </html>
    """
    parser = JobsAtAmazonSearchParser()
    jobs = parser.parse(
        html=html,
        source_url="https://www.jobsatamazon.co.uk/app#/jobDetail?jobId=JOB-UK-0000000449",
        max_items=10,
        expected_pay_text=None,
    )
    assert len(jobs) == 0


def test_jobsatamazon_parser_requires_apply_button_for_apply_enabled() -> None:
    from app.parsers.jobsatamazon_search import JobsAtAmazonSearchParser

    # Case 1: Page has job details but NO apply button -> apply_enabled must be false
    html_no_btn = """
    <html>
      <body>
        <h1>Warehouse Associate</h1>
        <div>Work address: Northampton NN1 1AA</div>
        <button>Get job alerts</button>
      </body>
    </html>
    """
    parser = JobsAtAmazonSearchParser()
    jobs = parser.parse(
        html=html_no_btn,
        source_url="https://www.jobsatamazon.co.uk/app#/jobDetail?jobId=JOB-UK-0000000449",
        max_items=10,
        expected_pay_text=None,
    )
    assert len(jobs) == 1
    assert jobs[0].raw_metadata.get("apply_enabled") == "false"

    # Case 2: Page has active "Select Shift" button -> apply_enabled must be true
    html_active = """
    <html>
      <body>
        <h1>Warehouse Associate</h1>
        <div>Work address: Northampton NN1 1AA</div>
        <button>Select Shift</button>
      </body>
    </html>
    """
    jobs2 = parser.parse(
        html=html_active,
        source_url="https://www.jobsatamazon.co.uk/app#/jobDetail?jobId=JOB-UK-0000000449",
        max_items=10,
        expected_pay_text=None,
    )
    assert len(jobs2) == 1
    assert jobs2[0].raw_metadata.get("apply_enabled") == "true"


def test_graphql_unposted_and_empty_schedule_cards_disables_apply() -> None:
    from app.parsers.jobsatamazon_graphql import best_graphql_extract

    # Case 1: UNPOSTED postingStatus
    caps_unposted = [
        {
            "data": {
                "getJobDetail": {
                    "jobId": "JOB-UK-0000000449",
                    "jobTitle": "Warehouse Associate",
                    "postingStatus": "UNPOSTED",
                }
            }
        }
    ]
    ext = best_graphql_extract(caps_unposted)
    assert ext is not None
    assert ext.apply_enabled is False

    # Case 2: empty scheduleCards
    caps_empty_cards = [
        {
            "data": {
                "getJobDetail": {
                    "jobId": "JOB-UK-0000000449",
                    "jobTitle": "Warehouse Associate",
                    "postingStatus": "POSTED",
                }
            }
        },
        {
            "data": {
                "searchScheduleCards": {
                    "scheduleCards": []
                }
            }
        },
    ]
    ext2 = best_graphql_extract(caps_empty_cards)
    assert ext2 is not None
    assert ext2.apply_enabled is False

