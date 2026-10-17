from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.assessment import assess_opportunity, haversine_km, normalize_url, validate_extraction_evidence
from app.models import (
    AssessOpportunityOutput, CityRef, ExtractOpportunityOutput, Preferences, Profile,
    RequirementMatch, Opportunity,
)


FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_ID = UUID("00000000-0000-4000-8000-000000000103")
SOURCE_TEXT = "SAMPLE Data Analyst Intern. Hybrid in Islamabad, Pakistan. Basic Python familiarity preferred. PKR 30,000–50,000 per month."


def validated_copy(model, **updates):
    return type(model).model_validate({**model.model_dump(mode="python"), **updates})


@pytest.fixture
def base_data():
    profile_data = json.loads((FIXTURES / "sample_profile.json").read_text(encoding="utf-8"))
    profile_data.pop("sample_label")
    opportunity_data = json.loads((FIXTURES / "sample_opportunity.json").read_text(encoding="utf-8"))
    opportunity_data.pop("sample_label")
    opportunity = Opportunity.model_validate(opportunity_data)
    profile = Profile.model_validate(profile_data)
    prefs = Preferences(
        id=UUID("00000000-0000-4000-8000-000000000201"), version=1,
        country_code="PK", country_scope="open_to_country", role_queries=["data analyst"],
        opportunity_types=["internship"], seniority=["any"], work_arrangements=["hybrid"],
        location_mode="anywhere", preferred_cities=[], preferred_cities_policy="preferred", excluded_cities=[], proximity=None,
        paid_policy="any", right_to_work="unknown", needs_sponsorship="unknown", availability_note=None,
        career_interests=[], preferred_industries=[], excluded_industries=[], employment_commitment="either",
        relocation_policy="unknown", company_search_mode="watchlist_plus_discovery", company_size_preferences=[], company_stage_preferences=[],
        compensation_preferences=[dict(opportunity_type="internship", preferred_amount=40000, minimum_amount=30000, currency="PKR", period="month", minimum_policy="preferred", include_undisclosed=True, allow_unpaid=True, basis="gross", component="base")],
    )
    match = AssessOpportunityOutput(
        requirement_matches=[RequirementMatch(requirement_id=opportunity.requirements[0].id, status="supported", profile_fact_ids=[profile.facts[0].id], explanation="Basic Python is in the confirmed profile.")],
        role_alignment="direct", role_explanation="Data analysis aligns with the target role.", experience_alignment="unknown",
    )
    return profile, opportunity, prefs, match


def test_url_normalization_preserves_job_ids_and_removes_tracking():
    assert normalize_url("HTTPS://Jobs.Example/path?job=42&utm_source=mail#description") == "https://jobs.example/path?job=42"
    with pytest.raises(ValueError):
        normalize_url("https://user:pass@example.com/role")


def test_haversine_distance_and_evidence_quote_validation(base_data):
    profile, opportunity, _prefs, _match = base_data
    assert haversine_km(0, 0, 0, 1) == pytest.approx(111.195, abs=0.01)
    for evidence in opportunity.field_evidence:
        from app.models import validate_evidence
        validate_evidence(evidence.evidence, SOURCE_ID, SOURCE_TEXT)
    with pytest.raises(ValueError):
        from app.models import EvidenceRef, validate_evidence
        validate_evidence(EvidenceRef(source_id=SOURCE_ID, quote="invented employer"), SOURCE_ID, SOURCE_TEXT)


def test_country_exclusion_wins_over_high_score_and_worldwide_remote_is_scope_sensitive(base_data):
    profile, opportunity, prefs, model = base_data
    wrong_country = validated_copy(opportunity, work_countries=["US"], work_cities=[], work_arrangement="onsite", remote_scope="not_remote")
    result = assess_opportunity(opportunity=wrong_country, profile=profile, preferences=prefs, model_assessment=model, source_text=SOURCE_TEXT)
    assert result.bucket == "excluded"
    assert next(rule for rule in result.eligibility_rules if rule.rule == "country_scope").outcome == "fail"
    worldwide = opportunity.model_copy(update={"work_countries":[], "work_cities":[], "work_arrangement":"remote", "remote_scope":"worldwide", "remote_countries":[], "availability":"open"})
    remote_prefs = validated_copy(prefs, work_arrangements=["hybrid", "remote"])
    assert assess_opportunity(opportunity=worldwide, profile=profile, preferences=remote_prefs, model_assessment=model, source_text=SOURCE_TEXT).bucket == "match"
    local_only = validated_copy(remote_prefs, country_scope="jobs_in_country")
    assert assess_opportunity(opportunity=worldwide, profile=profile, preferences=local_only, model_assessment=model, source_text=SOURCE_TEXT).bucket == "excluded"


