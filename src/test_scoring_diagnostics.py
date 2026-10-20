"""Offline scoring diagnostics and unchanged provider-fallback regressions."""

from contextlib import ExitStack, redirect_stdout
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pydantic import BaseModel, ValidationError

from src import score_backlog as backlog
from src.ai.resume_matcher import ResumeMatcherError
from src.scoring_diagnostics import log_scoring_failure, scoring_error_message, sanitize_diagnostic_text
from src.ui.daily_workflow_service import capture_stdout_lines


class ScoringDiagnosticsTests(unittest.TestCase):
    def run_backlog(self, gemini, openai=()):
        jobs = [dict(record_key=f'record-{i}', title=f'Engineer {i}', company_name='Fixture Co')
                for i in range(2)]
        saved = Mock()
        dbt = Mock()
        output = io.StringIO()
        replacements = dict(
            initialize_ai_tables=Mock(), get_model_name=Mock(return_value='fixture-model'),
            load_cached_profile=Mock(return_value=object()),
            load_cached_resume_text=Mock(
                return_value="Fixture resume with SQL, Python, and data engineering experience."
            ),
            count_cached_canonical_scores=Mock(return_value=0),
            screen_unscreened_jobs=Mock(return_value=SimpleNamespace(screened=0)),
            load_candidate_jobs=Mock(return_value=jobs),
            score_resume_against_job=Mock(side_effect=gemini),
            score_resume_against_job_openai=Mock(side_effect=openai),
            save_job_score=saved, run_dbt_build=dbt,
        )
        with patch.multiple(backlog, **replacements), redirect_stdout(output):
            summary = backlog.score_backlog('resume-fixture', 2, 0)
        return summary, output.getvalue(), saved, dbt, replacements

    @staticmethod
    def match():
        return SimpleNamespace(overall_score=85, recommendation='apply',
                               analysis=SimpleNamespace(confidence='high'))

    def test_gemini_reason_and_context_retained_without_changing_failure(self):
        summary, text, saved, dbt, mocks = self.run_backlog([
            ResumeMatcherError('A current FULL_JD is required before AI scoring.'), self.match(),
        ])
        for expected in ('Position: 1/2', 'Record key: record-0', 'Engineer 0 | Fixture Co',
                         'Provider: Gemini', 'ResumeMatcherError: A current FULL_JD is required'):
            self.assertIn(expected, text)
        self.assertEqual((summary.scores_saved, summary.jobs_failed), (1, 1))
        saved.assert_called_once()
        dbt.assert_called_once()
        mocks['score_resume_against_job_openai'].assert_not_called()

    def test_both_provider_failures_are_logged_and_fallback_stays_active(self):
        summary, text, saved, dbt, mocks = self.run_backlog([
            ResumeMatcherError('429 RESOURCE_EXHAUSTED quota exceeded'),
        ], [ResumeMatcherError('OpenAI API error: HTTP 503 service unavailable'), self.match()])
        self.assertIn('Provider: Gemini', text)
        self.assertIn('429 RESOURCE_EXHAUSTED quota exceeded', text)
        self.assertIn('Provider: OpenAI', text)
        self.assertIn('OpenAI API error: HTTP 503 service unavailable', text)
        self.assertEqual((summary.scores_saved, summary.jobs_failed, summary.openai_scores_saved), (1, 1, 1))
        self.assertEqual(mocks['score_resume_against_job'].call_count, 1)
        self.assertEqual(mocks['score_resume_against_job_openai'].call_count, 2)
        self.assertTrue(summary.quota_exhausted)
        saved.assert_called_once()

    def test_openai_active_provider_and_consistency_error_are_logged(self):
        summary, text, _, _, _ = self.run_backlog([
            ResumeMatcherError('PERMISSION_DENIED'),
        ], [self.match(), ResumeMatcherError('score/recommendation consistency failure')])
        self.assertIn('Position: 2/2', text)
        self.assertIn('Provider: OpenAI', text)
        self.assertIn('score/recommendation consistency failure', text)
        self.assertEqual((summary.scores_saved, summary.jobs_failed), (1, 1))

    def test_successes_unchanged_no_failure_log_or_fallback(self):
        matches = [self.match(), self.match()]
        summary, text, saved, dbt, mocks = self.run_backlog(matches)
        self.assertEqual((summary.scores_saved, summary.jobs_failed, summary.gemini_scores_saved), (2, 0, 2))
        self.assertEqual([call.kwargs['match'] for call in saved.call_args_list], matches)
        self.assertNotIn('Score failed:', text)
        mocks['score_resume_against_job_openai'].assert_not_called()
        dbt.assert_called_once()

    def test_secrets_payloads_resume_and_log_injection_are_omitted(self):
        with patch.dict('os.environ', {'TEST_API_KEY': 'environment-private-key'}):
            message = sanitize_diagnostic_text(
                'HTTP 429 quota exceeded environment-private-key Authorization: Bearer header-private '
                'token=token-private api_key="quoted-private" https://host.invalid/?key=url-private '
                'sk-fixturesecret12345\nforged line Response preview: private-resume-and-request')
        for forbidden in ('environment-private-key', 'header-private', 'token-private', 'quoted-private',
                          'url-private', 'sk-fixturesecret12345', 'private-resume-and-request', '\n'):
            self.assertNotIn(forbidden, message)
        self.assertIn('HTTP 429 quota exceeded', message)
        self.assertIn('[redacted]', message)

    def test_validation_details_retain_field_and_type_without_input_or_preview(self):
        class Payload(BaseModel):
            score: int
        try:
            Payload.model_validate(dict(score='private-resume-text'))
        except ValidationError as validation:
            error = ResumeMatcherError(f'invalid job-match response: {validation}\nResponse preview: private-model-output')
            error.__cause__ = validation
        text = scoring_error_message(error)
        self.assertIn('score', text)
        self.assertIn('int_parsing', text)
        self.assertNotIn('private-resume-text', text)
        self.assertNotIn('private-model-output', text)

    def test_stdout_capture_naturally_surfaces_diagnostics(self):
        _, lines = capture_stdout_lines(log_scoring_failure,
            dict(record_key='fixture', title='Engineer', company_name='Fixture Co'), 17, 208,
            'Gemini', ResumeMatcherError('schema response validation failed'))
        self.assertIn('Position: 17/208', lines)
        self.assertTrue(any('schema response validation failed' in line for line in lines))


if __name__ == '__main__':
    unittest.main()
