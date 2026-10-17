const $ = (selector) => document.querySelector(selector);
let csrfToken = "";
let editingProfile = null;
let displayedRunId = null;
let runRenderGeneration = 0;
const message = (element, text, kind = "") => { element.textContent = text; element.className = kind; };
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  if (options.method && options.method !== "GET") {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || body.error?.message || `Request failed (${response.status})`);
  return body;
}
function selectedValues(select) { return [...select.selectedOptions].map((option) => option.value); }
function lines(value) { return value.split(/\r?\n/).map((part) => part.trim()).filter(Boolean); }
function cityRef(name, countryCode) {
  const slug = name.trim().toLocaleLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  return { id: `${countryCode.toLowerCase()}-${slug}`, name: name.trim(), country_code: countryCode, admin_area: null, latitude: null, longitude: null, coordinate_precision: "unknown" };
}
function factData() {
  return [...document.querySelectorAll(".fact")].map((item) => {
    const kind = item.querySelector(".kind").value;
    return { kind, label: item.querySelector(".label").value.trim(), value: item.querySelector(".value").value.trim(), level: kind === "skill" ? item.querySelector(".level").value : null };
  }).filter((fact) => fact.label && fact.value);
}
async function refreshRunChoices() {
  const [profiles, preferences] = await Promise.all([api("/api/profiles"), api("/api/preferences")]);
  const profileSelect=$("#run-profile"), prefSelect=$("#run-preferences"); profileSelect.replaceChildren(); prefSelect.replaceChildren();
  const p0=document.createElement("option"); p0.value=""; p0.disabled=true; p0.selected=true; p0.textContent="Choose a confirmed profile"; profileSelect.append(p0);
  for(const item of profiles.items) { const option=document.createElement("option"); option.value=JSON.stringify({id:item.id,version:item.version}); option.disabled=!item.confirmed_at; option.textContent=`${item.display_name} · v${item.version} · ${item.fact_count} facts${item.confirmed_at?"":" · needs confirmation"}`; profileSelect.append(option); }
  const f0=document.createElement("option"); f0.value=""; f0.disabled=true; f0.selected=true; f0.textContent="Choose saved preferences"; prefSelect.append(f0);
  for(const item of preferences.items) { const option=document.createElement("option"); option.value=JSON.stringify({id:item.id,version:item.version}); option.textContent=`${item.role_queries.join(", ")} · ${item.country_code}/${item.country_scope} · v${item.version}`; prefSelect.append(option); }
}
async function refreshCompanyEvidence() {
  const select=$("#company-source"); select.replaceChildren();
  const empty=document.createElement("option");empty.value="";empty.disabled=true;empty.selected=true;empty.textContent="Choose a saved, successfully retrieved public page";select.append(empty);
  const runs=await api("/api/runs");
  for(const run of runs.items) {
    const sources=await api(`/api/runs/${run.id}/sources`);
    for(const source of sources.items) {
      if(source.source_status!=="retrieved"||source.truncated) continue;
      const option=document.createElement("option");option.value=source.id;option.textContent=`${source.title||"Untitled source"} · ${new URL(source.url).hostname}`;select.append(option);
    }
  }
}
async function refreshCompanies() {
  const host=$("#company-list");host.replaceChildren();
  try {
    const result=await api("/api/companies");
    if(!result.items.length){const empty=document.createElement("p");empty.textContent="No companies in your watchlist yet.";host.append(empty);return;}
    for(const item of result.items) {
      const company=item.company,membership=item.membership,card=document.createElement("article");card.className="result-card";
      const title=document.createElement("h3");title.textContent=company.name;card.append(title);
      const summary=document.createElement("p");summary.textContent=`${company.canonical_domain} · ${membership.status}${membership.preferred?" · preferred":""} · priority ${membership.priority}`;card.append(summary);
      const link=document.createElement("a");link.href=company.careers_url;link.target="_blank";link.rel="noopener noreferrer";link.textContent="Open careers page";card.append(link);
      const actions=document.createElement("div");actions.className="actions";
      if(membership.status==="suggested") { const approve=document.createElement("button");approve.className="button";approve.textContent="Approve company";approve.addEventListener("click",async()=>{approve.disabled=true;try{await api(`/api/companies/${company.id}/approve`,{method:"POST",body:JSON.stringify({expected_version:membership.version,preferred:false,priority:membership.priority})});await refreshCompanies();}catch(error){approve.disabled=false;message(host,error.message,"error");}});actions.append(approve); }
      if(membership.status==="approved"&&["greenhouse","lever"].includes(company.adapter)) { const check=document.createElement("button");check.className="secondary";check.textContent="Check public board";check.addEventListener("click",async()=>{check.disabled=true;try{const result=await api(`/api/companies/${company.id}/check`,{method:"POST"});const coverage=document.createElement("p");coverage.textContent=`Board coverage: ${result.status}; ${result.postings.length} postings checked across ${result.pages_fetched} pages${result.error?`. ${result.error}`:""}.`;card.append(coverage);for(const posting of result.postings){const row=document.createElement("p"),link=document.createElement("a");link.href=posting.url;link.target="_blank";link.rel="noopener noreferrer";link.textContent=posting.title;row.append(link);if(posting.location){const location=document.createElement("span");location.textContent=` · ${posting.location}`;row.append(location);}card.append(row);}}catch(error){message(host,error.message,"error");}finally{check.disabled=false;}});actions.append(check); }
      const toggle=document.createElement("button");toggle.className="secondary";toggle.textContent=membership.status==="blocked"?"Unblock company":"Block company";
      toggle.addEventListener("click",async()=>{toggle.disabled=true;try{await api(`/api/companies/${company.id}`,{method:"PATCH",body:JSON.stringify({expected_version:membership.version,status:membership.status==="blocked"?"suggested":"blocked"})});await refreshCompanies();}catch(error){toggle.disabled=false;message(host,error.message,"error");}});actions.append(toggle);
      if(membership.status==="approved") { const prefer=document.createElement("button");prefer.className="secondary";prefer.textContent=membership.preferred?"Remove preference":"Prefer company";prefer.addEventListener("click",async()=>{prefer.disabled=true;try{await api(`/api/companies/${company.id}`,{method:"PATCH",body:JSON.stringify({expected_version:membership.version,preferred:!membership.preferred})});await refreshCompanies();}catch(error){prefer.disabled=false;message(host,error.message,"error");}});actions.append(prefer); }
      card.append(actions);host.append(card);
    }
  } catch(error) { message(host,error.message,"error"); }
}
function renderProfile(profile, warnings = []) {
  const host = $("#profile-result"); host.replaceChildren();
  const heading = document.createElement("h3"); heading.textContent = `Profile version ${profile.version} — review each fact`; host.append(heading);
  const list = document.createElement("ul");
  for (const fact of profile.facts) {
    const row = document.createElement("li"); row.textContent = `${fact.kind}: ${fact.label} — ${fact.value}${fact.source_quote ? ` (CV quote: “${fact.source_quote}”)` : " (entered by you)"}`; list.append(row);
  }
  host.append(list);
  for (const warning of warnings) { const p = document.createElement("p"); p.className = "notice"; p.textContent = warning; host.append(p); }
  const edit = document.createElement("button"); edit.className = "secondary"; edit.textContent = "Edit these facts";
  edit.addEventListener("click", () => {
    editingProfile = profile;
    const form = $("#profile-form"); form.elements.display_name.value = profile.display_name || "";
    form.elements.professional_years.value = profile.professional_years ?? "";
    const facts = $("#facts"); facts.replaceChildren();
    for (const fact of profile.facts) {
      const row = document.createElement("div"); row.className = "fact";
      row.innerHTML = `<label>Fact type <select class="kind"><option value="skill">Skill</option><option value="employment">Employment</option><option value="project">Project</option><option value="education">Education</option><option value="achievement">Achievement</option><option value="language">Language</option><option value="availability">Availability</option><option value="other">Other</option></select></label><label>Label <input class="label" required maxlength="160"></label><label>Details <input class="value" required maxlength="2000"></label><label>Skill level <select class="level"><option value="unknown">Unknown</option><option value="basic">Basic</option><option value="intermediate">Intermediate</option><option value="advanced">Advanced</option></select></label>`;
      row.querySelector(".kind").value = fact.kind; row.querySelector(".label").value = fact.label; row.querySelector(".value").value = fact.value; row.querySelector(".level").value = fact.level || "unknown"; facts.append(row);
    }
    form.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  host.append(edit);
  const confirm = document.createElement("button"); confirm.className = "button"; confirm.textContent = "I reviewed this profile — confirm";
  confirm.addEventListener("click", async () => {
    confirm.disabled = true;
    try { const saved = await api(`/api/profiles/${profile.id}/confirm`, { method: "POST", body: JSON.stringify({ expected_version: profile.version }) }); editingProfile=saved; message(host, `Profile version ${saved.version} confirmed. You can now use it for a search.`, "success"); await refreshRunChoices(); }
    catch (error) { confirm.disabled = false; message(host, error.message, "error"); }
  });
  host.append(confirm);
}
async function boot() {
  try {
    const me = await api("/api/me");
    csrfToken = me.csrf_token; $("#workspace").hidden = false; $("#account").textContent = `Signed in as ${me.user.email}`;
    const countries = await api("/api/countries");
    const country = $("#country");
    const choose = document.createElement("option"); choose.value = ""; choose.disabled = true; choose.selected = true; choose.textContent = "Choose a country"; country.append(choose);
    for (const item of countries.items) { const option = document.createElement("option"); option.value = item.code; option.textContent = item.name; country.append(option); }
    await refreshRunChoices();
    await refreshRunHistory();
    await refreshTracker();
    await refreshDraftHistory();
    await refreshCompanyEvidence();
    await refreshCompanies();
  } catch (error) {
    if (error.message.includes("Authentication")) { $("#signed-out").hidden = false; return; }
    $("#signed-out").hidden = false; message($("#status"), error.message, "error");
  }
}
$("#company-form").addEventListener("submit",async(event)=>{
  event.preventDefault();const form=new FormData(event.currentTarget);const result=$("#company-result");
  try {
    const item=await api("/api/companies",{method:"POST",body:JSON.stringify({name:String(form.get("name")).trim(),canonical_domain:String(form.get("canonical_domain")).trim(),careers_url:String(form.get("careers_url")).trim(),evidence:{source_id:form.get("source_id"),quote:String(form.get("quote")).trim()}})});
    message(result,`${item.company.name} was added as suggested. Review and approve it before monitoring.`,"success");event.currentTarget.reset();await refreshCompanyEvidence();await refreshCompanies();
  } catch(error) { message(result,error.message,"error"); }
});
$("#add-fact").addEventListener("click", () => {
  const first = $(".fact"); const clone = first.cloneNode(true);
  clone.querySelector(".label").value = ""; clone.querySelector(".value").value = ""; clone.querySelector(".kind").value = "skill"; clone.querySelector(".level").value = "unknown";
  $("#facts").append(clone);
});
$("#sample-profile").addEventListener("click", async () => {
  const button=$("#sample-profile"), host=$("#profile-result");
  button.disabled=true; button.textContent="Loading sample profile…";
  message(host,"Loading the fictional profile…");
  try {
    const profile=await api("/api/profiles/sample",{method:"POST",body:"{}"});
    editingProfile=profile; renderProfile(profile);
  } catch(error) { message(host,error.message,"error"); }
  finally {
    button.disabled=false; button.textContent="Load fictional sample profile";
    host.focus({preventScroll:true});
    host.scrollIntoView({behavior:"smooth",block:"center"});
  }
});
$("#location-mode").addEventListener("change", (event) => {
  $("#city-fields").hidden = event.target.value === "anywhere";
  $("#nearby-fields").hidden = event.target.value !== "nearby";
});
$("#profile-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = new FormData(event.currentTarget);
  const data = { display_name: form.get("display_name") || null, professional_years: form.get("professional_years") ? Number(form.get("professional_years")) : null, facts: factData() };
  try {
    const profile = editingProfile
      ? await api(`/api/profiles/${editingProfile.id}`, { method: "PUT", body: JSON.stringify({ ...data, expected_version: editingProfile.version }) })
      : await api("/api/profiles/manual", { method: "POST", body: JSON.stringify(data) });
    editingProfile = profile; renderProfile(profile);
  }
  catch (error) { message($("#profile-result"), error.message, "error"); }
});
$("#pdf-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const file = new FormData(event.currentTarget).get("file");
  const headers = new Headers({ "X-CSRF-Token": csrfToken });
  try {
    const response = await fetch("/api/profiles/pdf", { method: "POST", headers, body: new FormData(event.currentTarget), credentials: "same-origin" });
    const result = await response.json().catch(() => ({})); if (!response.ok) throw new Error(result.detail || `Request failed (${response.status})`);
    editingProfile = result.profile; renderProfile(result.profile, result.warnings || []);
  } catch (error) { message($("#profile-result"), error.message, "error"); }
});
$("#text-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const text = new FormData(event.currentTarget).get("text");
  try { const result = await api("/api/profiles/text", { method: "POST", body: JSON.stringify({ text }) }); renderProfile(result.profile, result.warnings || []); }
  catch (error) { message($("#profile-result"), error.message, "error"); }
});
$("#preferences-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = new FormData(event.currentTarget); const countryCode = form.get("country_code");
  const types = selectedValues(event.currentTarget.elements.opportunity_types);
  const preferredCities = lines(form.get("preferred_cities")).map((name) => cityRef(name, countryCode));
  const excludedCities = lines(form.get("excluded_cities")).map((name) => cityRef(name, countryCode));
  const mode = form.get("location_mode");
  const refName = String(form.get("reference_city") || "").trim();
  const latitudeInput = String(form.get("reference_latitude") || "").trim();
  const longitudeInput = String(form.get("reference_longitude") || "").trim();
  if (mode === "nearby" && (!!latitudeInput !== !!longitudeInput)) { message($("#preferences-result"), "Enter both coordinates or leave both blank.", "error"); return; }
  const hasCoordinates = !!latitudeInput && !!longitudeInput;
  const reference = mode === "nearby" ? { ...cityRef(refName, countryCode), latitude: hasCoordinates ? Number(latitudeInput) : null, longitude: hasCoordinates ? Number(longitudeInput) : null, coordinate_precision: hasCoordinates ? "exact" : "unknown" } : null;
  const payload = {
    country_code: countryCode, country_scope: form.get("country_scope"), role_queries: lines(form.get("role_queries")),
    opportunity_types: types, seniority: [form.get("seniority")], work_arrangements: selectedValues(event.currentTarget.elements.work_arrangements),
    location_mode: mode, preferred_cities: preferredCities, preferred_cities_policy: form.get("preferred_cities_policy"), excluded_cities: excludedCities,
    proximity: mode === "nearby" ? { reference, radius_km: Number(form.get("radius_km")), policy: form.get("radius_policy") } : null,
    paid_policy: form.get("paid_policy"), right_to_work: form.get("right_to_work"), needs_sponsorship: form.get("needs_sponsorship"), availability_note: form.get("availability_note") || null,
    career_interests: [], preferred_industries: [], excluded_industries: [], employment_commitment: "either", relocation_policy: "unknown",
    company_search_mode: form.get("company_search_mode"), company_size_preferences: [], company_stage_preferences: [],
    compensation_preferences: types.map((opportunityType) => {
      const prefix=opportunityType==="job"?"job":"internship";
      const amount=(suffix)=>form.get(`${prefix}_${suffix}`)?Number(form.get(`${prefix}_${suffix}`)):null;
      return { opportunity_type:opportunityType,preferred_amount:amount("preferred_amount"),minimum_amount:amount("minimum_amount"),
        currency:String(form.get(`${prefix}_currency`)).trim().toUpperCase(),period:form.get(`${prefix}_period`),
        minimum_policy:form.get(`${prefix}_minimum_policy`),include_undisclosed:event.currentTarget.elements[`${prefix}_include_undisclosed`].checked,
        allow_unpaid:opportunityType==="internship"&&event.currentTarget.elements.allow_unpaid.checked,basis:"gross",component:"base" };
    })
  };
  try { const saved = await api("/api/preferences", { method: "POST", body: JSON.stringify(payload) }); message($("#preferences-result"), `Preferences version ${saved.version} saved.`, "success"); await refreshRunChoices(); }
  catch (error) { message($("#preferences-result"), error.message, "error"); }
});
$("#logout").addEventListener("click", async () => { try { await api("/api/logout", { method: "POST", body: "{}" }); location.reload(); } catch (error) { message($("#status"), error.message, "error"); } });
function renderOpportunities(items, host, run) {
  let heading=host.querySelector(":scope > [data-opportunity-heading]");
  if(!heading){heading=document.createElement("h3");heading.dataset.opportunityHeading="true";host.append(heading);}
  heading.textContent=`${items.length} verified individual vacancies`;
  for(const item of items) {
    const opportunity=item.opportunity;
    if(host.querySelector(`[data-opportunity-id="${CSS.escape(String(opportunity.id))}"]`)) continue;
    const assessment=item.assessment, card=document.createElement("article"); card.className="result-card";card.dataset.opportunityId=opportunity.id;
    const title=document.createElement("h3"); title.textContent=opportunity.title || "Untitled role"; card.append(title);
    const employer=document.createElement("p"); employer.textContent=`${opportunity.employer || "Employer unknown"} · ${opportunity.opportunity_type} · ${assessment?.bucket?.replaceAll("_"," ") || "not assessed"}${assessment?.score == null ? "" : ` · ${assessment.score}/100 sorting score`}`; card.append(employer);
    const details=document.createElement("p"); details.textContent=`Availability: ${opportunity.availability}. Source: ${opportunity.source_status}. ${assessment?.warnings?.join(" ") || ""}`; card.append(details);
    const geography=document.createElement("p"); geography.textContent=`Work arrangement: ${opportunity.work_arrangement}. Workplace countries: ${opportunity.work_countries.join(", ")||"not established"}. Remote scope: ${opportunity.remote_scope}.`;card.append(geography);
    if(assessment) {
      const evidence=document.createElement("details"), summary=document.createElement("summary"); summary.textContent="Evidence and eligibility details"; evidence.append(summary);
      const bucket=document.createElement("p"); bucket.textContent=`Eligibility bucket: ${assessment.bucket.replaceAll("_"," ")}. Sorting score: ${assessment.score==null?"insufficient detail":`${assessment.score}/100`}.`;evidence.append(bucket);
      const rules=document.createElement("ul"); for(const rule of assessment.eligibility_rules){const li=document.createElement("li");li.textContent=`${rule.rule}: ${rule.outcome} — ${rule.explanation}`;rules.append(li);}evidence.append(rules);
      for(const field of opportunity.field_evidence){const quote=document.createElement("blockquote");quote.textContent=`${field.field}: “${field.evidence.quote}”`;evidence.append(quote);}
      for(const requirement of opportunity.requirements){const match=assessment.requirement_matches.find((item)=>item.requirement_id===requirement.id);const quote=document.createElement("blockquote");quote.textContent=`Requirement (${requirement.importance}): ${requirement.text} — “${requirement.evidence.quote}”${match?` · profile support: ${match.status} — ${match.explanation}`:""}`;evidence.append(quote);}
      card.append(evidence);
    }
    if(item.source_excerpt) { const quote=document.createElement("blockquote"); quote.textContent=item.source_excerpt; card.append(quote); }
    const controls=document.createElement("div");controls.className="actions";
    const saveButton=document.createElement("button");saveButton.className="secondary";saveButton.textContent="Save to tracker";
    saveButton.addEventListener("click",async()=>{saveButton.disabled=true;try{const saved=await api("/api/saved",{method:"POST",body:JSON.stringify({opportunity_id:opportunity.id,status:"interested",note:null})});renderSaved(saved,card);await refreshTracker();}catch(error){saveButton.disabled=false;message(card,error.message,"error");}});controls.append(saveButton);
    const draftButton=document.createElement("button");draftButton.className="secondary";draftButton.textContent="Prepare application email";
    draftButton.addEventListener("click",async()=>{draftButton.disabled=true;try{const draft=await api("/api/drafts",{method:"POST",headers:{"Idempotency-Key":crypto.randomUUID()},body:JSON.stringify({opportunity_id:opportunity.id,profile_id:run.profile_id,profile_version:run.profile_version,kind:"email"})});renderDraft(draft,card);await refreshDraftHistory();}catch(error){draftButton.disabled=false;message(card,error.message,"error");}});controls.append(draftButton);
    const link=document.createElement("a"); link.href=opportunity.canonical_url; link.target="_blank"; link.rel="noopener noreferrer"; link.textContent="Open original listing"; controls.append(link);card.append(controls);host.append(card);
  }
}
const trackerStatuses=["interested","preparing","applied","interview","rejected","dismissed"];
function renderSaved(saved,host) {
  let panel=host.querySelector(".tracker-panel");if(!panel){panel=document.createElement("section");panel.className="tracker-panel";host.append(panel);}panel.replaceChildren();
  const heading=document.createElement("h4");heading.textContent="Application tracker";panel.append(heading);
  const select=document.createElement("select");for(const status of trackerStatuses){const option=document.createElement("option");option.value=status;option.textContent=status[0].toUpperCase()+status.slice(1);option.selected=status===saved.status;select.append(option);}panel.append(select);
  const note=document.createElement("textarea");note.rows=2;note.maxLength=1000;note.placeholder="Private note";note.value=saved.note||"";panel.append(note);
  const button=document.createElement("button");button.className="secondary";button.textContent="Update status";button.addEventListener("click",async()=>{button.disabled=true;try{const updated=await api(`/api/saved/${saved.id}`,{method:"PATCH",body:JSON.stringify({expected_updated_at:saved.updated_at,status:select.value,note:note.value||null})});renderSaved(updated,host);await refreshTracker();}catch(error){button.disabled=false;message(panel,error.message,"error");}});panel.append(button);
}
function renderDraft(draft,host) {
  let panel=host.querySelector(".draft-panel");if(!panel){panel=document.createElement("section");panel.className="draft-panel";host.append(panel);}panel.replaceChildren();
  const heading=document.createElement("h4");heading.textContent=`Application email template · version ${draft.version}`;panel.append(heading);
  const subject=document.createElement("p");subject.textContent=`Subject: ${draft.subject||""}`;panel.append(subject);
  const label=document.createElement("label");label.textContent="Email text (edit placeholders and wording before review)";
  const editor=document.createElement("textarea");editor.rows=12;editor.maxLength=10000;editor.value=draft.edited_text??draft.sentences.map((sentence)=>sentence.text).join("\n\n");label.append(editor);panel.append(label);
  const status=document.createElement("p");status.textContent=draft.edited_text?"User edited. The original sentence-level evidence remains available, but edited text is not automatically re-certified.":"Template generated only from confirmed facts; placeholders need your input.";panel.append(status);
  const actions=document.createElement("div");actions.className="actions";
  const save=document.createElement("button");save.className="secondary";save.textContent="Save edits";save.addEventListener("click",async()=>{save.disabled=true;try{const updated=await api(`/api/drafts/${draft.id}`,{method:"PUT",body:JSON.stringify({expected_version:draft.version,edited_text:editor.value})});renderDraft(updated,host);await refreshDraftHistory();}catch(error){save.disabled=false;message(panel,error.message,"error");}});actions.append(save);
  const review=document.createElement("button");review.className="button";review.textContent=draft.reviewed_at?"Reviewed":"Mark this version reviewed";review.disabled=!!draft.reviewed_at;review.addEventListener("click",async()=>{review.disabled=true;try{const reviewed=await api(`/api/drafts/${draft.id}/review`,{method:"POST",body:JSON.stringify({expected_version:draft.version})});renderDraft(reviewed,host);}catch(error){review.disabled=false;message(panel,error.message,"error");}});actions.append(review);
  const download=document.createElement("a");download.href=`/api/drafts/${draft.id}/download`;download.textContent="Download .txt";download.className="button";actions.append(download);
  panel.append(actions);
}
async function refreshTracker() {
  const host=$("#tracker-list");host.replaceChildren();
  try{const result=await api("/api/saved");if(!result.items.length){const empty=document.createElement("p");empty.textContent="No opportunities saved yet.";host.append(empty);return;}
    for(const item of result.items){const card=document.createElement("article");card.className="result-card";const title=document.createElement("h3");title.textContent=item.opportunity.title||"Untitled role";card.append(title);const who=document.createElement("p");who.textContent=`${item.opportunity.employer||"Employer unknown"} · ${item.saved.status}`;card.append(who);renderSaved(item.saved,card);host.append(card);}
  }catch(error){message(host,error.message,"error");}
}
async function refreshDraftHistory() {
  const host=$("#draft-history");host.replaceChildren();
  try{const result=await api("/api/drafts");if(!result.items.length){const empty=document.createElement("p");empty.textContent="No drafts yet.";host.append(empty);return;}
    for(const item of result.items){const button=document.createElement("button");button.className="secondary history-button";button.textContent=`${item.title||"Application"} · v${item.version} · ${item.reviewed_at?"reviewed":"not reviewed"}${item.edited?" · edited":""}`;button.addEventListener("click",async()=>{try{renderDraft(await api(`/api/drafts/${item.id}`),host);}catch(error){message(host,error.message,"error");}});host.append(button);}
  }catch(error){message(host,error.message,"error");}
}
async function showDecision(run, host) {
  if(!run.pending_decision_id) return;
  const decision=await api(`/api/decisions/${run.pending_decision_id}`), box=document.createElement("section"); box.className="decision-box";
  const heading=document.createElement("h3"); heading.textContent="Your decision is needed"; box.append(heading);
  const question=document.createElement("p"); question.textContent=decision.question; box.append(question);
  const form=document.createElement("form"), select=document.createElement("select"); select.required=true;
  for(const data of decision.options) { const option=document.createElement("option"); option.value=data.id; option.textContent=`${data.label}: ${data.explanation}`; select.append(option); }
  const note=document.createElement("textarea"); note.rows=3; note.maxLength=1000; note.placeholder="Your factual answer (only used if you add a profile fact)";
  form.append(select,note); const button=document.createElement("button"); button.className="button"; button.textContent="Save decision"; form.append(button);
  form.addEventListener("submit",async(event)=>{event.preventDefault();button.disabled=true;try{const updated=await api(`/api/decisions/${decision.id}/answer`,{method:"POST",body:JSON.stringify({expected_state_version:run.state_version,option_id:select.value,answer_note:note.value||null})});host.replaceChildren();await renderRun(updated,host);}catch(error){button.disabled=false;message(host,error.message,"error");}});
  box.append(form); host.append(box);
}
async function renderRun(run, host) {
  const sameRun=host.dataset.runId===run.id;
  const sameState=sameRun&&host.dataset.runState===run.state;
  if(host===$("#run-result")) displayedRunId=run.id;
  if(!sameState) {
    runRenderGeneration++;
    host.replaceChildren();host.dataset.runId=run.id;host.dataset.runState=run.state;
    const summary=document.createElement("p");summary.dataset.runSummary="true";host.append(summary);
    const actions=document.createElement("div");actions.dataset.runActions="true";host.append(actions);
    const events=document.createElement("ul");events.dataset.runEvents="true";host.append(events);
    const sources=document.createElement("details");sources.dataset.runSources="true";const sourceHeading=document.createElement("summary");sourceHeading.textContent="Source activity";sources.append(sourceHeading);host.append(sources);
    const results=document.createElement("section");results.dataset.runResults="true";host.append(results);
  }
  const generation=runRenderGeneration;
  host.dataset.runState=run.state;
  const summary=host.querySelector("[data-run-summary]");summary.className=run.state==="failed"?"error":"success";
  summary.textContent=`Run ${run.state.replaceAll("_"," ")} · ${run.source_count} saved sources · ${run.opportunity_count} assessed vacancies${run.last_error?` · ${run.last_error.message}`:""}`;
  const actions=host.querySelector("[data-run-actions]");
  if(!sameState) {
    actions.replaceChildren();
    if(run.state==="queued"||run.state==="running") {
      const cancel=document.createElement("button");cancel.className="secondary";cancel.textContent="Cancel run";
      cancel.addEventListener("click",async()=>{try{const stopped=await api(`/api/runs/${run.id}/cancel`,{method:"POST",body:JSON.stringify({expected_state_version:run.state_version})});await renderRun(stopped,host);await refreshRunHistory();}catch(error){message(host,error.message,"error");}});actions.append(cancel);
    } else if(run.state==="interrupted") {
      const resume=document.createElement("button");resume.className="button";resume.textContent="Resume saved run";
      resume.addEventListener("click",async()=>{try{const resumed=await api(`/api/runs/${run.id}/resume`,{method:"POST",body:JSON.stringify({expected_state_version:run.state_version})});await renderRun(resumed,host);await refreshRunHistory();}catch(error){message(host,error.message,"error");}});actions.append(resume);
    }
  }
  const events=await api(`/api/runs/${run.id}/events`),list=host.querySelector("[data-run-events]");
  for(const item of events.items.slice(list.children.length)){const li=document.createElement("li");li.textContent=item.message;list.append(li);}
  const sources=await api(`/api/runs/${run.id}/sources`),panel=host.querySelector("[data-run-sources]");
  for(const source of sources.items) {
    let row=panel.querySelector(`[data-source-id="${CSS.escape(String(source.id))}"]`);
    if(!row){row=document.createElement("p");row.dataset.sourceId=source.id;const link=document.createElement("a");link.href=source.url;link.target="_blank";link.rel="noopener noreferrer";link.textContent=source.title||"Untitled source";row.append(link);const state=document.createElement("span");state.dataset.sourceState="true";row.append(state);panel.append(row);}
    row.querySelector("[data-source-state]").textContent=` · ${source.source_status} · ${source.inspection_state}${source.truncated?" · text truncated":""}`;
    if(source.excerpt&&!row.querySelector("blockquote")){const quote=document.createElement("blockquote");quote.textContent=source.excerpt;row.append(quote);}
  }
  panel.querySelector("summary").textContent=`Source activity · ${sources.items.length} saved`;
  if(run.state==="awaiting_decision") { if(host.dataset.decisionId!==run.pending_decision_id){host.dataset.decisionId=run.pending_decision_id;await showDecision(run,host);} return; }
  if(run.opportunity_count) { const result=await api(`/api/runs/${run.id}/opportunities`); renderOpportunities(result.items,host.querySelector("[data-run-results]"),run); }
  if(run.state==="queued"||run.state==="running") setTimeout(async()=>{if(generation!==runRenderGeneration||host.dataset.runId!==run.id)return;try{await renderRun(await api(`/api/runs/${run.id}`),host);}catch(error){message(host,error.message,"error");}},2000);
}
async function refreshRunHistory() {
  const host=$("#run-history"); host.replaceChildren();
  try {
    const result=await api("/api/runs?limit=10"); if(!result.items.length) return;
    const heading=document.createElement("h3"); heading.textContent="Recent research"; host.append(heading);
    for(const run of result.items) { const button=document.createElement("button"); button.className="secondary history-button"; button.textContent=`${new Date(run.updated_at).toLocaleString()} · ${run.state.replaceAll("_"," ")} · ${run.opportunity_count} vacancies`;
      button.addEventListener("click",()=>renderRun(run,$("#run-result"))); host.append(button); }
    const active=result.items.find((run)=>["queued","running","awaiting_decision","interrupted"].includes(run.state));
    if(active&&displayedRunId!==active.id) await renderRun(active,$("#run-result"));
  } catch(error) { message(host,error.message,"error"); }
}
$("#run-form").addEventListener("submit",async(event)=>{
  event.preventDefault(); const profile=JSON.parse($("#run-profile").value),preferences=JSON.parse($("#run-preferences").value),host=$("#run-result");
  try { const run=await api("/api/runs",{method:"POST",headers:{"Idempotency-Key":crypto.randomUUID()},body:JSON.stringify({profile_id:profile.id,profile_version:profile.version,preference_id:preferences.id,preference_version:preferences.version})}); await renderRun(run,host); await refreshRunHistory(); }
  catch(error) { message(host,error.message,"error"); }
});
boot();
