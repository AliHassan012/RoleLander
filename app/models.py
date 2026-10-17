from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


def _valid_country(value: str) -> str:
    import pycountry

    if pycountry.countries.get(alpha_2=value) is None:
        raise ValueError("Country code must be an ISO 3166-1 alpha-2 code")
    return value


def _valid_currency(value: str) -> str:
    import pycountry

    if pycountry.currencies.get(alpha_3=value) is None:
        raise ValueError("Currency code must be an ISO 4217 code")
    return value


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class EvidenceRef(ContractModel):
    source_id: UUID
    quote: str = Field(min_length=1, max_length=600)


class Issue(ContractModel):
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=500)
    field: str | None


class ProfileFact(ContractModel):
    id: UUID
    kind: Literal["skill", "employment", "project", "education", "achievement", "language", "availability", "other"]
    label: str = Field(min_length=1, max_length=160)
    value: str = Field(min_length=1, max_length=2000)
    level: Literal["basic", "intermediate", "advanced", "unknown"] | None
    origin: Literal["cv", "user"]
    source_quote: str | None = Field(max_length=600)
    confirmed: bool

    @model_validator(mode="after")
    def source_matches_origin(self):
        if self.origin == "cv" and not self.source_quote:
            raise ValueError("CV facts require a source quote")
        if self.origin == "user" and self.source_quote is not None:
            raise ValueError("User supplied facts do not claim a CV quote")
        if self.kind != "skill" and self.level is not None:
            raise ValueError("Only skills can have a proficiency level")
        return self


