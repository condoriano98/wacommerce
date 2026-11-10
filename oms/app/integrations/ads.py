"""Advertising insights adapters: Meta Marketing API, Google Ads (GAQL), TikTok Ads reports (spec §13, §17).

Status **mock_tested**: normalisers are tested on documentation-derived row shapes; no live client is selected (no
credentials). Facts used (docs/04): Meta insights ``level=ad, time_increment=1`` with 28-day revisions; Google Ads API
cost in micros; TikTok async/basic reports with ``stat_time_day``. All rows map to ``daily_ad_insights``.
"""

from datetime import date, datetime
from decimal import Decimal

from app.integrations import mockstore

MESSAGING_ACTIONS = ("onsite_conversion.messaging_conversation_started_7d",)


def meta_row(r: dict) -> dict:
    convs = sum(int(a["value"]) for a in r.get("actions", []) if a["action_type"] in MESSAGING_ACTIONS)
    return {"report_date": date.fromisoformat(r["date_start"]), "campaign_external_id": r["campaign_id"],
            "ad_group_external_id": r.get("adset_id"), "ad_external_id": r["ad_id"], "spend": Decimal(r["spend"]),
            "impressions": int(r.get("impressions", 0)), "clicks": int(r.get("clicks", 0)),
            "messaging_conversations": convs}


def google_row(r: dict) -> dict:
    m = r["metrics"]
    return {"report_date": date.fromisoformat(r["segments"]["date"]), "campaign_external_id": str(r["campaign"]["id"]),
            "ad_group_external_id": str(r["adGroup"]["id"]), "ad_external_id": str(r["adGroupAd"]["ad"]["id"]),
            "spend": (Decimal(str(m["costMicros"])) / Decimal(1_000_000)).quantize(Decimal("0.01")),
            "impressions": int(m.get("impressions", 0)), "clicks": int(m.get("clicks", 0)),
            "messaging_conversations": 0}


def tiktok_row(r: dict) -> dict:
    d, m = r["dimensions"], r["metrics"]
    return {"report_date": datetime.strptime(d["stat_time_day"][:10], "%Y-%m-%d").date(),
            "campaign_external_id": str(m["campaign_id"]), "ad_group_external_id": str(m.get("adgroup_id")),
            "ad_external_id": str(d["ad_id"]), "spend": Decimal(str(m["spend"])),
            "impressions": int(m.get("impressions", 0)), "clicks": int(m.get("clicks", 0)),
            "messaging_conversations": 0}


NORMALISE = {"meta": meta_row, "google_ads": google_row, "tiktok_ads": tiktok_row}


class MockAds:
    def __init__(self, provider: str):
        self.provider = provider

    def insights(self, start: date, end: date) -> list[dict]:
        norm = NORMALISE[self.provider]
        with mockstore.state(f"ads_{self.provider}") as st:
            st["calls"] = st.get("calls", 0) + 1
            rows = [norm(r) for r in st.get("rows", [])]
        return [r for r in rows if start <= r["report_date"] <= end]

    @staticmethod
    def seed(provider: str, rows: list[dict], *, replace: bool = False) -> None:  # test helper (re-pull = replace)
        with mockstore.state(f"ads_{provider}") as st:
            st["rows"] = list(rows) if replace else st.get("rows", []) + list(rows)
