"""Deterministic positive/negative JD-content policy tests."""
import unittest
from src.description_quality import assess_description_quality
from src.repositories.recommendation_repository import description_state

BODY = ('Build reliable data models and design governed reporting systems. '
        'Collaborate with stakeholders to define metrics and deliver trusted analytics. ') * 40
MARKETING = ('Our company connects communities and celebrates innovation. '
             'We value our customers and invest in our future across global markets. ') * 40

class QualityTests(unittest.TestCase):
    def test_legitimate_sections(self):
        for heading in ("What you'll work on", 'What you will work on',
                        "What we’re looking for", 'What we are looking for',
                        'Responsibilities', 'Qualifications', 'Requirements', 'Duties',
                        "What you'll do", 'What you will do', "What you'll bring", 'What you will bring'):
            with self.subTest(heading=heading):
                self.assertTrue(assess_description_quality(heading+'\n'+BODY).full_jd)

    def test_exact_810_word_deepgram_style(self):
        text="What you'll work on\n" + ' '.join((BODY * 2).split()[:806])
        self.assertEqual(len(text.split()),810)
        self.assertEqual(description_state(dict(description=text, description_word_count=810)), 'FULL_JD')

    def test_generic_careers_page(self):
        self.assertFalse(assess_description_quality('Careers\nJoin our global team\n'+MARKETING).full_jd)

    def test_navigation_search_even_with_keywords(self):
        text=('Search results Filter by View all jobs Sign in Privacy policy Requirements skills experience ')*40
        self.assertFalse(assess_description_quality(text).full_jd)

    def test_navigation_only(self):
        self.assertFalse(assess_description_quality(('Navigation Sign in Sign up Privacy policy Cookie preferences ')*40).full_jd)

    def test_marketing_only(self):
        self.assertFalse(assess_description_quality(MARKETING).full_jd)

    def test_short_requirements(self):
        text='Requirements '+ 'SQL '*60
        self.assertFalse(assess_description_quality(text).full_jd)
        self.assertEqual(description_state(dict(description=text, description_word_count=61)), 'PARTIAL_JD')

    def test_multiple_weaker_independent_signals(self):
        self.assertTrue(assess_description_quality('The role\n'+BODY).full_jd)
        self.assertTrue(assess_description_quality('Experience with SQL. '+BODY).full_jd)
        for heading in ('Who you are', 'About you', 'About the role', 'Your role', 'Skills', 'Experience', 'Preferred'):
            self.assertTrue(assess_description_quality(heading+'\n'+BODY).full_jd)
        self.assertFalse(assess_description_quality('The role\n'+MARKETING).full_jd)
        self.assertFalse(assess_description_quality('Experience '*300).full_jd)

    def test_weak_signals_need_substantial_text(self):
        text='The role\nExperience with SQL. '+' '.join(BODY.split()[:90])
        self.assertFalse(assess_description_quality(text).full_jd)

    def test_footer_does_not_reject_genuine_jd(self):
        self.assertTrue(assess_description_quality('Responsibilities\n'+BODY+'\nPrivacy policy Sign in').full_jd)

    def test_metadata_not_quality_input(self):
        job=dict(description=MARKETING, description_word_count=len(MARKETING.split()),
                 title='Data Engineer',company_name='Deepgram',official_url_status='FOUND_VERIFIED',identity_confidence=1)
        self.assertEqual(description_state(job),'PARTIAL_JD')

    def test_duckdb_gate_uses_same_policy(self):
        import duckdb
        from src.repositories.recommendation_repository import register_exact_posting_identity_function
        with duckdb.connect(':memory:') as c:
            register_exact_posting_identity_function(c)
            for text in ("What you'll work on\n"+BODY, MARKETING, 'Requirements SQL', None):
                self.assertEqual(c.execute('select description_is_full_jd(?)',[text]).fetchone()[0],
                                 assess_description_quality(text).full_jd)

if __name__=='__main__':
    unittest.main()