class Profile(ContractModel):
    id: UUID
    version: int = Field(ge=1)
    display_name: str | None = Field(max_length=160)
    professional_years: Decimal | None = Field(ge=0)
    facts: list[ProfileFact]
    confirmed_at: datetime | None
    created_at: datetime

    @field_validator("confirmed_at", "created_at")
    @classmethod
    def timestamps_are_aware(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("Timestamps must include a timezone")
        return value


class CityRef(ContractModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=160)
    country_code: str = Field(pattern=r"^[A-Z]{2}$")
    admin_area: str | None = Field(max_length=160)
    latitude: float | None = Field(ge=-90, le=90)
    longitude: float | None = Field(ge=-180, le=180)
    coordinate_precision: Literal["city", "exact", "unknown"]

    @field_validator("country_code")
    @classmethod
    def valid_country(cls, value):
        return _valid_country(value)

    @model_validator(mode="after")
    def coordinate_pair(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Latitude and longitude must be present together")
        if self.latitude is not None and self.coordinate_precision == "unknown":
            raise ValueError("Coordinates require a known precision")
        return self


class Proximity(ContractModel):
    reference: CityRef
    radius_km: int = Field(ge=1, le=500)
    policy: Literal["required", "preferred"]


class CompensationPreference(ContractModel):
    opportunity_type: Literal["job", "internship"]
    preferred_amount: Decimal | None = Field(ge=0)
    minimum_amount: Decimal | None = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    period: Literal["month", "year"]
    minimum_policy: Literal["required", "preferred"]
    include_undisclosed: bool
    allow_unpaid: bool
    basis: Literal["gross", "net"]
    component: Literal["base", "total"]

    @field_validator("currency")
    @classmethod
    def valid_currency(cls, value):
        return _valid_currency(value)

    @model_validator(mode="after")
    def threshold_order(self):
        if self.opportunity_type == "job" and self.allow_unpaid:
            raise ValueError("Unpaid acceptance applies only to internships")
        if self.preferred_amount is not None and self.minimum_amount is not None:
            if self.preferred_amount < self.minimum_amount:
                raise ValueError("Preferred salary must be at least the minimum")
        return self


class Preferences(ContractModel):
    id: UUID
    version: int = Field(ge=1)
    country_code: str = Field(pattern=r"^[A-Z]{2}$")
    country_scope: Literal["jobs_in_country", "open_to_country"]
    role_queries: list[str] = Field(min_length=1, max_length=3)
    opportunity_types: list[Literal["job", "internship"]] = Field(min_length=1)
    seniority: list[Literal["intern", "entry", "mid", "senior", "lead", "executive", "any"]] = Field(min_length=1)
    work_arrangements: list[Literal["onsite", "hybrid", "remote"]] = Field(min_length=1)
    location_mode: Literal["anywhere", "cities", "nearby"]
    preferred_cities: list[CityRef] = Field(default_factory=list)
    preferred_cities_policy: Literal["required", "preferred"]
    excluded_cities: list[CityRef] = Field(default_factory=list)
    proximity: Proximity | None
    paid_policy: Literal["any", "paid_only"]
    right_to_work: Literal["yes", "no", "unknown"]
    needs_sponsorship: Literal["yes", "no", "unknown"]
    availability_note: str | None = Field(max_length=500)
    career_interests: list[str] = Field(default_factory=list)
    preferred_industries: list[str] = Field(default_factory=list)
    excluded_industries: list[str] = Field(default_factory=list)
    employment_commitment: Literal["full_time", "part_time", "either"]
    relocation_policy: Literal["yes", "no", "unknown"]
    company_search_mode: Literal["watchlist_only", "watchlist_plus_discovery"]
    company_size_preferences: list[str] = Field(default_factory=list)
    company_stage_preferences: list[str] = Field(default_factory=list)
    compensation_preferences: list[CompensationPreference]

    @field_validator("country_code")
    @classmethod
    def valid_country(cls, value):
        return _valid_country(value)

    @field_validator("role_queries")
    @classmethod
    def role_query_lengths(cls, values):
        if any(not 2 <= len(value.strip()) <= 80 for value in values):
            raise ValueError("Each role query must contain 2 to 80 characters")
        return values

    @model_validator(mode="after")
    def coherent_preferences(self):
        if ("any" in self.seniority) and len(self.seniority) != 1:
            raise ValueError("Seniority 'any' must be selected alone")
        if self.location_mode == "nearby" and self.proximity is None:
            raise ValueError("Nearby mode requires a reference and radius")
        if self.location_mode == "cities" and not self.preferred_cities:
            raise ValueError("Selected-cities mode requires at least one preferred city")
        if self.proximity is not None and self.proximity.reference.country_code != self.country_code:
            raise ValueError("Nearby reference must belong to the selected country")
        if self.location_mode != "nearby" and self.proximity is not None:
            raise ValueError("Proximity is only valid in nearby mode")
        if len(set(self.opportunity_types)) != len(self.opportunity_types):
            raise ValueError("Opportunity types must be unique")
        expected = set(self.opportunity_types)
        actual = {item.opportunity_type for item in self.compensation_preferences}
        if actual != expected or len(actual) != len(self.compensation_preferences):
            raise ValueError("Add exactly one compensation preference per selected type")
        cities = {(city.country_code, city.name.casefold()) for city in self.preferred_cities}
        excluded = {(city.country_code, city.name.casefold()) for city in self.excluded_cities}
        if cities & excluded:
            raise ValueError("A city cannot be both preferred and excluded")
        for city in [*self.preferred_cities, *self.excluded_cities]:
            if city.country_code != self.country_code:
                raise ValueError("Cities must belong to the selected country")
        return self


class Company(ContractModel):
    id: UUID
    name: str = Field(min_length=1, max_length=200)
    canonical_domain: str = Field(min_length=3, max_length=253)
    careers_url: AnyHttpUrl
    adapter: Literal["greenhouse", "lever", "public_search", "unavailable"]
    adapter_identifier: str | None = Field(max_length=200)
    adapter_region: Literal["global", "eu"] | None
    evidence_refs: list[EvidenceRef]
    verified_at: datetime | None


class UserCompany(ContractModel):
    user_id: UUID
    company_id: UUID
    status: Literal["suggested", "approved", "blocked"]
    preferred: bool
    priority: int = Field(ge=0, le=100)
    origin: Literal["user", "recommendation"]
    approved_at: datetime | None
    version: int = Field(ge=1)

    @model_validator(mode="after")
    def approval_timestamp(self):
        if self.status == "approved" and self.approved_at is None:
            raise ValueError("Approved companies require a human approval timestamp")
        if self.status != "approved" and self.approved_at is not None:
            raise ValueError("Only approved companies may have an approval timestamp")
        if self.status == "blocked" and self.preferred:
            raise ValueError("Blocked companies cannot be preferred")
        return self


class SalaryDetails(ContractModel):
    min_amount: Decimal | None = Field(ge=0)
    max_amount: Decimal | None = Field(ge=0)
    currency: str | None = Field(pattern=r"^[A-Z]{3}$")
    period: Literal["hour", "day", "month", "year", "unknown"]
    basis: Literal["gross", "net", "unknown"]
    component: Literal["base", "total", "unknown"]
    evidence_refs: list[EvidenceRef]

    @field_validator("currency")
    @classmethod
    def valid_currency(cls, value):
        return _valid_currency(value) if value is not None else value

    @model_validator(mode="after")
    def range_and_currency(self):
        if self.min_amount is not None and self.max_amount is not None and self.min_amount > self.max_amount:
            raise ValueError("Salary lower bound cannot exceed upper bound")
        if (self.min_amount is not None or self.max_amount is not None) and self.currency is None:
            raise ValueError("A stated salary amount requires an explicit currency")
        return self


class NormalizedCompensation(ContractModel):
    fx_snapshot_id: UUID | None
    target_currency: str | None = Field(pattern=r"^[A-Z]{3}$")
    target_period: Literal["month", "year"] | None
    minimum_amount: Decimal | None = Field(ge=0)
    maximum_amount: Decimal | None = Field(ge=0)
    warnings: list[str]
    stale: bool

    @model_validator(mode="after")
    def converted_range(self):
        if self.minimum_amount is not None and self.maximum_amount is not None and self.minimum_amount > self.maximum_amount:
            raise ValueError("Normalized salary lower bound cannot exceed upper bound")
        return self


class Requirement(ContractModel):
    id: UUID
    text: str = Field(min_length=1, max_length=500)
    importance: Literal["required", "preferred", "unknown"]
    evidence: EvidenceRef


class FieldEvidence(ContractModel):
    field: str = Field(min_length=1, max_length=100)
    evidence: EvidenceRef


class Opportunity(ContractModel):
    id: UUID
    user_id: UUID
    run_id: UUID
    source_id: UUID
    company_id: UUID | None
    stable_source_key: str = Field(min_length=1, max_length=500)
    discovery_sources: list[str]
    canonical_url: AnyHttpUrl
    page_type: Literal["individual_job", "collection", "article", "unknown"]
    title: str | None = Field(max_length=300)
    employer: str | None = Field(max_length=200)
    opportunity_type: Literal["job", "internship", "unknown"]
    seniority: Literal["intern", "entry", "mid", "senior", "lead", "executive", "unknown"]
    work_arrangement: Literal["onsite", "hybrid", "remote", "unknown"]
    work_countries: list[str]
    work_cities: list[CityRef]
    remote_scope: Literal["worldwide", "countries", "regional", "unknown", "not_remote"]
    remote_countries: list[str]
    remote_regions: list[str]
    excluded_applicant_countries: list[str]
    requires_local_work_authorization: Literal["yes", "no", "unknown"]
    sponsorship_available: Literal["yes", "no", "unknown"]
    compensation: Literal["paid", "unpaid", "unknown"]
    salary_text: str | None = Field(max_length=500)
    salary_details: SalaryDetails
    normalized_compensation: NormalizedCompensation | None
    minimum_years: Decimal | None = Field(ge=0)
    posted_at: datetime | None
    closes_at: datetime | None
    availability: Literal["open", "closed", "unknown"]
    requirements: list[Requirement] = Field(max_length=12)
    field_evidence: list[FieldEvidence]
    source_status: Literal["preview", "retrieved", "user_provided"]

    @field_validator("work_countries", "remote_countries", "excluded_applicant_countries")
    @classmethod
    def valid_country_codes(cls, values):
        return [_valid_country(value) for value in values]


class SourceDocument(ContractModel):
    id: UUID
    user_id: UUID
    run_id: UUID
    url: AnyHttpUrl
    title: str = Field(max_length=500)
    source_kind: Literal["search_preview", "extracted_page", "employer_api", "job_feed", "user_text"]
    body_text: str = Field(max_length=20000)
    truncated: bool
    retrieved_at: datetime
    published_at: datetime | None
    extraction_status: Literal["preview", "retrieved", "unavailable"]
    provider_request_id: str | None = Field(max_length=200)


class SearchHit(ContractModel):
    source_id: UUID
    title: str = Field(max_length=500)
    url: AnyHttpUrl
    snippet: str = Field(max_length=4000)
    provider_rank: int = Field(ge=0)


class ScoreComponents(ContractModel):
    skills: int = Field(ge=0, le=30)
    role: int = Field(ge=0, le=25)
    experience: int = Field(ge=0, le=15)
    location_preference: int = Field(ge=0, le=10)
    salary_preference: int = Field(ge=0, le=10)
    company_preference: int = Field(ge=0, le=5)
    recency: int = Field(ge=0, le=5)


class RequirementMatch(ContractModel):
    requirement_id: UUID
    status: Literal["supported", "partial", "not_evidenced", "unknown"]
    profile_fact_ids: list[UUID]
    explanation: str = Field(max_length=500)


class RuleResult(ContractModel):
    rule: str = Field(min_length=1, max_length=80)
    outcome: Literal["pass", "fail", "unknown", "not_applicable"]
    explanation: str = Field(max_length=500)
    evidence: list[EvidenceRef]


class Assessment(ContractModel):
    id: UUID
    opportunity_id: UUID
    profile_version: int = Field(ge=1)
    preference_version: int = Field(ge=1)
    requirement_matches: list[RequirementMatch]
    role_alignment: Literal["direct", "adjacent", "unrelated", "unknown"]
    role_explanation: str = Field(max_length=500)
    experience_alignment: Literal["meets", "stretch", "unknown"]
    eligibility_rules: list[RuleResult]
    distance_km: float | None = Field(ge=0)
    distance_precision: Literal["exact", "approximate", "not_applicable", "unknown"]
    bucket: Literal["match", "needs_verification", "excluded"]
    score: int | None = Field(ge=0, le=100)
    score_components: ScoreComponents
    warnings: list[str]

    @model_validator(mode="after")
    def final_score_matches_components(self):
        if self.score is not None and self.score != sum(self.score_components.model_dump().values()):
            raise ValueError("Final score must equal the server-computed components")
        return self


class ExchangeRateSnapshot(ContractModel):
    id: UUID
    provider: Literal["exchangerate-api"]
    base: Literal["USD"]
    rates: dict[str, Decimal]
    provider_updated_at: datetime
    next_update_at: datetime
    fetched_at: datetime

    @field_validator("rates")
    @classmethod
    def valid_rates(cls, value):
        if not value or "USD" not in value or any(not code.isalpha() or len(code) != 3 for code in value):
            raise ValueError("FX table must contain ISO-like currency codes and USD")
        if any(not rate.is_finite() or rate <= 0 for rate in value.values()):
            raise ValueError("FX rates must be positive finite decimals")
        return value


class MonitoringSettings(ContractModel):
    user_id: UUID
    enabled: bool = False
    frequency: Literal["manual", "daily", "weekly"]
    local_time: str | None
    timezone: str | None
    weekday: int | None = Field(ge=0, le=6)
    company_enabled: bool
    gmail_enabled: bool
    discovery_enabled: bool
    notify_in_app: bool
    notify_email: bool
    notification_account_id: UUID | None
    next_run_at: datetime | None
    version: int = Field(ge=1)

    @model_validator(mode="after")
    def schedule_fields(self):
        scheduled = self.frequency in {"daily", "weekly"}
        if scheduled and (self.local_time is None or self.timezone is None):
            raise ValueError("Daily/weekly schedules require local time and timezone")
        if scheduled and self.frequency == "weekly" and self.weekday is None:
            raise ValueError("Weekly schedules require a weekday")
        if self.local_time is not None:
            try:
                datetime.strptime(self.local_time, "%H:%M")
            except ValueError as exc:
                raise ValueError("local_time must use HH:MM") from exc
        if self.timezone is not None:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(self.timezone)
            except ZoneInfoNotFoundError as exc:
                raise ValueError("timezone must be a valid IANA timezone") from exc
        if self.notify_email and self.notification_account_id is None:
            raise ValueError("Email notifications require a selected connected account")
        return self


class NotificationDelivery(ContractModel):
    user_id: UUID
    event_key: str
    channel: Literal["in_app", "email"]
    destination: str
    state: Literal["pending", "sending", "sent", "failed", "unknown"]
    created_at: datetime
    updated_at: datetime


class SearchRun(ContractModel):
    id: UUID
    user_id: UUID
    state: Literal["queued", "running", "awaiting_decision", "completed", "failed", "interrupted", "cancelled"]
    state_version: int = Field(ge=1)
    profile_id: UUID
    profile_version: int = Field(ge=1)
    preference_id: UUID
    preference_version: int = Field(ge=1)
    action_count: int = Field(ge=0, le=8)
    search_call_count: int = Field(ge=0, le=4)
    extract_call_count: int = Field(ge=0, le=2)
    llm_call_count: int = Field(ge=0, le=22)
    decision_count: int = Field(ge=0, le=2)
    trigger: Literal["manual", "scheduled"]
    occurrence_id: UUID | None
    approved_source_snapshot: list[UUID]
    source_progress: dict
    deferred_counts: dict[str, int]
    claim_lease_until: datetime | None
    started_at: datetime | None
    updated_at: datetime
    last_error: Issue | None


class DecisionOption(ContractModel):
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    explanation: str = Field(min_length=1, max_length=500)
    patch: dict | None
    action: Literal["apply_preferences", "add_candidate_fact", "finish"]


class HumanDecision(ContractModel):
    id: UUID
    run_id: UUID
    state_version: int = Field(ge=1)
    kind: Literal["preference_tradeoff", "candidate_fact"]
    question: str = Field(min_length=1, max_length=800)
    options: list[DecisionOption] = Field(min_length=1, max_length=3)
    state: Literal["pending", "answered", "cancelled"]
    selected_option_id: str | None
    answer_note: str | None = Field(max_length=1000)
    created_at: datetime
    answered_at: datetime | None

    @model_validator(mode="after")
    def selected_option_exists(self):
        option_ids = {item.id for item in self.options}
        if self.selected_option_id is not None and self.selected_option_id not in option_ids:
            raise ValueError("Selected decision option must exist")
        if self.state == "pending" and self.selected_option_id is not None:
            raise ValueError("Pending decisions cannot have a selected option")
        return self


class RunEvent(ContractModel):
    id: UUID
    run_id: UUID
    sequence: int = Field(ge=1)
    type: Literal["started", "search", "detail", "assessment", "decision_requested", "decision_answered", "resumed", "finished", "warning", "error"]
    message: str = Field(min_length=1, max_length=500)
    metadata: dict
    created_at: datetime


class SavedOpportunity(ContractModel):
    id: UUID
    user_id: UUID
    opportunity_id: UUID
    status: Literal["interested", "preparing", "applied", "interview", "rejected", "dismissed"]
    note: str | None = Field(max_length=1000)
    created_at: datetime
    updated_at: datetime


class DraftSentence(ContractModel):
    text: str = Field(min_length=1, max_length=2000)
    profile_fact_ids: list[UUID]
    requirement_ids: list[UUID]
    is_personal_claim: bool
    needs_user_input: bool


class ApplicationDraft(ContractModel):
    id: UUID
    user_id: UUID
    opportunity_id: UUID
    profile_version: int = Field(ge=1)
    kind: Literal["email", "cover_letter"]
    subject: str | None = Field(max_length=200)
    sentences: list[DraftSentence]
    edited_text: str | None = Field(max_length=10000)
    version: int = Field(ge=1)
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ConnectedAccountRecord(ContractModel):
    id: UUID
    user_id: UUID
    provider: Literal["google"]
    subject: str
    verified_email: str
    granted_scopes: list[str]
    encrypted_refresh_token: SecretStr | None
    access_expires_at: datetime | None
    read_enabled: bool
    send_self_enabled: bool
    connection_state: Literal["connected", "reconnect_required", "disconnected"]
    version: int = Field(ge=1)


class ConnectedAccountSummary(ContractModel):
    id: UUID
    provider: Literal["google"]
    verified_email: str
    granted_scopes: list[str]
    read_enabled: bool
    send_self_enabled: bool
    connection_state: Literal["connected", "reconnect_required", "disconnected"]
    version: int = Field(ge=1)


class GmailSource(ContractModel):
    id: UUID
    user_id: UUID
    account_id: UUID
    enabled: bool
    effective_query: str = Field(max_length=1000)
    labels: list[str]
    senders: list[str]
    backfill_started_at: datetime | None
    completed_cursor: str | None
    pending_page_token: str | None
    last_success_at: datetime | None
    last_error: Issue | None


class PrivateLead(ContractModel):
    id: UUID
    user_id: UUID
    account_id: UUID
    message_id: str
    safe_subject: str = Field(max_length=300)
    public_urls: list[AnyHttpUrl]
    review_status: Literal["new", "needs_review", "dismissed", "linked"]
    received_at: datetime


class ScheduleOccurrence(ContractModel):
    id: UUID
    user_id: UUID
    settings_version: int = Field(ge=1)
    due_at: datetime
    state: Literal["queued", "claimed", "completed", "coalesced", "cancelled"]


class SourceCheck(ContractModel):
    id: UUID
    user_id: UUID
    source_key: str = Field(min_length=1, max_length=300)
    status: Literal["working", "failed", "partial", "complete", "not_configured"]
    coverage: Literal["complete", "partial", "unknown", "unsupported"]
    progress: dict
    checked_at: datetime


class NotificationItem(ContractModel):
    id: UUID
    user_id: UUID
    event_key: str
    payload: dict
    read_at: datetime | None


class CompanyRecommendation(ContractModel):
    id: UUID
    user_id: UUID
    company: Company
    why_it_fits: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[EvidenceRef] = Field(min_length=1)
    checked_at: datetime
    relevant_opening: bool | None


class CompanyRecommendationOperation(ContractModel):
    id: UUID
    user_id: UUID
    state: Literal["queued", "running", "completed", "failed", "interrupted"]
    search_attempts: int = Field(ge=0, le=4)
    extract_batches: int = Field(ge=0, le=2)
    structured_ai_attempts: int = Field(ge=0, le=2)
    recommendations: list[CompanyRecommendation] = Field(max_length=10)
    created_at: datetime
    updated_at: datetime


class CandidateFactProposal(ContractModel):
    kind: Literal["skill", "employment", "project", "education", "achievement", "language", "availability", "other"]
    label: str = Field(min_length=1, max_length=160)
    value: str = Field(min_length=1, max_length=2000)
    level: Literal["basic", "intermediate", "advanced", "unknown"] | None
    source_quote: str | None = Field(max_length=600)

    @model_validator(mode="after")
    def level_only_for_skill(self):
        if not self.source_quote:
            raise ValueError("CV fact proposals require a supporting quote")
        if self.kind != "skill" and self.level is not None:
            raise ValueError("Only skills may include proficiency")
        return self


class ExtractProfileOutput(ContractModel):
    display_name: str | None = Field(max_length=160)
    professional_years: Decimal | None = Field(ge=0)
    facts: list[CandidateFactProposal]


class DecisionProposal(ContractModel):
    kind: Literal["preference_tradeoff", "candidate_fact"]
    question: str = Field(min_length=1, max_length=300)
    preference_field: Literal[
        "role_queries", "work_arrangements", "seniority", "paid_policy",
        "opportunity_types", "preferred_cities_policy", "proximity.radius_km", "proximity.policy",
    ] | None
    proposed_value: str | bool | int | list[str] | None
    fact_question: str | None = Field(max_length=300)

    @model_validator(mode="after")
    def proposal_shape(self):
        if self.kind == "preference_tradeoff" and (self.preference_field is None or self.proposed_value is None):
            raise ValueError("Preference trade-offs require one allowed field and proposed value")
        if self.kind == "candidate_fact" and not self.fact_question:
            raise ValueError("Candidate-fact questions require a fact question")
        return self


class AgentAction(ContractModel):
    action: Literal["search", "inspect", "ask_user", "finish"]
    query: str | None = Field(max_length=500)
    opportunity_ids: list[UUID] = Field(max_length=5)
    decision_proposal: DecisionProposal | None
    public_summary: str = Field(max_length=240)

    @model_validator(mode="after")
    def action_fields_match(self):
        if self.action == "search" and (not self.query or self.opportunity_ids or self.decision_proposal is not None):
            raise ValueError("Search requires only a query")
        if self.action == "inspect" and (self.query is not None or not 1 <= len(self.opportunity_ids) <= 3 or self.decision_proposal is not None):
            raise ValueError("Inspect requires 1 to 3 existing opportunity IDs only")
        if self.action == "ask_user" and (self.query is not None or self.opportunity_ids or self.decision_proposal is None):
            raise ValueError("Ask-user requires only a decision proposal")
        if self.action == "finish" and (self.query is not None or self.opportunity_ids or self.decision_proposal is not None):
            raise ValueError("Finish accepts no other action inputs")
        return self


class OpportunityFields(ContractModel):
    title: str | None = Field(max_length=300)
    employer: str | None = Field(max_length=200)
    opportunity_type: Literal["job", "internship", "unknown"]
    seniority: Literal["intern", "entry", "mid", "senior", "lead", "executive", "unknown"]
    work_arrangement: Literal["onsite", "hybrid", "remote", "unknown"]
    work_countries: list[str]
    work_cities: list[CityRef]
    remote_scope: Literal["worldwide", "countries", "regional", "unknown", "not_remote"]
    remote_countries: list[str]
    remote_regions: list[str]
    excluded_applicant_countries: list[str]
    requires_local_work_authorization: Literal["yes", "no", "unknown"]
    sponsorship_available: Literal["yes", "no", "unknown"]
    compensation: Literal["paid", "unpaid", "unknown"]
    salary_text: str | None = Field(max_length=500)
    salary_details: SalaryDetails
    minimum_years: Decimal | None = Field(ge=0)
    posted_at: datetime | None
    closes_at: datetime | None
    availability: Literal["open", "closed", "unknown"]
    requirements: list[Requirement] = Field(max_length=12)
    field_evidence: list[FieldEvidence]

    @field_validator("work_countries", "remote_countries", "excluded_applicant_countries")
    @classmethod
    def valid_country_codes(cls, values):
        return [_valid_country(value) for value in values]


class ExtractOpportunityOutput(ContractModel):
    page_type: Literal["individual_job", "collection", "article", "unknown"]
    opportunity: OpportunityFields | None

    @model_validator(mode="after")
    def page_type_consistency(self):
        if self.page_type == "individual_job" and self.opportunity is None:
            raise ValueError("Individual job extraction requires vacancy fields")
        if self.page_type != "individual_job" and self.opportunity is not None:
            raise ValueError("Non-vacancy pages cannot contain vacancy fields")
        return self


class RoleAlignmentOutput(ContractModel):
    role_alignment: Literal["direct", "adjacent", "unrelated", "unknown"]
    role_explanation: str = Field(min_length=1, max_length=500)


class AssessOpportunityOutput(ContractModel):
    requirement_matches: list[RequirementMatch]
    role_alignment: Literal["direct", "adjacent", "unrelated", "unknown"]
    role_explanation: str = Field(max_length=500)
    experience_alignment: Literal["meets", "stretch", "unknown"]


class DraftOutput(ContractModel):
    subject: str | None = Field(max_length=200)
    sentences: list[DraftSentence]


def normalize_evidence_text(value: str) -> str:
    import unicodedata

    return " ".join(unicodedata.normalize("NFC", value).split())


def validate_evidence(ref: EvidenceRef, source_id: UUID, source_text: str) -> None:
    if ref.source_id != source_id or normalize_evidence_text(ref.quote) not in normalize_evidence_text(source_text):
        raise ValueError("Evidence quote does not occur in the named source")