def test_explicit_exclusion_and_unknown_authorization_are_not_scored_as_matches(base_data):
    profile, opportunity, prefs, model = base_data
    excluded = validated_copy(opportunity, excluded_applicant_countries=["PK"])
    assert assess_opportunity(opportunity=excluded, profile=profile, preferences=prefs, model_assessment=model, source_text=SOURCE_TEXT).bucket == "excluded"
    requires_auth = validated_copy(opportunity, requires_local_work_authorization="yes", sponsorship_available="unknown")
    unknown_user = validated_copy(prefs, right_to_work="unknown")
    result = assess_opportunity(opportunity=requires_auth, profile=profile, preferences=unknown_user, model_assessment=model, source_text=SOURCE_TEXT)
    assert result.bucket == "needs_verification"
    no_sponsor = validated_copy(requires_auth, sponsorship_available="no")
    no_work = validated_copy(prefs, right_to_work="no")
    assert assess_opportunity(opportunity=no_sponsor, profile=profile, preferences=no_work, model_assessment=model, source_text=SOURCE_TEXT).bucket == "excluded"


def test_excluded_office_does_not_hide_another_available_office(base_data):
    profile, opportunity, prefs, model = base_data
    lahore = CityRef(id="pk-lahore", name="Lahore", country_code="PK", admin_area=None, latitude=None, longitude=None, coordinate_precision="unknown")
    prefs = validated_copy(prefs, excluded_cities=[opportunity.work_cities[0]], location_mode="cities", preferred_cities=[lahore], preferred_cities_policy="required")
    multi_office = validated_copy(opportunity, work_countries=["PK"], work_cities=[opportunity.work_cities[0], lahore], availability="open")
    result = assess_opportunity(opportunity=multi_office, profile=profile, preferences=prefs, model_assessment=model, source_text=SOURCE_TEXT)
    assert result.bucket == "match"
    assert next(rule for rule in result.eligibility_rules if rule.rule == "excluded_cities").outcome == "pass"
    only_excluded = validated_copy(multi_office, work_cities=[opportunity.work_cities[0]])
    result = assess_opportunity(opportunity=only_excluded, profile=profile, preferences=prefs, model_assessment=model, source_text=SOURCE_TEXT)
    assert result.bucket == "excluded"


def test_city_center_radius_is_uncertain_even_inside_and_exact_outside_fails(base_data):
    profile, opportunity, prefs, model = base_data
    ref = CityRef(id="pk-ref", name="Islamabad", country_code="PK", admin_area=None, latitude=33.7, longitude=73.0, coordinate_precision="exact")
    proximity = {"reference":ref, "radius_km":25, "policy":"required"}
    nearby = validated_copy(prefs, location_mode="nearby", proximity=proximity)
    result = assess_opportunity(opportunity=opportunity, profile=profile, preferences=nearby, model_assessment=model, source_text=SOURCE_TEXT)
    assert result.distance_precision == "approximate" and result.bucket == "needs_verification"
    exact_office = validated_copy(opportunity.work_cities[0], latitude=33.6844, longitude=73.0479, coordinate_precision="exact")
    exact_opportunity = validated_copy(opportunity, work_cities=[exact_office], availability="open")
    result = assess_opportunity(opportunity=exact_opportunity, profile=profile, preferences=nearby, model_assessment=model, source_text=SOURCE_TEXT)
    assert result.bucket == "match" and result.distance_precision == "exact"
    far_ref = validated_copy(ref, latitude=24.8607, longitude=67.0011)
    far_nearby = validated_copy(nearby, proximity={"reference":far_ref, "radius_km":25, "policy":"required"})
    result = assess_opportunity(opportunity=exact_opportunity, profile=profile, preferences=far_nearby, model_assessment=model, source_text=SOURCE_TEXT)
    assert result.bucket == "excluded"


def test_paid_only_unknown_salary_and_study_projects_do_not_become_employment(base_data):
    profile, opportunity, prefs, model = base_data
    no_salary = validated_copy(opportunity, compensation="unknown", salary_details={"min_amount":None,"max_amount":None,"currency":None,"period":"unknown","basis":"unknown","component":"unknown","evidence_refs":[]})
    strict = validated_copy(prefs, paid_policy="paid_only")
    result = assess_opportunity(opportunity=no_salary, profile=profile, preferences=strict, model_assessment=model, source_text=SOURCE_TEXT)
    assert result.bucket == "needs_verification"
    assert result.score_components.experience == 0
    project_id = profile.facts[2].id
    bad_match = model.model_copy(update={"requirement_matches":[model.requirement_matches[0].model_copy(update={"profile_fact_ids":[project_id]})]})
    result = assess_opportunity(opportunity=opportunity, profile=profile, preferences=prefs, model_assessment=bad_match, source_text=SOURCE_TEXT)
    assert result.score_components.experience == 0


