"""Independent posting metadata extraction regression tests (offline)."""
import json
import unittest
from src.enrichment.job_description import extract_job_description_from_html
from src.enrichment.job_identity import validate_job_identity

G='https://job-boards.greenhouse.io/myfitnesspal/jobs/8191556'
W='https://dynata.wd108.myworkdayjobs.com/en-US/careers/job/Senior-Data-Engineer_REQ13768-2'

def result(url, page):
    return extract_job_description_from_html(url,url,200,page)

class MetadataTests(unittest.TestCase):
    def test_greenhouse_independent_bound_metadata(self):
        post={'title':'Staff Data Engineer','company_name':'MyFitnessPal','job_post_location':'Remote - US','public_url':G}
        payload={'state':{'loaderData':{'routes/$url_token_.jobs_.$job_post_id':{'jobPost':post}}}}
        page='<script>window.__remixContext = '+json.dumps(payload)+';</script>'
        r=result(G,page)
        self.assertEqual((r.resolved_title,r.resolved_company,r.resolved_location,r.resolved_job_id),('Staff Data Engineer','MyFitnessPal','Remote - US','8191556'))
        self.assertEqual(r.resolved_company_source,'greenhouse_remix_jobPost')
        self.assertIsNone(result(G,page.replace('8191556','123')).resolved_company)
        self.assertIsNone(result('https://example.com/jobs/8191556',page).resolved_company)
        self.assertIsNone(result('https://job-boards.greenhouse.io/myfitnesspal',page).resolved_company)

    def test_workday_authoritative_company_and_wrong_employer(self):
        node={'@type':'JobPosting','title':'Senior Data Engineer','hiringOrganization':{'name':''},'identifier':{'value':'REQ13768'},'description':'Qualifications. Dynata is an Equal Opportunity Employer.'}
        r=result(W,'<script type="application/ld+json">'+json.dumps(node)+'</script>')
        self.assertEqual(r.resolved_company,'Dynata')
        self.assertEqual(r.resolved_company_source,'workday_jobposting_employer_statement')
        self.assertEqual(r.resolved_job_id,'REQ13768')
        v=validate_job_identity(original_title='Senior Data Engineer Bilingual (Remote)',original_company='Embedded Shishya',original_location=None,resolved_title=r.resolved_title,resolved_company=r.resolved_company,resolved_location=None)
        self.assertFalse(v.accepted)
        self.assertIsNone(result(W,'<main>Senior Data Engineer</main>').resolved_company)

    def test_linkedin_visible_posting_top_card_recovers_identity_without_jsonld(self):
        url = "https://www.linkedin.com/jobs/view/4478490210/"
        description = (
            "Build analytics models and data pipelines with SQL and Python. "
            "Collaborate with university stakeholders to implement reliable "
            "cloud analytics solutions with AWS, dbt and Power BI. "
        ) * 12
        page = (
            "<html><head><link rel='canonical' href='" + url + "'></head>"
            "<body><main>"
            "<h1 class='top-card-layout__title'>Senior Analytics Engineer</h1>"
            "<a class='topcard__org-name-link'>Wake Forest University</a>"
            "<div class='show-more-less-html__markup'><h2>Responsibilities</h2>"
            + description + "</div>"
            "<section class='related-jobs'>"
            + ("Unrelated jobs in United States. " * 250)
            + "</section></main></body></html>"
        )
        parsed = result(url, page)
        self.assertEqual(parsed.status, "enriched")
        self.assertEqual(parsed.extraction_method, "html_selector")
        self.assertEqual(parsed.resolved_title, "Senior Analytics Engineer")
        self.assertEqual(parsed.resolved_company, "Wake Forest University")
        self.assertEqual(parsed.resolved_title_source, "linkedin_posting_top_card")
        self.assertEqual(parsed.resolved_company_source, "linkedin_posting_top_card")
        self.assertIn("cloud analytics solutions", parsed.description)
        self.assertNotIn("Unrelated jobs", parsed.description)
        validation = validate_job_identity(
            original_title="Senior Analytics Engineer",
            original_company="Wake Forest University",
            resolved_title=parsed.resolved_title,
            resolved_company=parsed.resolved_company,
        )
        self.assertTrue(validation.accepted, validation.reason)

    def test_linkedin_title_slug_canonical_must_match_numeric_posting_id(self):
        url = "https://www.linkedin.com/jobs/view/4478490210/"
        canonical = (
            "https://www.linkedin.com/jobs/view/"
            "senior-analytics-engineer-at-wake-forest-university-4478490210"
        )
        html = (
            "<head><link rel='canonical' href='" + canonical + "'></head>"
            "<h1 class='top-card-layout__title'>Senior Analytics Engineer</h1>"
            "<a class='topcard__org-name-link'>Wake Forest University</a>"
            "<div class='show-more-less-html__markup'>"
            + ("Build SQL models and Python data pipelines. " * 18)
            + "</div>"
        )
        parsed = result(url, html)
        self.assertEqual(parsed.resolved_company, "Wake Forest University")
        self.assertEqual(parsed.status, "enriched")

    def test_linkedin_missing_posting_description_rejects_generic_main(self):
        url = "https://www.linkedin.com/jobs/view/4478490210/"
        page = (
            "<main><h1 class='top-card-layout__title'>Senior Analytics Engineer</h1>"
            "<a class='topcard__org-name-link'>Wake Forest University</a>"
            + ("Unrelated Senior Engineer roles. Apply now. " * 150)
            + "</main>"
        )
        parsed = result(url, page)
        self.assertEqual(parsed.status, "no_description")
        self.assertEqual(parsed.word_count, 0)
        self.assertEqual(parsed.resolved_company, "Wake Forest University")

    def test_linkedin_search_page_cannot_claim_posting_header(self):
        url = "https://www.linkedin.com/jobs/senior-analytics-engineer-jobs"
        html = (
            "<main><h1 class='top-card-layout__title'>Senior Analytics Engineer</h1>"
            "<a class='topcard__org-name-link'>Wake Forest University</a>"
            "<div class='show-more-less-html__markup'>"
            + ("Senior Engineer job descriptions from unrelated companies. " * 15)
            + "</div></main>"
        )
        parsed = result(url, html)
        self.assertIsNone(parsed.resolved_title)
        self.assertIsNone(parsed.resolved_company)

    def test_linkedin_different_canonical_id_blocks_dom_metadata(self):
        url = "https://www.linkedin.com/jobs/view/4478490210/"
        html = (
            "<head><link rel='canonical' "
            "href='https://www.linkedin.com/jobs/view/9991230000/'></head>"
            "<main><h1 class='top-card-layout__title'>Senior Analytics Engineer</h1>"
            "<a class='topcard__org-name-link'>Wake Forest University</a>"
            "<div class='show-more-less-html__markup'>"
            + ("Build pipelines with SQL and Python. " * 20)
            + "</div></main>"
        )
        parsed = result(url, html)
        self.assertIsNone(parsed.resolved_title)
        self.assertIsNone(parsed.resolved_company)

    def test_linkedin_og_meta_only_is_not_independent_identity(self):
        url = "https://www.linkedin.com/jobs/view/4478490210/"
        page = (
            "<head><meta property='og:title' "
            "content='Senior Analytics Engineer at Wake Forest University'></head>"
            "<main>" + ("Build pipelines with SQL and Python. " * 30) + "</main>"
        )
        parsed = result(url, page)
        self.assertIsNone(parsed.resolved_title)
        self.assertIsNone(parsed.resolved_company)

    def test_linkedin_wrong_employer_is_still_rejected(self):
        url = "https://www.linkedin.com/jobs/view/4478490210/"
        description = "Build SQL data pipelines and develop Python analytics solutions. " * 12
        html = (
            "<h1 class='top-card-layout__title'>Senior Analytics Engineer</h1>"
            "<a class='topcard__org-name-link'>Wrong University</a>"
            "<div class='show-more-less-html__markup'>"
            + description + "</div>"
        )
        parsed = result(url, html)
        verdict = validate_job_identity(
            original_title="Senior Analytics Engineer",
            original_company="Wake Forest University",
            resolved_title=parsed.resolved_title,
            resolved_company=parsed.resolved_company,
        )
        self.assertFalse(verdict.accepted)
        self.assertIn("company mismatch", verdict.reason)

    def test_generic_careers_does_not_gain_identity(self):
        r=result('https://www.themesoft.com/careers/','<h1>Careers at Themesoft</h1><main>Explore jobs</main>')
        self.assertIsNone(r.resolved_title)
        self.assertIsNone(r.resolved_company)

    def test_jsonld_takes_precedence_and_no_slug_company(self):
        node={'@type':'JobPosting','title':'Engineer','hiringOrganization':{'name':'Independent Employer'}}
        r=result(W,'<script type="application/ld+json">'+json.dumps(node)+'</script>')
        self.assertEqual(r.resolved_company,'Independent Employer')
        self.assertEqual(r.resolved_company_source,'jobposting_json_ld')
        node['hiringOrganization']['name']=''
        r=result(W,'<script type="application/ld+json">'+json.dumps(node)+'</script>')
        self.assertIsNone(r.resolved_company)

if __name__=='__main__':unittest.main()
