"""Focused location conflict safeguard; all checks are offline."""
import json
import unittest
from dataclasses import replace
from src.enrichment.job_identity import validate_job_identity,location_compatibility
from src.enrichment.job_description import extract_job_description_from_html
from src.enrich_jobs import validate_enrichment_identity

class LocationTests(unittest.TestCase):
    def validation(self,original,resolved,source='jobposting_json_ld'):
        return validate_job_identity('Data Engineer','Example','Data Engineer','Example',original,resolved,source)

    def test_compatible_scopes(self):
        for original,resolved in [('Remote','Remote - US'),('United States','Boston, Massachusetts, United States'),('Boston, MA','Boston, Massachusetts, United States'),('Reston, VA','Remote, United States'),('United States','Remote - US; London, United Kingdom')]:
            with self.subTest(original=original,resolved=resolved):
                v=self.validation(original,resolved);self.assertTrue(v.accepted);self.assertEqual(v.location_compatibility,'compatible');self.assertEqual(v.confidence,1)

    def test_explicit_conflicts(self):
        for original,resolved in [('United States (Remote)','London, United Kingdom'),('Remote - US','Remote - GB'),('Boston, MA','Seattle, Washington, United States'),('Boston, MA','Cambridge, Massachusetts, United States')]:
            with self.subTest(original=original,resolved=resolved):
                v=self.validation(original,resolved);self.assertFalse(v.accepted);self.assertIn('location mismatch',v.reason);self.assertEqual(v.confidence,1)

    def test_unknown_does_not_veto(self):
        for original,resolved,source in [('Remote',None,'jobposting_json_ld'),('Remote','Remote - GB','jobposting_json_ld'),('US','London, UK','search_snippet'),('US','London, UK',None),('US','London', 'jobposting_json_ld')]:
            v=self.validation(original,resolved,source);self.assertTrue(v.accepted);self.assertEqual(v.location_compatibility,'unknown')

    def test_remote_country_requirements_extracted(self):
        node={'@type':'JobPosting','title':'Data Engineer','hiringOrganization':{'name':'Example'},'jobLocationType':'TELECOMMUTE','applicantLocationRequirements':{'@type':'Country','name':'GB'}}
        r=extract_job_description_from_html('https://example.com/job/1',None,200,'<script type="application/ld+json">'+json.dumps(node)+'</script>')
        self.assertEqual(r.resolved_location,'Remote - GB');self.assertEqual(r.resolved_location_source,'jobposting_json_ld')
        self.assertFalse(self.validation('US (Remote)',r.resolved_location).accepted)
        self.assertEqual(self.validation('Remote',r.resolved_location).location_compatibility,'unknown')
        self.assertFalse(validate_enrichment_identity({'source':'linkedin','location':'United States'},r).accepted)
        self.assertTrue(validate_enrichment_identity({'source':'linkedin','location':'Remote'},r).accepted)

    def test_title_company_thresholds_unchanged(self):
        v=validate_job_identity('Data Engineer','Example','Software Engineer','Other','Remote','Remote - US','jobposting_json_ld')
        self.assertFalse(v.accepted);self.assertIn('company mismatch',v.reason)

if __name__=='__main__':unittest.main()