def test_undisclosed_salary_exclusion_applies_without_a_minimum(base_data):
    profile, opportunity, prefs, model = base_data
    undisclosed = validated_copy(opportunity, compensation="paid", salary_details={"min_amount":None,"max_amount":None,"currency":None,"period":"unknown","basis":"unknown","component":"unknown","evidence_refs":[]})
    strict = validated_copy(prefs, compensation_preferences=[prefs.compensation_preferences[0].model_copy(update={"minimum_amount":None,"include_undisclosed":False})])
    result = assess_opportunity(opportunity=undisclosed, profile=profile, preferences=strict, model_assessment=model, source_text=SOURCE_TEXT)
    assert result.bucket == "excluded"
    assert next(rule for rule in result.eligibility_rules if rule.rule == "salary_disclosure").outcome == "fail"


def test_watchlist_only_excludes_unknown_and_blocked_companies(base_data):
    profile, opportunity, prefs, model = base_data
    watchlist = validated_copy(prefs, company_search_mode="watchlist_only")
    unknown = assess_opportunity(opportunity=opportunity, profile=profile, preferences=watchlist, model_assessment=model, source_text=SOURCE_TEXT)
    blocked = assess_opportunity(opportunity=opportunity, profile=profile, preferences=watchlist, model_assessment=model, source_text=SOURCE_TEXT, company_status="blocked")
    assert unknown.bucket == "excluded" and blocked.bucket == "excluded"
    approved = assess_opportunity(opportunity=opportunity, profile=profile, preferences=watchlist, model_assessment=model, source_text=SOURCE_TEXT, company_status="approved")
    assert approved.bucket != "excluded"


def test_role_assessment_context_changes_with_target_role(base_data):
    from app.runner import _assessment_payload
    profile, opportunity, prefs, _model = base_data
    analyst = _assessment_payload(opportunity, profile, prefs)
    recruiter_prefs = validated_copy(prefs, role_queries=["recruiter"])
    recruiter = _assessment_payload(opportunity, profile, recruiter_prefs)
    assert analyst["JOB"]["title"] == opportunity.title
    assert analyst["TARGET_ROLES"] == ["data analyst"]
    assert recruiter["TARGET_ROLES"] == ["recruiter"]
    assert analyst["JOB_REQUIREMENTS"] == recruiter["JOB_REQUIREMENTS"]


def test_extraction_prompt_supplies_host_owned_source_id():
    from datetime import datetime, timezone
    from app.assessment import extract_opportunity_fields
    from app.models import SourceDocument

    captured = {}
    class CaptureAdapter:
        async def generate_structured(self, **kwargs):
            captured.update(kwargs["user_payload"])
            raise RuntimeError("stop after capturing the request")
    doc = SourceDocument(id=SOURCE_ID, user_id=UUID(int=1), run_id=UUID(int=2), url="https://jobs.example.invalid/role", title="Role",
        source_kind="extracted_page", body_text=SOURCE_TEXT, truncated=False, retrieved_at=datetime.now(timezone.utc), published_at=None,
        extraction_status="retrieved", provider_request_id=None)
    request = extract_opportunity_fields(CaptureAdapter(), doc)
    with pytest.raises(RuntimeError):
        request.send(None)
    assert captured["SOURCE_ID"] == str(SOURCE_ID)
def test_extracted_evidence_must_belong_to_saved_source():
    extracted = ExtractOpportunityOutput.model_validate({"page_type":"individual_job", "opportunity":{
        "title":"Analyst", "employer":"Acme", "opportunity_type":"job", "seniority":"entry", "work_arrangement":"onsite",
        "work_countries":["PK"], "work_cities":[], "remote_scope":"not_remote", "remote_countries":[], "remote_regions":[],
        "excluded_applicant_countries":[], "requires_local_work_authorization":"unknown", "sponsorship_available":"unknown",
        "compensation":"unknown", "salary_text":None,
        "salary_details":{"min_amount":None,"max_amount":None,"currency":None,"period":"unknown","basis":"unknown","component":"unknown","evidence_refs":[]},
        "minimum_years":None,"posted_at":None,"closes_at":None,"availability":"unknown","requirements":[],
        "field_evidence":[
            {"field":"title","evidence":{"source_id":str(SOURCE_ID),"quote":"Analyst"}},
            {"field":"employer","evidence":{"source_id":str(SOURCE_ID),"quote":"FakeCorp"}},
            {"field":"workplace","evidence":{"source_id":str(SOURCE_ID),"quote":"Pakistan workplace"}},
            {"field":"opportunity_type","evidence":{"source_id":str(SOURCE_ID),"quote":"job"}},
            {"field":"work_arrangement","evidence":{"source_id":str(SOURCE_ID),"quote":"onsite"}},
        ],
    }})
    with pytest.raises(ValueError):
        validate_extraction_evidence(extracted, SOURCE_ID, "Analyst at Acme. Pakistan workplace. On-site job.")
