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
