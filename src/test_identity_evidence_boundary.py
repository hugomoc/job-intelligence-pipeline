"""Discovery fields cannot stand in for independently fetched identity."""
import unittest
from unittest.mock import patch
from dataclasses import replace
from src.enrichment.ats import OfficialJobCandidate
from src.enrichment.ats.workday import parse_workday_jobs
from src.enrichment.job_description import JobDescriptionResult
from src.enrichment import official_job_resolver as resolver

class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.job={'title':'Data Engineer','company_name':'Expected Company','location':'Remote'}
        self.candidate=OfficialJobCandidate('https://tenant.wd1.myworkdayjobs.com/en-US/careers/job/Data-Engineer_ID1','Data Engineer Expected Company','workday',title='Data Engineer',company='Expected Company',location='Remote - US')
        self.result=JobDescriptionResult(self.candidate.url,self.candidate.url,'enriched',200,'json_ld','Responsibilities and qualifications '*100,300,None,resolved_title='Data Engineer',resolved_company=None,resolved_title_source='jobposting_json_ld')

    def test_expected_discovery_company_not_sent_to_validator(self):
        with patch.object(resolver,'validate_job_identity',wraps=resolver.validate_job_identity) as spy:
            verified=resolver.validate_candidate_result(self.job,self.candidate,self.result)
        self.assertIsNone(spy.call_args.kwargs['resolved_company'])
        self.assertIsNone(verified.result.resolved_company)
        self.assertIsNone(verified.validation.company_similarity)
        # Existing missing-company title-only behavior is intentionally preserved.
        self.assertTrue(verified.validation.accepted)

    def test_workday_expected_company_stays_ranking_only(self):
        candidates=parse_workday_jobs([{'title':'Data Engineer','externalPath':'/job/Data-Engineer_ID1'}],self.candidate.url,'Expected Company')
        self.assertEqual(candidates[0].company,'Expected Company')
        with patch.object(resolver,'validate_job_identity',wraps=resolver.validate_job_identity) as spy:
            resolver.validate_candidate_result(self.job,candidates[0],self.result)
        self.assertIsNone(spy.call_args.kwargs['resolved_company'])

    def test_search_title_company_cannot_fill_missing_posting_identity(self):
        candidate=replace(self.candidate,source='search',company='Expected Company')
        self.assertIsNone(resolver.validate_candidate_result(self.job,candidate,replace(self.result,resolved_title=None)))

    def test_wrong_fetched_company_rejected_even_when_candidate_matches(self):
        r=replace(self.result,resolved_company='Wrong Employer',resolved_company_source='jobposting_json_ld')
        self.assertIsNone(resolver.validate_candidate_result(self.job,self.candidate,r))

    def test_correct_fetched_company_wins_over_wrong_discovery(self):
        r=replace(self.result,resolved_company='Expected Company',resolved_company_source='jobposting_json_ld')
        verified=resolver.validate_candidate_result(self.job,replace(self.candidate,company='Other'),r)
        self.assertTrue(verified.validation.accepted)
        self.assertEqual(verified.validation.company_similarity,1)

    def test_chosen_resolution_never_restores_discovery_company(self):
        verified=resolver.validate_candidate_result(self.job,self.candidate,self.result)
        resolution=resolver.choose_verified_candidate([verified])
        self.assertIsNone(resolution.resolved_company)
        self.assertIsNone(resolution.resolved_location)

    def test_verified_reuse_company_is_not_original_record_company(self):
        import src.enrich_jobs as enrichment
        job={'record_key':'new','title':'Data Engineer','company_name':'Expected Company','current_word_count':0}
        candidate={'record_key':'old','verified_posting_key':'trusted-exact-key',
                   'title':'Data Engineer','company_name':'Expected Company',
                   'official_resolved_title':'Data Engineer','official_resolved_company':None}
        with patch.object(enrichment,'load_verified_reuse_candidates',return_value=[candidate]), \
             patch.object(enrichment,'authoritative_verified_key_for_job',return_value='trusted-exact-key'), \
             patch.object(enrichment,'reuse_verified_description') as reuse, \
             patch.object(enrichment,'validate_job_identity',wraps=enrichment.validate_job_identity) as spy:
            count=enrichment.reuse_authoritative_verified_descriptions_before_queue([job],80)
        self.assertEqual(count,1)
        self.assertEqual(spy.call_args.kwargs['resolved_company'],'')
        self.assertIsNone(reuse.call_args.kwargs['validation'].company_similarity)

    def test_safe_ats_domains(self):
        positives={'careers-example.icims.com':'icims','jobs.icims.com':'icims','boards.greenhouse.io':'greenhouse','job-boards.greenhouse.io':'greenhouse','dynata.wd108.myworkdayjobs.com':'workday','tenant.wd3.myworkdaysite.com':'workday','jobs.lever.co':'lever','jobs.ashbyhq.com':'ashby','jobs.smartrecruiters.com':'smartrecruiters'}
        for host,provider in positives.items():
            with self.subTest(host=host):self.assertEqual(resolver.ats_type_for_url('https://'+host+'/job/1'),provider)
        self.assertEqual(resolver.ats_type_for_url('https://jobs.icims.com:443/job/1'),'icims')
        for host in ['www.efinancialcareers-canada.com','careers-example.com','icims.com.evil.example','noticims.com','greenhouse.io.evil.example','notgreenhouse.io','jobs.lever.co.evil.example','notmyworkdayjobs.com','icims.com@evil.example','evilashbyhq.com','notsmartrecruiters.com']:
            with self.subTest(host=host):self.assertIsNone(resolver.ats_type_for_url('https://'+host+'/job/1'))

if __name__=='__main__':unittest.main()
