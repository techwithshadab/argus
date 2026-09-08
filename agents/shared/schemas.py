"""Structured contracts exchanged between agents and stored by the platform."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Severity = Literal["low", "medium", "high"]
Confidence = Literal["low", "moderate", "high"]
AlertKind = Literal[
    "ais_gap", "mmsi_spoof", "loitering", "zone_incursion", "rendezvous"
]


class Evidence(BaseModel):
    source: str = Field(
        description="Tool or data source, e.g. ais.find_ais_gaps, registry.sanctions_screen"
    )
    summary: str = Field(description="One sentence of what the evidence shows")
    reference: str | None = Field(
        default=None,
        description="Id, URL or timestamp that lets an analyst re-check it",
    )


class AnomalyAlert(BaseModel):
    mmsi: int
    kind: AlertKind
    severity: Severity
    score: float = Field(ge=0, le=1, description="0 to 1, higher is more anomalous")
    started_at: str | None = None
    ended_at: str | None = None
    rationale: str
    evidence: list[Evidence] = []


class WatchAssessment(BaseModel):
    window: str
    alerts: list[AnomalyAlert]
    vessels_reviewed: int
    notes: str = ""


class InvestigationFindings(BaseModel):
    mmsi: int
    identity: str = Field(
        description="Name, IMO, flag, type as established from registry and AIS static data"
    )
    ownership: str = Field(
        description="Registered owner, operator, beneficial owner and how confident we are"
    )
    sanctions_exposure: str
    behaviour_summary: str = Field(
        description="What the vessel actually did, in time order, from AIS"
    )
    risk_indicators: list[str] = Field(
        description="Deceptive-shipping indicators observed, each tied to evidence"
    )
    counter_indicators: list[str] = Field(
        default_factory=list, description="Evidence of a benign explanation"
    )
    assessment: str
    confidence: Confidence
    evidence: list[Evidence]
    information_gaps: list[str] = []
    scope: str = Field(
        default="full", description="full | identity | behaviour (phase 4 branches)"
    )
    provenance: dict = Field(
        default_factory=dict,
        description="model provider/id/tier, prompt hash, attempts",
    )


class TaskingRecommendation(BaseModel):
    mmsi: int
    recommended: bool
    sensor: str | None = None
    tasking_id: str | None = Field(
        default=None,
        description="Id of the proposed request created for human approval",
    )
    aoi_center: list[float] | None = Field(default=None, description="[lon, lat]")
    aoi_radius_nm: float | None = None
    window_start: str | None = None
    window_end: str | None = None
    archived_scenes: list[str] = Field(
        default_factory=list, description="Existing scenes worth pulling first"
    )
    rationale: str
    provenance: dict = Field(default_factory=dict)


class VesselOfInterestReport(BaseModel):
    """Final product. Every claim must trace to an evidence entry."""

    mmsi: int
    vessel_name: str
    headline: str = Field(description="One sentence bottom line up front")
    priority: Severity
    confidence: Confidence
    summary: str
    timeline: list[str] = Field(
        description="Time-ordered bullet timeline of what happened"
    )
    identity_and_ownership: str
    sanctions_and_compliance: str
    indicators: list[str]
    counter_indicators: list[str] = []
    recommended_actions: list[str] = Field(
        description="Actions for a human watch officer; agents do not execute these"
    )
    collection_plan: str
    evidence: list[Evidence]
    information_gaps: list[str] = []
    caveats: str = Field(
        default="",
        description="Data provenance and limits; the orchestrator sets the source line",
    )

    @field_validator(
        "summary",
        "identity_and_ownership",
        "sanctions_and_compliance",
        "collection_plan",
        "caveats",
        mode="before",
    )
    @classmethod
    def _join_lists(cls, v):
        """Models sometimes return a bullet list where prose was asked for; keep the content."""
        if isinstance(v, list):
            return " ".join(str(x) for x in v)
        return v

    @field_validator(
        "timeline",
        "indicators",
        "counter_indicators",
        "recommended_actions",
        "information_gaps",
        mode="before",
    )
    @classmethod
    def _wrap_strings(cls, v):
        """And sometimes prose where a list was asked for."""
        if isinstance(v, str):
            return [line.strip(" -•") for line in v.splitlines() if line.strip(" -•")]
        return v
